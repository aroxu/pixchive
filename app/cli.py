import argparse
import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.data_state import DataStateError, data_access
from app.core.db import db, initialize
from app.migrations.runner import migrate


def write_secret(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as file:
        file.write(content)


def report(value: dict):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m app.cli")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create keys and the database without overwriting")
    commands.add_parser("migrate", help="apply pending schema migrations")
    commands.add_parser("seed-fixtures", help="import synthetic development samples")
    commands.add_parser("openapi", help="print the OpenAPI document")
    backup = commands.add_parser(
        "backup", help="consistent DB snapshot while running, optional media mirror"
    )
    backup.add_argument("--output", required=True, type=Path, help="backup root directory")
    backup.add_argument(
        "--media", action="store_true", help="also copy referenced files into OUTPUT/media-mirror"
    )
    restore = commands.add_parser("restore", help="restore a snapshot into an empty data dir")
    restore.add_argument("--from", dest="source", required=True, type=Path)
    restore.add_argument("--media-root", type=Path, help="defaults to SOURCE/../media-mirror")
    restore.add_argument(
        "--verify-hash",
        action="store_true",
        help="precheck mirror hashes and reject unverified avatars",
    )
    verify = commands.add_parser("verify-backup", help="check a snapshot and its media mirror")
    verify.add_argument("source", type=Path)
    verify.add_argument("--media-root", type=Path)
    verify.add_argument("--hash", action="store_true", help="check SHA-256 per file")
    return root


def main():
    args = parser().parse_args()
    settings = Settings.from_env()
    if args.command == "openapi":
        from app.main import create_app

        print(json.dumps(create_app(settings).openapi(), ensure_ascii=False, indent=2))
        return
    if args.command in ("restore", "verify-backup"):
        from app.services import backup

        try:
            if args.command == "restore":
                result = backup.restore(
                    settings, args.source, args.media_root, full_hash=args.verify_hash
                )
                result["missing_count"] = len(result["missing"])
                result["missing"] = result["missing"][:20]
                result["unverified_avatar_count"] = len(result["unverified_avatars"])
                result["unverified_avatars"] = result["unverified_avatars"][:20]
                report(result)
                if result["missing_count"]:
                    print(
                        "Some files were missing and are marked for repair. "
                        "Run the archive check after starting the services.",
                        file=sys.stderr,
                    )
                if result["unverified_avatar_count"]:
                    print(
                        "일부 과거 프로필 이미지에는 비교할 해시 기록이 없습니다. "
                        "복원 결과의 해당 경로를 확인해 주세요.",
                        file=sys.stderr,
                    )
            else:
                result = backup.verify(args.source, args.media_root, full_hash=args.hash)
                problems = 0
                for key in ("missing", "mismatched", "unverifiable"):
                    result[key + "_count"] = len(result[key])
                    problems += result[key + "_count"]
                    result[key] = result[key][:20]
                report(result)
                if problems:
                    sys.exit(1)
        except backup.BackupError as exc:
            print(exc.message, file=sys.stderr)
            sys.exit(2)
        except (OSError, sqlite3.DatabaseError):
            print("파일 권한과 남은 공간·스냅샷 상태를 확인해 주세요.", file=sys.stderr)
            sys.exit(2)
        return
    try:
        with data_access(settings.data_dir):
            try:
                run_local_command(args, settings)
            finally:
                if not db.is_closed():
                    db.close()
    except DataStateError as exc:
        print(exc.message, file=sys.stderr)
        sys.exit(2)
    except OSError:
        print("저장소의 파일 권한과 남은 공간을 확인해 주세요.", file=sys.stderr)
        sys.exit(2)


def run_local_command(args, settings):
    initialize(settings.data_dir)
    migrate()
    if args.command == "init":
        write_secret(settings.key_file, Fernet.generate_key())
        write_secret(settings.setup_token_file, secrets.token_urlsafe(32).encode())
        print(f"Initialized. Setup token file: {settings.setup_token_file}")
        print(f"Credential encryption key file: {settings.key_file}")
    elif args.command == "seed-fixtures":
        from app.services.fixtures import seed

        seed(settings)
        print("Synthetic fixtures imported. No Pixiv connection was used.")
    elif args.command == "backup":
        from app.core.time import now
        from app.services import backup
        from app.services.library import set_setting

        try:
            result = backup.create(settings, args.output, media=args.media)
        except backup.BackupError as exc:
            print(exc.message, file=sys.stderr)
            sys.exit(2)
        except (OSError, sqlite3.DatabaseError):
            print("파일 권한과 남은 공간·스냅샷 상태를 확인해 주세요.", file=sys.stderr)
            sys.exit(2)
        incomplete = args.media and result["media"]["complete"] is not True
        if not incomplete:
            set_setting("last_backup_at", str(now()))
        report(result)
        print(
            f"The credential key is not included. Store it separately: {settings.key_file}",
            file=sys.stderr,
        )
        if incomplete:
            print(
                "DB 스냅샷은 보존했지만 파일 백업을 완전히 검증하지 못했습니다. "
                "누락·손상·해시 기준 없음 기록을 확인해 주세요.",
                file=sys.stderr,
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
