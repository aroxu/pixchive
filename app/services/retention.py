"""Local deletion records (tombstones), explicit un-exclusion, and restoration requests."""

from peewee import fn

from app.core.db import db
from app.core.errors import DomainError
from app.core.time import now
from app.models.entities import (
    Artwork,
    Asset,
    DownloadTask,
    Job,
    Outbox,
    Tombstone,
    UgoiraBundle,
)
from app.services import jobs, library, preferences, storage
from app.services.execution import disk_operation


def stored_files(artwork_id: str) -> list[dict]:
    rows = (
        Asset.select(Asset.role, fn.COUNT(Asset.id), fn.COALESCE(fn.SUM(Asset.size), 0))
        .where((Asset.artwork == artwork_id) & Asset.state.in_(storage.FILE_STATES))
        .group_by(Asset.role)
        .order_by(Asset.role)
        .tuples()
    )
    return [{"role": role, "files": count, "bytes": size} for role, count, size in rows]


def preview(artwork_id: str) -> dict:
    artwork = library.require_artwork(artwork_id)
    roles = stored_files(artwork_id)
    return {
        "artwork_id": artwork.id,
        "title": artwork.title,
        "file_count": sum(item["files"] for item in roles),
        "bytes": sum(item["bytes"] for item in roles),
        "roles": roles,
    }


def request_delete(artwork_id: str, key: str) -> Job:
    """Hide the artwork immediately and delete its files in a resumable worker job."""
    with db.atomic("IMMEDIATE"):
        existing = Job.get_or_none(Job.idempotency_key == f"delete:{artwork_id}:{key}")
        if existing:
            return existing
        library.require_artwork(artwork_id)
        roles = stored_files(artwork_id)
        tombstone = Tombstone.create(
            artwork_id=artwork_id,
            state="deleting",
            file_count=sum(item["files"] for item in roles),
            freed_bytes=0,
        )
        job = Job.create(
            kind="delete",
            scope=artwork_id,
            idempotency_key=f"delete:{artwork_id}:{key}",
            active_key=f"delete:1:{artwork_id}",
            # Shares the conversion lease so a running conversion finishes first.
            lease_key=f"convert:{artwork_id}",
        )
        tombstone.job_id = job.id
        tombstone.save()
        Outbox.create(job=job)
        jobs.event(job, "queued")
        DownloadTask.update(state="excluded").where(
            (DownloadTask.artwork == artwork_id) & (DownloadTask.state == "pending")
        ).execute()
        bundles = [
            bundle.id for bundle in UgoiraBundle.select().where(UgoiraBundle.artwork == artwork_id)
        ]
        if bundles:
            for conversion in Job.select().where(
                (Job.kind == "convert")
                & Job.scope.in_(bundles)
                & Job.state.in_(
                    ("queued", "running", "pausing", "paused", "waiting_auth", "waiting_storage")
                )
            ):
                jobs.action(conversion.id, "cancel")
        for preview in Job.select().where(
            (Job.kind == "preview")
            & (Job.scope == artwork_id)
            & Job.state.in_(
                ("queued", "running", "pausing", "paused", "waiting_auth", "waiting_storage")
            )
        ):
            jobs.action(preview.id, "cancel")
        return job


async def execute_delete(ctx, job_id: str, owner: str):
    settings = ctx["settings"]
    job = jobs.require_job(job_id)
    artwork_id = job.scope
    tombstone = Tombstone.get_or_none(Tombstone.artwork_id == artwork_id)
    if tombstone is None:
        jobs.finish(job_id, owner, "failed", "not_found")
        return
    Job.update(phase="deleting", total=tombstone.file_count).where(Job.id == job_id).execute()
    while (
        asset := Asset.select()
        .where((Asset.artwork == artwork_id) & (Asset.state != "deleted"))
        .order_by(Asset.id)
        .first()
    ):
        if not jobs.owned(job_id, owner):
            return
        released = await disk_operation(storage.remove_file, settings, asset)
        with db.atomic("IMMEDIATE"):
            if not jobs.owned(job_id, owner):
                return
            Asset.update(state="deleted").where(Asset.id == asset.id).execute()
            Tombstone.update(freed_bytes=Tombstone.freed_bytes + released).where(
                Tombstone.artwork_id == artwork_id
            ).execute()
            Job.update(processed=Job.processed + 1, updated_at=now()).where(
                Job.id == job_id
            ).execute()
    # Include earlier deliveries' deleted rows so a restart before pruning can finish cleanup.
    roots = {
        "/".join(asset.path.split("/")[:3])
        for asset in Asset.select(Asset.path).where(Asset.artwork == artwork_id)
    }
    await disk_operation(storage.prune_directories, settings, sorted(roots))
    with db.atomic("IMMEDIATE"):
        if not jobs.owned(job_id, owner):
            return
        # Derived bundles are recreated with fresh conversion jobs after a restore.
        UgoiraBundle.delete().where(UgoiraBundle.artwork == artwork_id).execute()
        Artwork.update(archive_status="pending").where(Artwork.id == artwork_id).execute()
        Tombstone.update(state="deleted").where(Tombstone.artwork_id == artwork_id).execute()
        Job.update(phase="finished").where(Job.id == job_id).execute()
    jobs.finish(job_id, owner, "succeeded")


def exclusions(page: int = 1, page_size: int = 50) -> dict:
    show_sensitive = library.get_setting("show_sensitive") == "true"
    total = Tombstone.select().count()
    rows = db.execute_sql(
        """SELECT t.artwork_id, t.created_at, t.state, t.file_count, t.freed_bytes,
        a.title, ar.name, a.rating FROM tombstone t
        LEFT JOIN artwork a ON a.id = t.artwork_id
        LEFT JOIN artist ar ON ar.id = a.artist_id
        ORDER BY t.created_at DESC, t.artwork_id DESC LIMIT ? OFFSET ?""",
        [page_size, (page - 1) * page_size],
    )
    items = []
    for artwork_id, created, state, files, freed, title, artist, rating in rows:
        hidden = not show_sensitive and rating != "safe"
        items.append(
            {
                "artwork_id": artwork_id,
                "created_at": created,
                "state": state,
                "file_count": files,
                "freed_bytes": freed,
                "title": None if hidden else title,
                "artist_name": None if hidden else artist,
                "hidden": hidden,
            }
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def request_restore(artwork_id: str, key: str, download: bool) -> Job | None:
    """Remove the tombstone; optionally queue a fresh download of the current originals."""
    with db.atomic("IMMEDIATE"):
        if download:
            existing = Job.get_or_none(Job.idempotency_key == f"restore:1:{artwork_id}:{key}")
            if existing:
                return existing
        tombstone = Tombstone.get_or_none(Tombstone.artwork_id == artwork_id)
        if tombstone is None:
            raise DomainError("not_excluded", "수집 제외 목록에 없는 작품입니다.", 404)
        if tombstone.state != "deleted":
            raise DomainError("deletion_running", "삭제 작업이 끝난 뒤 다시 시도해 주세요.", 409)
        if download:
            artwork = Artwork.get_or_none(Artwork.id == artwork_id)
            if artwork:
                preferences.require_download(artwork.rating)
        tombstone.delete_instance()
        if not download:
            return None
        # Raises (and rolls back the un-exclusion) when Pixiv is not connected.
        return jobs.enqueue_account("restore", key, artwork_id)
