import asyncio
import contextlib

import pytest

from pixchive_browser.relay import MAX_FRAME, relay


class FakeWebSocket:
    def __init__(self, subprotocols=None):
        self.scope = {"subprotocols": ["binary"] if subprotocols is None else subprotocols}
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.protocol = None
        self.closed = None

    async def accept(self, subprotocol=None):
        self.protocol = subprotocol

    async def receive(self):
        return await self.incoming.get()

    async def send_bytes(self, data):
        await self.outgoing.put(data)

    async def close(self, code=1000):
        self.closed = code


async def live_relay(websocket):
    accepted = asyncio.Event()
    closed = asyncio.Event()
    received = []

    async def rfb(reader, writer):
        try:
            writer.write(b"RFB ")
            await writer.drain()
            await asyncio.sleep(0.01)
            writer.write(b"003.008\n")
            await writer.drain()
            accepted.set()
            while chunk := await reader.read(65536):
                received.append(chunk)
                writer.write(chunk)
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            closed.set()

    server = await asyncio.start_server(rfb, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def connect():
        return await asyncio.open_connection("127.0.0.1", port)

    task = asyncio.create_task(relay(websocket, connect=connect))
    await asyncio.wait_for(accepted.wait(), 2)
    return server, task, closed, received


async def test_real_tcp_fragmented_banner_and_bidirectional_stream_cleanup():
    websocket = FakeWebSocket()
    server, task, closed, received = await live_relay(websocket)
    try:
        banner = b""
        while len(banner) < 12:
            banner += await asyncio.wait_for(websocket.outgoing.get(), 2)
        assert banner == b"RFB 003.008\n"
        await websocket.incoming.put({"type": "websocket.receive", "bytes": b"rfb-client-hello"})
        assert await asyncio.wait_for(websocket.outgoing.get(), 2) == b"rfb-client-hello"
        assert b"".join(received) == b"rfb-client-hello"
        await websocket.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(task, 2)
        await asyncio.wait_for(closed.wait(), 2)
        assert websocket.closed == 1000
    finally:
        server.close()
        await server.wait_closed()
        task.cancel()


@pytest.mark.parametrize(
    "message,code",
    [
        ({"type": "websocket.receive", "text": "not-binary"}, 1003),
        ({"type": "websocket.receive", "bytes": b"x" * (MAX_FRAME + 1)}, 1009),
    ],
)
async def test_text_or_oversized_frame_closes_both_sides(message, code):
    websocket = FakeWebSocket()
    server, task, closed, _received = await live_relay(websocket)
    try:
        await websocket.incoming.put(message)
        await asyncio.wait_for(task, 2)
        await asyncio.wait_for(closed.wait(), 2)
        assert websocket.closed == code
    finally:
        server.close()
        await server.wait_closed()
        task.cancel()


async def test_cancellation_closes_tcp_and_cancels_opposite_pump():
    websocket = FakeWebSocket()
    server, task, closed, _received = await live_relay(websocket)
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(closed.wait(), 2)
        assert websocket.closed == 1000
    finally:
        server.close()
        await server.wait_closed()


async def test_upstream_failure_does_not_echo_exception():
    websocket = FakeWebSocket()

    async def fail():
        raise OSError("PRIVATE-SECRET")

    await relay(websocket, connect=fail)
    assert websocket.closed == 1011
