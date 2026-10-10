import asyncio
import json
import os
import sys

from pydantic import ValidationError

from app.adapters.pixiv.contracts import AdapterError, BookmarkListing, RemoteArtwork
from app.adapters.pixiv.transport import retry
from app.core.async_cleanup import settle


class CookiePixivAdapter:
    version = "pixiv-web-v1"

    def __init__(self, cookie: str, interval: float = 1.0):
        self.cookie = cookie
        self.interval = interval
        self.process = None
        self.on_retry = None

    async def __aenter__(self):
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.core.child_process",
            str(os.getpid()),
            sys.executable,
            "-m",
            "app.adapters.pixiv.bridge",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=16 * 1024 * 1024,
        )
        try:
            await self.command({"op": "open", "cookie": self.cookie, "interval": self.interval})
        except BaseException:
            await self.close()
            raise
        finally:
            self.cookie = None
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.kill()
            await settle(self.process.wait())

    async def command(self, data):
        async def once():
            try:
                self.process.stdin.write(json.dumps(data).encode() + b"\n")
                await self.process.stdin.drain()
                # Each command is one list page or one artwork. Per-request timeout
                # lives in the child, while the worker can terminate it immediately.
                async with asyncio.timeout(300):
                    line = await self.process.stdout.readline()
                result = json.loads(line)
            except (OSError, ValueError, TimeoutError):
                await self.close()
                raise AdapterError("network") from None
            if "error" in result:
                code = result["error"]
                if code not in {
                    "auth_expired",
                    "rate_limited",
                    "network",
                    "contract",
                    "unsupported",
                    "unavailable",
                    "access_denied",
                    "collection_changed",
                }:
                    code = "contract"
                raise AdapterError(code, result.get("retry_after"))
            return result["result"]

        return await retry(once, on_retry=self.on_retry)

    async def validate(self):
        return await self.command({"op": "validate"})

    async def listing(self, account_id, scope, cursor=None):
        result = await self.command(
            {"op": "listing", "account_id": account_id, "scope": scope, "cursor": cursor}
        )
        try:
            return BookmarkListing.model_validate(result)
        except ValidationError:
            raise AdapterError("contract") from None

    async def artwork(self, artwork_id):
        result = await self.command({"op": "artwork", "artwork_id": artwork_id})
        try:
            return RemoteArtwork.model_validate(result)
        except ValidationError:
            raise AdapterError("contract") from None

    async def verify(self, account_id, scope, total, first_bookmark_id):
        await self.command(
            {
                "op": "verify",
                "account_id": account_id,
                "scope": scope,
                "total": total,
                "first": first_bookmark_id,
            }
        )
