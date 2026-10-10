"""Manage local names without mutating work tags, bookmark tags, or translations."""

from app.core.db import db, normalize
from app.core.errors import DomainError
from app.models.entities import ArtworkTag, Tag
from app.services import library


def editable(tag_id: int) -> bool:
    row = db.execute_sql(
        f"SELECT {library.TAG_EDITABLE} FROM tag t WHERE t.id=?", [tag_id]
    ).fetchone()
    return bool(row and row[0])


def view(tag: Tag) -> dict:
    where, params = library.filters()
    count = db.execute_sql(
        f"""SELECT count(DISTINCT a.id) FROM artwork_tag r
        JOIN artwork a ON a.id=r.artwork_id JOIN artist ar ON ar.id=a.artist_id
        WHERE r.tag_id=? AND r.source='local' AND {where}""",
        [tag.id, *params],
    ).fetchone()[0]
    return {
        "id": tag.id,
        "name": tag.name,
        "source": "local",
        "artwork_count": count,
        "editable": editable(tag.id),
        "translations": library.tag_translations([tag.id]).get(tag.id, []),
    }


def catalog() -> list[dict]:
    result = library.tags()
    # Keep intentionally created empty tags. Hidden/excluded-only relationships stay hidden.
    for tag in Tag.select().where(
        Tag.local_created
        & Tag.id.not_in(ArtworkTag.select(ArtworkTag.tag).where(ArtworkTag.source == "local"))
    ):
        result.append(view(tag))
    return sorted(result, key=lambda item: (normalize(item["name"]), item["source"], item["id"]))


def create(name: str) -> dict:
    name = library.local_tag(name)
    with db.atomic("IMMEDIATE"):
        tag = library.ensure_tag(name)
        Tag.update(local_created=True).where(Tag.id == tag.id).execute()
    return view(tag)


def rename(tag_id: int, name: str) -> dict:
    name = library.local_tag(name)
    with db.atomic("IMMEDIATE"):
        tag = Tag.get_or_none(Tag.id == tag_id)
        if tag is None:
            raise DomainError("not_found", "태그를 찾을 수 없습니다.", 404)
        if not editable(tag_id):
            raise DomainError(
                "remote_tag_protected", "원격 태그와 공유된 이름은 변경할 수 없습니다.", 409
            )
        previous_name = tag.name
        target = Tag.get_or_none(Tag.name == name)
        merged = target is not None and target.id != tag.id
        if merged:
            # INSERT SELECT preserves all local relationships, including hidden works;
            # duplicates at the destination are harmless and remote relations are untouched.
            db.execute_sql(
                """INSERT OR IGNORE INTO artwork_tag (artwork_id, tag_id, source)
                SELECT artwork_id, ?, 'local' FROM artwork_tag
                WHERE tag_id=? AND source='local'""",
                [target.id, tag.id],
            )
            ArtworkTag.delete().where(
                (ArtworkTag.tag == tag) & (ArtworkTag.source == "local")
            ).execute()
            Tag.update(local_created=True).where(Tag.id == target.id).execute()
            tag.delete_instance()
            tag = target
        else:
            tag.name, tag.search_key, tag.local_created = name, normalize(name), True
            tag.save()
    return {"tag": view(tag), "previous_name": previous_name, "merged": merged}


def rename_preview(tag_id: int, name: str) -> dict:
    name = library.local_tag(name)
    if not Tag.select().where(Tag.id == tag_id).exists():
        raise DomainError("not_found", "태그를 찾을 수 없습니다.", 404)
    if not editable(tag_id):
        raise DomainError(
            "remote_tag_protected", "원격 태그와 공유된 이름은 변경할 수 없습니다.", 409
        )
    return {
        "name": name,
        "merge": Tag.select().where((Tag.name == name) & (Tag.id != tag_id)).exists(),
    }
