"""Bounded, cancellable network operations. Never include remote errors in logs."""

import asyncio
import math
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from app.adapters.pixiv.contracts import AdapterError

MAX_IMAGE_BYTES = 100 * 1024 * 1024


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
            delay = max(exc.retry_after or 0, 2**attempt + random.uniform(0, 1))
            if on_retry:
                on_retry(exc, delay)
            if delay > 300 or attempt == 3:
                raise
            await sleep(delay)


async def download(url: str, *, client: httpx.AsyncClient | None = None, on_retry=None) -> bytes:
    """Validate each redirect before sending it; no bearer token or ambient proxy."""
    if client is None:
        async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as own:
            return await download(url, client=own, on_retry=on_retry)

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
