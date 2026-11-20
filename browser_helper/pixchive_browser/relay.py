import asyncio
import contextlib

import anyio
from starlette.websockets import WebSocketDisconnect

MAX_FRAME = 1024 * 1024
TCP_CHUNK = 64 * 1024


class RelayClosed(Exception):
    def __init__(self, code: int):
        self.code = code


async def connect_vnc():
    # Intentionally fixed: never connect to a caller-supplied host or port.
    return await asyncio.open_connection("127.0.0.1", 5900, limit=MAX_FRAME)


async def relay(websocket, *, connect=connect_vnc) -> None:
    writer = None
    pumps = []
    close_code = 1000
    try:
        async with asyncio.timeout(5):
            reader, writer = await connect()
        offered = websocket.scope.get("subprotocols", [])
        if offered and "binary" not in offered:
            raise RelayClosed(1002)
        await websocket.accept(subprotocol="binary" if "binary" in offered else None)

        async def tcp_to_ws():
            # No readiness read: the very first RFB banner is forwarded without consuming it.
            while data := await reader.read(TCP_CHUNK):
                async with asyncio.timeout(15):
                    await websocket.send_bytes(data)

        async def ws_to_tcp():
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                data = message.get("bytes")
                if data is None:
                    raise RelayClosed(1003)
                if len(data) > MAX_FRAME:
                    raise RelayClosed(1009)
                writer.write(data)
                async with asyncio.timeout(15):
                    await writer.drain()

        pumps = [asyncio.create_task(tcp_to_ws()), asyncio.create_task(ws_to_tcp())]
        done, _pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except asyncio.CancelledError:
        raise
    except RelayClosed as error:
        close_code = error.code
    except WebSocketDisconnect:
        pass
    except Exception:
        close_code = 1011
    finally:
        # Release TCP synchronously even if the ASGI cancellation scope is already cancelled.
        if writer is not None:
            writer.close()
        for task in pumps:
            if not task.done():
                task.cancel()
        with anyio.CancelScope(shield=True):
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            if writer is not None:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(2):
                        await writer.wait_closed()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(2):
                    await websocket.close(code=close_code)
