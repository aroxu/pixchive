import base64
import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from .config import CONTROL_LABEL, STORAGE_LABEL

MAX_STATE_BYTES = 16 * 1024 * 1024
COOKIE_FIELDS = {
    "name",
    "value",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
    "partitionKey",
}


def pixiv_domain(domain: str) -> bool:
    domain = domain.removeprefix(".").lower()
    return domain == "pixiv.net" or domain.endswith(".pixiv.net")


def pixiv_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and pixiv_domain(parsed.hostname)
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        )
    except (TypeError, ValueError):
        return False


def filter_state(state: dict) -> dict:
    """Keep only Pixiv cookies, localStorage and IndexedDB, including on restore."""
    if not isinstance(state, dict):
        raise ValueError("Invalid browser state")
    cookies = []
    for cookie in state.get("cookies", []):
        if not isinstance(cookie, dict) or not isinstance(cookie.get("domain"), str):
            continue
        if not pixiv_domain(cookie["domain"]):
            continue
        if "partitionKey" in cookie and not pixiv_origin(cookie["partitionKey"]):
            continue
        cookies.append({key: value for key, value in cookie.items() if key in COOKIE_FIELDS})
    origins = []
    for origin in state.get("origins", []):
        if not isinstance(origin, dict) or not pixiv_origin(origin.get("origin", "")):
            continue
        origins.append(
            {
                key: value
                for key, value in origin.items()
                if key in {"origin", "localStorage", "indexedDB"}
            }
        )
    return {"cookies": cookies, "origins": origins}


def canonical_state(state: dict) -> bytes:
    payload = json.dumps(
        filter_state(state), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise ValueError("Browser state too large")
    return encoded


def sync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Secrets:
    def __init__(self, key_file: Path, derived_keys_file: Path | None = None):
        self.key_file = key_file
        self.derived_keys_file = derived_keys_file
        self._token: str | None = None
        self._cipher: Fernet | None = None

    def load(self) -> None:
        if self._cipher is not None:
            return
        if self.derived_keys_file is not None:
            descriptor = os.open(self.derived_keys_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source:
                payload = source.read(4097)
            if len(payload) > 4096:
                raise ValueError("Invalid derived keys")
            keys = json.loads(payload)
            token, storage_key = keys["control_token"], keys["storage_key"]
            if not isinstance(token, str) or len(token) != 64:
                raise ValueError("Invalid derived keys")
            if any(char not in "0123456789abcdef" for char in token):
                raise ValueError("Invalid derived keys")
            self._cipher = Fernet(storage_key.encode())
            self._token = token
            return
        # HMAC deliberately uses the EXACT file bytes, including any newline.
        raw = self.key_file.read_bytes()
        if len(raw) < 32 or len(raw) > 4096:
            raise ValueError("Invalid key file")
        token = hmac.new(raw, CONTROL_LABEL, hashlib.sha256).hexdigest()
        encryption_key = hmac.new(raw, STORAGE_LABEL, hashlib.sha256).digest()
        self._cipher = Fernet(base64.urlsafe_b64encode(encryption_key))
        self._token = token

    def authorized(self, headers) -> bool:
        self.load()
        values = headers.getlist("authorization")
        if len(values) != 1:
            return False
        parts = values[0].split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        supplied = parts[1]
        if len(supplied) != 64 or any(char not in "0123456789abcdef" for char in supplied):
            return False
        return hmac.compare_digest(supplied, self._token)

    @property
    def cipher(self) -> Fernet:
        self.load()
        assert self._cipher is not None
        return self._cipher


class StateStore:
    def __init__(self, path: Path, secrets: Secrets):
        self.path = path
        self.secrets = secrets
        self.fingerprint: bytes | None = None

    def load(self) -> dict | None:
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as source:
            encrypted = source.read(MAX_STATE_BYTES * 2 + 1)
        if len(encrypted) > MAX_STATE_BYTES * 2:
            raise ValueError("Browser state too large")
        plaintext = self.secrets.cipher.decrypt(encrypted)
        if len(plaintext) > MAX_STATE_BYTES:
            raise ValueError("Browser state too large")
        state = filter_state(json.loads(plaintext))
        self.fingerprint = hashlib.sha256(canonical_state(state)).digest()
        return state

    def save(self, state: dict) -> None:
        plaintext = canonical_state(state)
        fingerprint = hashlib.sha256(plaintext).digest()
        if self.path.exists() and self.fingerprint == fingerprint:
            return
        encrypted = self.secrets.cipher.encrypt(plaintext)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".auth-state-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                if os.name == "posix":
                    # Some filesystems override mkstemp's requested creation mode.
                    os.fchmod(destination.fileno(), 0o600)
                destination.write(encrypted)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, self.path)
            sync_directory(self.path.parent)
            self.fingerprint = fingerprint
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)
        if self.path.parent.exists():
            sync_directory(self.path.parent)
        self.fingerprint = None
