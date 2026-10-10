"""Deferred regression checks for DB checkpoints and expendable Redis deliveries."""

import asyncio
import contextlib
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.async_cleanup import settle
from app.core.db import db, initialize
from app.core.time import now
from app.models.entities import (
    Asset,
    IntegrityIssue,
    Job,
    JobEvent,
    Lease,
    Outbox,
    StorageReservation,
)
from app.services import backup, integrity, jobs, library, previews, storage
from app.services.execution import disk_operation, watch
from app.services.fixtures import image_bytes
from app.worker import run_job
from tests.test_library import remote


def position(phase="verifying", *, upper=7, last=3, last_artwork=""):
    return json.dumps(
        {
            "verification_checkpoint": 1,
            "upper": upper,
            "last": last,
            "last_artwork": last_artwork,
            "phase": phase,
        }
    )


def originals(settings, count=3):
    result = []
    for index in range(count):
        artwork_id = str(123 + index)
        library.ingest(remote(id=artwork_id), "public")
        result.append(storage.store_image(settings, artwork_id, 0, image_bytes(index)))
    return result


class RedisDeliveries:
    def __init__(self, retained=()):
        self.retained = set(retained)
        self.accepted = []

    async def enqueue_job(self, name, job_id, *, _job_id):
        if _job_id in self.retained:
            return None
        self.retained.add(_job_id)
        self.accepted.append((name, job_id, _job_id))
        return _job_id


async def test_orphaned_redis_execution_does_not_block_db_recovery(client):
    job = jobs.enqueue_integrity("one")
    old = jobs.claim(job.id)
    redis = RedisDeliveries([f"pixchive:{job.id}"])
    Job.update(lease_until=now() - 1).where(Job.id == job.id).execute()
    await jobs.dispatch(redis)
    first = redis.accepted[0]
    assert first[:2] == ("run_job", job.id)
    assert first[2].startswith(f"pixchive:{job.id}:")

    Outbox.update(delivered_at=now() - 11).where(Outbox.job == job).execute()
    await jobs.dispatch(redis)
    assert len(redis.accepted) == 2 and first[2] != redis.accepted[1][2]
    owner = jobs.claim(job.id)
    assert owner and owner != old
    assert jobs.claim(job.id) is None
    jobs.continue_interrupted(job.id, old)
    assert Job.get_by_id(job.id).owner == owner


async def test_undelivered_job_is_not_starved_by_older_queued_work(client):
    for index in range(101):
        job = Job.create(
            kind="preview",
            scope=str(index),
            idempotency_key=f"preview:{index}",
            lease_key=f"convert:{index}",
        )
        Outbox.create(job=job, delivered_at=now() - 20 if index < 100 else None)
    newest = job.id
    redis = RedisDeliveries()
    await jobs.dispatch(redis)
    assert len(redis.accepted) == 100
    assert redis.accepted[0][1] == newest


def test_interruption_preserves_progress_and_releases_only_current_ownership(client):
    job = jobs.enqueue_integrity("one")
    owner = jobs.claim(job.id)
    marker = position()
    Job.update(summary=marker, processed=3, failed=1).where(Job.id == job.id).execute()
    StorageReservation.create(job=job, owner=owner, bytes=1024)
    Outbox.update(delivered_at=now()).where(Outbox.job == job).execute()

    jobs.continue_interrupted(job.id, owner)

    result = Job.get_by_id(job.id)
    assert (result.state, result.processed, result.failed, result.summary) == (
        "queued",
        3,
        1,
        marker,
    )
    assert result.active_key == "integrity" and result.owner is None
    assert result.lease_until is None and not Lease.select().exists()
    assert not StorageReservation.select().exists()
    assert Outbox.get(Outbox.job == job).delivered_at is None
    assert JobEvent.get(JobEvent.code == "execution_interrupted").job_id == job.id


@pytest.mark.parametrize("command,state", [("pause", "paused"), ("cancel", "cancelled")])
def test_interruption_acknowledges_requested_stop_instead_of_requeue(client, command, state):
    job = jobs.enqueue_integrity("one")
    owner = jobs.claim(job.id)
    jobs.action(job.id, command)
    jobs.continue_interrupted(job.id, owner)
    assert Job.get_by_id(job.id).state == state
    assert not Lease.select().exists()
    assert not JobEvent.select().where(JobEvent.code == "execution_interrupted").exists()


@pytest.mark.parametrize("state", ["paused", "waiting_storage", "failed", "partial"])
@pytest.mark.parametrize("command", ["retry", "resume"])
def test_integrity_retry_resets_only_finished_failure(client, state, command):
    job = jobs.enqueue_integrity("one")
    marker = position("thumbnails", last_artwork="123")
    Job.update(state=state, summary=marker, processed=3, failed=1, total=3).where(
        Job.id == job.id
    ).execute()
    result = jobs.action(job.id, command)
    if state in ("failed", "partial"):
        assert (result.summary, result.processed, result.failed, result.total) == (None, 0, 0, None)
        assert result.phase == "verifying"
    else:
        assert (result.summary, result.processed, result.failed, result.total) == (marker, 3, 1, 3)


async def test_file_verification_continues_after_committed_cursor_without_duplicate_issues(
    client, settings, monkeypatch
):
    sources = originals(settings)
    storage.safe_path(settings, sources[0].path).unlink()
    job = jobs.enqueue_integrity("one")
    hashed = []
    interrupt = True

    async def operation(fn, *args, **kwargs):
        nonlocal interrupt
        if fn is storage.verify_asset:
            if hashed and interrupt:
                interrupt = False
                raise asyncio.CancelledError
            hashed.append(args[1].id)
        return await disk_operation(fn, *args, **kwargs)

    monkeypatch.setattr(integrity, "disk_operation", operation)
    with pytest.raises(asyncio.CancelledError):
        await run_job({"settings": settings}, job.id)
    interrupted = Job.get_by_id(job.id)
    marker = integrity.checkpoint(interrupted.summary)
    assert (interrupted.state, interrupted.processed, interrupted.failed) == ("queued", 1, 1)
    assert marker["last"] > 0 and marker["phase"] == "verifying"

    await run_job({"settings": settings}, job.id)

    result = Job.get_by_id(job.id)
    assert hashed == [source.id for source in sources]
    assert (result.state, result.processed, result.total, result.failed) == ("partial", 3, 3, 1)
    assert IntegrityIssue.select().where(IntegrityIssue.job == job).count() == 1


async def test_thumbnail_checkpoint_resumes_and_excludes_new_originals(
    client, settings, monkeypatch
):
    sources = originals(settings, 2)
    job = jobs.enqueue_integrity("one")
    generated = []
    interrupt = True

    async def operation(fn, *args, **kwargs):
        nonlocal interrupt
        if fn is previews.build:
            if generated and interrupt:
                interrupt = False
                raise asyncio.CancelledError
            generated.append(args[1])
        return await disk_operation(fn, *args, **kwargs)

    monkeypatch.setattr(integrity, "disk_operation", operation)
    with pytest.raises(asyncio.CancelledError):
        await run_job({"settings": settings}, job.id)
    interrupted = Job.get_by_id(job.id)
    marker = integrity.checkpoint(interrupted.summary)
    assert (marker["phase"], marker["last_artwork"]) == ("thumbnails", "123")
    assert (interrupted.processed, interrupted.total) == (2, 2)

    newer = storage.store_image(settings, "124", 0, image_bytes(8))
    library.ingest(remote(id="125"), "public")
    later = storage.store_image(settings, "125", 0, image_bytes(9))
    await run_job({"settings": settings}, job.id)

    assert generated == ["123", "124"]
    assert all(Asset.get_by_id(source.id).thumbnail_id for source in sources)
    assert Asset.get_by_id(newer.id).thumbnail_id is None
    assert Asset.get_by_id(later.id).thumbnail_id is None
    assert Job.get_by_id(job.id).processed == 2
    assert Job.get_by_id(job.id).state == "succeeded"


async def test_resumed_thumbnail_failure_does_not_duplicate_a_files_previous_issue(
    client, settings, monkeypatch
):
    source = originals(settings, 1)[0]
    job = jobs.enqueue_integrity("one")
    Job.update(
        summary=position("thumbnails", upper=1, last=1), processed=1, failed=1, total=1
    ).where(Job.id == job.id).execute()
    IntegrityIssue.create(job=job, asset=source, code="file_missing")

    def failed(*args):
        raise OSError("synthetic preview failure")

    monkeypatch.setattr(previews, "build", failed)
    await run_job({"settings": settings}, job.id)

    result = Job.get_by_id(job.id)
    assert (result.state, result.failed) == ("partial", 1)
    assert IntegrityIssue.select().where(IntegrityIssue.job == job).count() == 1
    assert IntegrityIssue.get(IntegrityIssue.job == job).code == "file_missing"


def test_restore_resets_unfinished_verification_but_keeps_terminal_history(
    client, settings, tmp_path
):
    source = originals(settings, 1)[0]
    terminal = jobs.enqueue_integrity("finished")
    owner = jobs.claim(terminal.id)
    marker = position("finished", upper=1, last=1)
    Job.update(summary=marker, processed=1, failed=1, total=1).where(
        Job.id == terminal.id
    ).execute()
    IntegrityIssue.create(job=terminal, asset=source, code="file_corrupt")
    jobs.finish(terminal.id, owner, "partial")
    unfinished = jobs.enqueue_integrity("pending")
    Job.update(summary=marker, processed=1, failed=1, total=1).where(
        Job.id == unfinished.id
    ).execute()
    IntegrityIssue.create(job=unfinished, asset=source, code="file_missing")
    snapshot = backup.create(settings, tmp_path / "backups", media=True)
    target = replace(settings, data_dir=tmp_path / "restored")

    backup.restore(target, Path(snapshot["path"]), None, full_hash=True)
    initialize(target.data_dir)
    try:
        restored = Job.get_by_id(unfinished.id)
        assert (restored.summary, restored.processed, restored.failed, restored.total) == (
            None,
            0,
            0,
            None,
        )
        assert restored.phase == "verifying"
        assert not IntegrityIssue.select().where(IntegrityIssue.job == unfinished).exists()
        assert Job.get_by_id(terminal.id).summary == marker
        assert IntegrityIssue.get(IntegrityIssue.job == terminal).code == "file_corrupt"
    finally:
        db.close()


async def test_repeated_cancellation_waits_for_actual_disk_write_completion():
    started, release, completed = threading.Event(), threading.Event(), threading.Event()

    def write():
        started.set()
        release.wait(3)
        completed.set()

    task = asyncio.create_task(disk_operation(write))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not completed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert completed.is_set()
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_repeated_cancellation_waits_for_operation_cleanup():
    started, closing, release, completed = (asyncio.Event() for _ in range(4))

    async def operation():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            closing.set()
            await settle(release.wait())
            completed.set()

    task = asyncio.create_task(watch(operation(), lambda: None))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not completed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert completed.is_set()
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
