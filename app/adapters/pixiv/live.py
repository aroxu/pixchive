import asyncio
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from app.adapters.pixiv.contracts import AdapterError, BookmarkListing, RemoteArtwork
from app.adapters.pixiv.transport import retry
from app.adapters.pixiv.web import COOKIE_PATTERN, identifier
from app.core.async_cleanup import settle


class CookiePixivAdapter:
    version = "pixiv-web-v1"

    def __init__(self, cookie: str, interval: float = 1.0):
        self.cookie = cookie
        self.interval = interval
        self.process = None
        self.on_retry = None
        # This callback is private credential transport, never command results.
        self.on_cookie: Callable[[str, str | None], Awaitable[None]] | None = None
        self._pending_cookie = None
        self._account_id = None
        self._command_lock = asyncio.Lock()

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
        try:
            if self.process and self.process.returncode is None:
                self.process.kill()
                await settle(self.process.wait())
        finally:
            self._pending_cookie = None

    async def _exchange(self, data):
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
        if not isinstance(result, dict):
            raise AdapterError("contract")
        if "cookie_rotation" in result:
            cookie = result["cookie_rotation"]
            if not isinstance(cookie, str) or not re.fullmatch(COOKIE_PATTERN, cookie):
                raise AdapterError("contract")
            self._pending_cookie = cookie
        return result

    @staticmethod
    def _result(output):
        if "error" in output:
            code = output["error"]
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
            raise AdapterError(code, output.get("retry_after"))
        if "result" not in output:
            raise AdapterError("contract")
        return output["result"]

    async def _notify_cookie(self, account_id):
        if self._pending_cookie is None or self.on_cookie is None:
            return
        if account_id is None:
            # A session first seen during an unvalidated operation cannot prove
            # continuity with the session that started this adapter.
            raise AdapterError("auth_expired")
        for _ in range(4):
            candidate = self._pending_cookie
            verified = identifier(self._result(await self._exchange({"op": "validate"})))
            if verified != account_id:
                raise AdapterError("auth_expired")
            if self._pending_cookie == candidate:
                await self.on_cookie(candidate, verified)
                self._pending_cookie = None
                return
        # A session that keeps changing during identity checks has no verified
        # final candidate. Do not recurse or persist an earlier candidate.
        raise AdapterError("auth_expired")

    async def command(self, data):
        async def once():
            output = await self._exchange(data)
            # Preserve failures and the existing retry policy before sending any
            # identity request (especially Retry-After on a rotated 429 response).
            # The private candidate remains pending until a successful command.
            result = self._result(output)
            account_id = self._account_id
            if data["op"] == "validate":
                verified = identifier(result)
                if account_id is not None and verified != account_id:
                    raise AdapterError("auth_expired")
                account_id = verified
                # Retain the initial verified identity across transient failures
                # while checking a rotated session.
                self._account_id = account_id
            await self._notify_cookie(account_id)
            self._account_id = account_id
            if self.on_cookie is None:
                self._pending_cookie = None
            return result

        # Rotation verification uses the same private stream. Serialize the whole
        # exchange so an identity check cannot consume another command's response.
        async with self._command_lock:
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
