"""Regenerate a library thumbnail from a kept original, without Pixiv access."""

import errno

from app.core.db import db
from app.core.errors import DomainError
from app.models.entities import Asset, IntegrityIssue, Job, Tombstone
from app.services import jobs, library, storage, storage_budget
from app.services.execution import Interrupted, disk_operation


def request(artwork_id: str, key: str) -> Job:
    with db.atomic("IMMEDIATE"):
        artwork = library.require_artwork(artwork_id)
        if artwork.kind == "ugoira":
            raise DomainError(
                "invalid_artwork_type", "우고이라는 재생 파일 생성을 사용해 주세요.", 409
            )
        if storage.thumbnail_source(artwork_id) is None:
            raise DomainError("source_missing", "보관한 첫 페이지 원본이 필요합니다.", 409)
        return jobs.enqueue_preview(artwork_id, key)


def build(settings, artwork_id: str, source: Asset, check):
    with db.connection_context():
        check()
        state = storage.verify_asset(settings, source, check)
        if state != "ready":
            code = f"file_{state}" if state is not None else "source_missing"
            raise DomainError(code, "첫 페이지 원본을 확인하거나 복구해 주세요.", 409)
        check()
        return storage.make_thumbnail(settings, artwork_id, source, check)


def record_failure(job_id: str, owner: str, source: Asset | None, code: str):
    with db.atomic("IMMEDIATE"):
        job = jobs.owned(job_id, owner)
        if job is None or job.state != "running":
            return
        Job.update(failed=1).where(Job.id == job_id).execute()
        if (
            source is not None
            and Asset.select().where((Asset.id == source.id) & (Asset.state != "deleted")).exists()
            and not Tombstone.select().where(Tombstone.artwork_id == source.artwork_id).exists()
        ):
            IntegrityIssue.create(job=job_id, asset=source, code=code)


async def execute(ctx, job_id: str, owner: str):
    settings = ctx["settings"]
    artwork_id = jobs.require_job(job_id).scope
    source = None

    def check():
        job = jobs.owned(job_id, owner)
        if job is None or job.state != "running":
            raise Interrupted
        if Tombstone.select().where(Tombstone.artwork_id == artwork_id).exists():
            raise storage.excluded_error()

    try:
        with db.atomic("IMMEDIATE"):
            check()
            Job.update(total=1, processed=0, failed=0, phase="thumbnails").where(
                Job.id == job_id
            ).execute()
            IntegrityIssue.delete().where(IntegrityIssue.job == job_id).execute()
        source = storage.thumbnail_source(artwork_id)
        if source is None:
            raise DomainError("source_missing", "보관한 첫 페이지 원본이 필요합니다.", 409)
        with storage_budget.hold(settings, job_id, owner, storage_budget.PREVIEW_BYTES, check):
            await disk_operation(build, settings, artwork_id, source, check)
        with db.atomic("IMMEDIATE"):
            check()
            Job.update(processed=1, phase="finished").where(Job.id == job_id).execute()
        jobs.finish(job_id, owner, "succeeded")
    except Interrupted:
        jobs.finish(job_id, owner, "succeeded")  # Applies the durable pause/cancel request.
    except DomainError as exc:
        record_failure(job_id, owner, source, exc.code)
        jobs.finish(job_id, owner, "failed", exc.code)
    except OSError as exc:
        full = exc.errno in (errno.ENOSPC, errno.EDQUOT)
        if not full:
            record_failure(job_id, owner, source, "storage_error")
        jobs.finish(
            job_id,
            owner,
            "waiting_storage" if full else "failed",
            "storage_full" if full else "storage_error",
        )
