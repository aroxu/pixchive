import unicodedata
from pathlib import Path

from peewee import SqliteDatabase

from app.core.data_state import RESTORE_DATABASE, RESTORE_MARKER, DataStateError, require_ready

db = SqliteDatabase(
    None,
    pragmas={"journal_mode": "wal", "foreign_keys": 1},
    timeout=10,
)


def normalize(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold()


db.register_function(normalize, "normalize", 1, deterministic=True)


def initialize(data_dir: Path, *, restoring: bool = False):
    if restoring:
        if not (data_dir / RESTORE_MARKER).is_file():
            raise DataStateError("restore_not_started", "복원 시작 표식을 확인할 수 없습니다.")
    else:
        require_ready(data_dir)
    if not db.is_closed():
        db.close()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db.init(str(data_dir / (RESTORE_DATABASE if restoring else "pixchive.db")))
