import hashlib
import io
import os
import re
import uuid
from contextlib import ExitStack, nullcontext
from pathlib import Path

from peewee import SQL
from PIL import Image, ImageOps

from app.core.config import Settings
from app.core.db import db
from app.core.errors import DomainError
from app.core.files import locked_file, sync_directory
from app.models.entities import Artist, Artwork, Asset, Tombstone
from app.services import tempfiles
from app.services.library import require_artwork

THUMBNAIL_BOX = (640, 1600)
MAX_AVATAR_BYTES = 5 * 1024 * 1024
FILE_STATES = ("ready", "staged", "corrupt", "unreadable")
# Kept files that later failed verification. They need checking/repair, not a first download.
DAMAGED_STATES = ("corrupt", "missing", "unreadable")


def excluded_error():
    return DomainError("excluded", "수집에서 제외한 작품입니다.", 409)


def safe_path(settings: Settings, relative: str) -> Path:
    root = settings.data_dir.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root / "media"):
        raise DomainError("invalid_path", "올바르지 않은 파일 경로입니다.", 400)
    return path


def media_lock(settings: Settings, guard=None):
    """Serialize file commits and verification across threads and worker processes."""
    return locked_file(settings.data_dir / ".media.lock", guard)


def refresh_archive(artwork_id: str):
    a = Artwork.get_by_id(artwork_id)
    if a.kind == "ugoira":
        archived_roles = ("ugoira_zip", "ugoira_timing")
        archives = {
            x.revision
            for x in Asset.select().where(
                (Asset.artwork == a) & (Asset.role == "ugoira_zip") & (Asset.state == "ready")
            )
        }
        timings = {
            x.revision
            for x in Asset.select().where(
                (Asset.artwork == a) & (Asset.role == "ugoira_timing") & (Asset.state == "ready")
            )
        }
        status = (
            "complete" if archives & timings else "partial" if archives or timings else "pending"
        )
    else:
        archived_roles = ("original",)
        count = (
            Asset.select(Asset.page)
            .where(
                (Asset.artwork == a)
                & (Asset.role == "original")
                & (Asset.state == "ready")
                & (Asset.page >= 0)
                & (Asset.page < a.page_count)
            )
            .distinct()
            .count()
        )
        status = "complete" if count == a.page_count else "partial" if count else "pending"
    if (
        status == "pending"
        and Asset.select()
        .where(
            (Asset.artwork == a)
            & Asset.role.in_(archived_roles)
            & Asset.state.in_(DAMAGED_STATES)
            & (Asset.page >= 0)
            & (Asset.page < a.page_count)
        )
        .exists()
    ):
        status = "partial"
    if status != a.archive_status:
        # Do not rewrite remote metadata or local edits for an unchanged status.
        Artwork.update(archive_status=status).where(
            (Artwork.id == artwork_id) & (Artwork.archive_status != status)
        ).execute()


def store_image(
    settings: Settings,
    artwork_id: str,
    page: int,
    content: bytes,
    *,
    after_move=None,
    on_staged=None,
    guard=None,
    role="original",
    revision=None,
    provenance=None,
) -> Asset:
    a = Artwork.get_by_id(artwork_id)
    if Tombstone.get_or_none(Tombstone.artwork_id == a.id):
        raise DomainError("excluded", "수집에서 제외한 작품입니다.", 409)
    if not all(re.fullmatch(r"[0-9]{1,20}", v) for v in (a.id, a.artist_id)):
        raise DomainError("invalid_id", "올바르지 않은 작품 ID입니다.")
    if not 0 <= page < a.page_count:
        raise DomainError("invalid_page", "올바르지 않은 페이지입니다.")
    try:
        with Image.open(io.BytesIO(content)) as img:
            img.verify()
        with Image.open(io.BytesIO(content)) as img:
            img.load()
            width, height, fmt = img.width, img.height, img.format
        extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}[fmt]
    except (OSError, ValueError, KeyError, Image.DecompressionBombError):
        raise DomainError("invalid_media", "이미지 파일을 검증할 수 없습니다.") from None
    return store_blob(
        settings,
        artwork_id,
        page,
        content,
        role=role,
        extension=extension,
        media_type=Image.MIME[fmt],
        width=width,
        height=height,
        revision=revision,
        provenance=provenance,
        after_move=after_move,
        on_staged=on_staged,
        guard=guard,
    )


def store_blob(
    settings,
    artwork_id,
    page,
    content,
    *,
    role,
    extension,
    media_type,
    width=0,
    height=0,
    revision=None,
    after_move=None,
    on_staged=None,
    guard=None,
    provenance=None,
):
    a = Artwork.get_by_id(artwork_id)
    provenance = (
        "synthetic"
        if a.is_fixture
        else provenance
        or {
            "thumbnail": "local_thumbnail",
            "video": "local_video",
            "ugoira_timing": "provided_frames",
        }.get(role)
    )
    if provenance not in (
        None,
        "pixiv_pages_original",
        "pixiv_ugoira_original_src",
        "gallery_url",
        "synthetic",
        "local_thumbnail",
        "local_video",
        "provided_frames",
    ):
        raise DomainError("invalid_media", "파일 수집 경로를 확인할 수 없습니다.")
    if Tombstone.get_or_none(Tombstone.artwork_id == a.id):
        raise DomainError("excluded", "수집에서 제외한 작품입니다.", 409)
    if (
        not all(re.fullmatch(r"[0-9]{1,20}", v) for v in (a.id, a.artist_id))
        or role not in {"original", "ugoira_zip", "ugoira_timing", "thumbnail", "video"}
        or extension not in {"png", "jpg", "gif", "webp", "zip", "json", "mp4"}
        or not 0 <= page < a.page_count
    ):
        raise DomainError("invalid_path", "올바르지 않은 파일 경로입니다.")
    if role == "video" and (extension != "mp4" or media_type != "video/mp4"):
        raise DomainError("invalid_media", "우고이라 재생 파일은 MP4 형식이어야 합니다.")
    sha = hashlib.sha256(content).hexdigest()
    revision = revision or sha
    if not re.fullmatch(r"[a-f0-9]{64}", revision):
        raise DomainError("invalid_path", "올바르지 않은 파일 경로입니다.")
    relative = f"media/{a.artist_id}/{a.id}/{role}/{revision}/p{page:04d}.{extension}"
    destination = safe_path(settings, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfiles.blob_workspace(settings, guard) as working, ExitStack() as cleanup:
        tmp_path = working.path / f"content-{uuid.uuid4().hex}.part"
        with tmp_path.open("xb") as file:
            # A borrowed workspace outlives this call. Release unused bytes even
            # on duplicate reuse, rejected commits, or a failed write/fsync.
            cleanup.callback(tmp_path.unlink, missing_ok=True)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        publication = (
            cleanup.enter_context(tempfiles.publication_workspace(settings, guard))
            if settings.cache_dir is not None
            else None
        )
        with media_lock(settings, guard):
            asset = Asset.get_or_none(Asset.path == relative)
            expected = {
                "artwork_id": a.id,
                "revision": revision,
                "page": page,
                "role": role,
                "sha256": sha,
                "size": len(content),
                "width": width,
                "height": height,
                "media_type": media_type,
            }
            if asset and any(getattr(asset, key) != value for key, value in expected.items()):
                raise DomainError("integrity_error", "보관 파일의 기록과 받은 내용이 다릅니다.")
            replace = not destination.exists()
            if not replace:
                if not destination.is_file():
                    raise DomainError("integrity_error", "보관 파일 경로를 확인해 주세요.")
                replace = (
                    destination.stat().st_size != len(content) or hash_file(destination) != sha
                )
                if replace and asset is None:
                    # Never claim or overwrite unknown bytes at an immutable path.
                    raise DomainError("integrity_error", "기존 파일의 보관 기록을 확인해 주세요.")
            with db.atomic("IMMEDIATE"):
                if guard:
                    guard()
                if Tombstone.get_or_none(Tombstone.artwork_id == a.id):
                    raise excluded_error()
                if asset is None:
                    asset = Asset.create(path=relative, provenance=provenance, **expected)
                if replace or asset.state != "ready":
                    Asset.update(state="staged").where(Asset.id == asset.id).execute()
                if on_staged:
                    on_staged(asset)
            # Hashing, waiting for the media lock and atomic replacement never hold
            # a SQLite write transaction. Only identical recorded bytes can repair damage.
            if guard:
                guard()
            if replace:
                publishing = (
                    _copy_for_publication(tmp_path, publication, len(content), sha, cleanup, guard)
                    if publication is not None
                    else tmp_path
                )
                os.replace(publishing, destination)
                sync_directory(destination.parent)
            if after_move:
                after_move()
            excluded = False
            with db.atomic("IMMEDIATE"):
                if guard:
                    guard()
                if Tombstone.get_or_none(Tombstone.artwork_id == a.id):
                    excluded = True
                    Asset.update(state="deleted").where(Asset.id == asset.id).execute()
                else:
                    Asset.update(state="ready").where(Asset.id == asset.id).execute()
                    if provenance:
                        Asset.update(provenance=provenance).where(
                            (Asset.id == asset.id) & Asset.provenance.is_null()
                        ).execute()
                    if role in ("original", "ugoira_zip", "ugoira_timing"):
                        refresh_archive(a.id)
            if excluded:
                destination.unlink(missing_ok=True)
                sync_directory(destination.parent)
                raise excluded_error()
            return Asset.get_by_id(asset.id)


def _copy_for_publication(source, working, size, sha, cleanup, guard=None):
    """Copy cache bytes to NAS, verifying actual durable bytes before rename.

    The caller owns the publication workspace before taking the media lock.
    Blob callers keep that lock here, without a SQLite write transaction.
    """
    if guard:
        guard()
    if source.stat().st_size != size or hash_file(source) != sha:
        raise DomainError("integrity_error", "임시 파일의 내용을 확인할 수 없습니다.")
    publishing = working.path / f"content-{uuid.uuid4().hex}.part"
    with source.open("rb") as incoming, publishing.open("xb") as outgoing:
        cleanup.callback(publishing.unlink, missing_ok=True)
        os.fchmod(outgoing.fileno(), 0o600)
        copied = 0
        while chunk := incoming.read(1024 * 1024):
            if guard:
                guard()
            copied += len(chunk)
            if copied > size:
                raise DomainError("integrity_error", "임시 파일의 내용을 확인할 수 없습니다.")
            outgoing.write(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if guard:
        guard()
    if publishing.stat().st_size != size or hash_file(publishing) != sha:
        raise DomainError("integrity_error", "보관할 파일의 내용을 확인할 수 없습니다.")
    if guard:
        guard()
    return publishing


def hash_file(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def inspect_asset(settings: Settings, asset: Asset) -> str:
    try:
        path = safe_path(settings, asset.path)
        if not path.is_file():
            return "missing"
        return (
            "ready"
            if path.stat().st_size == asset.size and hash_file(path) == asset.sha256
            else "corrupt"
        )
    except (OSError, DomainError):
        return "unreadable"


def quick_check(settings: Settings, asset: Asset) -> str:
    """Cheap reuse check for immutable files; full hashes belong to the integrity job."""
    try:
        path = safe_path(settings, asset.path)
        if not path.is_file():
            return "missing"
        return "ready" if path.stat().st_size == asset.size else "corrupt"
    except (OSError, DomainError):
        return "unreadable"


def verify_asset(settings: Settings, asset: Asset, guard=None, *, full_hash=True) -> str | None:
    """Persist a fresh inspection before a writer can change the inspected file."""
    connection = db.connection_context() if db.is_closed() else nullcontext()
    with connection, media_lock(settings, guard):
        current = Asset.get_or_none(Asset.id == asset.id)
        if (
            current is None
            or current.state == "deleted"
            or Tombstone.get_or_none(Tombstone.artwork_id == current.artwork_id)
        ):
            return None
        # A previous failure is never cleared by a matching file size alone.
        inspect = inspect_asset if full_hash or current.state != "ready" else quick_check
        state = inspect(settings, current)
        if state == current.state == "ready":
            # Immutable bytes already match the ready record. Reuse is read-only;
            # ownership/deletion still need checking after the file inspection.
            if guard:
                guard()
            if Tombstone.select().where(Tombstone.artwork_id == current.artwork_id).exists():
                return None
            if (
                not Asset.select()
                .where((Asset.id == current.id) & (Asset.state == "ready"))
                .exists()
            ):
                return None
            return state
        with db.atomic("IMMEDIATE"):
            if guard:
                guard()
            if Tombstone.get_or_none(Tombstone.artwork_id == current.artwork_id):
                return None
            changed = (
                Asset.update(state=state)
                .where((Asset.id == current.id) & (Asset.state != "deleted"))
                .execute()
            )
            if not changed:
                return None
            refresh_archive(current.artwork_id)
        return state


def reconcile(settings: Settings):
    for asset in Asset.select().where(Asset.state == "staged"):
        if Tombstone.get_or_none(Tombstone.artwork_id == asset.artwork_id):
            continue
        verify_asset(settings, asset)


def media(settings: Settings, asset_id: str) -> tuple[Path, str]:
    asset = Asset.get_or_none(Asset.id == asset_id)
    if not asset:
        raise DomainError("not_found", "파일을 찾을 수 없습니다.", 404)
    require_artwork(asset.artwork_id)
    path = safe_path(settings, asset.path)
    if asset.state != "ready" or not path.is_file():
        raise DomainError("media_unavailable", "보관 파일을 사용할 수 없습니다.", 404)
    return path, asset.media_type


def thumbnail_source(artwork_id: str, *, upper: int | None = None) -> Asset | None:
    """Choose the latest kept first-page original; never silently fall back after corruption."""
    query = Asset.select().where(
        (Asset.artwork == artwork_id)
        & (Asset.page == 0)
        & (Asset.role == "original")
        & (Asset.state != "deleted")
    )
    if upper is not None:
        query = query.where(SQL("rowid") <= upper)
    return query.order_by(SQL("rowid DESC")).first()


def needs_thumbnail(artwork_id: str, *, source: Asset | None = None) -> Asset | None:
    """A shared thumbnail's creation order cannot establish its original revision."""
    source = source if source is not None else thumbnail_source(artwork_id)
    if source is None or source.state != "ready":
        return None
    thumbnail = source.thumbnail if source.thumbnail_id else None
    if (
        thumbnail is not None
        and thumbnail.state == "ready"
        and thumbnail.role == "thumbnail"
        and thumbnail.artwork_id == artwork_id
        and thumbnail.page == 0
    ):
        return None
    return source


def make_thumbnail(settings: Settings, artwork_id: str, source: Asset, guard=None) -> Asset:
    """Derive a small WebP preview from a verified original. The original is untouched."""
    path = safe_path(settings, source.path)
    try:
        with Image.open(path) as image:
            image.draft("RGB", (THUMBNAIL_BOX[0] * 2, THUMBNAIL_BOX[1] * 2))
            image.seek(0)
            preview = ImageOps.exif_transpose(image)
            alpha = "A" in preview.getbands() or "transparency" in preview.info
            preview = preview.convert("RGBA" if alpha else "RGB")
            preview.thumbnail(THUMBNAIL_BOX, Image.Resampling.LANCZOS)
            output = io.BytesIO()
            preview.save(output, "WEBP", quality=82, method=4)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        if isinstance(exc, OSError) and exc.errno is not None:
            raise
        raise DomainError("invalid_media", "이미지 파일을 검증할 수 없습니다.") from None
    thumbnail = store_image(
        settings, artwork_id, 0, output.getvalue(), role="thumbnail", guard=guard
    )
    with db.atomic("IMMEDIATE"):
        if guard:
            guard()
        if Tombstone.select().where(Tombstone.artwork_id == artwork_id).exists():
            raise excluded_error()
        linked = (
            Asset.update(thumbnail=thumbnail)
            .where(
                (Asset.id == source.id)
                & (Asset.artwork == artwork_id)
                & (Asset.page == 0)
                & (Asset.role == "original")
                & (Asset.state == "ready")
            )
            .execute()
        )
        if not linked:
            raise DomainError("source_missing", "첫 페이지 원본을 확인하거나 복구해 주세요.", 409)
    return thumbnail


def remove_file(settings: Settings, asset: Asset) -> int:
    """Permanently delete one archived file. Returns the bytes released."""
    try:
        path = safe_path(settings, asset.path)
    except DomainError:
        return 0
    with media_lock(settings):
        try:
            size = path.stat().st_size
            path.unlink()
            sync_directory(path.parent)
        except FileNotFoundError:
            return 0
        return size


def prune_directories(settings: Settings, relative_paths: list[str]):
    """Remove now-empty per-artwork directories; never touches non-empty ones."""
    root = (settings.data_dir / "media").resolve()
    folders = set()
    for relative in relative_paths:
        parts = Path(relative).parts
        if len(parts) >= 3 and parts[0] == "media":
            folders.add(safe_path(settings, "/".join(parts[:3])))
    for folder in folders:
        if not folder.is_dir():
            continue
        for directory, _, _ in sorted(os.walk(folder), key=lambda x: len(x[0]), reverse=True):
            try:
                os.rmdir(directory)
            except OSError:
                pass
        parent = folder.parent
        if parent != root and parent.is_relative_to(root):
            try:
                os.rmdir(parent)
            except OSError:
                pass


def avatar_file(settings: Settings, relative: str) -> Path:
    root = (settings.data_dir / "avatars").resolve()
    path = (settings.data_dir / relative).resolve()
    if not path.is_relative_to(root):
        raise DomainError("invalid_path", "올바르지 않은 파일 경로입니다.", 400)
    return path


def avatar(settings: Settings, artist_id: str) -> tuple[Path, str]:
    artist = Artist.get_or_none(Artist.id == artist_id)
    if not artist or not artist.avatar_path:
        raise DomainError("not_found", "프로필 이미지를 찾을 수 없습니다.", 404)
    path = avatar_file(settings, artist.avatar_path)
    if not path.is_file():
        raise DomainError("media_unavailable", "보관 파일을 사용할 수 없습니다.", 404)
    return path, artist.avatar_media_type or "application/octet-stream"


def store_avatar(settings: Settings, artist_id: str, content: bytes) -> tuple[str, str]:
    """Store a validated profile image under an ID and content-hash path."""
    if not re.fullmatch(r"[0-9]{1,20}", artist_id) or len(content) > MAX_AVATAR_BYTES:
        raise DomainError("invalid_media", "이미지 파일을 검증할 수 없습니다.")
    try:
        with Image.open(io.BytesIO(content)) as img:
            img.verify()
        with Image.open(io.BytesIO(content)) as img:
            if img.width * img.height > 4_000_000:
                raise ValueError
            img.load()
            fmt = img.format
        extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}[fmt]
    except (OSError, ValueError, KeyError, Image.DecompressionBombError):
        raise DomainError("invalid_media", "이미지 파일을 검증할 수 없습니다.") from None
    sha = hashlib.sha256(content).hexdigest()
    relative = f"avatars/{artist_id}/{sha}.{extension}"
    destination = avatar_file(settings, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        with tempfiles.workspace(settings, "avatar") as working, ExitStack() as cleanup:
            tmp_path = working.path / f"content-{uuid.uuid4().hex}.part"
            with tmp_path.open("xb") as file:
                cleanup.callback(tmp_path.unlink, missing_ok=True)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            if settings.cache_dir is not None:
                publication = cleanup.enter_context(tempfiles.publication_workspace(settings))
                publishing = _copy_for_publication(
                    tmp_path, publication, len(content), sha, cleanup
                )
            else:
                publishing = tmp_path
            os.replace(publishing, destination)
            sync_directory(destination.parent)
    return relative, Image.MIME[fmt]
