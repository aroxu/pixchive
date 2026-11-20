import asyncio
from functools import partial

import httpx
import pytest

from app.adapters.pixiv import transport
from app.adapters.pixiv.contracts import AdapterError
from app.adapters.pixiv.transport import MediaDownloadSession, download

MEDIA_URL = "https://i.pximg.net/image"


@pytest.fixture(autouse=True)
def disable_media_jitter_for_session_contracts(monkeypatch):
    # Keep the existing concurrency/cooldown schedules exact. Nonzero request
    # jitter and its interaction with these contracts are covered separately.
    monkeypatch.setattr(transport, "media_request_delay", lambda: 0)


class TrackedTransport(httpx.MockTransport):
    def __init__(self, handler):
        super().__init__(handler)
        self.closed = False

    async def aclose(self):
        self.closed = True
        await super().aclose()


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False
        self.read = False

    async def __aiter__(self):
        self.read = True
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


@pytest.fixture
def fast_retry(monkeypatch):
    delays = []
    current = 0.0

    async def sleep(delay):
        nonlocal current
        delays.append(delay)
        current += delay

    monkeypatch.setattr(transport, "monotonic", lambda: current)
    monkeypatch.setattr(transport.random, "uniform", lambda start, end: 0)
    monkeypatch.setattr(transport, "retry", partial(transport.retry, sleep=sleep))
    return delays


async def test_reuses_one_bounded_client_and_closes_it(monkeypatch):
    clients = []
    original_client = httpx.AsyncClient
    requests = []

    def create_client(**kwargs):
        assert kwargs["limits"].max_connections == 3
        assert kwargs["limits"].max_keepalive_connections == 3
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        client = original_client(**kwargs)
        clients.append(client)
        return client

    def handler(request):
        requests.append(request)
        assert request.headers["Referer"] == "https://www.pixiv.net/"
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 30
        )
        return httpx.Response(200, content=request.url.path.encode())

    monkeypatch.setattr(transport.httpx, "AsyncClient", create_client)
    mock = TrackedTransport(handler)
    async with MediaDownloadSession(transport=mock) as fetch:
        for index in range(10):
            assert await fetch(f"{MEDIA_URL}/{index}") == f"/image/{index}".encode()
        assert len(clients) == 1
        assert not clients[0].is_closed
        assert not mock.closed
    assert len(requests) == 10
    assert clients[0].is_closed
    assert mock.closed


async def test_cannot_fetch_outside_context():
    mock = TrackedTransport(lambda request: pytest.fail("Unexpected request"))
    session = MediaDownloadSession(transport=mock)
    with pytest.raises(RuntimeError, match="active async context"):
        await session(MEDIA_URL)
    async with session:
        pass
    with pytest.raises(RuntimeError, match="active async context"):
        await session(MEDIA_URL)
    assert mock.closed


async def test_strips_final_credentials_after_client_auth_and_defaults(monkeypatch):
    original_client = httpx.AsyncClient

    def create_client(**kwargs):
        return original_client(
            **kwargs,
            headers={
                "Cookie": "synthetic-header-cookie=secret",
                "Authorization": "Bearer synthetic-token",
                "Proxy-Authorization": "Basic synthetic-proxy",
            },
            cookies={"synthetic-jar-cookie": "secret"},
            auth=("synthetic-user", "synthetic-password"),
        )

    def handler(request):
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        assert "proxy-authorization" not in request.headers
        return httpx.Response(200, content=b"image")

    monkeypatch.setattr(transport.httpx, "AsyncClient", create_client)
    async with MediaDownloadSession(transport=httpx.MockTransport(handler)) as fetch:
        assert await fetch(MEDIA_URL) == b"image"


async def test_response_cookies_never_follow_redirects_retries_or_later_downloads(fast_retry):
    visited = []
    first_retry = True

    def handler(request):
        nonlocal first_retry
        visited.append(str(request.url))
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        cookie = "synthetic-pximg=secret; Domain=.pximg.net; Path=/; Secure"
        if request.url.path == "/redirect":
            return httpx.Response(
                302,
                headers={"location": "https://i2.pximg.net/retry", "set-cookie": cookie},
            )
        if request.url.path == "/retry" and first_retry:
            first_retry = False
            return httpx.Response(503, headers={"set-cookie": cookie})
        return httpx.Response(200, headers={"set-cookie": cookie}, content=b"image")

    async with MediaDownloadSession(transport=httpx.MockTransport(handler)) as fetch:
        assert await fetch("https://i.pximg.net/redirect") == b"image"
        assert await fetch(MEDIA_URL) == b"image"
    assert visited == [
        "https://i.pximg.net/redirect",
        "https://i2.pximg.net/retry",
        "https://i.pximg.net/redirect",
        "https://i2.pximg.net/retry",
        MEDIA_URL,
    ]
    assert fast_retry == [1]


@pytest.mark.parametrize("redirect", [False, True])
@pytest.mark.parametrize(
    "unsafe",
    [
        "http://i.pximg.net/image",
        "https://127.0.0.1/image",
        "https://pximg.net/image",
        "https://evilpximg.net/image",
        "https://i.pximg.net.evil.example/image",
        "https://user:secret@i.pximg.net/image",
        "https://i.pximg.net:8443/image",
        "https://i.pximg.net:not-a-port/image",
        "https://i.pximg.net/image#fragment",
        "https://i.pximg.net/with space",
        "https://i.pximg.net/with\\backslash",
    ],
)
async def test_checks_urls_before_every_request(unsafe, redirect, fast_retry):
    visited = []
    # HTTPX parses Location even with follow_redirects=False, so a malformed
    # port raises its protocol error before our URL validator sees the target.
    code = "network" if redirect and ":not-a-port" in unsafe else "contract"

    def handler(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": unsafe})

    async with MediaDownloadSession(transport=httpx.MockTransport(handler)) as fetch:
        with pytest.raises(AdapterError, match=code):
            await fetch(MEDIA_URL if redirect else unsafe)
    assert visited == ([MEDIA_URL] * (4 if code == "network" else 1) if redirect else [])


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_valid_relative_redirects_remain_supported(status):
    visited = []

    def handler(request):
        visited.append(request.url.path)
        if request.url.path == "/image":
            return httpx.Response(status, headers={"location": "/final"})
        return httpx.Response(200, content=b"image")

    async with MediaDownloadSession(transport=httpx.MockTransport(handler)) as fetch:
        assert await fetch(MEDIA_URL) == b"image"
    assert visited == ["/image", "/final"]


@pytest.mark.parametrize("location,expected_requests", [("/image", 5), (None, 1)])
async def test_redirect_loops_and_missing_locations_are_bounded(location, expected_requests):
    responses = []

    def handler(request):
        response = httpx.Response(302, headers={"location": location} if location else {})
        responses.append(response)
        return response

    async with MediaDownloadSession(transport=httpx.MockTransport(handler)) as fetch:
        with pytest.raises(AdapterError, match="contract"):
            await fetch(MEDIA_URL)
    assert len(responses) == expected_requests
    assert all(response.is_closed for response in responses)


@pytest.mark.parametrize("length", ["9", "-1", "not-a-number"])
async def test_rejects_invalid_or_excessive_size_before_reading(length, monkeypatch):
    monkeypatch.setattr(transport, "MAX_IMAGE_BYTES", 8)
    stream = TrackedStream([b"image"])
    mock = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-length": length}, stream=stream)
    )
    async with MediaDownloadSession(transport=mock) as fetch:
        with pytest.raises(AdapterError, match="unsupported"):
            await fetch(MEDIA_URL)
    assert not stream.read
    assert stream.closed


@pytest.mark.parametrize(
    "chunks,allowed", [([b"1234", b"5678"], True), ([b"1234", b"56789"], False)]
)
async def test_stream_size_bound_and_exact_limit(chunks, allowed, monkeypatch):
    monkeypatch.setattr(transport, "MAX_IMAGE_BYTES", 8)
    stream = TrackedStream(chunks)
    mock = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    async with MediaDownloadSession(transport=mock) as fetch:
        if allowed:
            assert await fetch(MEDIA_URL) == b"12345678"
        else:
            with pytest.raises(AdapterError, match="unsupported"):
                await fetch(MEDIA_URL)
    assert stream.closed


@pytest.mark.parametrize(
    "status,code",
    [(401, "auth_expired"), (403, "unavailable"), (404, "unavailable"), (400, "contract")],
)
async def test_terminal_errors_are_not_retried(status, code, fast_retry):
    callbacks = []
    mock = httpx.MockTransport(lambda request: httpx.Response(status))
    async with MediaDownloadSession(
        transport=mock, on_retry=lambda error, delay: callbacks.append((error.code, delay))
    ) as fetch:
        with pytest.raises(AdapterError, match=code):
            await fetch(MEDIA_URL)
    assert callbacks == []
    assert fast_retry == []


@pytest.mark.parametrize(
    "status,code,expected_delays",
    [(429, "rate_limited", [12, 12, 12, 12]), (503, "network", [1, 2, 4, 8])],
)
async def test_retries_and_callbacks_remain_bounded(status, code, expected_delays, fast_retry):
    requests = []
    callbacks = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"retry-after": "12"} if status == 429 else {})

    async with MediaDownloadSession(
        transport=httpx.MockTransport(handler),
        on_retry=lambda error, delay: callbacks.append((error.code, delay)),
    ) as fetch:
        with pytest.raises(AdapterError, match=code):
            await fetch(MEDIA_URL)
    assert len(requests) == 4
    assert callbacks == [(code, delay) for delay in expected_delays]
    assert fast_retry == expected_delays[:3]


async def test_long_retry_after_is_not_shortened(fast_retry):
    callbacks = []
    mock = httpx.MockTransport(lambda request: httpx.Response(429, headers={"retry-after": "301"}))
    async with MediaDownloadSession(
        transport=mock, on_retry=lambda error, delay: callbacks.append((error.code, delay))
    ) as fetch:
        with pytest.raises(AdapterError, match="rate_limited"):
            await fetch(MEDIA_URL)
    assert callbacks == [("rate_limited", 301)]
    assert fast_retry == []


async def test_read_failure_retries_with_clean_bytes_and_same_session(fast_retry):
    streams = []
    callbacks = []

    def handler(request):
        stream = (
            TrackedStream([b"partial" * 10000], error=httpx.ReadError("synthetic read failure"))
            if not streams
            else TrackedStream([b"complete"])
        )
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    async with MediaDownloadSession(
        transport=httpx.MockTransport(handler),
        on_retry=lambda error, delay: callbacks.append((error.code, delay)),
    ) as fetch:
        assert await fetch(MEDIA_URL) == b"complete"
    assert len(streams) == 2
    assert all(stream.closed for stream in streams)
    assert callbacks == [("network", 1)]
    assert fast_retry == [1]


@pytest.mark.parametrize("during_backoff", [False, True])
async def test_cancellation_closes_stream_and_session_without_retry(monkeypatch, during_backoff):
    started = asyncio.Event()
    callbacks = []
    streams = []
    requests = []

    async def block():
        started.set()
        await asyncio.Future()

    class BlockingStream(TrackedStream):
        async def __aiter__(self):
            await block()
            yield b"unreachable"

    async def sleep(delay):
        await block()

    if during_backoff:
        monkeypatch.setattr(transport, "retry", partial(transport.retry, sleep=sleep))

    def handler(request):
        requests.append(request)
        stream = TrackedStream([]) if during_backoff else BlockingStream([])
        streams.append(stream)
        return httpx.Response(503 if during_backoff else 200, stream=stream)

    mock = TrackedTransport(handler)

    async def collect():
        async with MediaDownloadSession(
            transport=mock, on_retry=lambda error, delay: callbacks.append(error.code)
        ) as fetch:
            await fetch(MEDIA_URL)

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(requests) == 1
    assert streams[0].closed
    assert mock.closed
    assert callbacks == (["network"] if during_backoff else [])


async def test_standalone_download_still_owns_and_closes_its_client(monkeypatch, fast_retry):
    original_client = httpx.AsyncClient
    clients = []
    requests = []
    callbacks = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"retry-after": "12"})
        return httpx.Response(200, content=b"image")

    mock = TrackedTransport(handler)

    def create_client(**kwargs):
        kwargs["transport"] = mock
        client = original_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(transport.httpx, "AsyncClient", create_client)
    assert (
        await download(
            MEDIA_URL, on_retry=lambda error, delay: callbacks.append((error.code, delay))
        )
        == b"image"
    )
    assert len(clients) == 1
    assert clients[0].is_closed
    assert mock.closed
    assert callbacks == [("rate_limited", 12)]
    assert fast_retry == [12]


async def test_explicit_download_client_is_still_caller_owned():
    mock = TrackedTransport(lambda request: httpx.Response(200, content=b"image"))
    async with httpx.AsyncClient(transport=mock) as client:
        assert await download(MEDIA_URL, client=client) == b"image"
        assert not client.is_closed
        assert not mock.closed
    assert mock.closed


class ManualClock:
    """Advance retry time explicitly while real tasks wait on cancellable futures."""

    def __init__(self):
        self.current = 100.0
        self.pending = []
        self.sleeps = asyncio.Queue()

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        entry = (self.current + delay, future)
        self.pending.append(entry)
        self.sleeps.put_nowait((asyncio.current_task().get_name(), delay))
        try:
            await future
        finally:
            self.pending.remove(entry)

    def advance(self, seconds):
        self.current += seconds
        for deadline, future in self.pending:
            if deadline <= self.current and not future.done():
                future.set_result(None)

    async def next_sleep(self):
        return await asyncio.wait_for(self.sleeps.get(), 2)


@pytest.fixture
def manual_clock(monkeypatch):
    clock = ManualClock()
    monkeypatch.setattr(transport, "monotonic", lambda: clock.current)
    monkeypatch.setattr(transport.random, "uniform", lambda start, end: 0)
    monkeypatch.setattr(transport, "retry", partial(transport.retry, sleep=clock.sleep))
    return clock


async def checkpoint():
    # Run already-ready tasks without releasing any response/timer barrier.
    for _ in range(3):
        await asyncio.sleep(0)


async def cancel_all(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_three_body_reads_and_cancelling_active_or_queued_downloads():
    started = asyncio.Queue()
    releases = [asyncio.Event() for _ in range(9)]
    streams = []
    visited = []
    active = 0
    peak = 0

    class BlockedBody(TrackedStream):
        def __init__(self, index):
            super().__init__([str(index).encode()])
            self.index = index

        async def __aiter__(self):
            await releases[self.index].wait()
            yield self.chunks[0]

        async def aclose(self):
            nonlocal active
            if not self.closed:
                active -= 1
            await super().aclose()

    def handler(request):
        nonlocal active, peak
        index = int(request.url.path.rsplit("/", 1)[1])
        visited.append(index)
        active += 1
        peak = max(peak, active)
        stream = BlockedBody(index)
        streams.append(stream)
        started.put_nowait(index)
        return httpx.Response(200, stream=stream)

    mock = TrackedTransport(handler)
    async with MediaDownloadSession(transport=mock) as fetch:
        tasks = [asyncio.create_task(fetch(f"{MEDIA_URL}/{index}")) for index in range(9)]
        try:
            first = [await asyncio.wait_for(started.get(), 2) for _ in range(3)]
            await checkpoint()
            assert first == [0, 1, 2]
            assert visited == first
            assert active == 3
            tasks[3].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[3]
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            assert await asyncio.wait_for(started.get(), 2) == 4
            await checkpoint()
            assert active == 3
            assert visited == [0, 1, 2, 4]
            for release in releases:
                release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
            for index, result in enumerate(results):
                if index in (0, 3):
                    assert isinstance(result, asyncio.CancelledError)
                else:
                    assert result == str(index).encode()
        finally:
            await cancel_all(tasks)
    assert peak == 3
    assert active == 0
    assert all(stream.closed for stream in streams)
    assert mock.closed


async def test_429_gates_peer_retry_redirect_and_queued_download(manual_clock):
    clock = manual_clock
    release_rate = asyncio.Event()
    release_redirect = asyncio.Event()
    started = asyncio.Queue()
    visited = []
    attempts = {}
    callbacks = []

    async def handler(request):
        path = request.url.path
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        visited.append((path, clock.current))
        attempts[path] = attempts.get(path, 0) + 1
        started.put_nowait(path)
        if path == "/rate" and attempts[path] == 1:
            await release_rate.wait()
            return httpx.Response(
                429,
                headers={
                    "retry-after": "12",
                    "set-cookie": "synthetic=secret; Domain=.pximg.net; Path=/; Secure",
                },
            )
        if path == "/retry" and attempts[path] == 1:
            return httpx.Response(503)
        if path == "/redirect":
            await release_redirect.wait()
            return httpx.Response(302, headers={"location": "https://i2.pximg.net/final"})
        return httpx.Response(200, content=b"image")

    async with MediaDownloadSession(
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        on_retry=lambda error, delay: callbacks.append((error.code, delay)),
    ) as fetch:
        tasks = [
            asyncio.create_task(fetch(f"https://i.pximg.net/{path}"), name=path)
            for path in ("rate", "retry", "redirect", "queued")
        ]
        try:
            assert [await asyncio.wait_for(started.get(), 2) for _ in range(3)] == [
                "/rate",
                "/retry",
                "/redirect",
            ]
            assert await clock.next_sleep() == ("retry", 1)
            release_rate.set()
            assert await clock.next_sleep() == ("rate", 12)
            release_redirect.set()
            assert await clock.next_sleep() == ("redirect", 12)
            clock.advance(1)
            assert await clock.next_sleep() == ("retry", 11)
            clock.advance(10)
            await checkpoint()
            assert len(visited) == 3
            clock.advance(1)
            assert await asyncio.wait_for(asyncio.gather(*tasks), 2) == [b"image"] * 4
        finally:
            await cancel_all(tasks)
    assert len(visited) == 7
    assert all(timestamp == 112 for _, timestamp in visited[3:])
    assert callbacks == [("network", 1), ("rate_limited", 12)]
    assert clock.pending == []


async def test_overlapping_429s_never_shorten_deadline_and_waiters_recheck_extension(manual_clock):
    clock = manual_clock
    releases = {name: asyncio.Event() for name in ("long", "short", "extend")}
    delays = {"long": 20, "short": 2, "extend": 30}
    started = asyncio.Queue()
    visited = []
    attempts = {}
    callbacks = []

    async def handler(request):
        name = request.url.path.lstrip("/")
        visited.append((name, clock.current))
        attempts[name] = attempts.get(name, 0) + 1
        started.put_nowait(name)
        if attempts[name] == 1:
            await releases[name].wait()
            return httpx.Response(429, headers={"retry-after": str(delays[name])})
        return httpx.Response(200, content=b"image")

    async with MediaDownloadSession(
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        on_retry=lambda error, delay: callbacks.append(clock.current + delay),
    ) as fetch:
        tasks = [
            asyncio.create_task(fetch(f"https://i.pximg.net/{name}"), name=name)
            for name in releases
        ]
        try:
            assert [await asyncio.wait_for(started.get(), 2) for _ in range(3)] == list(releases)
            releases["long"].set()
            assert await clock.next_sleep() == ("long", 20)
            clock.advance(5)
            releases["short"].set()
            assert await clock.next_sleep() == ("short", 2)
            assert callbacks == [120, 120]
            clock.advance(2)
            assert await clock.next_sleep() == ("short", 13)
            releases["extend"].set()
            assert await clock.next_sleep() == ("extend", 30)
            assert callbacks == [120, 120, 137]
            clock.advance(13)
            waits = [await clock.next_sleep() for _ in range(2)]
            assert sorted(waits) == [("long", 17), ("short", 17)]
            clock.advance(16)
            await checkpoint()
            assert len(visited) == 3
            clock.advance(1)
            assert await asyncio.wait_for(asyncio.gather(*tasks), 2) == [b"image"] * 3
        finally:
            await cancel_all(tasks)
    assert all(timestamp == 137 for _, timestamp in visited[3:])
    assert clock.pending == []


async def test_cooldown_published_before_response_close_survives_origin_and_waiter_cancel(
    manual_clock,
):
    clock = manual_clock
    closing = asyncio.Event()
    release_close = asyncio.Event()
    visited = []
    callbacks = []

    class SlowClose(TrackedStream):
        async def aclose(self):
            closing.set()
            try:
                await release_close.wait()
            finally:
                self.closed = True

    stream = SlowClose([])

    def handler(request):
        visited.append((request.url.path, clock.current))
        if request.url.path == "/rate":
            return httpx.Response(429, headers={"retry-after": "12"}, stream=stream)
        return httpx.Response(200, content=b"image")

    mock = TrackedTransport(handler)
    async with MediaDownloadSession(
        transport=mock,
        sleep=clock.sleep,
        on_retry=lambda error, delay: callbacks.append(error.code),
    ) as fetch:
        tasks = [asyncio.create_task(fetch("https://i.pximg.net/rate"), name="rate")]
        try:
            await asyncio.wait_for(closing.wait(), 2)
            tasks.extend(
                asyncio.create_task(fetch(f"https://i.pximg.net/{name}"), name=name)
                for name in ("cancelled-peer", "surviving-peer")
            )
            waits = [await clock.next_sleep() for _ in range(2)]
            assert waits == [("cancelled-peer", 12), ("surviving-peer", 12)]
            assert callbacks == []
            for task in tasks[:2]:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            tasks.append(asyncio.create_task(fetch(MEDIA_URL), name="replacement"))
            assert await clock.next_sleep() == ("replacement", 12)
            clock.advance(11)
            await checkpoint()
            assert visited == [("/rate", 100)]
            clock.advance(1)
            assert await asyncio.wait_for(asyncio.gather(*tasks[2:]), 2) == [b"image"] * 2
        finally:
            release_close.set()
            await cancel_all(tasks)
    assert stream.closed
    assert mock.closed
    assert callbacks == []
    assert all(timestamp == 112 for _, timestamp in visited[1:])
    assert clock.pending == []


async def test_long_retry_after_aborts_session_and_blocks_all_subsequent_requests(manual_clock):
    clock = manual_clock
    release_rate = asyncio.Event()
    release_redirect = asyncio.Event()
    started = asyncio.Queue()
    visited = []
    callbacks = []

    async def handler(request):
        path = request.url.path
        visited.append(path)
        started.put_nowait(path)
        if path == "/rate":
            await release_rate.wait()
            return httpx.Response(429, headers={"retry-after": "301"})
        if path == "/retry":
            return httpx.Response(503)
        if path == "/redirect":
            await release_redirect.wait()
            return httpx.Response(302, headers={"location": "/final"})
        pytest.fail("An aborted session sent another request")

    async with MediaDownloadSession(
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        on_retry=lambda error, delay: callbacks.append((error.code, delay)),
    ) as fetch:
        tasks = [
            asyncio.create_task(fetch(f"https://i.pximg.net/{name}"), name=name)
            for name in ("rate", "retry", "redirect", "queued")
        ]
        try:
            assert [await asyncio.wait_for(started.get(), 2) for _ in range(3)] == [
                "/rate",
                "/retry",
                "/redirect",
            ]
            assert await clock.next_sleep() == ("retry", 1)
            release_rate.set()
            with pytest.raises(AdapterError, match="rate_limited") as error:
                await asyncio.wait_for(tasks[0], 2)
            assert error.value.retry_after == 301
            release_redirect.set()
            clock.advance(1)
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
            assert all(isinstance(result, AdapterError) for result in results)
            assert all(result.code == "rate_limited" for result in results)
            assert all(result.retry_after >= 301 for result in results)
            clock.advance(1_000)
            with pytest.raises(AdapterError, match="rate_limited"):
                await fetch(MEDIA_URL)
        finally:
            await cancel_all(tasks)
    assert visited == ["/rate", "/retry", "/redirect"]
    assert all(delay >= 301 for code, delay in callbacks if code == "rate_limited")
    assert clock.pending == []
    assert clock.sleeps.empty()
    async with MediaDownloadSession(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"image"))
    ) as fresh:
        assert await fresh(MEDIA_URL) == b"image"
