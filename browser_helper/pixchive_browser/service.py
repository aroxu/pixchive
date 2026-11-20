import asyncio
import contextlib
import hashlib
import json
import math
import random
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from .browser import BrowserFactory
from .config import HOME, LOGIN, Settings
from .identity import IDENTITY_SCRIPT, classify, session_cookie
from .storage import Secrets, StateStore


class Unavailable(Exception):
    pass


class ProfileOccupied(Exception):
    pass


class BrowserService:
    def __init__(self, settings: Settings, *, factory=None):
        self.settings = settings
        self.secrets = Secrets(settings.key_file, settings.derived_keys_file)
        self.store = StateStore(settings.state_file, self.secrets)
        self.factory = factory or BrowserFactory(settings)
        self.lock = asyncio.Lock()
        self.session = None
        self.state = "starting"
        self.last_checked: int | None = None
        self.retry_at = 0
        self._cookie: str | None = None
        self._cookies_fingerprint: bytes | None = None
        self._last_visit = 0.0
        self._last_save = 0.0
        self._last_boot = 0.0
        self._navigation_failed = False
        self._seed_eligible = False
        self._disconnected = False
        self._reset_pending = False
        self._login_pending = False
        self._completion_ready = False
        self._login_generation = 0
        self._viewers = 0
        self._stop = asyncio.Event()
        self._supervisor = None
        self._disk_tasks: set[asyncio.Task] = set()

    def status(self) -> dict:
        return {
            "state": self.state,
            "last_checked": self.last_checked,
            "cookie_available": self.state == "connected" and self._cookie is not None,
            "viewing": self._viewers > 0,
            "retry_at": self.retry_at,
            "login_pending": self._login_pending,
            "login_generation": self._login_generation,
        }

    def credential(self) -> str | None:
        return self._cookie if self.state == "connected" else None

    @property
    def viewers(self) -> int:
        return self._viewers

    @contextlib.asynccontextmanager
    async def view(self):
        # Increment before any await: boot, polling and timer navigation observe this immediately.
        self._viewers += 1
        try:
            yield
        finally:
            self._viewers -= 1

    async def _disk(self, function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self._disk_tasks.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._disk_tasks.discard(task)

    def _error(self) -> None:
        self.state = "error"
        self._cookie = None

    async def start(self) -> None:
        self._supervisor = asyncio.create_task(self._run(), name="browser-supervisor")

    async def close(self) -> None:
        self._stop.set()
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
        if self._disk_tasks:
            await asyncio.gather(*self._disk_tasks, return_exceptions=True)
            self._disk_tasks.clear()
        async with self.lock:
            if self.session is not None:
                if not self._disconnected and not self._seed_eligible and not self._login_pending:
                    with contextlib.suppress(Exception):
                        await self._save_locked()
                with contextlib.suppress(Exception):
                    await self.session.close(reset=self._reset_pending)
                self.session = None

    async def _ensure_locked(self, *, visit: bool = True) -> None:
        if self._reset_pending:
            raise Unavailable("Browser reset required")
        if self.session is not None and not self.session.closed:
            return
        if self.session is not None:
            with contextlib.suppress(Exception):
                await self.session.close()
            self.session = None
        self._last_boot = time.monotonic()
        self.state = "starting"
        try:
            await self._disk(self.secrets.load)
            restored = await self._disk(self.store.load)
            self.session = await self.factory.start()
            if restored is not None:
                # Dict is kept in memory; there is never a plaintext storage-state file.
                await self.session.context.set_storage_state(restored)
            self._disconnected = False
            if self._login_pending:
                # Opening a manual login must not snapshot or inspect the live profile.
                self._seed_eligible = False
                self.state, self._cookie = "needs_login", None
                return
            current = await self.session.context.storage_state(indexed_db=True)
            pages = self.session.context.pages
            blank = all(page.url in ("about:blank", "") for page in pages)
            self._seed_eligible = (
                restored is None
                and not current.get("cookies")
                and not current.get("origins")
                and blank
            )
            if self._seed_eligible:
                self.state = "needs_login"
                self._cookie = None
            elif visit and self._viewers == 0 and not self.settings.managed_navigation:
                await self._reauth_locked()
            else:
                await self._poll_locked()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._error()
            if self.session is not None:
                with contextlib.suppress(Exception):
                    await self.session.close()
                self.session = None
            raise Unavailable("Browser unavailable") from None

    async def _page_locked(self):
        pages = [page for page in self.session.context.pages if not page.is_closed()]
        for page in reversed(pages):
            if urlsplit(page.url).hostname in {"www.pixiv.net", "accounts.pixiv.net"}:
                return page
        return pages[-1] if pages else await self.session.context.new_page()

    async def _save_locked(self, *, completing: bool = False) -> None:
        if self._disconnected or (self._login_pending and not completing):
            return
        state = await self.session.context.storage_state(indexed_db=True)
        if self._disconnected or (self._login_pending and not completing):
            return
        await self._disk(self.store.save, state)
        self._last_save = time.monotonic()

    async def _poll_locked(
        self, *, http_status: int | None = None, completing: bool = False
    ) -> dict | None:
        if self._login_pending and not completing:
            return
        if self.session.closed:
            raise Unavailable("Browser unavailable")
        cookies = await self.session.context.cookies([HOME])
        if self._login_pending and not completing:
            return
        cookie = session_cookie(cookies)
        pages = [page for page in self.session.context.pages if not page.is_closed()]
        page = await self._page_locked() if pages else None
        if self._login_pending and not completing:
            return
        if cookies or (page is not None and page.url not in ("", "about:blank")):
            self._seed_eligible = False
        snapshot = None
        if page is None or page.url in ("", "about:blank"):
            self.state, self._cookie = "needs_login", None
        else:
            async with asyncio.timeout(5):
                snapshot = await page.evaluate(IDENTITY_SCRIPT)
            if self._login_pending and not completing:
                return
            self.state = classify(snapshot, cookie, http_status)
            if self._navigation_failed and self.state == "connected" and not completing:
                self.state = "error"
            self._cookie = cookie if self.state == "connected" else None
            self.last_checked = int(time.time())
        fingerprint = hashlib.sha256(
            json.dumps(cookies, sort_keys=True, separators=(",", ":")).encode()
        ).digest()
        now = time.monotonic()
        changed = fingerprint != self._cookies_fingerprint
        # Skip persisting the untouched empty profile so a first seed stays possible.
        if (
            not completing
            and not self._seed_eligible
            and (changed or now - self._last_save >= self.settings.save_seconds)
        ):
            await self._save_locked()
        self._cookies_fingerprint = fingerprint
        return snapshot

    async def _visit_locked(self, url: str, *, inspect: bool = True) -> dict | None:
        if self._login_pending and inspect:
            return
        if time.time() < self.retry_at:
            self._error()
            return
        self._seed_eligible = False
        self.state = "reauthenticating" if inspect else "needs_login"
        self._cookie = None
        self._last_visit = time.monotonic()
        try:
            page = await self._page_locked()
            if self._login_pending and inspect:
                return
            # Normal browser navigation uses the existing profile. No HTTPX/API auth workaround.
            response = await page.goto(url, wait_until="domcontentloaded")
            if self._login_pending and inspect:
                return
            await page.bring_to_front()
            if response is not None and response.status == 429:
                self.retry_at = retry_deadline(response.headers.get("retry-after"))
            self._navigation_failed = response is not None and response.status >= 400
            if inspect:
                return await self._poll_locked(http_status=response.status if response else None)
            if self._navigation_failed:
                self._error()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.last_checked = int(time.time())
            self._navigation_failed = True
            self._error()

    async def _reauth_locked(self) -> None:
        if self._login_pending:
            return
        snapshot = await self._visit_locked(HOME)
        if (
            self._login_pending
            or self.state != "needs_login"
            or self._navigation_failed
            or not snapshot
            or snapshot.get("identity_known") is not True
            or snapshot.get("authenticated") is not False
            or snapshot.get("login_form")
            or snapshot.get("challenge")
        ):
            return
        parsed = urlsplit(snapshot.get("url", ""))
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.pixiv.net"
            or re.search(r"/(?:login|auth|captcha|challenge|two-factor|mfa)(?:/|$)", parsed.path)
        ):
            return
        # A normal HOME can stay guest instead of redirecting to accounts. Exercise existing
        # SSO once in this same context; forms/challenges remain for the user, without retries.
        await self._visit_locked(LOGIN)

    async def reauth(self) -> dict:
        if self._login_pending:
            return self.status()
        async with self.lock:
            if self._login_pending:
                return self.status()
            await self._ensure_locked(visit=False)
            await self._reauth_locked()
            return self.status()

    def _begin_login(self) -> None:
        # Claim manual control before waiting for background work to release the lock.
        self._login_pending = True
        self._completion_ready = False
        self._login_generation += 1
        self._seed_eligible = False

    async def open(self) -> dict:
        self._begin_login()
        async with self.lock:
            self._completion_ready = False
            await self._ensure_locked(visit=False)
            pages = [page for page in self.session.context.pages if not page.is_closed()]
            if all(page.url in ("", "about:blank") for page in pages):
                await self._visit_locked(LOGIN, inspect=False)
            # Existing pages/popups retain their navigation, focus and unfinished input.
            return self.status()

    async def login(self) -> dict:
        """User-requested official login, preserving both the current profile and app cookie."""
        self._begin_login()
        async with self.lock:
            self._completion_ready = False
            await self._ensure_locked(visit=False)
            await self._visit_locked(LOGIN, inspect=False)
            return self.status()

    async def complete(self) -> dict:
        """Inspect once on explicit confirmation; the app validates before resuming automation."""
        self._login_pending = True
        self._completion_ready = False
        self._seed_eligible = False
        generation = self._login_generation
        async with self.lock:
            if generation != self._login_generation or not self._login_pending:
                raise Unavailable("Browser unavailable")
            await self._ensure_locked(visit=False)
            if generation != self._login_generation:
                raise Unavailable("Browser unavailable")
            try:
                await self._poll_locked(completing=True)
                if generation != self._login_generation:
                    raise Unavailable("Browser unavailable")
                if self.state != "connected":
                    self.state, self._cookie = "needs_login", None
                    return self.status()
                await self._save_locked(completing=True)
            except Exception:
                self._error()
                raise Unavailable("Browser unavailable") from None
            if generation != self._login_generation:
                raise Unavailable("Browser unavailable")
            self._navigation_failed = False
            self._completion_ready = True
            return self.status()

    async def resume(self, generation: int) -> dict:
        """Called only after the app validates the explicitly completed account."""
        async with self.lock:
            if (
                generation != self._login_generation
                or not self._login_pending
                or not self._completion_ready
            ):
                raise Unavailable("Browser unavailable")
            self._login_pending = False
            self._completion_ready = False
            return self.status()

    async def seed(self, cookie: str) -> dict:
        if self._login_pending:
            raise ProfileOccupied("Browser profile is not empty")
        async with self.lock:
            if self._login_pending:
                raise ProfileOccupied("Browser profile is not empty")
            await self._ensure_locked(visit=False)
            if self._login_pending:
                raise ProfileOccupied("Browser profile is not empty")
            current = await self.session.context.storage_state(indexed_db=True)
            navigated = any(
                page.url not in ("", "about:blank") for page in self.session.context.pages
            )
            if (
                self._login_pending
                or not self._seed_eligible
                or navigated
                or current.get("cookies")
                or current.get("origins")
            ):
                raise ProfileOccupied("Browser profile is not empty")
            self._seed_eligible = False
            await self.session.context.add_cookies(
                [
                    {
                        "name": "PHPSESSID",
                        "value": cookie,
                        "domain": "www.pixiv.net",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            await self._visit_locked(HOME)
            return self.status()

    async def disconnect(self) -> dict:
        async with self.lock:
            self._disconnected = True
            self._reset_pending = True
            self._login_pending = False
            self._completion_ready = False
            self._login_generation += 1
            self._cookie = None
            self._seed_eligible = False
            failed = False
            if self.session is not None:
                # Stop page scripts/network before removing the persistent profile.
                with contextlib.suppress(Exception):
                    await self.session.context.set_storage_state({"cookies": [], "origins": []})
                try:
                    await self.session.close(reset=True)
                except Exception:
                    failed = True
                self.session = None
            else:
                try:
                    await self.factory.reset()
                except Exception:
                    failed = True
            try:
                await self._disk(self.store.delete)
            except Exception:
                failed = True
            if failed:
                self._error()
                raise Unavailable("Browser reset incomplete") from None
            self._reset_pending = False
            self._cookies_fingerprint = None
            self._navigation_failed = False
            self.last_checked = int(time.time())
            self.state = "needs_login"
            return self.status()

    async def tick(self) -> None:
        """One supervisor iteration; public for isolated scheduling tests."""
        if self._login_pending:
            return
        async with self.lock:
            now = time.monotonic()
            if self._disconnected or self._login_pending:
                return
            if self.session is None or self.session.closed:
                if now - self._last_boot >= self.settings.retry_seconds:
                    await self._ensure_locked()
                return
            try:
                await self._poll_locked()
            except Exception:
                self._error()
            interval = (
                self.settings.retry_seconds
                if self.state == "error"
                else self.settings.reauth_seconds
            )
            if (
                self._viewers == 0
                and not self.settings.managed_navigation
                and not self._seed_eligible
                and self.state in {"connected", "error", "needs_login"}
                and now - self._last_visit >= interval
            ):
                await self._reauth_locked()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Third-party exceptions can include URLs, state, or cookie values. Never log them.
                self._error()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.poll_seconds)
            except TimeoutError:
                pass


def retry_deadline(value: str | None) -> int:
    current = time.time()
    delay = 60.0
    if value:
        try:
            seconds = float(value)
            if math.isfinite(seconds):
                delay = max(0.0, seconds)
        except ValueError:
            try:
                delay = max(0.0, parsedate_to_datetime(value).timestamp() - current)
            except (ValueError, TypeError, OverflowError):
                pass
    return math.ceil(current + delay + random.uniform(1, 2))
