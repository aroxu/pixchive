"""Bounded, cancellable network operations. Never include remote errors in logs."""

import asyncio
import math
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from urllib.parse import urlparse

import httpx

from app.adapters.pixiv.contracts import AdapterError

MAX_IMAGE_BYTES = 100 * 1024 * 1024


def media_request_delay() -> float:
    """Small independent delay before each media request, including redirects/retries."""
    return random.uniform(0.1, 1.3)


async def _strip_media_credentials(request: httpx.Request) -> None:
    # HTTPX merges client defaults, auth and response cookies before this hook.
    # Clear the final request, not the shared cookie jar, so concurrent requests
    # and cookies received on redirects/retries cannot leak credentials.
    for header in ("Cookie", "Authorization", "Proxy-Authorization"):
        request.headers.pop(header, None)


class MediaDownloadSession:
    """Single-use async context yielding a callable with a shared, credential-free pool."""

    def __init__(
        self,
        *,
        on_retry=None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
    ):
        self._on_retry = on_retry
        self._sleep = sleep
        self._requests = asyncio.Semaphore(3)
        self._cooldown_until = 0.0
        self._fatal_retry_after: float | None = None
        self._active = False
        self._client = httpx.AsyncClient(
            timeout=30,
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=3, max_keepalive_connections=3),
            event_hooks={"request": [self._before_request], "response": [self._response]},
            transport=transport,
        )

    async def __aenter__(self):
        await self._client.__aenter__()
        self._active = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self._active = False
        await self._client.__aexit__(exc_type, exc, traceback)

    async def __call__(self, url: str) -> bytes:
        if not self._active:
            raise RuntimeError("MediaDownloadSession requires an active async context")
        self._raise_if_aborted()
        # HTTPX limits do not apply to injected transports. Bound whole downloads
        # too, including response bodies, queued retries and cancellation cleanup.
        async with self._requests:
            if not self._active:
                raise RuntimeError("MediaDownloadSession requires an active async context")
            self._raise_if_aborted()
            return await download(url, client=self._client, on_retry=self._backoff)

    async def _before_request(self, request: httpx.Request) -> None:
        self._raise_if_aborted()
        jitter = media_request_delay()
        ready_at = monotonic() + jitter
        # Sample once per actual HTTP request, without serializing body reads.
        # Peers can extend the cooldown while we wait; keep this request's small
        # delay after that deadline as well, and recheck before sending anything.
        while True:
            self._raise_if_aborted()
            delay = max(ready_at, self._cooldown_until + jitter) - monotonic()
            if delay <= 0:
                break
            await self._sleep(delay)
        await _strip_media_credentials(request)

    async def _response(self, response: httpx.Response) -> None:
        if response.status_code == 429:
            # Publish before response close can yield to a queued request. The
            # retry callback below adds its exponential/jitter delay afterwards.
            delay = max(retry_after(response.headers.get("retry-after")) or 0, 1)
            self._postpone(delay)

    def _raise_if_aborted(self) -> None:
        if self._fatal_retry_after is not None:
            raise AdapterError("rate_limited", retry_after=self._fatal_retry_after)

    def _postpone(self, delay: float) -> float:
        current = monotonic()
        self._cooldown_until = max(self._cooldown_until, current + delay)
        if delay > 300:
            self._fatal_retry_after = max(self._fatal_retry_after or 0, delay)
        return self._cooldown_until - current

    def _backoff(self, error: AdapterError, delay: float) -> None:
        if error.code == "rate_limited":
            delay = self._postpone(delay)
        # No await between publishing and notifying: concurrent callbacks cannot
        # replace a longer peer cooldown with a shorter Retry-After value.
        if self._on_retry:
            self._on_retry(error, delay)


def media_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".pximg.net")
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.fragment
            or "\\" in url
            or any(ord(c) < 33 for c in url)
        ):
            raise ValueError
    except ValueError:
        raise AdapterError("contract") from None
    return url


def retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        result = float(value)
        return max(0, result) if math.isfinite(result) else None
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def check_status(status: int, headers, *, auth=False):
    if status == 429:
        raise AdapterError("rate_limited", retry_after(headers.get("retry-after")))
    if status >= 500:
        raise AdapterError("network")
    if status == 401 or (auth and status in (400, 403)):
        raise AdapterError("auth_expired")
    if status in (403, 404):
        raise AdapterError("unavailable")
    if not 200 <= status < 300:
        raise AdapterError("contract")


async def retry(operation, *, sleep=asyncio.sleep, on_retry=None):
    for attempt in range(4):
        try:
            return await operation()
        except AdapterError as exc:
            if exc.code not in ("rate_limited", "network"):
                raise
            # A long server delay is never shortened. Surface it for an explicit retry
            # instead of holding the account's lease for hours.
            delay = max(exc.retry_after or 0, 2**attempt)
            if delay <= 300 and attempt < 3:
                # Jitter actual retries without crossing the session's long-delay limit.
                delay = min(300, delay + random.uniform(0, 1))
            if on_retry:
                on_retry(exc, delay)
            if delay > 300 or attempt == 3:
                raise
            await sleep(delay)


async def download(url: str, *, client: httpx.AsyncClient | None = None, on_retry=None) -> bytes:
    """Validate each redirect before sending it; no bearer token or ambient proxy."""
    if client is None:
        async with MediaDownloadSession(on_retry=on_retry) as fetch:
            return await fetch(url)

    async def once():
        current = media_url(url)
        try:
            for _ in range(5):
                async with client.stream(
                    "GET",
                    current,
                    headers={"Referer": "https://www.pixiv.net/"},
                    follow_redirects=False,
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        from urllib.parse import urljoin

                        location = response.headers.get("location")
                        if not location:
                            raise AdapterError("contract")
                        current = media_url(urljoin(current, location))
                        continue
                    check_status(response.status_code, response.headers)
                    size = response.headers.get("content-length")
                    if size and (not size.isdigit() or int(size) > MAX_IMAGE_BYTES):
                        raise AdapterError("unsupported")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(64 * 1024):
                        if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                            raise AdapterError("unsupported")
                        data.extend(chunk)
                    return bytes(data)
            raise AdapterError("contract")
        except httpx.HTTPError:
            raise AdapterError("network") from None

    return await retry(once, on_retry=on_retry)
