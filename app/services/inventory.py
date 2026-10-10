"""Paginated file records, including kept historical revisions."""

import math

from peewee import SQL

from app.models.entities import Asset
from app.services import library


def files(artwork_id: str, page: int, page_size: int, role: str | None = None) -> dict:
    library.require_artwork(artwork_id)
    query = Asset.select().where((Asset.artwork == artwork_id) & (Asset.state != "deleted"))
    if role is not None:
        query = query.where(Asset.role == role)
    total = query.count()
    page = min(page, max(1, math.ceil(total / page_size)))
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
                "url": f"/api/media/{item.id}" if item.state == "ready" else None,
                "download_name": (
                    f"pixchive-{artwork_id}-{item.role}-{item.revision[:12]}"
                    f"-p{item.page:04d}.{item.path.rsplit('.', 1)[-1]}"
                ),
            }
            for item in query.order_by(SQL("rowid DESC")).paginate(page, page_size)
        ],
    }
