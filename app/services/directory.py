"""Artist and tag profiles, scoped to the same visible archive as the library."""

import math

from peewee import SQL

from app.core.db import db, normalize
from app.core.errors import DomainError
from app.models.entities import ArtistName, ArtworkTag, Tag
from app.services import library, local_tags
from app.services.search import TAG_RELATIONS


def artist_listing(page: int, page_size: int, q: str = "", sort: str = "name") -> dict:
    where, params = library.filters()
    term = normalize(q.strip())
    if term:
        where += """ AND (instr(normalize(ar.name), ?) > 0
        OR instr(normalize(ar.alias), ?) > 0 OR instr(normalize(ar.account), ?) > 0
        OR instr(ar.id, ?) > 0 OR EXISTS (SELECT 1 FROM artist_name old
        WHERE old.artist_id=ar.id AND instr(normalize(old.name), ?) > 0))"""
        params.extend([term] * 5)
    base = f"FROM artwork a JOIN artist ar ON ar.id=a.artist_id WHERE {where}"
    total = db.execute_sql(f"SELECT count(DISTINCT ar.id) {base}", params).fetchone()[0]
    page = min(page, max(1, math.ceil(total / page_size)))
    order = "normalize(ar.name), ar.name, ar.id"
    if sort == "count":
        order = "count(*) DESC, " + order
    rows = list(
        db.execute_sql(
            f"""SELECT ar.id, ar.name, ar.alias, count(*), ar.avatar_path, ar.account
            {base} GROUP BY ar.id ORDER BY {order} LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size],
        )
    )
    return {
        "items": library.artist_records(rows),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def tag_listing(
    page: int, page_size: int, q: str = "", sort: str = "name", source: str | None = None
) -> dict:
    where, params = library.filters()
    term = normalize(q.strip())
    search = ""
    search_params = []
    if term:
        search = """ AND (instr(normalize(t.name), ?) > 0 OR EXISTS (
        SELECT 1 FROM tag_translation tt WHERE tt.tag_id=t.id
        AND instr(normalize(tt.name), ?) > 0))"""
        search_params = [term, term]
    source_clause = " AND r.source=?" if source is not None else ""
    candidate = f"""SELECT t.id, t.name, r.source, count(DISTINCT a.id) artwork_count
        FROM ({TAG_RELATIONS}) r JOIN tag t ON t.id=r.tag_id
        JOIN artwork a ON a.id=r.artwork_id JOIN artist ar ON ar.id=a.artist_id
        WHERE {where}{search}{source_clause} GROUP BY t.id, r.source"""
    params.extend(search_params)
    if source is not None:
        params.append(source)
    if source in (None, "local"):
        # Truly unused local tags remain visible; hidden/excluded-only tags do not become empty.
        candidate += f""" UNION ALL SELECT t.id, t.name, 'local', 0 FROM tag t
            WHERE t.local_created=1 AND NOT EXISTS (SELECT 1 FROM artwork_tag local_rel
            WHERE local_rel.tag_id=t.id AND local_rel.source='local'){search}"""
        params.extend(search_params)
    total = db.execute_sql(f"SELECT count(*) FROM ({candidate})", params).fetchone()[0]
    page = min(page, max(1, math.ceil(total / page_size)))
    order = "normalize(c.name), c.name, c.id, c.source"
    if sort == "count":
        order = "c.artwork_count DESC, " + order
    rows = list(
        db.execute_sql(
            f"""SELECT c.id, c.name, c.source, c.artwork_count, {library.TAG_EDITABLE}
            FROM ({candidate}) c JOIN tag t ON t.id=c.id
            ORDER BY {order} LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size],
        )
    )
    translations = library.tag_translations(list(dict.fromkeys(row[0] for row in rows)))
    return {
        "items": [
            {
                "id": row[0],
                "name": row[1],
                "source": row[2],
                "artwork_count": row[3],
                "editable": row[2] == "local" and bool(row[4]),
                "translations": translations.get(row[0], []),
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def artist_detail(artist_id: str) -> dict:
    items = library.artists(artist=artist_id)
    if not items:
        raise DomainError("not_found", "작가를 찾을 수 없습니다.", 404)
    return {
        **items[0],
        "profile_url": f"https://www.pixiv.net/users/{artist_id}",
        "name_history": [
            {"name": item.name, "first_seen": item.first_seen, "last_seen": item.last_seen}
            for item in ArtistName.select()
            .where(ArtistName.artist == artist_id)
            .order_by(SQL("last_seen IS NULL"), ArtistName.last_seen.desc(), ArtistName.id.desc())
        ],
    }


def tag_detail(tag_id: int) -> dict:
    tag = Tag.get_or_none(Tag.id == tag_id)
    if tag is None:
        raise DomainError("not_found", "태그를 찾을 수 없습니다.", 404)
    where, params = library.filters(tag_id=tag_id)
    base = f"FROM artwork a JOIN artist ar ON ar.id=a.artist_id WHERE {where}"
    total = db.execute_sql(f"SELECT count(*) {base}", params).fetchone()[0]
    counts = [
        {"source": source, "artwork_count": count}
        for source, count in db.execute_sql(
            f"""SELECT r.source, count(DISTINCT a.id) FROM ({TAG_RELATIONS}) r
            JOIN artwork a ON a.id=r.artwork_id JOIN artist ar ON ar.id=a.artist_id
            WHERE r.tag_id=? AND {where} GROUP BY r.source ORDER BY r.source""",
            [tag_id, *params],
        )
    ]
    if not total and not (
        tag.local_created
        and not ArtworkTag.select()
        .where((ArtworkTag.tag == tag_id) & (ArtworkTag.source == "local"))
        .exists()
    ):
        raise DomainError("not_found", "태그를 찾을 수 없습니다.", 404)
    return {
        "id": tag.id,
        "name": tag.name,
        "artwork_count": total,
        "source_counts": counts,
        "translations": library.tag_translations([tag.id]).get(tag.id, []),
        "editable": local_tags.editable(tag.id),
    }
