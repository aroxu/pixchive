import asyncio

import pytest

from app.core.db import db, initialize
from app.core.errors import DomainError
from app.core.time import now
from app.migrations.runner import migrate
from app.models.entities import Artwork, Asset, Job, Lease, Outbox
from app.services import jobs, library, storage
from app.services.fixtures import image_bytes, seed
from app.worker import run_job
from tests.test_library import remote


def test_atomic_file_move_crash_recovery(client, settings):
    library.ingest(remote(), "public")

    def crash():
        raise RuntimeError("simulated power loss after move")

    with pytest.raises(RuntimeError):
        storage.store_image(settings, "123", 0, image_bytes(0), after_move=crash)
    assert Asset.get().state == "staged"
    assert Artwork.get().archive_status == "pending"
    storage.reconcile(settings)
    assert Asset.get().state == "ready"
    assert Artwork.get().archive_status == "complete"
    storage.store_image(settings, "123", 0, image_bytes(0))
    assert Asset.select().count() == 1


def test_original_revisions_and_path_guard(client, settings):
    library.ingest(remote(), "public")
    first = storage.store_image(settings, "123", 0, image_bytes(0))
    second = storage.store_image(settings, "123", 0, image_bytes(1))
    assert first.path != second.path
    assert storage.safe_path(settings, first.path).is_file()
    with pytest.raises(DomainError):
        storage.safe_path(settings, "../../secrets/key")
    with pytest.raises(DomainError):
        storage.store_image(settings, "123", 0, b"not an image")


def test_outbox_and_claim_are_idempotent(client):
    a = jobs.enqueue_integrity("one")
    assert jobs.enqueue_integrity("one").id == a.id
    assert jobs.enqueue_integrity("two").id == a.id
    assert Outbox.select().count() == 1
    assert jobs.claim(a.id)
    assert jobs.claim(a.id) is None


def test_expired_owner_cannot_finish_or_renew_after_recovery(client):
    a = jobs.enqueue_integrity("one")
    old = jobs.claim(a.id)
    Job.update(lease_until=now() - 1).where(Job.id == a.id).execute()
    jobs.recover_expired()
    new = jobs.claim(a.id)
    assert new != old
    assert not jobs.heartbeat(a.id, old)
    jobs.finish(a.id, old, "succeeded")
    assert Job.get_by_id(a.id).owner == new
    jobs.finish(a.id, new, "succeeded")
    assert Job.get_by_id(a.id).state == "succeeded"


def test_pause_and_cancel_require_worker_ack(client):
    a = jobs.enqueue_integrity("one")
    owner = jobs.claim(a.id)
    assert jobs.action(a.id, "pause").state == "pausing"
    jobs.finish(a.id, owner, "succeeded")
    assert Job.get_by_id(a.id).state == "paused"
    assert jobs.action(a.id, "resume").state == "queued"
    owner = jobs.claim(a.id)
    assert jobs.action(a.id, "cancel").state == "cancelling"
    jobs.finish(a.id, owner, "succeeded")
    assert Job.get_by_id(a.id).state == "cancelled"
    assert Lease.select().count() == 0


async def test_redis_loss_redelivers_persistent_jobs(client):
    a = jobs.enqueue_integrity("one")

    class FakeRedis:
        def __init__(self):
            self.messages = []

        async def enqueue_job(self, *args, **kwargs):
            self.messages.append(args)

    redis = FakeRedis()
    await jobs.dispatch(redis)
    assert redis.messages == [("run_job", a.id)]
    redis.messages.clear()
    Outbox.update(delivered_at=now() - 11).execute()
    await jobs.dispatch(redis)
    assert redis.messages == [("run_job", a.id)]


async def test_worker_real_integrity_and_restart(client, settings):
    seed(settings)
    a = jobs.enqueue_integrity("one")
    db.close()
    initialize(settings.data_dir)
    migrate()
    assert Job.get_by_id(a.id).state == "queued"
    await run_job({"settings": settings}, a.id)
    completed = Job.get_by_id(a.id)
    assert completed.state == "succeeded"
    assert completed.processed == completed.total == 8
    asset = Asset.select().first()
    storage.safe_path(settings, asset.path).unlink()
    b = jobs.enqueue_integrity("two")
    await run_job({"settings": settings}, b.id)
    assert Job.get_by_id(b.id).state == "partial"
    assert Job.get_by_id(b.id).failed == 1
    assert Asset.get_by_id(asset.id).state == "missing"


async def test_cancellation_during_io_waits_for_ack(client, settings, monkeypatch):
    seed(settings)
    started = asyncio.Event()
    release = asyncio.Event()
    original_thread = asyncio.to_thread

    async def delayed(fn, *args):
        started.set()
        await release.wait()
        return await original_thread(fn, *args)

    monkeypatch.setattr("app.worker.asyncio.to_thread", delayed)
    job = jobs.enqueue_integrity("io")
    task = asyncio.create_task(run_job({"settings": settings}, job.id))
    await started.wait()
    assert jobs.action(job.id, "cancel").state == "cancelling"
    assert Job.get_by_id(job.id).state == "cancelling"
    release.set()
    await task
    assert Job.get_by_id(job.id).state == "cancelled"
