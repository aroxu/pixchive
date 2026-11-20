import asyncio
import errno
import hashlib
from collections import namedtuple

import pytest

from app.adapters.pixiv.contracts import AdapterError
from app.core.db import db
from app.models.entities import Asset, DownloadTask, Job, StorageReservation, Tombstone
from app.services import collection, jobs, library, storage, storage_budget
from app.services.fixtures import image_bytes
from app.worker import run_job
from tests.test_collection import BookmarkPage, FakeAdapter, connected, context, work


async def test_same_artwork_pages_download_concurrently_in_bounded_groups(client, settings):
    connected(settings)
    works = [work(count=7), work("124", count=4)]
    ctx = context(
        settings,
        FakeAdapter({"public": [BookmarkPage(items=works, next_cursor=None, complete=True)]}),
    )
    entered = asyncio.Event()
    active, maximum = set(), 0
    calls = []

    async def fetch(url):
        nonlocal maximum
        assert not db.in_transaction()
        identity = url.split("/")[-2]
        assert all(other.split("/")[-2] == identity for other in active)
        active.add(url)
        calls.append(url)
        maximum = max(maximum, len(active))
        assert len(active) <= 3
        try:
            if len(calls) <= 3:
                if len(active) == 3:
                    entered.set()
                await asyncio.wait_for(entered.wait(), 5)
            await asyncio.sleep(0)
            return image_bytes(0)
        finally:
            active.remove(url)

    ctx["download"] = fetch
    job = jobs.enqueue_account("sync", "parallel-pages", "public")
    await asyncio.wait_for(run_job(ctx, job.id), 15)
    result = Job.get_by_id(job.id)
    assert (result.state, result.processed, result.failed) == ("succeeded", 11, 0)
    assert maximum == 3 and not active and len(calls) == len(set(calls)) == 11
    originals = list(Asset.select().where(Asset.role == "original"))
    assert len(originals) == 11
    for asset in originals:
        content = (settings.data_dir / asset.path).read_bytes()
        assert len(content) == asset.size and hashlib.sha256(content).hexdigest() == asset.sha256
    assert not StorageReservation.select().exists()
    assert list((settings.data_dir / "tmp").iterdir()) == []


@pytest.mark.parametrize("failure", ["auth_expired", "rate_limited"])
async def test_later_page_fatal_error_cancels_other_reads_without_refill(client, settings, failure):
    connected(settings)
    page = BookmarkPage(items=[work(count=6)], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))
    waiting = asyncio.Event()
    active, stopped, calls = set(), set(), []

    async def fetch(url):
        calls.append(url)
        active.add(url)
        try:
            if "p2" in url:
                await asyncio.wait_for(waiting.wait(), 5)
                raise AdapterError(failure, 600 if failure == "rate_limited" else None)
            if len(active) >= 2:
                waiting.set()
            await asyncio.Future()
        finally:
            active.remove(url)
            stopped.add(url)

    ctx["download"] = fetch
    job = jobs.enqueue_account("sync", "fatal-parallel", "public")
    await asyncio.wait_for(run_job(ctx, job.id), 10)
    result = Job.get_by_id(job.id)
    assert result.state == ("waiting_auth" if failure == "auth_expired" else "failed")
    assert result.error_code == failure
    assert len(calls) == 3 and stopped == set(calls) and not active
    assert not Asset.select().exists()
    assert all(item.state == "pending" for item in DownloadTask.select())
    assert not StorageReservation.select().exists()
    assert list((settings.data_dir / "tmp").iterdir()) == []


async def test_one_page_failure_preserves_successful_sibling_pages(client, settings):
    connected(settings)
    page = BookmarkPage(items=[work(count=3)], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))

    async def fetch(url):
        if "p1" in url:
            raise AdapterError("unavailable")
        return image_bytes(0)

    ctx["download"] = fetch
    job = jobs.enqueue_account("sync", "partial-parallel", "public")
    await run_job(ctx, job.id)
    assert [item.state for item in DownloadTask.select().order_by(DownloadTask.page)] == [
        "ready",
        "failed",
        "ready",
    ]
    result = Job.get_by_id(job.id)
    assert (result.state, result.processed, result.failed) == ("partial", 3, 1)


async def test_window_shrinks_when_only_two_pages_fit_storage_budget(client, settings, monkeypatch):
    connected(settings)
    page = BookmarkPage(items=[work(count=5)], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))
    free = (
        settings.min_free_bytes
        + 2 * (collection.MAX_IMAGE_BYTES + storage_budget.PREVIEW_BYTES)
        + storage_budget.PREVIEW_BYTES
    )
    usage = namedtuple("Usage", "total used free")(free * 2, free, free)
    monkeypatch.setattr(storage_budget.shutil, "disk_usage", lambda _: usage)
    active, maximum = 0, 0
    two_started = asyncio.Event()
    calls = []

    async def fetch(url):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        calls.append(url)
        try:
            if len(calls) <= 2:
                if active == 2:
                    two_started.set()
                await asyncio.wait_for(two_started.wait(), 5)
            await asyncio.sleep(0)
            return image_bytes(0)
        finally:
            active -= 1

    ctx["download"] = fetch
    job = jobs.enqueue_account("sync", "tight-parallel", "public")
    await asyncio.wait_for(run_job(ctx, job.id), 10)
    assert Job.get_by_id(job.id).state == "succeeded"
    assert maximum == 2 and active == 0 and len(calls) == 5
    assert not StorageReservation.select().exists()


@pytest.mark.parametrize("failure", ["auth_expired", "rate_limited"])
async def test_already_failed_ahead_stops_before_launching_group_siblings(
    client, settings, failure
):
    credential = connected(settings)
    library.ingest(work(count=4), "public")
    job = jobs.enqueue_account("sync", "failed-before-group", "public")
    owner = jobs.claim(job.id)
    items = [
        DownloadTask.create(
            job=job, artwork="123", page=page, url=f"https://i.pximg.net/{page}.png"
        )
        for page in range(4)
    ]
    calls = []

    async def fetch(url):
        calls.append(url)
        raise AdapterError(failure)

    ahead = collection._AheadDownload(fetch, job.id, owner, credential)
    ahead.start(items[0])
    await asyncio.gather(ahead.pending[2], return_exceptions=True)
    DownloadTask.update(state="excluded").where(DownloadTask.id == items[0].id).execute()

    def check():
        collection.guard(job.id, owner, credential)

    with pytest.raises(AdapterError, match=failure):
        await collection._download_batch(
            {"settings": settings, "download": fetch}, job.id, owner, credential, check, ahead
        )
    await ahead.close()
    assert calls == [items[1].url]
    assert not StorageReservation.select().exists()


@pytest.mark.parametrize("action", ["pause", "cancel"])
async def test_stopping_parallel_group_waits_for_all_network_cleanup(client, settings, action):
    connected(settings)
    page = BookmarkPage(items=[work(count=5)], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))
    all_started = asyncio.Event()
    active, cleaned = set(), set()

    async def fetch(url):
        active.add(url)
        if len(active) == 3:
            all_started.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            active.remove(url)
            cleaned.add(url)

    ctx["download"] = fetch
    job = jobs.enqueue_account("sync", f"stop-parallel-{action}", "public")
    task = asyncio.create_task(run_job(ctx, job.id))
    await asyncio.wait_for(all_started.wait(), 5)
    jobs.action(job.id, action)
    await asyncio.wait_for(task, 5)
    assert Job.get_by_id(job.id).state == ("paused" if action == "pause" else "cancelled")
    assert not active and len(cleaned) == 3
    assert not StorageReservation.select().exists()
    assert not Asset.select().exists()
    assert list((settings.data_dir / "tmp").iterdir()) == []


async def test_parallel_publication_disk_full_leaves_downloads_pending(
    client, settings, monkeypatch
):
    connected(settings)
    page = BookmarkPage(items=[work(count=3)], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "synthetic disk full")

    monkeypatch.setattr(storage, "store_image", full)
    job = jobs.enqueue_account("sync", "parallel-disk-full", "public")
    await run_job(ctx, job.id)
    assert Job.get_by_id(job.id).state == "waiting_storage"
    assert all(item.state == "pending" for item in DownloadTask.select())
    assert not StorageReservation.select().exists()
    assert list((settings.data_dir / "tmp").iterdir()) == []


async def test_deleted_artwork_after_parallel_reads_never_publishes_files(client, settings):
    connected(settings)
    page = BookmarkPage(items=[work(count=3)], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))
    received = 0

    async def fetch(url):
        nonlocal received
        received += 1
        if received == 3:
            Tombstone.create(artwork_id="123")
        return image_bytes(0)

    ctx["download"] = fetch
    job = jobs.enqueue_account("sync", "parallel-deleted", "public")
    await run_job(ctx, job.id)
    assert received == 3
    assert all(item.state == "excluded" for item in DownloadTask.select())
    assert not Asset.select().exists()
    assert not StorageReservation.select().exists()
