"""Deferred regression coverage for parent actions and shared conversions."""

import uuid

import pytest

from app.core.errors import DomainError
from app.core.time import now
from app.models.entities import Asset, Job, JobDependency, JobEvent, Outbox
from app.services import jobs


def make_job(*, kind="sync", scope="public", state="queued", mode="normal", **values):
    job = Job.create(
        kind=kind,
        scope=scope,
        state=state,
        mode=mode if kind == "sync" else None,
        lease_key=f"convert:{scope}" if kind == "convert" else "account:1",
        idempotency_key=str(uuid.uuid4()),
        **values,
    )
    if state not in ("succeeded", "failed", "partial", "cancelled"):
        job.active_key = jobs.dedupe_key(job)
        job.save()
    Outbox.create(job=job, delivered_at=1)
    return job


def conversion(state="running"):
    return make_job(
        kind="convert",
        scope="shared-source",
        state=state,
        owner="conversion-owner" if state in jobs.ACTIVE else None,
        lease_until=now() + 30 if state in jobs.ACTIVE else None,
    )


def link(parent, child):
    return JobDependency.create(parent=parent, child=child)


def test_merged_resume_does_not_reactivate_original_children(client):
    original = make_job(state="paused")
    child = conversion("paused")
    link(original, child)
    full = make_job(mode="full")

    result = jobs.action(original.id, "resume")

    assert result.id == full.id
    assert Job.get_by_id(original.id).state == "paused"
    assert Job.get_by_id(child.id).state == "paused"
    assert not JobEvent.select().exists()
    assert all(row.delivered_at == 1 for row in Outbox.select())


@pytest.mark.parametrize("state,command", [("paused", "resume"), ("partial", "retry")])
def test_accepted_parent_reactivates_paused_conversion(client, state, command):
    parent = make_job(state=state)
    child = conversion("paused")
    link(parent, child)

    assert jobs.action(parent.id, command).id == parent.id

    assert Job.get_by_id(parent.id).state == "queued"
    assert Job.get_by_id(child.id).state == "queued"
    assert all(row.delivered_at is None for row in Outbox.select())


def test_parent_retry_follows_replacement_conversion_and_keeps_failed_history(client):
    parent = make_job(state="partial")
    old = conversion("failed")
    link(parent, old)
    jobs.event(old, "failed", "conversion_failed")
    replacement = conversion("queued")

    jobs.action(parent.id, "retry")

    assert JobDependency.get(JobDependency.parent == parent).child_id == replacement.id
    assert Job.get_by_id(old.id).state == "failed"
    assert JobEvent.get(JobEvent.job == old).code == "conversion_failed"
    assert Job.get_by_id(parent.id).state == "queued"


@pytest.mark.parametrize("state", ["pausing", "cancelling"])
def test_parent_retry_while_conversion_stops_rolls_back_every_change(client, state):
    parent = make_job(state="partial", processed=3, failed=1, total=4)
    child = conversion(state)
    link(parent, child)

    with pytest.raises(DomainError) as error:
        jobs.action(parent.id, "retry")

    assert error.value.code == "job_stopping"
    original = Job.get_by_id(parent.id)
    assert (original.state, original.processed, original.failed, original.total) == (
        "partial",
        3,
        1,
        4,
    )
    assert Job.get_by_id(child.id).state == state
    assert all(row.delivered_at == 1 for row in Outbox.select())
    assert not JobEvent.select().exists()


@pytest.mark.parametrize("command,expected", [("pause", "paused"), ("cancel", "cancelled")])
def test_shared_conversion_continues_for_another_running_parent(client, command, expected):
    parent = make_job(state="waiting_children")
    other = make_job(scope="private", state="waiting_children")
    child = conversion()
    link(parent, child)
    link(other, child)

    assert jobs.action(parent.id, command).state == expected
    assert Job.get_by_id(child.id).state == "running"
    assert Job.get_by_id(child.id).owner == "conversion-owner"
    assert Job.get_by_id(other.id).state == "waiting_children"


def test_last_paused_parent_waits_for_shared_conversion_acknowledgement(client):
    first = make_job(state="waiting_children")
    last = make_job(scope="private", state="waiting_children")
    child = conversion()
    link(first, child)
    link(last, child)

    assert jobs.action(first.id, "pause").state == "paused"
    assert jobs.action(last.id, "pause").state == "pausing"
    assert Job.get_by_id(child.id).state == "pausing"
    jobs.finish(child.id, "conversion-owner", "succeeded")
    jobs.resolve_children()

    assert Job.get_by_id(child.id).state == "paused"
    assert Job.get_by_id(first.id).state == "paused"
    assert Job.get_by_id(last.id).state == "paused"


def test_cancel_keeps_conversion_resumable_for_another_paused_parent(client):
    parent = make_job(state="waiting_children")
    other = make_job(scope="private", state="paused")
    child = conversion()
    link(parent, child)
    link(other, child)

    assert jobs.action(parent.id, "cancel").state == "cancelling"
    assert Job.get_by_id(child.id).state == "pausing"
    jobs.finish(child.id, "conversion-owner", "succeeded")
    jobs.resolve_children()

    assert Job.get_by_id(parent.id).state == "cancelled"
    assert Job.get_by_id(child.id).state == "paused"
    assert Job.get_by_id(other.id).state == "paused"
    jobs.action(other.id, "resume")
    assert Job.get_by_id(child.id).state == "queued"


def test_queued_parent_pause_waits_for_its_running_conversion(client):
    parent = make_job()
    child = conversion()
    link(parent, child)

    assert jobs.action(parent.id, "pause").state == "pausing"
    jobs.finish(child.id, "conversion-owner", "succeeded")
    jobs.resolve_children()

    assert Job.get_by_id(parent.id).state == "paused"


def test_parent_retry_creates_new_conversion_after_cancel_preserving_originals(client, settings):
    from tests.test_ugoira import store

    _, old = store(settings)
    jobs.action(old.id, "cancel")
    parent = make_job(state="partial")
    link(parent, old)
    original_ids = {row.id for row in Asset.select()}

    jobs.action(parent.id, "retry")

    replacement = JobDependency.get(JobDependency.parent == parent).child
    assert replacement.id != old.id
    assert replacement.state == "queued"
    assert Job.get_by_id(old.id).state == "cancelled"
    assert {row.id for row in Asset.select()} == original_ids
