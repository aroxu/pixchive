"""Local stars and named lists, independent of Pixiv bookmarks and collection jobs."""

import unicodedata

from peewee import IntegrityError

from app.core.db import db, normalize
from app.core.errors import DomainError
from app.models.entities import Artwork, SavedCollection, SavedCollectionArtwork
from app.services import library


def clean_name(value: str) -> str:
    name = unicodedata.normalize("NFC", value.strip())
    if not name or len(name) > 100 or any(unicodedata.category(c).startswith("C") for c in name):
        raise DomainError(
            "invalid_collection_name", "목록 이름은 제어 문자 없이 1~100자로 입력해 주세요."
        )
    return name


def require_collection(identity: str) -> SavedCollection:
    item = SavedCollection.get_or_none(SavedCollection.id == identity)
    if item is None:
        raise DomainError("not_found", "보관 목록을 찾을 수 없습니다.", 404)
    return item


def save_state(artwork_id: str) -> dict:
    artwork = library.require_artwork(artwork_id)
    return {
        "favorite": artwork.favorite,
        "collection_ids": [
            item.collection_id
            for item in SavedCollectionArtwork.select()
            .where(SavedCollectionArtwork.artwork == artwork)
            .order_by(SavedCollectionArtwork.collection)
        ],
    }


def favorite(artwork_id: str, value: bool) -> dict:
    with db.atomic("IMMEDIATE"):
        library.require_artwork(artwork_id)
        Artwork.update(favorite=value).where(Artwork.id == artwork_id).execute()
        return save_state(artwork_id)


def view(item: SavedCollection) -> dict:
    where, params = library.filters(collection_id=item.id)
    count = db.execute_sql(
        f"SELECT count(*) FROM artwork a JOIN artist ar ON ar.id=a.artist_id WHERE {where}",
        params,
    ).fetchone()[0]
    return {"id": item.id, "name": item.name, "artwork_count": count, "created_at": item.created_at}


def detail(identity: str) -> dict:
    return view(require_collection(identity))


def listing(q="", page=1, page_size=20, sort="name") -> dict:
    # Count only the same visible, non-tombstoned works the library can display.
    where, params = library.filters()
    search = normalize(q.strip())
    total = db.execute_sql(
        "SELECT count(*) FROM saved_collection WHERE instr(name_key,?)>0", [search]
    ).fetchone()[0]
    order = "artwork_count DESC, c.name_key, c.id" if sort == "count" else "c.name_key, c.id"
    rows = db.execute_sql(
        f"""SELECT c.id,c.name,c.created_at,coalesce(v.artwork_count,0) AS artwork_count
        FROM saved_collection c LEFT JOIN (
          SELECT sca.collection_id,count(*) AS artwork_count
          FROM saved_collection_artwork sca JOIN artwork a ON a.id=sca.artwork_id
          JOIN artist ar ON ar.id=a.artist_id WHERE {where} GROUP BY sca.collection_id
        ) v ON v.collection_id=c.id WHERE instr(c.name_key,?)>0
        ORDER BY {order} LIMIT ? OFFSET ?""",
        [*params, search, page_size, (page - 1) * page_size],
    )
    return {
        "items": [
            {"id": r[0], "name": r[1], "created_at": r[2], "artwork_count": r[3]} for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def create(name: str, *, artwork_id: str | None = None) -> dict:
    name = clean_name(name)
    try:
        with db.atomic("IMMEDIATE"):
            if artwork_id is not None:
                library.require_artwork(artwork_id)
            item = SavedCollection.create(name=name, name_key=normalize(name))
            if artwork_id is not None:
                SavedCollectionArtwork.create(collection=item, artwork=artwork_id)
            return view(item)
    except IntegrityError:
        raise DomainError(
            "collection_exists", "같은 이름의 보관 목록이 이미 있습니다.", 409
        ) from None


def rename(identity: str, name: str) -> dict:
    name = clean_name(name)
    try:
        with db.atomic("IMMEDIATE"):
            require_collection(identity)
            SavedCollection.update(name=name, name_key=normalize(name)).where(
                SavedCollection.id == identity
            ).execute()
            return detail(identity)
    except IntegrityError:
        raise DomainError(
            "collection_exists", "같은 이름의 보관 목록이 이미 있습니다.", 409
        ) from None


def delete(identity: str) -> None:
    with db.atomic("IMMEDIATE"):
        item = require_collection(identity)
        # FK cascade removes list relations only; it never deletes artwork or files.
        item.delete_instance()


def membership(identity: str, artwork_id: str, included: bool) -> dict:
    with db.atomic("IMMEDIATE"):
        require_collection(identity)
        library.require_artwork(artwork_id)
        if included:
            SavedCollectionArtwork.insert(
                collection=identity, artwork=artwork_id
            ).on_conflict_ignore().execute()
        else:
            SavedCollectionArtwork.delete().where(
                (SavedCollectionArtwork.collection == identity)
                & (SavedCollectionArtwork.artwork == artwork_id)
            ).execute()
        return save_state(artwork_id)
