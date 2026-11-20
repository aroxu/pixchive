"""Run only helper checks, with temporary keys/state confined to this directory."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    temporary = root / ".tmp"
    temporary.mkdir(exist_ok=True)
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "TMPDIR": str(temporary),
    }
    checks = [
        ["ruff", "check", "."],
        ["ruff", "format", "--check", "."],
        ["pip", "check"],
    ]
    with tempfile.TemporaryDirectory(prefix="tests-", dir=temporary) as scratch:
        checks.append(["pytest", "-q", "--basetemp", str(Path(scratch) / "pytest")])
        for arguments in checks:
            result = subprocess.run([sys.executable, "-m", *arguments], cwd=root, env=environment)
            if result.returncode:
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
