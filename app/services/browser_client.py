"""Private authenticated transport to the login browser, without credential logging."""

import hashlib
import hmac
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.errors import DomainError

STATES = {"starting", "connected", "reauthenticating", "needs_login", "error"}


class BrowserClient:
    def __init__(self, settings):
        if not settings.browser_url:
            raise DomainError("browser_disabled", "로그인 브라우저가 연결되지 않았습니다.", 409)
        self.url = settings.browser_url
        try:
            key = settings.key_file.read_bytes()
        except OSError:
            raise DomainError("key_unavailable", "인증 보관 키를 확인해 주세요.", 503) from None
        token = hmac.new(key, b"pixchive-browser-control-v1", hashlib.sha256).hexdigest()
        self.headers = {"Authorization": f"Bearer {token}"}

    async def request(self, method, path, payload=None):
        try:
            async with httpx.AsyncClient(
                base_url=self.url,
                headers=self.headers,
                timeout=httpx.Timeout(40 if method == "POST" else 5, connect=3),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.request(method, path, json=payload)
                if path == "/seed" and response.status_code == 409:
                    # An existing login/MFA profile always wins over a seed attempt.
                    return {"occupied": True}
                if response.status_code != 200 or len(response.content) > 65536:
                    raise ValueError
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except (httpx.HTTPError, ValueError):
            # Do not propagate upstream bodies, cookies, Authorization or remote exceptions.
            raise DomainError(
                "browser_unavailable", "로그인 브라우저에 연결할 수 없습니다.", 503
            ) from None

    async def status(self):
        result = await self.request("GET", "/status")
        if result.get("state") not in STATES:
            raise DomainError("browser_unavailable", "로그인 브라우저 상태를 확인해 주세요.", 503)
        checked = result.get("last_checked")
        return {
            "state": result["state"],
            "last_checked": checked if type(checked) is int else None,
            "cookie_available": result.get("cookie_available") is True,
            "viewing": result.get("viewing") is True,
            "login_pending": result.get("login_pending") is True,
            "retry_at": result.get("retry_at") if type(result.get("retry_at")) is int else 0,
        }

    async def credential(self):
        result = await self.request("GET", "/credential")
        cookie = result.get("cookie")
        if cookie is not None and not isinstance(cookie, str):
            raise DomainError("browser_unavailable", "로그인 브라우저 상태를 확인해 주세요.", 503)
        return cookie

    @asynccontextmanager
    async def view(self):
        from websockets.asyncio.client import connect

        parsed = urlsplit(self.url)
        url = urlunsplit(
            ("wss" if parsed.scheme == "https" else "ws", parsed.netloc, "/view", "", "")
        )
        async with connect(
            url,
            additional_headers=self.headers,
            subprotocols=["binary"],
            max_size=8 * 1024 * 1024,
            max_queue=8,
            open_timeout=5,
            close_timeout=2,
            proxy=None,
        ) as socket:
            yield socket
