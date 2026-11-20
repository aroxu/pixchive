"""Backup-only adapter: official CLI plus private, byte-exact recovery files."""

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path("/backup")
SECRETS = Path("/run/pixchive-secrets")
STATE = Path("/backup-browser-state/auth-state.enc")


def read_regular(path, *, optional=False):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if optional:
            return None
        raise
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("Expected a regular recovery file")
        return source.read()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        # NAS creation policies may override the requested creation mode.
        os.fchmod(target.fileno(), 0o600)
        target.write(content)
        target.flush()
        os.fsync(target.fileno())


def main():
    os.umask(0o077)
    key = read_regular(SECRETS / "credential.key")
    if not 32 <= len(key) <= 4096:
        raise ValueError("Invalid original key")
    # Atomic helper replacements are read via a single open descriptor. This is
    # an independent encrypted snapshot, not a cross-service transaction.
    state = read_regular(STATE, optional=True)
    files = {
        "credential.key": key,
        "auth-state.enc": state,
        "setup-token": read_regular(SECRETS / "setup-token", optional=True),
        "storage-id": read_regular(Path("/app/data/.pixchive-storage-id")),
    }
    with tempfile.TemporaryDirectory(prefix=".recovery-", dir=ROOT) as temporary:
        stage = Path(temporary)
        stage.chmod(0o700)
        for name, content in files.items():
            if content is not None:
                write_private(stage / name, content)
        write_private(
            stage / "manifest.json",
            json.dumps(
                {"format": 1, "files": [name for name, value in files.items() if value is not None]},
                indent=2,
            ).encode() + b"\n",
        )
        sync_directory(stage)
        result = subprocess.run(
            [sys.executable, "-m", "app.cli", "backup", "--output", str(ROOT), "--media"],
            check=False,
            stdout=subprocess.PIPE,
            text=True,
        )
        if result.returncode not in (0, 1):
            print("Official backup CLI failed; no recovery bundle was published.", file=sys.stderr)
            return result.returncode
        report = json.loads(result.stdout)
        snapshot = Path(report["path"])
        if (
            snapshot.parent != ROOT
            or not snapshot.name.startswith("pixchive-backup-")
            or snapshot.is_symlink()
            or not snapshot.is_dir()
        ):
            raise ValueError("Unexpected backup destination")
        if read_regular(SECRETS / "credential.key") != key:
            raise ValueError("Original key changed during backup")
        os.rename(stage, snapshot / "recovery")
        if result.returncode == 0:
            write_private(snapshot / ".recovery-complete", b"1\n")
        sync_directory(snapshot)
        sync_directory(ROOT)
        if result.returncode:
            print("Incomplete media backup; recovery files retained; not a complete recovery point.", file=sys.stderr)
        else:
            print(f"Backup and private recovery bundle complete: {snapshot}")
        return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError):
        # Never print key/ciphertext bytes, subprocess output or exception details.
        print("Backup failed; check storage and the original recovery files. No complete recovery point was confirmed.", file=sys.stderr)
        raise SystemExit(2) from None
