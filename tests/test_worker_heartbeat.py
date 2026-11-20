"""Job leases must outlive event-loop stalls without reviving fenced owners."""

import asyncio
import queue
import threading
from types import SimpleNamespace

import pytest
from peewee import OperationalError

from app import worker
from app.core.db import db
from app.models.entities import Job, Lease
from app.services import jobs


class SteppedEvent:
    """Drive the heartbeat's five-second wait with barriers instead of wall time."""

    def __init__(self):
        self.requested = threading.Event()
        self.steps = threading.Semaphore(0)
        self.waiting = queue.Queue()
        self.timed_out = False

    def is_set(self):
        return self.requested.is_set()

    def set(self):
        self.requested.set()
        self.steps.release()

    def wait(self, timeout):
        self.waiting.put((timeout, db.is_closed()))
        if not self.steps.acquire(timeout=5):
            self.timed_out = True
            return True
        return self.is_set()

    def tick(self):
        self.steps.release()

    def renewed(self):
        assert self.waiting.get(timeout=3) == (5, True)


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(current=jobs.now())
    monkeypatch.setattr(jobs, "now", lambda: clock.current)
    return clock


@pytest.fixture
def pulses(monkeypatch):
    original = worker.JobHeartbeat
    instances = []

    class ControlledHeartbeat(original):
        def __init__(self, job_id, owner):
            super().__init__(job_id, owner)
            self.stop = SteppedEvent()
            self.closed_on_exit = False
            instances.append(self)

        def run(self):
            try:
                super().run()
            finally:
                self.closed_on_exit = db.is_closed()

    monkeypatch.setattr(worker, "JobHeartbeat", ControlledHeartbeat)
    yield instances
    for pulse in instances:
        pulse.stop.set()
    for pulse in instances:
        if pulse.thread.ident is not None:
            pulse.thread.join(timeout=5)
        assert not pulse.thread.is_alive()
        assert not pulse.stop.timed_out


def new_job(name="heartbeat"):
    return Job.create(kind="integrity", idempotency_key=name, lease_key=name)


async def checkpoint():
    for _ in range(3):
        await asyncio.sleep(0)


async def test_heartbeat_renews_while_async_main_is_blocked(client, clock, pulses, monkeypatch):
    job = new_job()
    main_connection = db.connection()
    main_thread = threading.get_ident()
    observations = []
    loop_ran = threading.Event()
    original = jobs.heartbeat

    def heartbeat(job_id, owner):
        observations.append((threading.get_ident(), db.connection()))
        return original(job_id, owner)

    async def blocked_main(ctx, job_id, owner):
        pulse = pulses[0]
        # No await: both barriers block the event-loop thread itself.
        pulse.stop.renewed()
        assert not loop_ran.is_set()
        for _ in range(2):
            clock.current += 35
            pulse.stop.tick()
            pulse.stop.renewed()
            assert not loop_ran.is_set()
            current = Job.get_by_id(job_id)
            assert current.heartbeat == clock.current
            assert current.lease_until == clock.current + 120
            assert Lease.get_by_id(current.lease_key).expires_at == current.lease_until
        jobs.finish(job_id, owner, "succeeded")

    monkeypatch.setattr(jobs, "heartbeat", heartbeat)
    monkeypatch.setattr(worker.integrity, "execute", blocked_main)
    asyncio.get_running_loop().call_soon(loop_ran.set)
    ctx = {}
    await worker.run_job(ctx, job.id)
    assert len(observations) == 3
    assert all(identity != main_thread for identity, _ in observations)
    assert all(connection is not main_connection for _, connection in observations)
    assert Job.get_by_id(job.id).state == "succeeded"
    assert pulses[0].closed_on_exit
    assert not pulses[0].thread.is_alive()
    assert ctx["job_heartbeats"] == set()


@pytest.mark.parametrize("cancel_join", [False, True])
async def test_job_waits_for_heartbeat_join_even_with_repeated_cancellation(
    client, pulses, monkeypatch, cancel_join
):
    job = new_job()
    entered, release = threading.Event(), threading.Event()
    original = jobs.heartbeat
    resolved = []

    def heartbeat(job_id, owner):
        entered.set()
        assert release.wait(5)
        return original(job_id, owner)

    async def complete(ctx, job_id, owner):
        assert entered.wait(3)
        jobs.finish(job_id, owner, "succeeded")

    def resolve():
        assert pulses[0].closed_on_exit
        assert not pulses[0].thread.is_alive()
        resolved.append(True)

    monkeypatch.setattr(jobs, "heartbeat", heartbeat)
    monkeypatch.setattr(worker.integrity, "execute", complete)
    monkeypatch.setattr(jobs, "resolve_children", resolve)
    ctx = {}
    task = asyncio.create_task(worker.run_job(ctx, job.id))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        pulse = pulses[0]
        assert await asyncio.to_thread(pulse.stop.requested.wait, 3)
        assert pulse.thread.is_alive()
        assert not task.done()
        if cancel_join:
            task.cancel()
            await checkpoint()
            task.cancel()
            await checkpoint()
            assert not task.done()
        release.set()
        if cancel_join:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            await asyncio.wait_for(task, 3)
        assert resolved == [True]
        assert ctx["job_heartbeats"] == set()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_shutdown_joins_heartbeat_before_database_and_data_lock_close(
    client, pulses, monkeypatch
):
    job = new_job()
    owner = jobs.claim(job.id)
    entered, release, main_closed, access_closed = (threading.Event() for _ in range(4))
    original_heartbeat, original_close = jobs.heartbeat, db.close
    main_thread = threading.get_ident()

    def heartbeat(job_id, owner):
        entered.set()
        assert release.wait(5)
        return original_heartbeat(job_id, owner)

    def close():
        if threading.get_ident() == main_thread:
            assert pulses[0].closed_on_exit
            assert not pulses[0].thread.is_alive()
            main_closed.set()
        return original_close()

    class Access:
        def __exit__(self, *args):
            assert main_closed.is_set()
            assert not pulses[0].thread.is_alive()
            access_closed.set()

    monkeypatch.setattr(jobs, "heartbeat", heartbeat)
    monkeypatch.setattr(db, "close", close)
    pulse = worker.JobHeartbeat(job.id, owner)
    pulse.start()
    ctx = {"job_heartbeats": {pulse}, "data_access": Access()}
    task = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task = asyncio.create_task(worker.shutdown(ctx))
        assert await asyncio.to_thread(pulse.stop.requested.wait, 3)
        task.cancel()
        await checkpoint()
        task.cancel()
        await checkpoint()
        assert not task.done()
        assert not main_closed.is_set()
        assert not access_closed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert main_closed.is_set() and access_closed.is_set()
        assert db.is_closed()
        assert ctx["job_heartbeats"] == set()
    finally:
        release.set()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await worker.stop_heartbeats((pulse,))


@pytest.mark.parametrize("expired", [False, True])
async def test_transient_database_failure_is_contained_without_reviving_expired_owner(
    client, clock, pulses, monkeypatch, caplog, expired
):
    job = new_job()
    owner = jobs.claim(job.id)
    original = jobs.heartbeat
    attempts = []

    def heartbeat(job_id, owner):
        attempts.append(threading.get_ident())
        if len(attempts) == 1:
            raise OperationalError("synthetic-private-database-error")
        return original(job_id, owner)

    monkeypatch.setattr(jobs, "heartbeat", heartbeat)
    pulse = worker.JobHeartbeat(job.id, owner)
    pulse.start()
    try:
        pulse.stop.renewed()
        before = Job.get_by_id(job.id)
        clock.current += 120 if expired else 5
        pulse.stop.tick()
        if expired:
            pulse.thread.join(timeout=3)
            assert not pulse.thread.is_alive()
            current = Job.get_by_id(job.id)
            assert (current.heartbeat, current.lease_until) == (
                before.heartbeat,
                before.lease_until,
            )
        else:
            pulse.stop.renewed()
            current = Job.get_by_id(job.id)
            assert current.heartbeat == clock.current
            assert current.lease_until == clock.current + 120
            assert pulse.thread.is_alive()
        assert len(attempts) == 2
    finally:
        await worker.stop_heartbeats((pulse,))
    assert pulse.closed_on_exit
    assert "worker_job_heartbeat_retry" in caplog.text
    assert "synthetic-private-database-error" not in caplog.text


@pytest.mark.parametrize(
    "lost",
    ["job_expired", "job_replaced", "lease_expired", "lease_replaced", "lease_missing", "finished"],
)
def test_heartbeat_never_renews_lost_or_expired_fences(client, clock, lost):
    job = new_job()
    owner = jobs.claim(job.id)
    assert Job.get_by_id(job.id).lease_until == clock.current + 120
    assert Lease.get_by_id(job.lease_key).expires_at == clock.current + 120
    if lost == "job_expired":
        Job.update(lease_until=clock.current).where(Job.id == job.id).execute()
    elif lost == "job_replaced":
        Job.update(owner="replacement-owner").where(Job.id == job.id).execute()
    elif lost == "lease_expired":
        Lease.update(expires_at=clock.current).where(Lease.key == job.lease_key).execute()
    elif lost == "lease_replaced":
        Lease.update(owner="replacement-owner").where(Lease.key == job.lease_key).execute()
    elif lost == "lease_missing":
        Lease.delete().where(Lease.key == job.lease_key).execute()
    else:
        Job.update(state="succeeded").where(Job.id == job.id).execute()
    before_job = Job.get_by_id(job.id).__data__.copy()
    before_lease = list(Lease.select().dicts())
    assert jobs.heartbeat(job.id, owner) is False
    assert Job.get_by_id(job.id).__data__ == before_job
    assert list(Lease.select().dicts()) == before_lease


async def test_ownership_loss_stops_thread_without_reacquiring(client, clock, pulses):
    job = new_job()
    owner = jobs.claim(job.id)
    pulse = worker.JobHeartbeat(job.id, owner)
    pulse.start()
    try:
        pulse.stop.renewed()
        Job.update(owner="replacement-owner").where(Job.id == job.id).execute()
        Lease.update(owner="replacement-owner").where(Lease.key == job.lease_key).execute()
        before = Job.get_by_id(job.id).heartbeat
        clock.current += 5
        pulse.stop.tick()
        pulse.thread.join(timeout=3)
        assert not pulse.thread.is_alive()
        assert Job.get_by_id(job.id).heartbeat == before
        assert Lease.get_by_id(job.lease_key).owner == "replacement-owner"
    finally:
        await worker.stop_heartbeats((pulse,))
    assert pulse.closed_on_exit


def test_dead_worker_remains_recoverable_after_120_seconds(client, clock):
    job = new_job()
    owner = jobs.claim(job.id)
    assert jobs.LEASE_SECONDS == 120
    clock.current += 119
    jobs.recover_expired()
    assert Job.get_by_id(job.id).owner == owner
    clock.current += 1
    jobs.recover_expired()
    current = Job.get_by_id(job.id)
    assert current.state == "queued" and current.owner is None
    assert not Lease.select().exists()
    assert jobs.heartbeat(job.id, owner) is False


async def test_rejected_claim_does_not_start_heartbeat(client, monkeypatch):
    job = new_job()
    assert jobs.claim(job.id)

    def unexpected(*args):
        pytest.fail("A rejected delivery started another heartbeat")

    monkeypatch.setattr(worker, "JobHeartbeat", unexpected)
    ctx = {}
    await worker.run_job(ctx, job.id)
    assert "job_heartbeats" not in ctx


async def test_failed_thread_start_releases_claim_and_registry(client, pulses, monkeypatch):
    job = new_job()

    def failed_start(self):
        raise RuntimeError("synthetic-thread-start-failure")

    monkeypatch.setattr(worker.JobHeartbeat, "start", failed_start)
    ctx = {}
    with pytest.raises(RuntimeError, match="archive_job_failed"):
        await worker.run_job(ctx, job.id)
    assert Job.get_by_id(job.id).state == "failed"
    assert not Lease.select().exists()
    assert ctx["job_heartbeats"] == set()
    assert pulses[0].thread.ident is None
