import shlex
from datetime import UTC, datetime, time

from app.core.db import normalize
from app.core.errors import DomainError
from app.core.query import ArtworkQuery

TAG_RELATIONS = """
 SELECT at.artwork_id, at.tag_id, at.source FROM artwork_tag at
 UNION ALL
 SELECT b.artwork_id, bt.tag_id, 'pixiv_bookmark' FROM bookmark_tag bt
 JOIN bookmark b ON b.id = bt.bookmark_id WHERE b.account_id=1
"""

ORDERS = {
    "collected": "a.collected_at DESC, a.id DESC",
    "created": "julianday(a.created_at) DESC, a.id DESC",
    "title": "normalize(a.title), a.id DESC",
    "artist": "normalize(ar.name), a.id DESC",
}


def conditions(query: ArtworkQuery, show_sensitive: bool) -> tuple[str, list]:
    clauses = ["NOT EXISTS (SELECT 1 FROM tombstone x WHERE x.artwork_id = a.id)"]
    params: list = []
    if not show_sensitive:
        clauses.append("a.rating = 'safe'")
    for field, column in (
        ("artist", "artist_id"),
        ("kind", "kind"),
        ("rating", "rating"),
        ("archive_status", "archive_status"),
        ("remote_status", "remote_status"),
    ):
        if value := getattr(query, field):
            clauses.append(f"a.{column} = ?")
            params.append(value)
    if query.visibility or query.bookmark_status:
        bookmark = ["b.artwork_id=a.id", "b.account_id=1"]
        for field, column in (("visibility", "visibility"), ("bookmark_status", "status")):
            if value := getattr(query, field):
                bookmark.append(f"b.{column}=?")
                params.append(value)
        clauses.append(f"EXISTS (SELECT 1 FROM bookmark b WHERE {' AND '.join(bookmark)})")
    for prefix in ("created", "collected"):
        for suffix, comparison in (("from", ">="), ("to", "<")):
            value = getattr(query, f"{prefix}_{suffix}")
            if value:
                if prefix == "created":
                    clauses.append(f"julianday(a.created_at) {comparison} julianday(?) + ?")
                    params.extend([value.isoformat(), 1 if suffix == "to" else 0])
                else:
                    boundary = int(datetime.combine(value, time(), UTC).timestamp())
                    clauses.append(f"a.collected_at {comparison} ?")
                    params.append(boundary + (86400 if suffix == "to" else 0))

    source = " AND r.source=?" if query.tag_source else ""
    source_params = [query.tag_source] if query.tag_source else []
    if query.tag_id is not None:
        clauses.append(
            f"EXISTS (SELECT 1 FROM ({TAG_RELATIONS}) r "
            f"WHERE r.artwork_id=a.id AND r.tag_id=?{source})"
        )
        params.extend([query.tag_id, *source_params])

    def tag_exists(names):
        values = list(dict.fromkeys(names))
        markers = ",".join("?" for _ in values)
        params.extend([*values, *source_params])
        return f"""EXISTS (SELECT 1 FROM ({TAG_RELATIONS}) r JOIN tag t ON t.id=r.tag_id
            WHERE r.artwork_id=a.id AND t.name IN ({markers}){source})"""

    if query.tag:
        if query.tag_match == "any":
            clauses.append(tag_exists(query.tag))
        else:
            for tag in dict.fromkeys(query.tag):
                clauses.append(tag_exists([tag]))
    if query.exclude_tag:
        clauses.append("NOT " + tag_exists(query.exclude_tag))
    if query.tag_source and not query.tag and not query.exclude_tag and not query.q:
        clauses.append(
            f"EXISTS (SELECT 1 FROM ({TAG_RELATIONS}) r WHERE r.artwork_id=a.id{source})"
        )
        params.extend(source_params)
    try:
        terms = shlex.split(query.q)
    except ValueError:
        raise DomainError("invalid_search", "검색어의 따옴표를 닫아 주세요.") from None
    for term in terms:
        term = normalize(term)
        clauses.append(f"""(
            instr(normalize(a.title), ?) > 0 OR instr(a.id, ?) > 0
            OR instr(a.artist_id, ?) > 0 OR instr(normalize(ar.name), ?) > 0
            OR instr(normalize(ar.alias), ?) > 0
            OR instr(normalize(ar.account), ?) > 0
            OR EXISTS (SELECT 1 FROM artist_name an WHERE an.artist_id=a.artist_id
                AND instr(normalize(an.name), ?) > 0)
            OR EXISTS (SELECT 1 FROM ({TAG_RELATIONS}) r JOIN tag t ON t.id=r.tag_id
                WHERE r.artwork_id=a.id AND (instr(t.search_key, ?) > 0
                OR EXISTS (SELECT 1 FROM tag_translation tt WHERE tt.tag_id=t.id
                    AND instr(normalize(tt.name), ?) > 0)){source})
        )""")
        params.extend([term] * 9 + source_params)
    return " AND ".join(clauses), params
