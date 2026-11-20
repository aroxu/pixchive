import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    key_file: Path = field(
        default_factory=lambda: Path(
            os.environ.get("PIXCHIVE_KEY_FILE", "/run/pixchive-secrets/credential.key")
        )
    )
    state_file: Path = Path("/state/auth-state.enc")
    managed_navigation: bool = field(
        default_factory=lambda: os.environ.get("PIXCHIVE_BROWSER_MANAGED_NAVIGATION") == "1"
    )
    derived_keys_file: Path | None = field(
        default_factory=lambda: (
            Path(os.environ["PIXCHIVE_BROWSER_DERIVED_KEYS_FILE"])
            if os.environ.get("PIXCHIVE_BROWSER_DERIVED_KEYS_FILE")
            else None
        )
    )
    profile: Path = Path("/run/browser/profile")
    poll_seconds: float = 5.0
    reauth_seconds: float = 600.0
    save_seconds: float = 30.0
    retry_seconds: float = 30.0
    navigation_timeout_ms: int = 30_000


HOME = "https://www.pixiv.net/"
LOGIN = "https://accounts.pixiv.net/login"
CONTROL_LABEL = b"pixchive-browser-control-v1"
STORAGE_LABEL = b"pixchive-browser-storage-v1"
