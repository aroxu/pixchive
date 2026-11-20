"""Private temporary workspaces with kernel ownership and recoverable cleanup."""

import fcntl
import json
import os
import re
import shutil
import stat
import threading
import uuid
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
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
_blob_reuse = ContextVar("temporary_blob_reuse", default=None)


@dataclass(frozen=True)
class Workspace:
    path: Path
    lock_fd: int


@dataclass
class _BlobReuse:
    workspace: Workspace
    guard: Callable[[], None] | None
    lock: threading.Lock
    closed: bool = False


def active_fds() -> tuple[int, ...]:
    """Keep workspace ownership in FFmpeg/ffprobe until the child actually exits."""
    return _active_fds.get()


def root(settings: Settings, *, create=False) -> Path:
    base = settings.cache_dir if settings.cache_dir is not None else settings.data_dir
    path = base / "tmp"
    try:
        # Cache is an explicitly configured mount, never a relative path or a
        # symlink through which recovery could reach an unrelated directory.
        invalid_cache = settings.cache_dir is not None and (
            not base.is_absolute() or base != base.resolve()
        )
        if invalid_cache or path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
            raise DomainError("invalid_path", "임시 저장 경로를 확인해 주세요.")
    except (OSError, RuntimeError):
        raise DomainError("invalid_path", "임시 저장 경로를 확인해 주세요.") from None
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
            # An unlinked open file is kept as .nfs* on NFS, preventing rmdir.
            # No workspace was published, so release ownership before removal.
            if owner_fd is not None:
                os.close(owner_fd)
                owner_fd = None
            remove(creating)
            remove(published)
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
            # The cleaning name already excludes new users. Close the ownership
            # file before unlinking it; NFS otherwise retains a .nfs* placeholder.
            os.close(owner_fd)
            owner_fd = None
            remove(cleaning)
        finally:
            if owner_fd is not None:
                os.close(owner_fd)


@contextmanager
def reuse_blobs(settings: Settings, *, guard=None):
    """Own a blob workspace and, with cache, NAS publication space for one batch.

    Enter/exit in the async caller, outside its disk_operation/to_thread calls.
    All disk operations and children must settle before leaving this scope.
    Nested scopes own separate workspaces and restore the enclosing scope.
    """
    with ExitStack() as owners:
        scopes = {}
        targets = [settings]
        if settings.cache_dir is not None:
            targets.append(replace(settings, cache_dir=None))
        for target in targets:
            working = owners.enter_context(workspace(target, "blob", guard))
            scopes[_blob_key(target)] = _BlobReuse(working, guard, threading.Lock())
        token = _blob_reuse.set(scopes)
        try:
            yield scopes[_blob_key(settings)].workspace
        finally:
            _blob_reuse.reset(token)
            # Copies of this context share the lock and the closed flag. Never
            # let a late borrower use a workspace whose owner FD was released.
            for scope in reversed(list(scopes.values())):
                with scope.lock:
                    scope.closed = True


def _blob_key(settings: Settings):
    return settings, settings.data_dir.resolve(), root(settings).resolve()


@contextmanager
def blob_workspace(settings: Settings, guard=None):
    """Borrow a batch's blob workspace exclusively, or use the ordinary lifecycle."""
    scopes = _blob_reuse.get()
    scope = scopes.get(_blob_key(settings)) if scopes is not None else None
    if scope is None:
        with workspace(settings, "blob", guard) as working:
            yield working
        return

    def check():
        if scope.guard:
            scope.guard()
        if guard and guard is not scope.guard:
            guard()

    while True:
        check()
        if scope.lock.acquire(timeout=0.05):
            break
    try:
        if scope.closed:
            raise RuntimeError("Blob reuse scope has already ended")
        check()
        yield scope.workspace
    finally:
        scope.lock.release()


def publication_workspace(settings: Settings, guard=None):
    """Borrow NAS blob space; enter before acquiring the media commit lock."""
    return blob_workspace(replace(settings, cache_dir=None), guard)


def reap(settings: Settings, limit: int = 50) -> dict:
    """Reclaim cache and older NAS workspaces, preserving live and unknown files."""
    result = {"removed": 0, "failed": 0, "active": 0, "unknown": 0}
    targets = [settings]
    if settings.cache_dir is not None:
        targets.append(replace(settings, cache_dir=None))
    seen = set()
    for target in targets:
        directory = root(target)
        canonical = directory.resolve()
        if canonical in seen:
            continue
        seen.add(canonical)
        _reap_directory(target, directory, limit, result)
        if result["removed"] >= limit:
            break
    return result


def _reap_directory(settings: Settings, directory: Path, limit: int, result: dict):
    try:
        directory.stat()
    except FileNotFoundError:
        return
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
                    if owner_fd is not None:
                        os.close(owner_fd)
                        owner_fd = None
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
