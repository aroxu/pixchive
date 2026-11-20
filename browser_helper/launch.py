"""Prepare domain-separated keys in tmpfs, then permanently drop root before launch."""

import base64
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path


def prepare():
    if os.getuid() != 0:
        return
    from entrypoint import tmpfs_mount

    root = Path("/run/browser")
    if not tmpfs_mount(root):
        raise RuntimeError
    source = Path(os.environ.get("PIXCHIVE_KEY_FILE", "/run/pixchive-secrets/credential.key"))
    raw = source.read_bytes()
    if not 32 <= len(raw) <= 4096:
        raise ValueError
    private = root / "keys"
    private.mkdir(mode=0o710, exist_ok=True)
    os.chown(private, 0, 1000)
    private.chmod(0o710)
    target = private / "derived.json"
    payload = {
        "control_token": hmac.new(raw, b"pixchive-browser-control-v1", hashlib.sha256).hexdigest(),
        "storage_key": base64.urlsafe_b64encode(
            hmac.new(raw, b"pixchive-browser-storage-v1", hashlib.sha256).digest()
        ).decode(),
    }
    raw = None
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(payload, output)
    os.chown(target, 1000, 1000)
    target.chmod(0o600)
    os.environ["PIXCHIVE_BROWSER_DERIVED_KEYS_FILE"] = str(target)
    os.environ.pop("PIXCHIVE_KEY_FILE", None)
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)


if __name__ == "__main__":
    try:
        os.umask(0o077)
        prepare()
        os.execv(sys.executable, [sys.executable, "entrypoint.py"])
    except Exception:
        print("Browser helper key preparation unavailable.", file=sys.stderr)
        raise SystemExit(1) from None
