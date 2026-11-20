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


def enqueue_missing(job_id: str, limit: int = 100) -> list[Job]:
    """Queue at most 100 local preview recoveries from completed original tasks.

    Internal collection recovery follows kept files, including hidden ratings;
    it does not apply UI visibility or Pixiv download/credential policy. Only
    the latest kept first-page original is eligible. A stable source key gives
    each source one automatic attempt; failed/cancelled jobs require an explicit
    retry. Already active previews and earlier attempts do not consume the limit.
    """
    if type(limit) is not int or not 0 <= limit <= 100:
        raise ValueError("Preview recovery limit must be an integer between 0 and 100")
    if not limit:
        return []
    with db.atomic("IMMEDIATE"):
        parent = jobs.require_job(job_id)
        if parent.kind not in jobs.PARENTS:
            return []
        # Filter/deduplicate before LIMIT so old attempts, ready thumbnails and
        # duplicate task references cannot permanently hide later candidates.
        rows = db.execute_sql(
            """SELECT DISTINCT a.rowid, a.id, a.artwork_id
            FROM download_task d JOIN asset a ON a.id = d.asset_id
            JOIN artwork w ON w.id = a.artwork_id
            WHERE d.job_id = ? AND d.state = 'ready' AND d.page = 0
              AND d.artwork_id = a.artwork_id
              AND a.role = 'original' AND a.page = 0 AND a.state = 'ready'
              AND w.kind IN ('illustration', 'manga')
              AND NOT EXISTS (SELECT 1 FROM tombstone t WHERE t.artwork_id = a.artwork_id)
              AND NOT EXISTS (
                SELECT 1 FROM asset n WHERE n.artwork_id = a.artwork_id
                AND n.role = 'original' AND n.page = 0 AND n.state != 'deleted'
                AND n.rowid > a.rowid)
              AND NOT EXISTS (
                SELECT 1 FROM asset p WHERE p.id = a.thumbnail_id
                AND p.role = 'thumbnail' AND p.page = 0 AND p.artwork_id = a.artwork_id
                AND p.state = 'ready')
              AND NOT EXISTS (
                SELECT 1 FROM job j
                WHERE j.idempotency_key =
                  'preview:1:' || a.artwork_id || ':recover-source:' || a.id)
              AND NOT EXISTS (
                SELECT 1 FROM job j WHERE j.active_key = 'preview:1:' || a.artwork_id)
            ORDER BY a.rowid LIMIT ?""",
            (job_id, limit),
        ).fetchall()
        return [
            jobs.enqueue_preview(artwork_id, f"recover-source:{source_id}")
            for _, source_id, artwork_id in rows
        ]


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
