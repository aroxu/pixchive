"""A bounded network/disk pipeline must retain normal recovery and stop semantics."""

import asyncio
import threading

import pytest

from app.adapters.pixiv.contracts import AdapterError
from app.core.db import db
from app.models.entities import Asset, DownloadTask, Job
from app.services import accounts, collection, jobs, library
from app.services.fixtures import image_bytes
from app.worker import run_job
from tests.test_collection import BookmarkPage, FakeAdapter, connected, context, work


async def test_next_download_overlaps_current_disk_write_without_parallel_network(
    client, settings, monkeypatch
):
    connected(settings)
    pages = [work(), work("124"), work("125")]
    adapter = FakeAdapter({"public": [BookmarkPage(items=pages, next_cursor=None, complete=True)]})
    ctx = context(settings, adapter)
    first_saving = threading.Event()
    next_started = threading.Event()
    calls, active = [], 0
    original = collection.save_original

    async def fetch(url):
        nonlocal active
        active += 1
        assert active == 1
        try:
            calls.append(url)
            if "/124/" in url:
                next_started.set()
                assert await asyncio.to_thread(first_saving.wait, 5)
            await asyncio.sleep(0)
            return image_bytes(0)
        finally:
            active -= 1

    def save(*args, **kwargs):
        if args[1].artwork_id == "123":
            first_saving.set()
            assert next_started.wait(5), "Next network read should overlap the current disk write"
        return original(*args, **kwargs)

    monkeypatch.setattr(collection, "save_original", save)
    ctx["download"] = fetch
    job = jobs.enqueue_account("sync", "pipeline", "public")
    await asyncio.wait_for(run_job(ctx, job.id), 10)
    result = Job.get_by_id(job.id)
    assert (result.state, result.processed, result.failed) == ("succeeded", 3, 0)
    assert len(calls) == 3 and active == 0
    assert Asset.select().where(Asset.role == "original").count() == 3


async def test_pause_waits_for_prefetch_cleanup_and_keeps_completed_files(client, settings):
    connected(settings)
    page = BookmarkPage(items=[work(count=4)], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))
    second_started, stopped = asyncio.Event(), asyncio.Event()

    async def fetch(url):
        if "p3" not in url:
            return image_bytes(0)
        second_started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    ctx["download"] = fetch
    job = jobs.enqueue_account("sync", "pause-ahead", "public")
    task = asyncio.create_task(run_job(ctx, job.id))
    await asyncio.wait_for(second_started.wait(), 5)
    while (
        not DownloadTask.select()
        .where(
            (DownloadTask.job == job.id)
            & (DownloadTask.page == 2)
            & (DownloadTask.state == "ready")
        )
        .exists()
    ):
        await asyncio.sleep(0.01)
    assert jobs.action(job.id, "pause").state == "pausing"
    await asyncio.wait_for(task, 5)
    assert stopped.is_set() and Job.get_by_id(job.id).state == "paused"
    assert list((settings.data_dir / "tmp").iterdir()) == []
    jobs.action(job.id, "resume")
    calls = []
    await run_job(context(settings, FakeAdapter(), calls), job.id)
    assert len(calls) == 1 and all("p3" in url for url in calls)
    assert Job.get_by_id(job.id).state == "succeeded"


@pytest.mark.parametrize("failure", ["auth_expired", "rate_limited"])
async def test_prefetch_fatal_error_never_starts_a_later_request(client, settings, failure):
    connected(settings)
    page = BookmarkPage(items=[work(count=4)], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))
    calls = []

    async def fetch(url):
        calls.append(url)
        if "p1" in url:
            raise AdapterError(failure, 600 if failure == "rate_limited" else None)
        return image_bytes(0)

    ctx["download"] = fetch
    job = jobs.enqueue_account("sync", "fatal-ahead", "public")
    await run_job(ctx, job.id)
    result = Job.get_by_id(job.id)
    assert result.state == ("waiting_auth" if failure == "auth_expired" else "failed")
    assert result.error_code == failure
    assert len(calls) <= 3 and not any("p3" in url for url in calls)
    assert DownloadTask.get(DownloadTask.page == 0).state == "pending"
    assert DownloadTask.get(DownloadTask.page == 1).state == "pending"


async def test_credential_change_during_prefetch_does_not_write_fetched_bytes(client, settings):
    connected(settings)
    page = BookmarkPage(items=[work(count=2)], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))
    ctx["settings"] = settings

    async def fetch(url):
        if "p1" in url:
            accounts.store_credential(settings, "different-synthetic-cookie")
        return image_bytes(0)

    ctx["download"] = fetch
    job = jobs.enqueue_account("sync", "credential-ahead", "public")
    await run_job(ctx, job.id)
    assert Job.get_by_id(job.id).state == "waiting_auth"
    assert (
        not DownloadTask.select()
        .where((DownloadTask.page == 1) & (DownloadTask.state == "ready"))
        .exists()
    )
    assert list((settings.data_dir / "tmp").iterdir()) == []


async def test_prefetch_skips_an_existing_ready_source_without_network(client, settings):
    credential = connected(settings)
    library.ingest(work(count=2), "public")
    job = jobs.enqueue_account("sync", "skip-kept", "public")
    owner = jobs.claim(job.id)
    existing = Asset.create(
        artwork="123",
        revision="a" * 64,
        page=1,
        role="original",
        path="media/kept",
        sha256="a" * 64,
        size=1,
        width=1,
        height=1,
        media_type="image/png",
        state="ready",
    )
    current = DownloadTask.create(job=job, artwork="123", page=0, url="https://i.pximg.net/0.png")
    DownloadTask.create(job=job, artwork="123", page=1, url="https://i.pximg.net/1.png")
    other = jobs.enqueue_account("sync", "previous-run", "private")
    DownloadTask.create(
        job=other,
        artwork="123",
        page=1,
        url="https://i.pximg.net/1.png",
        state="ready",
        asset=existing,
    )
    calls = []

    async def fetch(url):
        calls.append(url)
        return b"unexpected"

    ahead = collection._AheadDownload(fetch, job.id, owner, credential)
    ahead.start(current)
    await asyncio.sleep(0)
    await ahead.close()
    assert calls == [] and db.in_transaction() is False


@pytest.mark.parametrize("failure", ["auth_expired", "rate_limited"])
@pytest.mark.parametrize("operation", ["skip_to_later_item", "finish_batch"])
async def test_unused_prefetch_preserves_fatal_errors(client, settings, failure, operation):
    credential = connected(settings)
    library.ingest(work(count=3), "public")
    job = jobs.enqueue_account("sync", "unused-prefetch", "public")
    owner = jobs.claim(job.id)
    items = [
        DownloadTask.create(
            job=job, artwork="123", page=page, url=f"https://i.pximg.net/{page}.png"
        )
        for page in range(3)
    ]
    calls = []

    async def fetch(url):
        calls.append(url)
        raise AdapterError(failure)

    ahead = collection._AheadDownload(fetch, job.id, owner, credential)
    ahead.start(items[0])
    DownloadTask.update(state="skipped").where(DownloadTask.id == items[1].id).execute()
    with pytest.raises(AdapterError, match=failure):
        if operation == "skip_to_later_item":
            await ahead.take(items[2])
        else:
            await ahead.close(drain=True)
    await ahead.close()
    assert calls == [items[1].url]


async def test_skipped_prefetch_finishes_backoff_before_new_network(client, settings):
    credential = connected(settings)
    library.ingest(work(count=3), "public")
    job = jobs.enqueue_account("sync", "backoff-prefetch", "public")
    owner = jobs.claim(job.id)
    items = [
        DownloadTask.create(
            job=job, artwork="123", page=page, url=f"https://i.pximg.net/{page}.png"
        )
        for page in range(3)
    ]
    retry_after_finished, waiting = asyncio.Event(), asyncio.Event()
    calls = []

    async def fetch(url):
        calls.append(url)
        if url == items[1].url:
            waiting.set()
            await retry_after_finished.wait()
        return b"content"

    ahead = collection._AheadDownload(fetch, job.id, owner, credential)
    ahead.start(items[0])
    await asyncio.wait_for(waiting.wait(), 5)
    next_request = asyncio.create_task(ahead.take(items[2]))
    await asyncio.sleep(0)
    assert calls == [items[1].url]
    retry_after_finished.set()
    assert await asyncio.wait_for(next_request, 5) == b"content"
    assert calls == [items[1].url, items[2].url]
    await ahead.close()
