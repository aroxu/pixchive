"""Local filesystem primitives; callers supply their own cancellation guard."""

import errno
import fcntl
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked_file(path: Path, guard=None):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "invalid_lock_file")
        while True:
            if guard:
                guard()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.05)
        if guard:
            guard()
        yield fd
    finally:
        os.close(fd)


def sync_directory(path: Path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
