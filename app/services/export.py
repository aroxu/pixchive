"""Portable exports: one artwork as ZIP, or the whole library's metadata as JSON."""

import asyncio
import hashlib
import json
import os
import stat
import zipfile
from datetime import UTC, datetime

from peewee import SQL

from app.core.config import Settings
from app.core.errors import DomainError
from app.core.time import now
from app.models.entities import (
    ArtistName,
    Artwork,
    ArtworkTag,
    Asset,
    Bookmark,
    BookmarkTag,
    Tag,
    TagTranslation,
    Tombstone,
    UgoiraBundle,
)
from app.services import library, storage

SCHEMA_VERSION = 2
APP_VERSION = "0.1.0.dev0"
CHUNK = 1024 * 1024


def iso(timestamp: int | None) -> str | None:
    return datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp else None


def tags_of(artwork: Artwork) -> list[dict]:
    translations: dict[int, dict[str, str]] = {}
    relations = [
        (row.tag_id, row.tag.name, row.source)
        for row in ArtworkTag.select(ArtworkTag, Tag).join(Tag).where(ArtworkTag.artwork == artwork)
    ]
    bookmark = Bookmark.get_or_none((Bookmark.account == 1) & (Bookmark.artwork == artwork))
    if bookmark:
        relations += [
            (row.tag_id, row.tag.name, "pixiv_bookmark")
            for row in BookmarkTag.select(BookmarkTag, Tag)
            .join(Tag)
            .where(BookmarkTag.bookmark == bookmark)
        ]
    ids = {tag_id for tag_id, _, _ in relations}
    if ids:
        for item in TagTranslation.select().where(TagTranslation.tag.in_(list(ids))):
            translations.setdefault(item.tag_id, {})[item.language] = item.name
    return [
        {"name": name, "source": source, "translations": translations.get(tag_id, {})}
        for tag_id, name, source in sorted(relations, key=lambda x: (x[1], x[2]))
    ]


def current_files(artwork: Artwork) -> list[tuple[str, Asset]]:
    """Newest ready originals per page plus the newest ugoira source and playback."""
    files: dict[str, Asset] = {}
    pages: set[int] = set()
    for asset in (
        Asset.select()
        .where(
            (Asset.artwork == artwork)
            & (Asset.role == "original")
            & (Asset.state == "ready")
            & (Asset.page >= 0)
            & (Asset.page < artwork.page_count)
        )
        .order_by(SQL("rowid DESC"))
    ):
        if asset.page in pages:
            continue
        pages.add(asset.page)
        name = f"original/p{asset.page:04d}.{asset.path.rsplit('.', 1)[-1]}"
        files.setdefault(name, asset)
    bundle = (
        UgoiraBundle.select()
        .where(UgoiraBundle.artwork == artwork)
        .order_by(SQL("rowid DESC"))
        .first()
    )
    if bundle:
        for folder, asset in (
            ("ugoira", bundle.archive),
            ("ugoira", bundle.timing),
            ("playback", bundle.video if bundle.video_id else None),
        ):
            if asset and asset.state == "ready":
                files[f"{folder}/p{asset.page:04d}.{asset.path.rsplit('.', 1)[-1]}"] = asset
    return sorted(files.items())


def record(artwork: Artwork, files: list[tuple[str, Asset]] | None = None) -> dict:
    artist = artwork.artist
    bookmark = Bookmark.get_or_none((Bookmark.account == 1) & (Bookmark.artwork == artwork))
    return {
        "id": artwork.id,
        "title": artwork.title,
        "caption": artwork.caption,
        "kind": artwork.kind,
        "rating": artwork.rating,
        "rating_evidence": library.rating_evidence(artwork),
        "pixiv_url": f"https://www.pixiv.net/artworks/{artwork.id}"
        if not artwork.is_fixture
        else None,
        "page_count": artwork.page_count,
        "created_at": artwork.created_at,
        "collected_at": iso(artwork.collected_at),
        "metadata_fetched_at": iso(artwork.metadata_fetched_at),
        "archive_status": artwork.archive_status,
        "remote_status": artwork.remote_status,
        "note": artwork.note,
        "sample": artwork.is_fixture,
        "artist": {
            "id": artist.id,
            "name": artist.name,
            "account": artist.account,
            "profile_url": f"https://www.pixiv.net/users/{artist.id}"
            if not artwork.is_fixture
            else None,
            "alias": artist.alias,
            "name_at_collection": artwork.artist_name_at_collection,
            "known_names": [
                row.name for row in ArtistName.select().where(ArtistName.artist == artist)
            ],
        },
        "bookmark": {
            "visibility": bookmark.visibility,
            "status": bookmark.status,
            "bookmarked_at": bookmark.bookmarked_at,
            "first_seen": iso(bookmark.first_seen),
            "last_seen": iso(bookmark.last_seen),
        }
        if bookmark
        else None,
        "tags": tags_of(artwork),
        "files": [
            {
                "file": name,
                "role": asset.role,
                "page": asset.page,
                "revision": asset.revision,
                "sha256": asset.sha256,
                "size": asset.size,
                "media_type": asset.media_type,
                "width": asset.width,
                "height": asset.height,
                "state": asset.state,
                "provenance": asset.provenance,
            }
            for name, asset in (
                files
                if files is not None
                else (
                    (asset.path, asset)
                    for asset in Asset.select()
                    .where((Asset.artwork == artwork) & (Asset.state != "deleted"))
                    .order_by(SQL("rowid DESC"))
                )
            )
        ],
    }


class Sink:
    """A write-only target for zipfile; bytes are drained into the HTTP stream."""

    def __init__(self):
        self.buffer = bytearray()

    def write(self, data):
        self.buffer.extend(data)
        return len(data)

    def flush(self):
        pass

    def take(self) -> bytes:
        data = bytes(self.buffer)
        self.buffer.clear()
        return data


def artwork_archive(settings: Settings, artwork_id: str):
    """Validate access and resolve files now; stream bytes later without a temp file."""
    artwork = library.require_artwork(artwork_id)
    files = current_files(artwork)
    paths = [(name, asset, storage.safe_path(settings, asset.path)) for name, asset in files]
    for _, asset, path in paths:
        try:
            info = path.stat()
        except FileNotFoundError:
            raise DomainError(
                "file_missing", "보관 파일이 없습니다. 원본 복구를 실행해 주세요.", 409
            ) from None
        except OSError:
            raise DomainError("file_unreadable", "보관 파일을 읽을 수 없습니다.", 409) from None
        if not stat.S_ISREG(info.st_mode):
            raise DomainError("file_unreadable", "보관 파일을 읽을 수 없습니다.", 409)
        if info.st_size != asset.size:
            raise DomainError(
                "file_corrupt", "보관 파일의 크기가 다릅니다. 원본 복구를 실행해 주세요.", 409
            )
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "exported_at": iso(now()),
        "artwork": record(artwork, files),
    }
    manifest = json.dumps(metadata, ensure_ascii=False, indent=2).encode()

    async def stream():
        sink = Sink()
        archive = zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True)
        try:
            library.require_artwork(artwork_id)
            archive.writestr(zipfile.ZipInfo("metadata.json", date_time=now_tuple()), manifest)
            yield sink.take()
            for name, asset, path in paths:
                library.require_artwork(artwork_id)
                info = zipfile.ZipInfo(name, date_time=now_tuple())
                info.file_size = asset.size
                digest, size = hashlib.sha256(), 0
                # Never skip a file named by the manifest. Any race/error aborts the ZIP.
                with path.open("rb") as source:
                    observed = os.fstat(source.fileno())
                    if not stat.S_ISREG(observed.st_mode) or observed.st_size != asset.size:
                        raise DomainError("file_corrupt", "보관 파일을 검증하지 못했습니다.", 409)
                    with archive.open(info, "w") as target:
                        while chunk := await asyncio.to_thread(source.read, CHUNK):
                            digest.update(chunk)
                            size += len(chunk)
                            if size > asset.size:
                                raise DomainError(
                                    "file_corrupt", "보관 파일을 검증하지 못했습니다.", 409
                                )
                            target.write(chunk)
                            yield sink.take()
                if size != asset.size or digest.hexdigest() != asset.sha256:
                    raise DomainError("file_corrupt", "보관 파일을 검증하지 못했습니다.", 409)
                yield sink.take()
            library.require_artwork(artwork_id)
            archive.close()
            yield sink.take()
        finally:
            # Cleanup on disconnect/error; only the successful path yields a central directory.
            archive.close()

    return f"pixchive-{artwork.id}.zip", stream()


def now_tuple():
    moment = datetime.now(UTC)
    return (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)


def library_metadata():
    """All archived artworks regardless of display preference, plus exclusion records."""

    async def stream():
        head = {"schema_version": SCHEMA_VERSION, "app_version": APP_VERSION}
        head["exported_at"] = iso(now())
        yield json.dumps(head, ensure_ascii=False)[:-1].encode() + b',"artworks":['
        last, first = "", True
        while True:
            batch = list(
                Artwork.select()
                .where(
                    (Artwork.id > last) & Artwork.id.not_in(Tombstone.select(Tombstone.artwork_id))
                )
                .order_by(Artwork.id)
                .limit(200)
            )
            if not batch:
                break
            chunk = []
            for artwork in batch:
                chunk.append(json.dumps(record(artwork), ensure_ascii=False))
            yield (b"" if first else b",") + ",".join(chunk).encode()
            first, last = False, batch[-1].id
            await asyncio.sleep(0)
        exclusions = [
            {"artwork_id": item.artwork_id, "created_at": iso(item.created_at), "state": item.state}
            for item in Tombstone.select().order_by(Tombstone.created_at)
        ]
        yield b'],"exclusions":' + json.dumps(exclusions, ensure_ascii=False).encode() + b"}"

    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return f"pixchive-metadata-{stamp}.json", stream()
