"""Process lifetime access and durable incomplete-restore protection for one data root."""

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path

RESTORE_MARKER = ".restore-incomplete"
RESTORE_DATABASE = "pixchive.db.restoring"


class DataStateError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def require_ready(data_dir: Path):
    for name in (
        RESTORE_MARKER,
        RESTORE_DATABASE,
        RESTORE_DATABASE + "-wal",
        RESTORE_DATABASE + "-shm",
        RESTORE_DATABASE + "-journal",
    ):
        try:
            (data_dir / name).lstat()
        except FileNotFoundError:
            continue
        raise DataStateError(
            "restore_incomplete",
            "완료되지 않은 복원 자료가 있습니다. "
            "새 빈 디렉터리에 다시 복원한 뒤 서비스를 시작해 주세요.",
        )


@contextmanager
def data_access(data_dir: Path, *, exclusive: bool = False):
    """Shared for applications/CLI writes; exclusive for an empty-directory restore."""
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(
        data_dir / ".data-access.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DataStateError("invalid_data_lock", "저장소의 사용 잠금 파일을 확인해 주세요.")
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            message = (
                "저장소를 사용 중입니다. "
                "복원 전에 web·worker와 이 저장소의 명령을 모두 종료해 주세요."
                if exclusive
                else "복원 작업이 저장소를 사용 중입니다. 복원이 끝난 뒤 다시 시작해 주세요."
            )
            raise DataStateError(
                "data_in_use",
                message,
            ) from None
        require_ready(data_dir)
        yield
    finally:
        os.close(fd)
