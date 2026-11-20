import asyncio
from functools import partial
from unittest.mock import Mock, call

import httpx
import pytest

from app.adapters.pixiv import transport
from app.adapters.pixiv.contracts import AdapterError


@pytest.fixture
def positive_jitter(monkeypatch):
    monkeypatch.setattr(transport, "media_request_delay", lambda: 0)
    sample = Mock(return_value=0.5)
    monkeypatch.setattr(transport.random, "uniform", sample)
    return sample


async def test_429_retry_after_keeps_server_minimum_and_adds_varying_jitter(positive_jitter):
    positive_jitter.side_effect = [0.25, 0.5, 0.75]
    attempts = 0
    sleeps = []
    callbacks = []

    async def operation():
        nonlocal attempts
        attempts += 1
        transport.check_status(429, {"retry-after": "12"})

    async def sleep(delay):
        sleeps.append(delay)

    with pytest.raises(AdapterError, match="rate_limited") as error:
        await transport.retry(
            operation,
            sleep=sleep,
            on_retry=lambda exc, delay: callbacks.append((exc, delay)),
        )

    assert attempts == 4
    assert sleeps == [12.25, 12.5, 12.75]
    assert [exc.retry_after for exc, _ in callbacks] == [12] * 4
    # Exhaustion still notifies the caller but adds no jitter or fourth sleep.
    assert [delay for _, delay in callbacks] == [*sleeps, 12]
    assert callbacks[-1][0] is error.value
    assert positive_jitter.call_args_list == [call(0, 1)] * 3


@pytest.mark.parametrize(
    "code,server_delay", [("network", None), ("rate_limited", None), ("rate_limited", 0.25)]
)
async def test_retry_jitter_preserves_exponential_floor(code, server_delay, positive_jitter):
    attempts = 0
    sleeps = []
    callbacks = []

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise AdapterError(code, retry_after=server_delay)
        return b"image"

    async def sleep(delay):
        sleeps.append(delay)

    assert (
        await transport.retry(
            operation, sleep=sleep, on_retry=lambda exc, delay: callbacks.append(delay)
        )
        == b"image"
    )
    assert attempts == 4
    assert sleeps == [1.5, 2.5, 4.5]
    assert callbacks == sleeps
    assert positive_jitter.call_args_list == [call(0, 1)] * 3


@pytest.mark.parametrize("server_delay", [300.01, 301, 3600])
async def test_long_retry_after_is_reported_unchanged_without_retry(server_delay, positive_jitter):
    attempts = 0
    sleeps = []
    callbacks = []
    original_error = AdapterError("rate_limited", retry_after=server_delay)

    async def operation():
        nonlocal attempts
        attempts += 1
        raise original_error

    async def sleep(delay):
        sleeps.append(delay)

    with pytest.raises(AdapterError, match="rate_limited") as error:
        await transport.retry(
            operation,
            sleep=sleep,
            on_retry=lambda exc, delay: callbacks.append((exc, delay)),
        )

    assert error.value is original_error
    assert attempts == 1
    assert sleeps == []
    assert callbacks == [(original_error, server_delay)]
    positive_jitter.assert_not_called()


@pytest.mark.parametrize("server_delay,expected_delay", [(12, 12.5), (299.75, 300), (300, 300)])
async def test_jitter_respects_media_cooldown_without_causing_long_limit_abort(
    server_delay, expected_delay, positive_jitter, monkeypatch
):
    current = 100.0
    sleeps = []
    callbacks = []
    requests = []

    async def sleep(delay):
        nonlocal current
        sleeps.append(delay)
        current += delay

    def handler(request):
        requests.append(current)
        if len(requests) == 1:
            return httpx.Response(429, headers={"retry-after": str(server_delay)})
        return httpx.Response(200, content=b"image")

    monkeypatch.setattr(transport, "monotonic", lambda: current)
    monkeypatch.setattr(transport, "retry", partial(transport.retry, sleep=sleep))
    async with transport.MediaDownloadSession(
        transport=httpx.MockTransport(handler),
        sleep=sleep,
        on_retry=lambda exc, delay: callbacks.append((exc.code, delay)),
    ) as fetch:
        assert await fetch("https://i.pximg.net/image") == b"image"
        assert await fetch("https://i.pximg.net/next") == b"image"

    assert sleeps == [expected_delay]
    assert callbacks == [("rate_limited", expected_delay)]
    assert requests == [100, 100 + expected_delay, 100 + expected_delay]
    positive_jitter.assert_called_once_with(0, 1)


@pytest.mark.parametrize("during_sleep", [False, True])
async def test_cancellation_propagates_without_another_attempt(during_sleep, positive_jitter):
    attempts = 0
    sleeps = []
    callbacks = []

    async def operation():
        nonlocal attempts
        attempts += 1
        if not during_sleep:
            raise asyncio.CancelledError
        raise AdapterError("network")

    async def sleep(delay):
        sleeps.append(delay)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await transport.retry(
            operation, sleep=sleep, on_retry=lambda exc, delay: callbacks.append(delay)
        )

    assert attempts == 1
    assert sleeps == ([1.5] if during_sleep else [])
    assert callbacks == sleeps
    assert positive_jitter.call_count == int(during_sleep)
