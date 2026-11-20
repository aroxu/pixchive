import asyncio
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock, call

import httpx
import pytest

from app.adapters.pixiv import transport
from app.adapters.pixiv.contracts import AdapterError
from app.adapters.pixiv.transport import MediaDownloadSession
from tests.test_media_session import ManualClock, TrackedStream, cancel_all, checkpoint

MEDIA_URL = "https://i.pximg.net"


@pytest.fixture
def media_clock(monkeypatch):
    # Import helpers, not the other module's fixture that disables media jitter.
    clock = ManualClock()
    monkeypatch.setattr(transport, "monotonic", lambda: clock.current)
    return clock


class HeldCloseStream(TrackedStream):
    """Hold a 429 after its response hook publishes the shared cooldown."""

    def __init__(self):
        super().__init__([])
        self.closing = asyncio.Event()
        self.release = asyncio.Event()

    async def aclose(self):
        self.closing.set()
        try:
            await self.release.wait()
        finally:
            await super().aclose()


def test_media_request_delay_samples_uniform_image_range_each_time(monkeypatch):
    uniform = Mock(side_effect=[0.1, 1.3, 0.7])
    monkeypatch.setattr(transport, "random", SimpleNamespace(uniform=uniform))

    assert [transport.media_request_delay() for _ in range(3)] == [0.1, 1.3, 0.7]
    assert uniform.call_args_list == [call(0.1, 1.3)] * 3


async def test_initial_redirect_and_retry_requests_each_wait_for_a_fresh_sample(
    monkeypatch, media_clock
):
    clock = media_clock
    sample = Mock(side_effect=[0.125, 0.25, 0.375, 0.125, 0.25])
    monkeypatch.setattr(transport, "media_request_delay", sample)
    # Keep retry jitter separate from the five per-request media samples.
    retry_sample = Mock(return_value=0.5)
    monkeypatch.setattr(transport, "random", SimpleNamespace(uniform=retry_sample))
    monkeypatch.setattr(transport, "retry", partial(transport.retry, sleep=clock.sleep))
    visited = []
    final_attempts = 0

    def handler(request):
        nonlocal final_attempts
        visited.append((request.url.path, clock.current))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://i2.pximg.net/final"})
        if request.url.path == "/final":
            final_attempts += 1
            if final_attempts == 1:
                return httpx.Response(503)
        return httpx.Response(200, content=b"image")

    async with MediaDownloadSession(
        transport=httpx.MockTransport(handler), sleep=clock.sleep
    ) as fetch:

        async def collect():
            return await fetch(f"{MEDIA_URL}/start"), await fetch(f"{MEDIA_URL}/next")

        task = asyncio.create_task(collect(), name="download")
        try:
            for delay, requests_before in [
                (0.125, 0),
                (0.25, 1),
                (1.5, 2),
                (0.375, 2),
                (0.125, 3),
                (0.25, 4),
            ]:
                assert await clock.next_sleep() == ("download", delay)
                assert len(visited) == requests_before
                clock.advance(delay / 2)
                await checkpoint()
                assert len(visited) == requests_before
                clock.advance(delay / 2)
            assert await asyncio.wait_for(task, 2) == (b"image", b"image")
        finally:
            await cancel_all([task])

    assert visited == [
        ("/start", 100.125),
        ("/final", 100.375),
        ("/start", 102.25),
        ("/final", 102.375),
        ("/next", 102.625),
    ]
    assert sample.call_args_list == [call()] * 5
    retry_sample.assert_called_once_with(0, 1)
    assert clock.pending == []


@pytest.mark.parametrize("jitter", [0.25, 1.25])
async def test_cancelling_during_jitter_sends_nothing_and_releases_a_slot(
    monkeypatch, media_clock, jitter
):
    clock = media_clock
    sample = Mock(return_value=jitter)
    monkeypatch.setattr(transport, "media_request_delay", sample)
    visited = []
    callbacks = []

    def handler(request):
        visited.append((request.url.path, clock.current))
        return httpx.Response(200, content=request.url.path.encode())

    async with MediaDownloadSession(
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        on_retry=lambda error, delay: callbacks.append(error.code),
    ) as fetch:
        tasks = [
            asyncio.create_task(fetch(f"{MEDIA_URL}/{index}"), name=f"image-{index}")
            for index in range(4)
        ]
        try:
            assert [await clock.next_sleep() for _ in range(3)] == [
                (f"image-{index}", jitter) for index in range(3)
            ]
            assert sample.call_count == 3
            assert visited == []
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(tasks[0], 2)
            assert await clock.next_sleep() == ("image-3", jitter)
            assert sample.call_count == 4
            clock.advance(jitter / 2)
            await checkpoint()
            assert visited == []
            clock.advance(jitter / 2)
            assert await asyncio.wait_for(asyncio.gather(*tasks[1:]), 2) == [
                b"/1",
                b"/2",
                b"/3",
            ]
        finally:
            await cancel_all(tasks)

    assert sorted(visited) == [(f"/{index}", 100 + jitter) for index in (1, 2, 3)]
    assert callbacks == []
    assert clock.pending == []


async def test_nonzero_jitter_keeps_three_body_reads_parallel(monkeypatch, media_clock):
    clock = media_clock
    sample = Mock(return_value=0.25)
    monkeypatch.setattr(transport, "media_request_delay", sample)
    started = asyncio.Queue()
    releases = [asyncio.Event() for _ in range(4)]
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
        index = int(request.url.path.lstrip("/"))
        visited.append((index, clock.current))
        active += 1
        peak = max(peak, active)
        stream = BlockedBody(index)
        streams.append(stream)
        started.put_nowait(index)
        return httpx.Response(200, stream=stream)

    async with MediaDownloadSession(
        transport=httpx.MockTransport(handler), sleep=clock.sleep
    ) as fetch:
        tasks = [
            asyncio.create_task(fetch(f"{MEDIA_URL}/{index}"), name=f"image-{index}")
            for index in range(4)
        ]
        try:
            assert [await clock.next_sleep() for _ in range(3)] == [
                (f"image-{index}", 0.25) for index in range(3)
            ]
            assert visited == []
            clock.advance(0.25)
            assert [await asyncio.wait_for(started.get(), 2) for _ in range(3)] == [0, 1, 2]
            await checkpoint()
            assert active == peak == 3
            assert sample.call_count == 3
            releases[0].set()
            assert await asyncio.wait_for(tasks[0], 2) == b"0"
            assert await clock.next_sleep() == ("image-3", 0.25)
            assert active == 2
            clock.advance(0.125)
            await checkpoint()
            assert visited == [(index, 100.25) for index in range(3)]
            clock.advance(0.125)
            assert await asyncio.wait_for(started.get(), 2) == 3
            assert active == peak == 3
            for release in releases:
                release.set()
            assert await asyncio.wait_for(asyncio.gather(*tasks), 2) == [b"0", b"1", b"2", b"3"]
        finally:
            await cancel_all(tasks)

    assert visited[-1] == (3, 100.5)
    assert sample.call_count == 4
    assert active == 0
    assert all(stream.closed for stream in streams)
    assert clock.pending == []


async def test_existing_server_cooldown_is_followed_by_the_full_media_jitter(
    monkeypatch, media_clock
):
    clock = media_clock
    sample = Mock(side_effect=[0.125, 0.375])
    monkeypatch.setattr(transport, "media_request_delay", sample)
    stream = HeldCloseStream()
    visited = []

    def handler(request):
        visited.append((request.url.path, clock.current))
        if request.url.path == "/rate":
            return httpx.Response(429, headers={"retry-after": "12"}, stream=stream)
        return httpx.Response(200, content=b"image")

    async with MediaDownloadSession(
        transport=httpx.MockTransport(handler), sleep=clock.sleep
    ) as fetch:
        tasks = [asyncio.create_task(fetch(f"{MEDIA_URL}/rate"), name="rate")]
        try:
            assert await clock.next_sleep() == ("rate", 0.125)
            clock.advance(0.125)
            await asyncio.wait_for(stream.closing.wait(), 2)
            tasks.append(asyncio.create_task(fetch(f"{MEDIA_URL}/waiter"), name="waiter"))
            assert await clock.next_sleep() == ("waiter", 12.375)
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(tasks[0], 2)
            clock.advance(12)
            await checkpoint()
            assert visited == [("/rate", 100.125)]
            clock.advance(0.25)
            await checkpoint()
            assert visited == [("/rate", 100.125)]
            clock.advance(0.125)
            assert await asyncio.wait_for(tasks[1], 2) == b"image"
        finally:
            stream.release.set()
            await cancel_all(tasks)

    assert visited == [("/rate", 100.125), ("/waiter", 112.5)]
    assert sample.call_count == 2
    assert stream.closed
    assert clock.pending == []


async def test_peer_429_during_jitter_and_later_extension_keep_the_original_sample(
    monkeypatch, media_clock
):
    clock = media_clock
    sample = Mock(side_effect=[0.125, 0.125, 0.375])
    monkeypatch.setattr(transport, "media_request_delay", sample)
    streams = {name: HeldCloseStream() for name in ("first", "extend")}
    releases = {name: asyncio.Event() for name in streams}
    delays = {"first": "12", "extend": "20"}
    started = asyncio.Queue()
    visited = []

    async def handler(request):
        name = request.url.path.lstrip("/")
        visited.append((name, clock.current))
        if name in streams:
            started.put_nowait(name)
            await releases[name].wait()
            return httpx.Response(429, headers={"retry-after": delays[name]}, stream=streams[name])
        return httpx.Response(200, content=b"image")

    async with MediaDownloadSession(
        transport=httpx.MockTransport(handler), sleep=clock.sleep
    ) as fetch:
        tasks = [asyncio.create_task(fetch(f"{MEDIA_URL}/{name}"), name=name) for name in streams]
        try:
            assert [await clock.next_sleep() for _ in range(2)] == [
                ("first", 0.125),
                ("extend", 0.125),
            ]
            clock.advance(0.125)
            assert [await asyncio.wait_for(started.get(), 2) for _ in range(2)] == [
                "first",
                "extend",
            ]
            tasks.append(asyncio.create_task(fetch(f"{MEDIA_URL}/waiter"), name="waiter"))
            assert await clock.next_sleep() == ("waiter", 0.375)
            clock.advance(0.125)
            releases["first"].set()
            await asyncio.wait_for(streams["first"].closing.wait(), 2)
            clock.advance(0.25)
            assert await clock.next_sleep() == ("waiter", 12.125)
            clock.advance(0.5)
            releases["extend"].set()
            await asyncio.wait_for(streams["extend"].closing.wait(), 2)
            await asyncio.wait_for(cancel_all(tasks[:2]), 2)
            clock.advance(11.25)
            await checkpoint()
            assert len(visited) == 2
            clock.advance(0.375)
            assert await clock.next_sleep() == ("waiter", 8.75)
            assert sample.call_count == 3
            clock.advance(8.375)
            await checkpoint()
            assert len(visited) == 2
            clock.advance(0.25)
            await checkpoint()
            assert len(visited) == 2
            clock.advance(0.125)
            assert await asyncio.wait_for(tasks[2], 2) == b"image"
        finally:
            for stream in streams.values():
                stream.release.set()
            await cancel_all(tasks)

    assert visited == [("first", 100.125), ("extend", 100.125), ("waiter", 121.375)]
    assert sample.call_args_list == [call()] * 3
    assert all(stream.closed for stream in streams.values())
    assert clock.pending == []


async def test_long_cooldown_published_during_jitter_aborts_before_any_send(
    monkeypatch, media_clock
):
    clock = media_clock
    sample = Mock(side_effect=[0.125, 0.375])
    monkeypatch.setattr(transport, "media_request_delay", sample)
    stream = HeldCloseStream()
    release_rate = asyncio.Event()
    started = asyncio.Event()
    visited = []

    async def handler(request):
        visited.append((request.url.path, clock.current))
        if request.url.path != "/rate":
            pytest.fail("An aborted jitter waiter sent a request")
        started.set()
        await release_rate.wait()
        return httpx.Response(429, headers={"retry-after": "301"}, stream=stream)

    async with MediaDownloadSession(
        transport=httpx.MockTransport(handler), sleep=clock.sleep
    ) as fetch:
        tasks = [asyncio.create_task(fetch(f"{MEDIA_URL}/rate"), name="rate")]
        try:
            assert await clock.next_sleep() == ("rate", 0.125)
            clock.advance(0.125)
            await asyncio.wait_for(started.wait(), 2)
            tasks.append(asyncio.create_task(fetch(f"{MEDIA_URL}/waiter"), name="waiter"))
            assert await clock.next_sleep() == ("waiter", 0.375)
            clock.advance(0.125)
            release_rate.set()
            await asyncio.wait_for(stream.closing.wait(), 2)
            clock.advance(0.25)
            with pytest.raises(AdapterError, match="rate_limited") as error:
                await asyncio.wait_for(tasks[1], 2)
            assert error.value.retry_after == 301
            clock.advance(1_000)
            with pytest.raises(AdapterError, match="rate_limited"):
                await asyncio.wait_for(fetch(f"{MEDIA_URL}/later"), 2)
        finally:
            stream.release.set()
            await cancel_all(tasks)

    assert visited == [("/rate", 100.125)]
    assert sample.call_count == 2
    assert stream.closed
    assert clock.pending == []
    assert clock.sleeps.empty()
