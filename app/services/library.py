import hashlib
import unicodedata

from peewee import SQL

from app.adapters.pixiv.contracts import RatingEvidence, RemoteArtwork
from app.core.db import db, normalize
from app.core.errors import DomainError
from app.core.query import DEFAULT_ARTWORK_SORT, ArtworkQuery
from app.core.time import now
from app.models.entities import (
    Artist,
    ArtistName,
    Artwork,
    ArtworkSnapshot,
    ArtworkTag,
    Asset,
    Bookmark,
    BookmarkTag,
    PixivAccount,
    Setting,
    Tag,
    TagTranslation,
    Tombstone,
    UgoiraBundle,
)
from app.services import preferences
from app.services.search import ORDERS, TAG_RELATIONS, conditions

TAG_EDITABLE = """(
 (t.local_created = 1 OR EXISTS (
  SELECT 1 FROM artwork_tag local_rel WHERE local_rel.tag_id=t.id AND local_rel.source='local'
 )) AND NOT EXISTS (
  SELECT 1 FROM artwork_tag remote_rel WHERE remote_rel.tag_id=t.id AND remote_rel.source!='local'
 ) AND NOT EXISTS (
  SELECT 1 FROM bookmark_tag bt WHERE bt.tag_id=t.id
 ) AND NOT EXISTS (
  SELECT 1 FROM tag_translation tt WHERE tt.tag_id=t.id
 ))"""


def get_setting(key: str, default: str = "false") -> str:
    item = Setting.get_or_none(Setting.key == key)
    return item.value if item else default


def set_setting(key: str, value: str):
    Setting.insert(key=key, value=value).on_conflict(
        conflict_target=[Setting.key], update={Setting.value: value}
    ).execute()


def ensure_tag(name: str) -> Tag:
    tag, _ = Tag.get_or_create(name=name, defaults={"search_key": normalize(name)})
    return tag


def ingest(remote: RemoteArtwork, scope: str | None, *, fixture: bool = False) -> Artwork | None:
    return observe_remote(remote, scope, fixture=fixture)[0]


def replace_bookmark_tags(bookmark: Bookmark, names: list[str]):
    current = {
        name
        for (name,) in Tag.select(Tag.name)
        .join(BookmarkTag, on=(BookmarkTag.tag == Tag.id))
        .where(BookmarkTag.bookmark == bookmark)
        .tuples()
    }
    if current == set(names):
        return
    BookmarkTag.delete().where(BookmarkTag.bookmark == bookmark).execute()
    for name in dict.fromkeys(names):
        BookmarkTag.get_or_create(bookmark=bookmark, tag=ensure_tag(name))


def touch_bookmark(
    artwork_id: str,
    scope: str,
    generation: str | None,
    bookmark_tags: list[str] | None,
    fingerprint: str | None = None,
):
    """Record a list observation without fetching details; local edits stay untouched."""
    account, _ = PixivAccount.get_or_create(id=1)
    bookmark, _ = Bookmark.get_or_create(
        account=account, artwork=artwork_id, defaults={"visibility": scope}
    )
    bookmark.visibility, bookmark.status, bookmark.last_seen = scope, "seen", now()
    bookmark.generation = generation
    bookmark.save()
    if bookmark_tags is not None:
        replace_bookmark_tags(bookmark, bookmark_tags)
    if fingerprint:
        Artwork.update(listing_fingerprint=fingerprint).where(
            (Artwork.id == artwork_id) & Artwork.listing_fingerprint.is_null()
        ).execute()


def observe_avatar(artist_id: str | None, url: str | None):
    if artist_id and url:
        Artist.update(avatar_url=url).where(
            (Artist.id == artist_id) & ((Artist.avatar_url.is_null()) | (Artist.avatar_url != url))
        ).execute()


def observe_remote(
    remote: RemoteArtwork,
    scope: str | None,
    *,
    fixture: bool = False,
    fingerprint: str | None = None,
) -> tuple[Artwork | None, bool]:
    """Atomically persist one successful metadata observation; never infer absent fields.

    Returns the artwork and whether this observation created a new remote snapshot.
    A None scope records artwork metadata only (explicit restoration), not a bookmark.
    """
    # Absent newly supported fields must not create a revision for every legacy observation.
    missing = {
        field: True
        for field in ("artist_account", "rating_evidence", "ugoira_source")
        if getattr(remote, field) is None
    }
    missing["pages"] = {
        index: {"source"} for index, page in enumerate(remote.pages) if page.source is None
    }
    metadata = remote.model_dump_json(exclude=missing)
    revision = hashlib.sha256(metadata.encode()).hexdigest()
    with db.atomic("IMMEDIATE"):
        if Tombstone.get_or_none(Tombstone.artwork_id == remote.id):
            return None, False
        observed = now()
        artist, _ = Artist.get_or_create(id=remote.artist_id, defaults={"name": remote.artist_name})
        if artist.name != remote.artist_name:
            # Keep the previous display name as history before replacing it.
            ArtistName.get_or_create(artist=artist, name=artist.name)
        current, _ = ArtistName.get_or_create(
            artist=artist, name=remote.artist_name, defaults={"first_seen": observed}
        )
        current.last_seen = observed
        current.save()
        artist.name = remote.artist_name
        if remote.artist_account is not None:
            artist.account = remote.artist_account
        artist.save()
        artwork, _ = Artwork.get_or_create(
            id=remote.id,
            defaults={
                "artist": artist,
                "artist_name_at_collection": remote.artist_name,
                "title": remote.title,
                "page_count": remote.page_count,
            },
        )
        artwork.artist = artist
        artwork.title, artwork.caption = remote.title, remote.caption
        artwork.kind = remote.kind
        # Evidence wins over an adapter's inconsistent normalized hint.
        artwork.rating = (
            remote.rating_evidence.normalized if remote.rating_evidence else remote.rating
        )
        artwork.rating_evidence = (
            remote.rating_evidence.model_dump_json() if remote.rating_evidence else None
        )
        artwork.page_count, artwork.created_at = remote.page_count, remote.created_at
        artwork.remote_status, artwork.is_fixture = "available", fixture
        artwork.metadata_fetched_at = observed
        artwork.listing_fingerprint = fingerprint
        artwork.save()
        _, changed = ArtworkSnapshot.get_or_create(
            artwork=artwork,
            revision=revision,
            defaults={
                "metadata": metadata,
            },
        )
        bookmark = None
        if scope is not None:
            account, _ = PixivAccount.get_or_create(id=1)
            bookmark, _ = Bookmark.get_or_create(
                account=account,
                artwork=artwork,
                defaults={
                    "visibility": scope,
                },
            )
            bookmark.visibility, bookmark.last_seen, bookmark.status = scope, observed, "seen"
            if remote.bookmarked_at is not None:
                bookmark.bookmarked_at = remote.bookmarked_at
            bookmark.save()
        if remote.work_tags is not None:
            ArtworkTag.delete().where(
                (ArtworkTag.artwork == artwork) & (ArtworkTag.source == "pixiv_work")
            ).execute()
            for remote_tag in remote.work_tags:
                tag = ensure_tag(remote_tag.name)
                ArtworkTag.get_or_create(artwork=artwork, tag=tag, source="pixiv_work")
                for language, name in remote_tag.translations.items():
                    TagTranslation.insert(
                        tag=tag, language=language, name=name, source="pixiv"
                    ).on_conflict(
                        conflict_target=[
                            TagTranslation.tag,
                            TagTranslation.language,
                            TagTranslation.source,
                        ],
                        update={TagTranslation.name: name},
                    ).execute()
        if bookmark is not None and remote.bookmark_tags is not None:
            replace_bookmark_tags(bookmark, remote.bookmark_tags)
        return artwork, changed


def local_tag(name: str) -> str:
    name = unicodedata.normalize("NFC", name.strip())
    if not 1 <= len(name) <= 100 or any(unicodedata.category(c).startswith("C") for c in name):
        raise DomainError("invalid_tag", "태그는 제어 문자 없이 1~100자로 입력해 주세요.")
    return name


def edit_tags(ids: list[str], name: str, action: str):
    name = local_tag(name)
    selected = list(dict.fromkeys(ids))
    with db.atomic("IMMEDIATE"):
        for artwork_id in selected:
            require_artwork(artwork_id)
        if action == "remove":
            tag = Tag.get_or_none(Tag.name == name)
            if tag:
                ArtworkTag.delete().where(
                    (ArtworkTag.artwork.in_(selected))
                    & (ArtworkTag.tag == tag)
                    & (ArtworkTag.source == "local")
                ).execute()
            return
        tag = ensure_tag(name)
        Tag.update(local_created=True).where(Tag.id == tag.id).execute()
        ArtworkTag.insert_many(
            [(artwork_id, tag.id, "local") for artwork_id in selected],
            fields=[ArtworkTag.artwork, ArtworkTag.tag, ArtworkTag.source],
        ).on_conflict_ignore().execute()


def require_artwork(artwork_id: str) -> Artwork:
    artwork = Artwork.get_or_none(Artwork.id == artwork_id)
    if not artwork or Tombstone.get_or_none(Tombstone.artwork_id == artwork_id):
        raise DomainError("not_found", "작품을 찾을 수 없습니다.", 404)
    if get_setting("show_sensitive") != "true" and artwork.rating != "safe":
        raise DomainError("hidden_rating", "설정에서 이 작품의 등급 표시를 허용해 주세요.", 404)
    return artwork


def filters(q="", artist=None, tag=None, **options) -> tuple[str, list]:
    query = ArtworkQuery(
        q=q, artist=artist, tag=[tag] if isinstance(tag, str) else tag or [], **options
    )
    return conditions(query, get_setting("show_sensitive") == "true")


def artwork_summary(a: Artwork) -> dict:
    first = None
    legacy = False
    if a.kind == "ugoira":
        bundle = (
            UgoiraBundle.select()
            .where(UgoiraBundle.artwork == a)
            .order_by(SQL("rowid DESC"))
            .first()
        )
        if bundle:
            first = bundle.thumbnail if bundle.thumbnail_id else None
        else:
            legacy = True
    else:
        source = (
            Asset.select()
            .where(
                (Asset.artwork == a)
                & (Asset.page == 0)
                & (Asset.role == "original")
                & (Asset.state != "deleted")
            )
            .order_by(SQL("rowid DESC"))
            .first()
        )
        if source:
            linked = source.thumbnail if source.thumbnail_id else None
            first = (
                linked
                if linked
                and linked.state == "ready"
                and linked.artwork_id == a.id
                and linked.page == 0
                and linked.role == "thumbnail"
                else source
            )
        else:
            legacy = True
    if legacy:
        first = (
            Asset.select()
            .where(
                (Asset.artwork == a)
                & (Asset.page == 0)
                & (Asset.role == "thumbnail")
                & (Asset.state == "ready")
            )
            .order_by(SQL("rowid DESC"))
            .first()
        )
    if first and (
        first.state != "ready"
        or first.artwork_id != a.id
        or first.page != 0
        or first.role not in ("original", "thumbnail")
    ):
        first = None
    return {
        "id": a.id,
        "title": a.title,
        "artist_id": a.artist_id,
        "artist_name": a.artist.name,
        "kind": a.kind,
        "rating": a.rating,
        "page_count": a.page_count,
        "archive_status": a.archive_status,
        "collected_at": a.collected_at,
        "created_at": a.created_at,
        "is_fixture": a.is_fixture,
        "thumbnail_url": f"/api/media/{first.id}" if first else None,
        "width": first.width if first else None,
        "height": first.height if first else None,
    }


def list_artworks(
    q="", artist=None, tag=None, page=1, page_size=24, sort=DEFAULT_ARTWORK_SORT, **options
) -> dict:
    where, params = filters(q, artist, tag, **options)
    base = f"FROM artwork a JOIN artist ar ON ar.id=a.artist_id WHERE {where}"
    total = db.execute_sql(f"SELECT count(*) {base}", params).fetchone()[0]
    ids = [
        r[0]
        for r in db.execute_sql(
            f"SELECT a.id {base} ORDER BY {ORDERS[sort]} LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        )
    ]
    return {
        "items": [artwork_summary(Artwork.get_by_id(i)) for i in ids],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def neighbors(artwork_id: str, query: ArtworkQuery) -> dict:
    require_artwork(artwork_id)
    where, params = conditions(query, get_setting("show_sensitive") == "true")
    base = f"FROM artwork a JOIN artist ar ON ar.id=a.artist_id WHERE {where}"
    order = ORDERS[query.sort]
    row = db.execute_sql(
        f"""SELECT previous, next, position, total FROM (
        SELECT a.id, lag(a.id) OVER (ORDER BY {order}) AS previous,
        lead(a.id) OVER (ORDER BY {order}) AS next,
        row_number() OVER (ORDER BY {order}) AS position, count(*) OVER () AS total
        {base}) WHERE id=?""",
        [*params, artwork_id],
    ).fetchone()
    if row:
        return dict(zip(("previous", "next", "position", "total"), row, strict=True))
    total = db.execute_sql(f"SELECT count(*) {base}", params).fetchone()[0]
    return {"previous": None, "next": None, "position": None, "total": total}


def edit_note(artwork_id: str, note: str):
    with db.atomic("IMMEDIATE"):
        artwork = require_artwork(artwork_id)
        artwork.note = note
        artwork.save(only=[Artwork.note])
    return detail(artwork_id)


def edit_alias(artist_id: str, alias: str):
    alias = unicodedata.normalize("NFC", alias.strip())
    if any(unicodedata.category(c).startswith("C") for c in alias):
        raise DomainError("invalid_alias", "별칭에 제어 문자를 사용할 수 없습니다.")
    with db.atomic("IMMEDIATE"):
        if not artists(artist=artist_id):
            raise DomainError("not_found", "작가를 찾을 수 없습니다.", 404)
        Artist.update(alias=alias).where(Artist.id == artist_id).execute()
    return artists(artist=artist_id)[0]


def detail(artwork_id: str) -> dict:
    a = require_artwork(artwork_id)
    tags: dict[int, dict] = {}
    for tag_id, name, source in db.execute_sql(
        f"""
        SELECT t.id, t.name, r.source FROM ({TAG_RELATIONS}) r
        JOIN tag t ON r.tag_id=t.id WHERE r.artwork_id=? ORDER BY t.name, r.source
    """,
        [a.id],
    ):
        item = tags.setdefault(tag_id, {"id": tag_id, "name": name, "sources": []})
        item["sources"].append(source)
    translations = tag_translations(list(tags))
    for tag_id, item in tags.items():
        item["translations"] = translations.get(tag_id, [])
    pages: dict[int, dict] = {}
    for asset in (
        Asset.select()
        .where(
            (Asset.artwork == a)
            & (Asset.role == "original")
            & (Asset.state == "ready")
            & (Asset.page >= 0)
            & (Asset.page < a.page_count)
        )
        .order_by(SQL("rowid DESC"))
    ):
        pages.setdefault(
            asset.page,
            {
                "index": asset.page,
                "url": f"/api/media/{asset.id}",
                "width": asset.width,
                "height": asset.height,
            },
        )
    bundle = (
        UgoiraBundle.select().where(UgoiraBundle.artwork == a).order_by(SQL("rowid DESC")).first()
    )

    def asset_url(asset):
        return f"/api/media/{asset.id}" if asset and asset.state == "ready" else None

    bookmark = Bookmark.get_or_none((Bookmark.account == 1) & (Bookmark.artwork == a))
    previous_names = [
        row.name
        for row in ArtistName.select()
        .where((ArtistName.artist == a.artist) & (ArtistName.name != a.artist.name))
        .order_by(SQL("last_seen IS NULL"), ArtistName.last_seen.desc(), ArtistName.id.desc())
    ]
    return {
        **artwork_summary(a),
        "download_allowed": preferences.download_allowed(a.rating),
        "rating_evidence": rating_evidence(a),
        "caption": a.caption,
        "tags": list(tags.values()),
        "pages": [pages[k] for k in sorted(pages)],
        "note": a.note,
        "artist_name_at_collection": a.artist_name_at_collection,
        "artist_account": a.artist.account,
        "artist_alias": a.artist.alias,
        "artist_previous_names": previous_names,
        "artist_avatar_url": avatar_url(a.artist),
        "remote_status": a.remote_status,
        "metadata_fetched_at": a.metadata_fetched_at,
        "bookmark": {
            "visibility": bookmark.visibility,
            "status": bookmark.status,
            "first_seen": bookmark.first_seen,
            "last_seen": bookmark.last_seen,
            "bookmarked_at": bookmark.bookmarked_at,
        }
        if bookmark
        else None,
        "playback_url": asset_url(bundle.video) if bundle and bundle.video_id else None,
        "conversion_state": bundle.state if bundle else None,
        "conversion_error": bundle.error_code if bundle else None,
        "archive_url": asset_url(bundle.archive) if bundle else None,
        "timing_url": asset_url(bundle.timing) if bundle else None,
    }


def avatar_url(artist: Artist) -> str | None:
    return f"/api/artists/{artist.id}/avatar" if artist.avatar_path else None


def rating_evidence(artwork: Artwork) -> dict | None:
    if not artwork.rating_evidence:
        return None
    try:
        return RatingEvidence.model_validate_json(artwork.rating_evidence).model_dump(mode="json")
    except ValueError:
        # An old/corrupt value is unavailable; never invent evidence from the normalized rating.
        return None


def tag_names(value) -> set[str] | None:
    if not isinstance(value, list):
        return None
    names = set()
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.add(item["name"])
        elif isinstance(item, str):
            names.add(item)
    return names


def files_signature(data: dict):
    pages = data.get("pages") if isinstance(data.get("pages"), list) else []
    return (
        [page.get("url") for page in pages if isinstance(page, dict)],
        data.get("ugoira_zip_url"),
        data.get("ugoira_frames"),
    )


def history(artwork_id: str) -> list[dict]:
    """Compatibility view of the latest 50 records, using bounded history reads."""
    from app.services import remote_history

    return [
        {**item, "revision": item["revision"][:12]}
        for item in remote_history.listing(artwork_id, 1, 50)["items"]
    ]


def artists(**options) -> list[dict]:
    where, params = filters(**options)
    rows = list(
        db.execute_sql(
            f"""SELECT ar.id, ar.name, ar.alias, count(*), ar.avatar_path, ar.account FROM artwork a
        JOIN artist ar ON ar.id=a.artist_id WHERE {where}
        GROUP BY ar.id ORDER BY ar.name, ar.id""",
            params,
        )
    )
    return artist_records(rows)


def artist_records(rows) -> list[dict]:
    """Add names for only the requested artist rows, shared by list and profile views."""
    names: dict[str, list[str]] = {}
    ids = [row[0] for row in rows]
    for start in range(0, len(ids), 500):
        for item in (
            ArtistName.select(ArtistName, Artist)
            .join(Artist)
            .where(
                ArtistName.artist.in_(ids[start : start + 500]) & (ArtistName.name != Artist.name)
            )
            .order_by(ArtistName.name)
        ):
            names.setdefault(item.artist_id, []).append(item.name)
    return [
        {
            "id": r[0],
            "name": r[1],
            "alias": r[2],
            "artwork_count": r[3],
            "avatar_url": f"/api/artists/{r[0]}/avatar" if r[4] else None,
            "previous_names": names.get(r[0], []),
            "account": r[5],
        }
        for r in rows
    ]


def tags(**options) -> list[dict]:
    where, params = filters(**options)
    rows = list(
        db.execute_sql(
            f"""SELECT t.id, t.name, r.source, count(DISTINCT a.id), {TAG_EDITABLE}
        FROM ({TAG_RELATIONS}) r JOIN tag t ON r.tag_id=t.id
        JOIN artwork a ON a.id=r.artwork_id JOIN artist ar ON ar.id=a.artist_id
        WHERE {where} GROUP BY t.id, r.source ORDER BY t.name, r.source""",
            params,
        )
    )
    translations = tag_translations(list(dict.fromkeys(row[0] for row in rows)))
    return [
        {
            "id": r[0],
            "name": r[1],
            "source": r[2],
            "artwork_count": r[3],
            "editable": r[2] == "local" and bool(r[4]),
            "translations": translations.get(r[0], []),
        }
        for r in rows
    ]


def tag_translations(ids: list[int]) -> dict[int, list[dict]]:
    result: dict[int, list[dict]] = {}
    for start in range(0, len(ids), 500):
        for item in (
            TagTranslation.select()
            .where(TagTranslation.tag.in_(ids[start : start + 500]))
            .order_by(TagTranslation.language, TagTranslation.source, TagTranslation.name)
        ):
            result.setdefault(item.tag_id, []).append(
                {"language": item.language, "name": item.name, "source": item.source}
            )
    return result
