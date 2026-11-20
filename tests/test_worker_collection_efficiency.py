"""Bounded collection accounting and avatar publication retain durable recovery."""

import asyncio
import errno
import hashlib
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from app.adapters.pixiv.contracts import AdapterError
from app.core.errors import DomainError
from app.models.entities import (
    Artist,
    Artwork,
    Asset,
    CollectionItem,
    CollectionRun,
    DownloadTask,
    Job,
    StorageReservation,
)
from app.services import accounts, collection, jobs, library, previews, storage, tempfiles
from app.services.execution import Interrupted
from app.services.fixtures import image_bytes
from app.worker import run_job
from tests.test_collection import BookmarkPage, FakeAdapter, connected, context, work


def pending(settings, count=3):
    credential = connected(settings)
    remote = work(count=count)
    library.ingest(remote, "public")
    job = jobs.enqueue_account("sync", "collection-efficiency", "public")
    owner = jobs.claim(job.id)
    collection.plan_downloads(job.id, remote.id, remote)
    return job, owner, credential


async def test_chunk_reports_durable_success_and_file_failure_once(client, settings, monkeypatch):
    job, owner, credential = pending(settings)
    snapshots = []
    progress = collection.progress

    def report(job_id):
        progress(job_id)
        current = Job.get_by_id(job_id)
        snapshots.append((current.processed, current.failed))

    async def fetch(url):
        if "p1" in url:
            raise AdapterError("unavailable")
        return image_bytes(0)

    monkeypatch.setattr(collection, "progress", report)
    await collection.download_pending(
        {"settings": settings, "download": fetch}, job.id, owner, credential
    )
    assert snapshots == [(3, 1)]
    assert [item.state for item in DownloadTask.select().order_by(DownloadTask.page)] == [
        "ready",
        "failed",
        "ready",
    ]
    assert not StorageReservation.select().exists()


def test_progress_counts_terminal_files_and_metadata_without_counting_unavailable(client, settings):
    job, _, _ = pending(settings, count=5)
    for item, state in zip(
        DownloadTask.select().order_by(DownloadTask.page),
        ("ready", "failed", "excluded", "skipped", "pending"),
        strict=True,
    ):
        DownloadTask.update(state=state).where(DownloadTask.id == item.id).execute()
    run = CollectionRun.create(job=job, scope="public")
    CollectionItem.create(run=run, artwork_id="999", error_code="network")
    CollectionItem.create(run=run, artwork_id="998", error_code="unavailable")
    collection.progress(job.id)
    result = Job.get_by_id(job.id)
    assert (result.processed, result.failed) == (4, 2)
    DownloadTask.delete().where(DownloadTask.job == job).execute()
    CollectionItem.delete().where(CollectionItem.run == run).execute()
    collection.progress(job.id)
    result = Job.get_by_id(job.id)
    assert (result.processed, result.failed) == (0, 0)


async def test_chunk_disk_failure_flushes_committed_count_before_waiting_storage(
    client, settings, monkeypatch
):
    connected(settings)
    listing = BookmarkPage(items=[work(count=3)], next_cursor=None, complete=True)
    save = collection.save_original

    def fail_second(settings, item, *args):
        if item.page == 1:
            raise OSError(errno.ENOSPC, "synthetic disk full")
        return save(settings, item, *args)

    monkeypatch.setattr(collection, "save_original", fail_second)
    job = jobs.enqueue_account("sync", "partial-chunk-disk-full", "public")
    await run_job(context(settings, FakeAdapter({"public": [listing]})), job.id)
    result = Job.get_by_id(job.id)
    assert (result.state, result.processed, result.failed) == ("waiting_storage", 1, 0)
    assert [item.state for item in DownloadTask.select().order_by(DownloadTask.page)] == [
        "ready",
        "pending",
        "pending",
    ]
    assert Artwork.get_by_id("123").archive_status == "partial"
    assert not StorageReservation.select().exists()
    assert list((settings.data_dir / "tmp").iterdir()) == []


@pytest.mark.parametrize("action", ["pause", "cancel", "delivery_cancel"])
async def test_stopped_chunk_flushes_committed_pages_and_resume_keeps_original(
    client, settings, monkeypatch, action
):
    connected(settings)
    listing = BookmarkPage(items=[work(count=3)], next_cursor=None, complete=True)
    started, release = threading.Event(), threading.Event()
    save = collection.save_original

    def hold_second(settings, item, *args):
        if item.page == 1:
            started.set()
            assert release.wait(5), "Test must release the in-flight disk operation"
        return save(settings, item, *args)

    monkeypatch.setattr(collection, "save_original", hold_second)
    job = jobs.enqueue_account("sync", f"stop-chunk-{action}", "public")
    task = asyncio.create_task(
        run_job(context(settings, FakeAdapter({"public": [listing]})), job.id)
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        source = Asset.get(
            (Asset.artwork == "123") & (Asset.role == "original") & (Asset.page == 0)
        )
        before = storage.safe_path(settings, source.path).read_bytes()
        if action == "delivery_cancel":
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        else:
            jobs.action(job.id, action)
            release.set()
            await asyncio.wait_for(task, 5)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    result = Job.get_by_id(job.id)
    expected_state = {"pause": "paused", "cancel": "cancelled", "delivery_cancel": "queued"}
    assert result.state == expected_state[action]
    ready = DownloadTask.select().where(DownloadTask.state == "ready").count()
    # A cancelled delivery waits for its current write to settle; that write may
    # finish, but its durable task state and aggregate must still agree.
    assert ready >= 1 and result.processed == ready and result.failed == 0
    assert not StorageReservation.select().exists()
    assert list((settings.data_dir / "tmp").iterdir()) == []
    if action != "cancel":
        expected_fetches = (
            DownloadTask.select()
            .where((DownloadTask.state == "pending") & DownloadTask.asset.is_null())
            .count()
        )
        monkeypatch.setattr(collection, "save_original", save)
        if action == "pause":
            jobs.action(job.id, "resume")
        calls = []
        await run_job(context(settings, FakeAdapter(), calls), job.id)
        result = Job.get_by_id(job.id)
        assert (result.state, result.processed, result.failed) == ("succeeded", 3, 0)
        assert len(calls) == expected_fetches and not any("p0" in url for url in calls)
        assert storage.safe_path(settings, source.path).read_bytes() == before


@pytest.mark.parametrize("reuse", ["staged_task", "previous_task"])
async def test_reused_original_recomputes_completeness_after_page_count_change(
    client, settings, reuse
):
    job, owner, credential = pending(settings, count=2)
    item = DownloadTask.get(DownloadTask.page == 0)
    source = storage.store_image(settings, "123", 0, image_bytes(0))
    DownloadTask.delete().where(DownloadTask.page == 1).execute()
    if reuse == "staged_task":
        DownloadTask.update(asset=source).where(DownloadTask.id == item.id).execute()
    else:
        prior = jobs.enqueue_account("sync", "prior-kept-source", "private")
        DownloadTask.create(
            job=prior, artwork="123", page=0, url=item.url, asset=source, state="ready"
        )
    Artwork.update(page_count=1, archive_status="partial").where(Artwork.id == "123").execute()

    async def no_network(url):
        pytest.fail("The recorded source must be reused")

    await collection.download_pending(
        {"settings": settings, "download": no_network}, job.id, owner, credential
    )
    assert DownloadTask.get_by_id(item.id).state == "ready"
    assert Artwork.get_by_id("123").archive_status == "complete"
    assert (
        hashlib.sha256(storage.safe_path(settings, source.path).read_bytes()).hexdigest()
        == source.sha256
    )


async def test_pending_cursor_final_probe_recovers_an_older_pending_row(client, settings):
    job, owner, credential = pending(settings, count=2)
    older, current = list(DownloadTask.select().order_by(DownloadTask.id))
    DownloadTask.update(state="excluded").where(DownloadTask.id == older.id).execute()
    calls = []

    async def fetch(url):
        calls.append(url)
        if url == current.url:
            DownloadTask.update(state="pending").where(DownloadTask.id == older.id).execute()
        return image_bytes(0)

    await collection.download_pending(
        {"settings": settings, "download": fetch}, job.id, owner, credential
    )
    assert calls == [current.url, older.url]
    assert all(item.state == "ready" for item in DownloadTask.select())
    assert Job.get_by_id(job.id).processed == 2


async def test_single_file_pause_before_preview_flushes_count_and_resume_queues_recovery(
    client, settings, monkeypatch
):
    connected(settings)
    job = jobs.enqueue_account("sync", "single-preview-pause", "public")
    make_preview = collection.make_preview

    async def pause_preview(*args, **kwargs):
        jobs.action(job.id, "pause")
        raise Interrupted

    monkeypatch.setattr(collection, "make_preview", pause_preview)
    listing = BookmarkPage(items=[work()], next_cursor=None, complete=True)
    await run_job(context(settings, FakeAdapter({"public": [listing]})), job.id)
    result = Job.get_by_id(job.id)
    assert (result.state, result.processed, result.failed) == ("paused", 1, 0)
    source = Asset.get(Asset.role == "original")
    assert source.state == "ready" and source.thumbnail_id is None
    monkeypatch.setattr(collection, "make_preview", make_preview)
    jobs.action(job.id, "resume")
    calls = []
    await run_job(context(settings, FakeAdapter(), calls), job.id)
    assert not calls
    recovery = Job.get((Job.kind == "preview") & (Job.scope == "123"))
    assert recovery.state == "queued"
    await run_job({"settings": settings}, recovery.id)
    assert Job.get_by_id(recovery.id).state == "succeeded"
    source = Asset.get_by_id(source.id)
    assert source.thumbnail_id and source.thumbnail.state == "ready"


def avatar_sources(settings):
    credential = connected(settings)
    sources = {}
    for index in range(3):
        artist_id = str(500 + index)
        library.ingest(work(str(200 + index), artist_id=artist_id), "public")
        url = f"https://i.pximg.net/avatar/{artist_id}.png"
        Artist.update(avatar_url=url).where(Artist.id == artist_id).execute()
        sources[url] = image_bytes(index)
    job = jobs.enqueue_account("sync", "avatar-efficiency", "public")
    return job, jobs.claim(job.id), credential, sources


@pytest.mark.parametrize("cached", [False, True])
async def test_avatar_batch_reuses_nas_publication_preserving_hashes_and_local_scratch(
    client, settings, monkeypatch, cached
):
    job, owner, credential, sources = avatar_sources(settings)
    created = []
    workspace = tempfiles.workspace

    @contextmanager
    def tracked(settings, purpose, guard=None):
        with workspace(settings, purpose, guard) as working:
            created.append((purpose, working.path))
            yield working

    monkeypatch.setattr(tempfiles, "workspace", tracked)
    with tempfile.TemporaryDirectory(prefix="pixchive-avatar-efficiency-", dir="/dev/shm") as name:
        cache = Path(name)
        assert os.stat(cache).st_dev != os.stat(settings.data_dir).st_dev
        configured = replace(settings, cache_dir=cache) if cached else settings

        async def fetch(url):
            return sources[url]

        ctx = {"settings": configured, "download": fetch}
        await collection.fetch_avatars(ctx, job.id, owner, credential, set())
        for artist in Artist.select():
            content = storage.avatar_file(configured, artist.avatar_path).read_bytes()
            assert content == sources[artist.avatar_url]
            assert hashlib.sha256(content).hexdigest() == Path(artist.avatar_path).stem
            assert artist.avatar_fetched_url == artist.avatar_url
        scratch = cache if cached else settings.data_dir
        assert (
            len(
                [
                    path
                    for purpose, path in created
                    if purpose == "avatar" and path.is_relative_to(scratch)
                ]
            )
            == 3
        )
        publication = [
            path
            for purpose, path in created
            if purpose == "blob" and path.is_relative_to(settings.data_dir)
        ]
        assert len(publication) == (1 if cached else 0)
        before = len(created)
        await collection.fetch_avatars(ctx, job.id, owner, credential, set())
        assert len(created) == before, "An empty avatar batch must not create workspaces"
        assert all(not path.exists() for _, path in created)
        assert list((settings.data_dir / "tmp").iterdir()) == []
        if cached:
            assert list((cache / "tmp").iterdir()) == []
    assert not StorageReservation.select().exists()


@pytest.mark.parametrize("failure", ["rate_limited", "credential_changed"])
async def test_avatar_batch_interruption_cleans_shared_workspaces_and_preserves_first_avatar(
    client, settings, failure
):
    job, owner, credential, sources = avatar_sources(settings)
    with tempfile.TemporaryDirectory(prefix="pixchive-avatar-stop-", dir="/dev/shm") as name:
        cached = replace(settings, cache_dir=Path(name))
        calls = []

        async def fetch(url):
            calls.append(url)
            if len(calls) == 2:
                if failure == "rate_limited":
                    raise AdapterError(failure, 600)
                accounts.store_credential(settings, "replacement-synthetic-cookie")
            return sources[url]

        expected = AdapterError if failure == "rate_limited" else DomainError
        with pytest.raises(expected, match=failure):
            await collection.fetch_avatars(
                {"settings": cached, "download": fetch}, job.id, owner, credential, set()
            )
        kept = list(Artist.select().where(Artist.avatar_path.is_null(False)))
        assert len(kept) == 1 and len(calls) == 2
        assert storage.avatar_file(cached, kept[0].avatar_path).read_bytes() == sources[calls[0]]
        assert list((cached.cache_dir / "tmp").iterdir()) == []
        assert list((settings.data_dir / "tmp").iterdir()) == []
    assert not StorageReservation.select().exists()


@pytest.mark.parametrize("boundary", ["scan", "finish"])
async def test_cancelled_collection_never_enqueues_preview_recovery(
    client, settings, monkeypatch, boundary
):
    job, owner, credential = pending(settings)
    jobs.action(job.id, "cancel")

    def no_recovery(*args, **kwargs):
        pytest.fail("A stopping collection must not enqueue derivative work")

    monkeypatch.setattr(previews, "enqueue_missing", no_recovery)
    with pytest.raises(Interrupted):
        if boundary == "scan":
            await collection.scan(
                {"settings": settings}, FakeAdapter(), job, "42", owner, credential
            )
        else:
            collection.finish_collection(job.id, owner, credential)
