"""Private temporary workspaces with kernel ownership and recoverable cleanup."""

import fcntl
import json
import os
import re
import shutil
import stat
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.errors import DomainError
from app.core.files import locked_file, sync_directory

FORMAT = 1
PURPOSES = ("blob", "avatar", "ugoira")
NAME = re.compile(r"(work|\.creating|\.cleaning)-(blob|avatar|ugoira)-([a-f0-9]{32})")
MANIFEST = ".workspace.json"
OWNER_LOCK = ".owner.lock"
_active_fds = ContextVar("temporary_workspace_fds", default=())


@dataclass(frozen=True)
class Workspace:
    path: Path
    lock_fd: int


def active_fds() -> tuple[int, ...]:
    """Keep workspace ownership in FFmpeg/ffprobe until the child actually exits."""
    return _active_fds.get()


def root(settings: Settings, *, create=False) -> Path:
    path = settings.data_dir / "tmp"
    if path.is_symlink() or not path.resolve().is_relative_to(settings.data_dir.resolve()):
        raise DomainError("invalid_path", "임시 저장 경로를 확인해 주세요.")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def remove(path: Path) -> bool:
    try:
        shutil.rmtree(path)
        return True
    except FileNotFoundError:
        return False


def valid_manifest(path: Path, purpose: str, nonce: str) -> bool:
    expected = {"format": FORMAT, "purpose": purpose, "nonce": nonce}
    try:
        fd = os.open(path / MANIFEST, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
                return False
            content = source.read(1025)
        parsed = json.loads(content)
        return (
            len(content) <= 1024
            and isinstance(parsed, dict)
            and type(parsed.get("format")) is int
            and parsed == expected
        )
    except (OSError, ValueError):
        return False


@contextmanager
def workspace(settings: Settings, purpose: str, guard=None):
    if purpose not in PURPOSES:
        raise ValueError("Invalid temporary workspace purpose")
    nonce = uuid.uuid4().hex
    owner_fd = None
    with locked_file(settings.data_dir / ".media.lock", guard):
        directory = root(settings, create=True)
        creating = directory / f".creating-{purpose}-{nonce}"
        published = directory / f"work-{purpose}-{nonce}"
        cleaning = directory / f".cleaning-{purpose}-{nonce}"
        creating.mkdir(mode=0o700)
        try:
            owner_fd = os.open(
                creating / OWNER_LOCK,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fsync(owner_fd)
            fd = os.open(
                creating / MANIFEST,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "w") as target:
                json.dump({"format": FORMAT, "purpose": purpose, "nonce": nonce}, target)
                target.flush()
                os.fsync(target.fileno())
            sync_directory(creating)
            os.rename(creating, published)
            sync_directory(directory)
        except BaseException:
            try:
                remove(creating)
                remove(published)
            finally:
                if owner_fd is not None:
                    os.close(owner_fd)
            raise
    token = _active_fds.set((*active_fds(), owner_fd))
    try:
        if guard:
            guard()
        yield Workspace(published, owner_fd)
    finally:
        _active_fds.reset(token)
        try:
            # Publishing "cleaning" means the body and its child/thread I/O have ended.
            # Rename under the same gate as creation/recovery, then remove outside it.
            with locked_file(settings.data_dir / ".media.lock"):
                if published.exists():
                    os.rename(published, cleaning)
                    sync_directory(directory)
            remove(cleaning)
        finally:
            os.close(owner_fd)


def reap(settings: Settings, limit: int = 50) -> dict:
    """Reclaim our abandoned workspaces; preserve live work and unknown legacy files."""
    directory = root(settings)
    result = {"removed": 0, "failed": 0, "active": 0, "unknown": 0}
    try:
        directory.stat()
    except FileNotFoundError:
        return result
    with os.scandir(directory) as entries:
        for entry in entries:
            owner_fd = None
            try:
                match = NAME.fullmatch(entry.name)
                if not match or not entry.is_dir(follow_symlinks=False):
                    result["unknown"] += 1
                    continue
                with locked_file(settings.data_dir / ".media.lock"):
                    path = directory / entry.name
                    if not stat.S_ISDIR(path.lstat().st_mode):
                        result["unknown"] += 1
                        continue
                    state, purpose, nonce = match.groups()
                    cleaning = directory / f".cleaning-{purpose}-{nonce}"
                    if state == "work":
                        owner_fd = os.open(
                            path / OWNER_LOCK, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
                        )
                        if not stat.S_ISREG(os.fstat(owner_fd).st_mode):
                            result["unknown"] += 1
                            continue
                        try:
                            fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            result["active"] += 1
                            continue
                        if not valid_manifest(path, purpose, nonce):
                            result["unknown"] += 1
                            continue
                    # A creating directory can only survive this gate after its creator
                    # has died. A cleaning directory has no remaining writer by contract.
                    if path != cleaning:
                        os.rename(path, cleaning)
                        sync_directory(directory)
                result["removed"] += remove(cleaning)
                if result["removed"] >= limit:
                    break
            except FileNotFoundError:
                continue
            except OSError:
                result["failed"] += 1
            finally:
                if owner_fd is not None:
                    os.close(owner_fd)
    return result
