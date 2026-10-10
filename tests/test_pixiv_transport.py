import asyncio
from pathlib import Path

import httpx
import pytest

from app.adapters.pixiv.contracts import AdapterError
from app.adapters.pixiv.live import CookiePixivAdapter
from app.adapters.pixiv.transport import download, retry


async def test_download_redirect_is_checked_before_following():
    visited = []

    def handler(request):
        visited.append(str(request.url))
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdapterError, match="contract"):
            await download("https://i.pximg.net/image", client=client)
    assert len(visited) == 1


async def test_download_valid_redirect_and_size_limit():
    visited = []

    def handler(request):
        visited.append(str(request.url))
        if request.url.path == "/first":
            return httpx.Response(302, headers={"location": "https://i2.pximg.net/final"})
        return httpx.Response(200, content=b"synthetic-image")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await download("https://i.pximg.net/first", client=client) == b"synthetic-image"
    assert len(visited) == 2
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-length": str(101 * 1024**2)},
            )
        )
    ) as client:
        with pytest.raises(AdapterError, match="unsupported"):
            await download("https://i.pximg.net/huge", client=client)


async def test_retry_after_respected_and_retries_bounded():
    delays = []
    count = 0

    async def operation():
        nonlocal count
        count += 1
        raise AdapterError("rate_limited", retry_after=12)

    async def sleep(delay):
        delays.append(delay)

    with pytest.raises(AdapterError, match="rate_limited"):
        await retry(operation, sleep=sleep)
    assert count == 4 and len(delays) == 3 and min(delays) >= 12

    async def forbidden():
        raise AdapterError("unavailable")

    with pytest.raises(AdapterError, match="unavailable"):
        await retry(forbidden, sleep=sleep)
    assert len(delays) == 3


async def test_real_bridge_process_exits_and_does_not_put_cookie_in_arguments():
    adapter = CookiePixivAdapter("synthetic-cookie-not-sent-to-pixiv")
    async with adapter:
        command = Path(f"/proc/{adapter.process.pid}/cmdline").read_bytes()
        assert b"synthetic-cookie" not in command
        assert adapter.cookie is None
        # Open starts the process and its library, but does not contact Pixiv.
        assert adapter.process.returncode is None
    assert adapter.process.returncode is not None


async def test_cancelled_bridge_waits_for_child_exit():
    adapter = CookiePixivAdapter("synthetic-cookie-not-sent-to-pixiv")
    ready = asyncio.Event()

    async def run():
        async with adapter:
            ready.set()
            await asyncio.Future()

    task = asyncio.create_task(run())
    await asyncio.wait_for(ready.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.process.returncode is not None
