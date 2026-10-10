"""Explicitly repair current originals without removing metadata or local edits."""

from app.core.db import db
from app.models.entities import Asset, UgoiraBundle
from app.services import jobs, library, preferences, storage
from app.services.execution import disk_operation


def request(artwork_id: str, key: str):
    with db.atomic("IMMEDIATE"):
        artwork = library.require_artwork(artwork_id)
        preferences.require_download(artwork.rating)
        return jobs.enqueue_account("repair", key, artwork_id)


async def inspect(settings, artwork_id: str, check):
    # File hashing is outside the write transaction and cancellable between assets.
    last = ""
    while (
        asset := Asset.select()
        .where((Asset.artwork == artwork_id) & (Asset.state != "deleted") & (Asset.id > last))
        .order_by(Asset.id)
        .first()
    ):
        check()
        await disk_operation(storage.verify_asset, settings, asset, check)
        check()
        last = asset.id
    with db.atomic("IMMEDIATE"):
        check()
        for bundle in UgoiraBundle.select().where(UgoiraBundle.artwork == artwork_id):
            if any(
                asset and asset.state != "ready"
                for asset in (bundle.archive, bundle.timing, bundle.video, bundle.thumbnail)
            ):
                UgoiraBundle.update(state="pending", error_code=None).where(
                    UgoiraBundle.id == bundle.id
                ).execute()
        storage.refresh_archive(artwork_id)
