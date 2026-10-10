import asyncio
import contextlib

from app.core.async_cleanup import settle


class Interrupted(Exception):
    pass


async def watch(operation, check):
    task = asyncio.ensure_future(operation)
    try:
        while True:
            check()
            done, _ = await asyncio.wait({task}, timeout=0.2)
            if done:
                value = await task
                check()
                return value
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await settle(task)


async def disk_operation(fn, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A thread must finish its file write before cancellation is acknowledged.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await settle(task)
        raise
