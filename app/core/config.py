import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def normalize_origin(value: str) -> str:
    """Compare exact scheme/host/port origins, never suffixes or URL prefixes."""
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("Expected an http(s) origin")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in parsed.netloc)
        or parsed.netloc.endswith(":")
    ):
        raise ValueError("Expected an http(s) origin without credentials or a path")
    port = parsed.port
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = (
        f":{port}" if port is not None and port != {"http": 80, "https": 443}[parsed.scheme] else ""
    )
    return f"{parsed.scheme}://{host}{suffix}"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    key_file: Path
    setup_token_file: Path
    public_url: str = "http://localhost:8000"
    redis_url: str = "redis://localhost:6379/0"
    frontend_dir: Path = Path("frontend/dist")
    # New downloads wait for storage below this free-space reserve.
    min_free_bytes: int = 1024 * 1024 * 1024
    allowed_origins: tuple[str, ...] = ()

    @property
    def secure_cookie(self) -> bool:
        return self.public_url.startswith("https://")

    def allows_origin(self, origin: str | None) -> bool:
        try:
            candidate = normalize_origin(origin)
        except (TypeError, ValueError):
            return False
        return candidate in {
            normalize_origin(item) for item in (self.public_url, *self.allowed_origins)
        }

    @classmethod
    def from_env(cls):
        origin = normalize_origin(
            os.getenv("PIXCHIVE_PUBLIC_URL", "http://localhost:8000").rstrip("/")
        )
        extras = tuple(
            normalize_origin(item.strip().rstrip("/"))
            for item in os.getenv("PIXCHIVE_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        )
        reserve = os.getenv("PIXCHIVE_MIN_FREE_MB", "1024")
        if not reserve.isdigit():
            raise ValueError("PIXCHIVE_MIN_FREE_MB must be a non-negative integer")
        return cls(
            data_dir=Path(os.getenv("PIXCHIVE_DATA_DIR", "data")),
            key_file=Path(os.getenv("PIXCHIVE_KEY_FILE", "secrets/credential.key")),
            setup_token_file=Path(os.getenv("PIXCHIVE_SETUP_TOKEN_FILE", "secrets/setup-token")),
            public_url=origin,
            redis_url=os.getenv("PIXCHIVE_REDIS_URL", "redis://localhost:6379/0"),
            frontend_dir=Path(os.getenv("PIXCHIVE_FRONTEND_DIR", "frontend/dist")),
            min_free_bytes=int(reserve) * 1024 * 1024,
            allowed_origins=tuple(dict.fromkeys(extras)),
        )
