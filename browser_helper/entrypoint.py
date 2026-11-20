"""Nonroot supervisor for a dedicated tmpfs desktop and the private control API."""

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def tmpfs_mount(path: Path) -> bool:
    target = str(path.resolve())
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    matches = []
    for line in mounts:
        fields = line.split()
        separator = fields.index("-")
        mount = fields[4].replace("\\040", " ")
        if target == mount or target.startswith(mount.rstrip("/") + "/"):
            matches.append((len(mount), fields[separator + 1]))
    return bool(matches) and max(matches)[1] == "tmpfs"


def main() -> int:
    children = []
    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    os.umask(0o077)
    try:
        if os.getuid() == 0 or not tmpfs_mount(Path("/run/browser")):
            raise RuntimeError("Nonroot tmpfs browser required")
        for name in ("home", "tmp", "cache", "config", "xdg"):
            (Path("/run/browser") / name).mkdir(mode=0o700, exist_ok=True)
        for name in ("DEBUG", "PWDEBUG", "PLAYWRIGHT_DEBUG"):
            os.environ.pop(name, None)

        def spawn(arguments):
            child = subprocess.Popen(
                arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            children.append(child)
            return child

        display = spawn(["Xvfb", ":99", "-screen", "0", "1280x900x24", "-nolisten", "tcp", "-ac"])
        deadline = time.monotonic() + 15
        while not stopping:
            probe = subprocess.run(
                ["xdpyinfo", "-display", ":99"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            if probe.returncode == 0:
                break
            if display.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Display unavailable")
            time.sleep(0.1)
        if stopping:
            return 0
        spawn(
            [
                "x11vnc",
                "-display",
                ":99",
                "-rfbport",
                "5900",
                "-listen",
                "127.0.0.1",
                "-localhost",
                "-forever",
                "-shared",
                "-nopw",
                "-noxdamage",
                "-noclipboard",
                "-quiet",
            ]
        )
        # Single worker is mandatory: one profile/context, lock and view counter.
        spawn(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "pixchive_browser.api:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8765",
                "--workers",
                "1",
                "--no-access-log",
                "--log-level",
                "critical",
                "--no-proxy-headers",
                "--ws-max-size",
                "1048576",
                "--ws-max-queue",
                "8",
            ]
        )
        while not stopping:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("Browser component stopped")
            time.sleep(0.2)
        return 0
    except Exception:
        # Do not print exception values, child output, environment or secret paths.
        print(
            "Browser helper unavailable; restart after checking its configuration.", file=sys.stderr
        )
        return 1
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    child.kill()
                    with contextlib.suppress(Exception):
                        child.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
