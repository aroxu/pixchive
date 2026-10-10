"""Consistent SQLite snapshots, an incremental media mirror, and new-install restore.

Original files are immutable and content-addressed, so a mirror only ever gains files.
A snapshot directory pins one database state; restore copies only the files it references.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from app.core.config import Settings
from app.core.data_state import RESTORE_DATABASE, RESTORE_MARKER, DataStateError, data_access
from app.core.files import locked_file, sync_directory

FORMAT = 1
APP_VERSION = "0.1.0.dev0"
MIRROR = "media-mirror"


class BackupError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def relative_ok(relative: str) -> bool:
    if not isinstance(relative, str):
        return False
    path = PurePosixPath(relative)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and len(path.parts) >= 2
        and str(path) == relative
        and path.parts[0] in ("media", "avatars")
        and re.fullmatch(r"[A-Za-z0-9._/-]+", relative) is not None
    )


def connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def check_snapshot(path: Path, known_versions: set[int] | None = None) -> dict:
    if path.is_symlink():
        raise BackupError("unsafe_file", "스냅샷 DB는 심볼릭 링크가 아닌 파일이어야 합니다.")
    try:
        info = path.stat()
    except FileNotFoundError:
        raise BackupError("snapshot_missing", f"스냅샷 DB가 없습니다: {path}") from None
    if not stat.S_ISREG(info.st_mode):
        raise BackupError("unsafe_file", "스냅샷 DB는 일반 파일이어야 합니다.")
    connection = connect_readonly(path)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("snapshot_corrupt", "스냅샷 DB 무결성 검사에 실패했습니다.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("snapshot_corrupt", "스냅샷 DB의 연결 관계를 확인할 수 없습니다.")
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_version")}
        if (
            not versions
            or any(type(version) is not int or version < 1 for version in versions)
            or any(version != index for index, version in enumerate(sorted(versions), 1))
        ):
            raise BackupError("snapshot_corrupt", "스냅샷 DB의 스키마 이력이 올바르지 않습니다.")
        if known_versions is not None and versions - known_versions:
            raise BackupError("schema_newer", "이 버전보다 새로운 스키마의 백업입니다.")
        artworks = connection.execute(
            "SELECT count(*) FROM artwork WHERE id NOT IN (SELECT artwork_id FROM tombstone)"
        ).fetchone()[0]
        assets, size = connection.execute(
            "SELECT count(*), coalesce(sum(size), 0) FROM asset WHERE state IN ('ready', 'staged')"
        ).fetchone()
        return {
            "schema_version": max(versions) if versions else 0,
            "artworks": artworks,
            "files": assets,
            "file_bytes": size,
        }
    except sqlite3.DatabaseError:
        raise BackupError("snapshot_corrupt", "스냅샷 DB를 읽을 수 없습니다.") from None
    finally:
        connection.close()


def referenced(path: Path) -> Iterator[tuple[str, int | None, str | None]]:
    """Stream every reference; an unsafe or incomplete record is never silently omitted."""
    connection = connect_readonly(path)
    try:
        for relative, size, sha in connection.execute(
            "SELECT path, size, sha256 FROM asset WHERE state IN ('ready', 'staged')"
        ):
            if (
                not relative_ok(relative)
                or type(size) is not int
                or size < 0
                or not isinstance(sha, str)
                or re.fullmatch(r"[a-f0-9]{64}", sha) is None
            ):
                raise BackupError(
                    "snapshot_reference", "스냅샷의 파일 경로·크기·해시 기록을 확인해 주세요."
                )
            yield relative, size, sha
        for (relative,) in connection.execute(
            "SELECT avatar_path FROM artist WHERE avatar_path IS NOT NULL"
        ):
            if not relative_ok(relative) or PurePosixPath(relative).parts[0] != "avatars":
                raise BackupError(
                    "snapshot_reference", "스냅샷의 프로필 이미지 경로를 확인해 주세요."
                )
            # Current avatars have a full content hash in their immutable filename.
            stem = PurePosixPath(relative).stem
            sha = stem if re.fullmatch(r"[a-f0-9]{64}", stem) else None
            yield relative, None, sha
    except sqlite3.DatabaseError:
        raise BackupError("snapshot_corrupt", "스냅샷의 파일 기록을 읽을 수 없습니다.") from None
    finally:
        connection.close()


def referenced_path(root: Path, relative: str) -> Path:
    base = root.resolve()
    path = base / relative
    if not relative_ok(relative) or not path.resolve().is_relative_to(base):
        raise BackupError("unsafe_file", "백업 파일 경로가 지정한 디렉터리를 벗어납니다.")
    return path


@contextmanager
def read_file(path: Path, *, require_source: bool = False):
    # Nonblocking open also prevents a substituted FIFO from hanging a backup.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if require_source:
            raise BackupError("source_missing", "복사할 원본 파일이 없어졌습니다.") from None
        raise
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise BackupError("unsafe_file", "백업 대상은 일반 파일이어야 합니다.")
        os.set_blocking(file.fileno(), True)
        yield file


def signature(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass(frozen=True)
class FileCheck:
    state: str
    size: int | None = None
    sha256: str | None = None


def inspect_file(path: Path, size: int | None, sha: str | None, *, full_hash: bool) -> FileCheck:
    try:
        with read_file(path) as file:
            before = os.fstat(file.fileno())
            if size is not None and before.st_size != size:
                return FileCheck("mismatched", before.st_size)
            actual = hashlib.file_digest(file, "sha256").hexdigest() if full_hash else None
            if signature(before) != signature(os.fstat(file.fileno())) or (
                full_hash and sha is not None and actual != sha
            ):
                return FileCheck("mismatched", before.st_size, actual)
            state = "unverifiable" if full_hash and sha is None else "ready"
            return FileCheck(state, before.st_size, actual)
    except FileNotFoundError:
        return FileCheck("missing")


def hash_file(path: Path) -> str:
    with read_file(path) as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def make_directory(path: Path):
    """Persist newly created directory entries before a backup is published."""
    try:
        path.mkdir(mode=0o700)
    except FileNotFoundError:
        make_directory(path.parent)
        make_directory(path)
    except FileExistsError:
        if not path.is_dir():
            raise BackupError(
                "unsafe_file", "백업 파일을 저장할 디렉터리를 확인해 주세요."
            ) from None
    else:
        sync_directory(path.parent)


def copy_file(
    source: Path,
    destination: Path,
    *,
    expected_size: int | None = None,
    expected_sha: str | None = None,
):
    """Validate actual copied bytes before replacing any existing destination."""
    make_directory(destination.parent)
    temporary = None
    try:
        with read_file(source, require_source=True) as origin:
            before = os.fstat(origin.fileno())
            size = before.st_size if expected_size is None else expected_size
            if before.st_size != size:
                raise BackupError("file_changed", "복사할 파일의 크기·해시가 기록과 다릅니다.")
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".partial",
                delete=False,
            ) as output:
                temporary = Path(output.name)
                digest, copied = hashlib.sha256(), 0
                while chunk := origin.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > size:
                        raise BackupError("file_changed", "복사 중 파일의 크기가 바뀌었습니다.")
                    digest.update(chunk)
                    output.write(chunk)
                sha = digest.hexdigest()
                if (
                    copied != size
                    or signature(before) != signature(os.fstat(origin.fileno()))
                    or (expected_sha is not None and sha != expected_sha)
                ):
                    raise BackupError("file_changed", "복사 중 파일의 크기·해시가 바뀌었습니다.")
                output.flush()
                os.fsync(output.fileno())
            copied_file = inspect_file(temporary, size, sha, full_hash=True)
            if copied_file.state != "ready":
                raise BackupError(
                    "file_changed", "복사한 임시 파일의 크기·해시를 확인할 수 없습니다."
                )
            os.replace(temporary, destination)
            sync_directory(destination.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def create(settings: Settings, output: Path, *, media: bool) -> dict:
    database = settings.data_dir / "pixchive.db"
    try:
        info = database.stat()
    except FileNotFoundError:
        raise BackupError("database_missing", "백업할 DB가 없습니다.") from None
    if not stat.S_ISREG(info.st_mode) or database.is_symlink():
        raise BackupError("unsafe_file", "백업할 DB는 심볼릭 링크가 아닌 일반 파일이어야 합니다.")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    make_directory(output)
    with locked_file(output / ".backup.lock"):
        stage = Path(tempfile.mkdtemp(dir=output, prefix=".pixchive-backup-"))
        try:
            snapshot = stage / "pixchive.db"
            os.close(os.open(snapshot, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
            with (
                closing(connect_readonly(database)) as source,
                closing(sqlite3.connect(snapshot)) as destination,
            ):
                source.backup(destination)
                destination.execute("PRAGMA journal_mode=DELETE")
            with read_file(snapshot) as file:
                os.fsync(file.fileno())
            summary = check_snapshot(snapshot)
            copied = skipped = missing = mismatched = unavailable = unverifiable = 0
            if media:
                mirror = output / MIRROR
                if mirror.is_symlink():
                    raise BackupError(
                        "unsafe_file", "파일 미러는 심볼릭 링크가 아닌 디렉터리여야 합니다."
                    )
                make_directory(mirror)
                for relative, size, sha in referenced(snapshot):
                    if sha is None:
                        unverifiable += 1
                    origin = referenced_path(settings.data_dir, relative)
                    copy = referenced_path(mirror, relative)
                    current = inspect_file(origin, size, sha, full_hash=True)
                    if current.state == "missing":
                        missing += 1
                    elif current.state == "mismatched":
                        mismatched += 1
                    usable = current.state in ("ready", "unverifiable")
                    expected_size = current.size if usable else size
                    expected_sha = current.sha256 if usable else sha
                    existing = inspect_file(copy, expected_size, expected_sha, full_hash=True)
                    if existing.state == "ready" and expected_sha is not None:
                        skipped += 1
                    elif usable:
                        try:
                            copy_file(
                                origin, copy, expected_size=expected_size, expected_sha=expected_sha
                            )
                        except BackupError as exc:
                            if exc.code not in ("file_changed", "source_missing"):
                                raise
                            if exc.code == "source_missing":
                                missing += 1
                            else:
                                mismatched += 1
                            unavailable += 1
                        else:
                            copied += 1
                    else:
                        unavailable += 1
            else:
                # Validate references even when the operator requests only a DB snapshot.
                for _ in referenced(snapshot):
                    pass
            manifest = {
                "format": FORMAT,
                "app_version": APP_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                **summary,
                "media": {
                    "mirror": f"../{MIRROR}" if media else None,
                    "copied": copied,
                    "already_present": skipped,
                    "missing_in_source": missing,
                    "mismatched_in_source": mismatched,
                    "unavailable": unavailable,
                    "unverifiable": unverifiable,
                    "complete": unavailable == unverifiable == 0 if media else None,
                    "verification": "sha256" if media else None,
                },
                "secrets_included": False,
            }
            marker = stage / "manifest.json"
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump(manifest, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            sync_directory(stage)
            target, suffix = output / f"pixchive-backup-{stamp}", 1
            while target.exists() or target.is_symlink():
                suffix += 1
                target = output / f"pixchive-backup-{stamp}-{suffix}"
            os.rename(stage, target)
            sync_directory(output)
            return {"path": str(target), **manifest}
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def default_mirror(snapshot_dir: Path) -> Path:
    return snapshot_dir.parent / MIRROR


def verify(snapshot_dir: Path, media_root: Path | None, *, full_hash: bool) -> dict:
    snapshot = snapshot_dir / "pixchive.db"
    summary = check_snapshot(snapshot)
    root = media_root or default_mirror(snapshot_dir)
    missing, mismatched, unverifiable = [], [], []
    for relative, size, sha in referenced(snapshot):
        checked = inspect_file(referenced_path(root, relative), size, sha, full_hash=full_hash)
        if checked.state != "ready":
            {"missing": missing, "mismatched": mismatched, "unverifiable": unverifiable}[
                checked.state
            ].append(relative)
    return {
        **summary,
        "media_root": str(root),
        "verification": "sha256" if full_hash else "size",
        "missing": missing,
        "mismatched": mismatched,
        "unverifiable": unverifiable,
    }


def restore(settings: Settings, snapshot_dir: Path, media_root: Path | None, *, full_hash: bool):
    """Restore into an empty data directory only; never merges into a live archive."""
    try:
        with data_access(settings.data_dir, exclusive=True):
            return _restore_locked(settings, snapshot_dir, media_root, full_hash=full_hash)
    except DataStateError as exc:
        raise BackupError(exc.code, exc.message) from None


def _restore_locked(
    settings: Settings, snapshot_dir: Path, media_root: Path | None, *, full_hash: bool
):
    """The exclusive access lock outlives the marker, copied files, and final DB publication."""
    from app.core.db import db, initialize
    from app.migrations.runner import migrate

    snapshot = snapshot_dir / "pixchive.db"
    known = {
        int(path.stem.split("_", 1)[0])
        for path in (Path(__file__).parent.parent / "migrations").glob("[0-9]*.sql")
    }
    summary = check_snapshot(snapshot, known)
    for _ in referenced(snapshot):
        pass
    data = settings.data_dir
    for name in (
        "pixchive.db",
        "pixchive.db-wal",
        "pixchive.db-shm",
        "pixchive.db-journal",
        "media",
        "avatars",
    ):
        entry = data / name
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if name.startswith("pixchive.db") or not stat.S_ISDIR(info.st_mode) or any(entry.iterdir()):
            raise BackupError("target_not_empty", f"복원 대상이 비어 있지 않습니다: {data}")
    marker = data / RESTORE_MARKER
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(b"pixchive-restore-v1\n")
        file.flush()
        os.fsync(file.fileno())
    sync_directory(data)
    root = media_root or default_mirror(snapshot_dir)
    restored_snapshot = data / RESTORE_DATABASE
    copy_file(snapshot, restored_snapshot, expected_sha=hash_file(snapshot))
    summary = check_snapshot(restored_snapshot, known)
    restored, missing, unverified_avatars = 0, [], []
    for relative, size, sha in referenced(restored_snapshot):
        source = referenced_path(root, relative)
        checked = inspect_file(source, size, sha, full_hash=full_hash)
        if checked.state != "ready":
            missing.append(relative)
            continue
        try:
            copy_file(
                source,
                referenced_path(data, relative),
                expected_size=checked.size,
                expected_sha=sha,
            )
        except BackupError as exc:
            if exc.code not in ("file_changed", "source_missing"):
                raise
            missing.append(relative)
            continue
        restored += 1
        if sha is None:
            unverified_avatars.append(relative)
    initialize(data, restoring=True)
    try:
        migrate()
        from app.core.time import now
        from app.models.entities import (
            Artist,
            Asset,
            IntegrityIssue,
            Job,
            Lease,
            Setting,
            StorageReservation,
        )
        from app.services import storage

        with db.atomic("IMMEDIATE"):
            lost = sorted(set(missing))
            for start in range(0, len(lost), 500):
                batch = lost[start : start + 500]
                for asset in Asset.select().where(Asset.path.in_(batch)):
                    asset.state = "missing"
                    asset.save()
                    storage.refresh_archive(asset.artwork_id)
                Artist.update(avatar_path=None, avatar_fetched_url=None).where(
                    Artist.avatar_path.in_(batch)
                ).execute()
            # Runtime ownership never survives a restore; queued work is re-dispatched.
            # A saved verification cursor proves nothing about files in a restored volume.
            unfinished_checks = Job.select(Job.id).where(
                (Job.kind == "integrity")
                & Job.state.not_in(("succeeded", "partial", "failed", "cancelled"))
            )
            IntegrityIssue.delete().where(IntegrityIssue.job.in_(unfinished_checks)).execute()
            Job.update(summary=None, processed=0, failed=0, total=None, phase="verifying").where(
                Job.id.in_(unfinished_checks)
            ).execute()
            Lease.delete().execute()
            StorageReservation.delete().execute()
            Job.update(storage_budget=None).execute()
            Job.update(lease_until=0).where(
                Job.state.in_(("running", "pausing", "cancelling"))
            ).execute()
            Setting.delete().where(
                Setting.key.in_(("worker_heartbeat", "pixiv_retry_at"))
            ).execute()
            Setting.insert(key="restored_at", value=str(now())).on_conflict(
                conflict_target=[Setting.key], update={Setting.value: str(now())}
            ).execute()
        # The final database must be independent of temporary WAL/SHM files before rename.
        if db.execute_sql("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
            raise BackupError("restore_checkpoint", "복원 DB의 쓰기 내용을 확정하지 못했습니다.")
        if db.execute_sql("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise BackupError("restore_checkpoint", "복원 DB의 임시 기록을 정리하지 못했습니다.")
    finally:
        db.close()
    completed = check_snapshot(restored_snapshot, known)
    with read_file(restored_snapshot) as file:
        os.fsync(file.fileno())
    os.replace(restored_snapshot, data / "pixchive.db")
    sync_directory(data)
    marker.unlink()
    sync_directory(data)
    return {
        **summary,
        "restored_files": restored,
        "missing": missing,
        "unverified_avatars": unverified_avatars,
        "preflight_hash": full_hash,
        "restored_schema_version": completed["schema_version"],
        "media_root": str(root),
    }
