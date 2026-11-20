"""Paginated file records, including kept historical revisions."""

import math

from peewee import SQL

from app.models.entities import Artwork, Asset, UgoiraBundle
from app.services import library


def current_ids(artwork: Artwork) -> set[str]:
    """Files of the newest kept revision; older records are earlier copies.

    A damaged newest file stays current so the list shows what needs repair.
    """
    current: set[str] = set()
    pages: set[int] = set()
    for asset in (
        Asset.select(Asset.id, Asset.page, Asset.thumbnail)
        .where(
            (Asset.artwork == artwork)
            & (Asset.role == "original")
            & (Asset.state != "deleted")
            & (Asset.page >= 0)
            & (Asset.page < artwork.page_count)
        )
        .order_by(SQL("rowid DESC"))
    ):
        if asset.page in pages:
            continue
        pages.add(asset.page)
        current.add(asset.id)
        if asset.thumbnail_id:
            current.add(asset.thumbnail_id)
    bundle = (
        UgoiraBundle.select()
        .where(UgoiraBundle.artwork == artwork)
        .order_by(SQL("rowid DESC"))
        .first()
    )
    if bundle:
        current.update(
            asset_id
            for asset_id in (
                bundle.archive_id,
                bundle.timing_id,
                bundle.thumbnail_id,
                bundle.video_id,
            )
            if asset_id
        )
    elif not pages:
        # Files collected before originals/bundles were linked show this preview.
        legacy = (
            Asset.select(Asset.id)
            .where(
                (Asset.artwork == artwork)
                & (Asset.page == 0)
                & (Asset.role == "thumbnail")
                & (Asset.state == "ready")
            )
            .order_by(SQL("rowid DESC"))
            .first()
        )
        if legacy:
            current.add(legacy.id)
    return current


def files(artwork_id: str, page: int, page_size: int, role: str | None = None) -> dict:
    artwork = library.require_artwork(artwork_id)
    query = Asset.select().where((Asset.artwork == artwork_id) & (Asset.state != "deleted"))
    if role is not None:
        query = query.where(Asset.role == role)
    total = query.count()
    page = min(page, max(1, math.ceil(total / page_size)))
    current = current_ids(artwork)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": item.id,
                "revision": item.revision,
                "page": item.page,
                "role": item.role,
                "state": item.state,
                "sha256": item.sha256,
                "size": item.size,
                "width": item.width,
                "height": item.height,
                "media_type": item.media_type,
                "provenance": item.provenance,
                "current": item.id in current,
                "url": f"/api/media/{item.id}" if item.state == "ready" else None,
                "download_name": (
                    f"pixchive-{artwork_id}-{item.role}-{item.revision[:12]}"
                    f"-p{item.page:04d}.{item.path.rsplit('.', 1)[-1]}"
                ),
            }
            for item in query.order_by(SQL("rowid DESC")).paginate(page, page_size)
        ],
    }
