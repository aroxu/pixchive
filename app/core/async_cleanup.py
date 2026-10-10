"""Wait for an already-started operation while preserving caller cancellation."""

import asyncio


async def settle(task):
    """Do not pass repeated cancellation into cleanup or an irreversible disk write."""
    task = asyncio.ensure_future(task)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            break
    if interrupted:
        raise asyncio.CancelledError
    return task.result()
