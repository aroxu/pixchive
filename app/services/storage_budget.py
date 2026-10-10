"""Conservative temporary-space budgets shared by every worker on the data volume."""

import errno
import json
import shutil
from contextlib import contextmanager, nullcontext

from peewee import fn

from app.core.db import db
from app.core.time import now
from app.models.entities import Job, StorageReservation
from app.services.execution import Interrupted

ACTIVE = ("running", "pausing", "cancelling")
PREVIEW_BYTES = 8 * 1024 * 1024
BUDGET_FIELDS = ("free_bytes", "additional_bytes", "reserved_bytes", "reserve_bytes", "checked_at")


def live():
    return (
        StorageReservation.select()
        .join(Job)
        .where(
            (Job.owner == StorageReservation.owner)
            & (Job.lease_until > now())
            & Job.state.in_(ACTIVE)
        )
    )


def cleanup():
    """Caller holds the short write transaction; dead owners do not reserve space."""
    StorageReservation.delete().where(
        StorageReservation.job.not_in(live().select(StorageReservation.job))
    ).execute()


def total() -> int:
    return live().select(fn.COALESCE(fn.SUM(StorageReservation.bytes), 0)).scalar()


def release(job_id: str, owner: str):
    StorageReservation.delete().where(
        (StorageReservation.job == job_id) & (StorageReservation.owner == owner)
    ).execute()


def view(value: str | None) -> dict | None:
    try:
        parsed = json.loads(value) if value else None
        if not isinstance(parsed, dict) or not all(
            type(parsed.get(key)) is int and parsed[key] >= 0 for key in BUDGET_FIELDS
        ):
            return None
        return {key: parsed[key] for key in BUDGET_FIELDS}
    except (TypeError, ValueError):
        return None


@contextmanager
def hold(settings, job_id: str, owner: str, additional_bytes: int, check):
    """Budget before I/O. Nested holds add to, then restore, their owner's budget."""
    if type(additional_bytes) is not int or additional_bytes < 0:
        raise ValueError("Invalid storage budget")
    connection = db.connection_context() if db.is_closed() else nullcontext()
    with connection:
        check()
        with db.atomic("IMMEDIATE"):
            check()
            job = Job.get_or_none(
                (Job.id == job_id)
                & (Job.owner == owner)
                & (Job.lease_until > now())
                & (Job.state == "running")
            )
            if job is None:
                raise Interrupted
            cleanup()
            # Sample after taking the reservation lock: an earlier owner may have
            # finished a write and released its budget while we were waiting.
            free = shutil.disk_usage(settings.data_dir).free
            current = StorageReservation.get_or_none(StorageReservation.job == job_id)
            previous = current.bytes if current is not None else 0
            allocated = previous + additional_bytes
            reserved = (
                live()
                .where(StorageReservation.job != job_id)
                .select(fn.COALESCE(fn.SUM(StorageReservation.bytes), 0))
                .scalar()
            )
            budget = {
                "free_bytes": free,
                "additional_bytes": allocated,
                "reserved_bytes": reserved,
                "reserve_bytes": settings.min_free_bytes,
                "checked_at": now(),
            }
            Job.update(storage_budget=json.dumps(budget)).where(Job.id == job_id).execute()
            sufficient = free >= allocated + reserved + settings.min_free_bytes
            if sufficient:
                StorageReservation.insert(job=job_id, owner=owner, bytes=allocated).on_conflict(
                    conflict_target=[StorageReservation.job],
                    update={StorageReservation.owner: owner, StorageReservation.bytes: allocated},
                ).execute()
        if not sufficient:
            # Persist the estimate before waiting_storage; never write a partial output first.
            raise OSError(errno.ENOSPC, "storage_budget")
        try:
            yield
        finally:
            with db.atomic("IMMEDIATE"):
                owned = (
                    (StorageReservation.job == job_id)
                    & (StorageReservation.owner == owner)
                    & (StorageReservation.bytes == allocated)
                )
                if previous:
                    StorageReservation.update(bytes=previous).where(owned).execute()
                else:
                    StorageReservation.delete().where(owned).execute()
