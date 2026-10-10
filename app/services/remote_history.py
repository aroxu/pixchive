"""Bounded history reads and safe projections of preserved remote revisions."""

import math

from pydantic import ValidationError

from app.adapters.pixiv.contracts import RemoteArtwork
from app.core.errors import DomainError
from app.models.entities import ArtworkSnapshot
from app.services import library


def read(snapshot: ArtworkSnapshot | None) -> RemoteArtwork | None:
    if snapshot is None:
        return None
    try:
        remote = RemoteArtwork.model_validate_json(snapshot.metadata)
        return remote if remote.id == snapshot.artwork_id else None
    except ValidationError:
        return None


def visible(remote: RemoteArtwork | None) -> bool:
    return remote is not None and (
        remote.rating == "safe" or library.get_setting("show_sensitive") == "true"
    )


def source_changes(before, after, source: str) -> dict | None:
    # Missing remote fields provide no evidence that tags were removed.
    if before is None or after is None:
        return None
    old = library.tag_names(before)
    new = library.tag_names(after)
    if old == new:
        return None
    return {"source": source, "added": sorted(new - old), "removed": sorted(old - new)}


def entry(snapshot: ArtworkSnapshot, previous: ArtworkSnapshot | None) -> dict:
    current, before = read(snapshot), read(previous)
    available = visible(current)
    comparable = available and (previous is None or visible(before))
    changes, tag_changes = [], []
    if comparable and before is not None:
        old, new = before.model_dump(), current.model_dump()
        for field in (
            "title",
            "artist_id",
            "artist_name",
            "caption",
            "kind",
            "rating",
            "rating_evidence",
            "page_count",
            "created_at",
            "bookmarked_at",
        ):
            if old[field] != new[field]:
                changes.append(field)
        if current.artist_account is not None and current.artist_account != before.artist_account:
            changes.append("artist_account")
        for field, source in (("work_tags", "pixiv_work"), ("bookmark_tags", "pixiv_bookmark")):
            if old[field] is not None and new[field] is not None and old[field] != new[field]:
                changes.append(field)
                diff = source_changes(old[field], new[field], source)
                if diff:
                    tag_changes.append(diff)
        if library.files_signature(old) != library.files_signature(new):
            changes.append("files")
    return {
        "id": snapshot.id,
        "revision": snapshot.revision,
        "fetched_at": snapshot.fetched_at,
        "initial": previous is None,
        "state": "unreadable" if current is None else "available" if available else "hidden",
        "comparison_available": comparable,
        "title": current.title if available else "",
        "artist_name": current.artist_name if available else "",
        "changes": changes,
        "tag_changes": tag_changes,
        # Retain the combined fields for clients using the original history endpoint.
        "added_tags": list(dict.fromkeys(tag for group in tag_changes for tag in group["added"])),
        "removed_tags": list(
            dict.fromkeys(tag for group in tag_changes for tag in group["removed"])
        ),
    }


def listing(artwork_id: str, page: int, page_size: int) -> dict:
    library.require_artwork(artwork_id)
    query = ArtworkSnapshot.select().where(ArtworkSnapshot.artwork == artwork_id)
    total = query.count()
    page = min(page, max(1, math.ceil(total / page_size)))
    # One extra older row supplies the comparison across the page boundary.
    rows = list(
        query.order_by(ArtworkSnapshot.fetched_at.desc(), ArtworkSnapshot.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size + 1)
    )
    items = [
        entry(row, rows[index + 1] if index + 1 < len(rows) else None)
        for index, row in enumerate(rows[:page_size])
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def detail(artwork_id: str, snapshot_id: int) -> dict:
    library.require_artwork(artwork_id)
    snapshot = ArtworkSnapshot.get_or_none(
        (ArtworkSnapshot.id == snapshot_id) & (ArtworkSnapshot.artwork == artwork_id)
    )
    if snapshot is None:
        raise DomainError("not_found", "작품 변경 기록을 찾을 수 없습니다.", 404)
    remote = read(snapshot)
    if remote is None:
        raise DomainError("snapshot_unreadable", "저장된 작품 변경 기록을 읽을 수 없습니다.", 409)
    if not visible(remote):
        raise DomainError("hidden_rating", "설정에서 이 기록의 등급 표시를 허용해 주세요.", 404)
    # Explicitly project public metadata: never return stored JSON, media URLs or credentials.
    return {
        "id": snapshot.id,
        "revision": snapshot.revision,
        "fetched_at": snapshot.fetched_at,
        "title": remote.title,
        "artist_id": remote.artist_id,
        "artist_name": remote.artist_name,
        "artist_account": remote.artist_account,
        "caption": remote.caption,
        "kind": remote.kind,
        "rating": remote.rating,
        "rating_evidence": remote.rating_evidence.model_dump() if remote.rating_evidence else None,
        "page_count": remote.page_count,
        "created_at": remote.created_at,
        "bookmarked_at": remote.bookmarked_at,
        "work_tags": [tag.model_dump() for tag in remote.work_tags]
        if remote.work_tags is not None
        else None,
        "bookmark_tags": remote.bookmark_tags,
    }
