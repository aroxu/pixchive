import asyncio
import contextlib
import json
import os
import shutil
from pathlib import Path

from .config import Settings

PREFERENCES = {
    "credentials_enable_service": False,
    "profile": {"password_manager_enabled": False},
    "autofill": {"profile_enabled": False, "credit_card_enabled": False},
}


def prepare_profile(profile: Path) -> None:
    default = profile / "Default"
    default.mkdir(parents=True, exist_ok=True, mode=0o700)
    preferences_path = default / "Preferences"
    preferences = {}
    if preferences_path.exists():
        preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
    for key, value in PREFERENCES.items():
        if isinstance(value, dict):
            preferences.setdefault(key, {}).update(value)
        else:
            preferences[key] = value
    preferences_path.write_text(json.dumps(preferences), encoding="utf-8")
    preferences_path.chmod(0o600)


class BrowserSession:
    def __init__(self, context, playwright, lock, profile: Path):
        self.context = context
        self.playwright = playwright
        self.lock = lock
        self.profile = profile
        self.closed = False
        context.on("close", self._closed)

    def _closed(self, *_args) -> None:
        self.closed = True

    async def close(self, *, reset: bool = False) -> None:
        try:
            try:
                async with asyncio.timeout(15):
                    await self.context.close()
            finally:
                async with asyncio.timeout(15):
                    await self.playwright.stop()
            if reset:
                # Fixed dedicated profile only; lifetime lock is held until removal finishes.
                await asyncio.to_thread(shutil.rmtree, self.profile, True)
                if self.profile.exists():
                    raise OSError("Profile reset failed")
        finally:
            self.lock.close()
            self.closed = True


class BrowserFactory:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _lock(self):
        import fcntl

        # The container launcher also verifies that this entire directory is tmpfs.
        if self.settings.profile != Path("/run/browser/profile") or os.getuid() == 0:
            raise RuntimeError("Dedicated nonroot browser required")
        lock = (self.settings.profile.parent / ".profile.lock").open("a+b")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            lock.close()
            raise
        return lock

    async def start(self) -> BrowserSession:
        from playwright.async_api import async_playwright

        lock = self._lock()
        playwright = None
        try:
            await asyncio.to_thread(prepare_profile, self.settings.profile)
            playwright = await async_playwright().start()
            context = await playwright.chromium.launch_persistent_context(
                str(self.settings.profile),
                headless=False,
                chromium_sandbox=True,
                accept_downloads=False,
                viewport={"width": 1280, "height": 900},
                args=["--disable-save-password-bubble", "--window-size=1280,900"],
                # No credential/key environment is inherited by Chromium.
                env={
                    name: value
                    for name, value in os.environ.items()
                    if name
                    in {
                        "PATH",
                        "HOME",
                        "DISPLAY",
                        "LANG",
                        "LC_ALL",
                        "TMPDIR",
                        "XDG_CACHE_HOME",
                        "XDG_CONFIG_HOME",
                        "XDG_RUNTIME_DIR",
                    }
                },
                timeout=self.settings.navigation_timeout_ms,
            )
            context.set_default_timeout(5_000)
            context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
            return BrowserSession(context, playwright, lock, self.settings.profile)
        except BaseException:
            if playwright is not None:
                with contextlib.suppress(Exception):
                    await playwright.stop()
            lock.close()
            raise

    async def reset(self) -> None:
        lock = self._lock()
        try:
            await asyncio.to_thread(shutil.rmtree, self.settings.profile, True)
            if self.settings.profile.exists():
                raise OSError("Profile reset failed")
        finally:
            lock.close()
