import shutil

from peewee import fn

from app.core.config import Settings
from app.core.time import now
from app.models.entities import Artist, Artwork, Asset, Job, Tombstone
from app.services import schedule, storage_budget
from app.services.library import get_setting

WAITING = ("waiting_auth", "waiting_storage", "waiting_children", "paused")


def database_bytes(settings: Settings) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += (settings.data_dir / f"pixchive.db{suffix}").stat().st_size
        except OSError:
            pass
    return total


def status(settings: Settings) -> dict:
    usage = shutil.disk_usage(settings.data_dir)
    reserved = storage_budget.total()
    value = get_setting("worker_heartbeat", "")
    seen = int(value) if value else None
    archive = [
        {"role": role, "files": count, "bytes": size}
        for role, count, size in Asset.select(
            Asset.role, fn.COUNT(Asset.id), fn.COALESCE(fn.SUM(Asset.size), 0)
        )
        .where(Asset.state == "ready")
        .group_by(Asset.role)
        .order_by(Asset.role)
        .tuples()
    ]
    states = dict(
        Job.select(Job.state, fn.COUNT(Job.id))
        .where(Job.state.in_(("queued", "running", "pausing", "cancelling", *WAITING)))
        .group_by(Job.state)
        .tuples()
    )
    visible = Artwork.select().where(Artwork.id.not_in(Tombstone.select(Tombstone.artwork_id)))
    return {
        "storage_free": usage.free,
        "storage_total": usage.total,
        "storage_reserve": settings.min_free_bytes,
        "storage_reserved": reserved,
        "storage_low": usage.free < settings.min_free_bytes + reserved,
        "database_bytes": database_bytes(settings),
        "archive": archive,
        "artwork_count": visible.count(),
        "artist_count": Artist.select()
        .where(Artist.id.in_(visible.select(Artwork.artist)))
        .count(),
        "excluded_count": Tombstone.select().count(),
        "queue": {
            "queued": states.get("queued", 0),
            "running": sum(states.get(s, 0) for s in ("running", "pausing", "cancelling")),
            "waiting": sum(states.get(s, 0) for s in WAITING),
        },
        "worker_last_seen": seen,
        "worker_status": "unknown"
        if seen is None
        else "online"
        if now() - seen < 30
        else "offline",
        "last_backup_at": schedule.timestamp("last_backup_at"),
        "version": "0.1.0.dev0",
    }
