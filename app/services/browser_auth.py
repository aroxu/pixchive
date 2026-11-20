"""Automatic reauthentication using an isolated, persistent login browser.

Only the worker imports verified credentials. HTTP/WS routes expose state and pixels,
never the browser's cookies. State in Setting contains no credentials or account IDs.
"""

import asyncio
import contextlib
import hashlib
import json
import math
import re
import secrets
import uuid

from app.adapters.pixiv.contracts import AdapterError
from app.adapters.pixiv.live import CookiePixivAdapter
from app.adapters.pixiv.web import COOKIE_PATTERN
from app.core.db import db
from app.core.errors import DomainError
from app.core.time import now
from app.models.entities import Credential, Lease, PixivAccount
from app.services import accounts, auth, jobs, library
from app.services.browser_client import BrowserClient

STATE_KEY = "pixiv_browser_state"
BINDING_KEY = "pixiv_browser_binding"
LEASE_KEY = "pixiv-browser-auth"
VIEW_CHECK_SECONDS = 5


def cached():
    try:
        value = json.loads(library.get_setting(STATE_KEY, "{}"))
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def update_state(**changes):
    value = cached()
    value.update(changes)
    library.set_setting(STATE_KEY, json.dumps(value))
    return value


def bound():
    # Enabling the optional helper reuses a previously registered cookie once.
    # Explicit manual credential changes/disconnection disable this binding.
    selected = library.get_setting(BINDING_KEY, "auto")
    if selected != "auto":
        return selected == "enabled"
    return Credential.select().where(Credential.kind == "pixiv_cookie").exists()


def public_state(settings, value=None):
    value = cached() if value is None else value
    enabled = bool(settings.browser_url)
    active = enabled and bound()
    pending = active and value.get("login_pending", False)
    state = value.get("state", "starting") if active else "disabled"
    if pending and state not in {"reauthenticating", "unavailable", "error"}:
        state = "awaiting_confirmation"
    return {
        "enabled": enabled,
        "state": state,
        "last_checked": value.get("last_checked"),
        "last_synced": value.get("last_synced"),
        "auto_reauth": active,
        "login_pending": bool(pending),
        "view_available": active and value.get("available", False),
        "message_code": value.get("message_code") if enabled else None,
    }


async def status(settings, client_factory=BrowserClient):
    if not settings.browser_url or not bound():
        return public_state(settings)
    value = cached()
    try:
        remote = await client_factory(settings).status()
        value["available"] = True
        value["login_pending"] = value.get("login_pending", False) or remote.get(
            "login_pending", False
        )
        # A helper's cookie alone is never proof that Pixchive has verified the account.
        if (
            value["login_pending"]
            and not value.get("requested")
            and value.get("state") not in {"error", "unavailable"}
        ):
            value["state"] = "awaiting_confirmation"
        elif remote["state"] in {"needs_login", "error", "reauthenticating", "starting"}:
            value["state"] = remote["state"]
        elif value.get("state") not in {"connected", "error"}:
            value["state"] = "reauthenticating"
    except DomainError:
        value.update(state="unavailable", available=False, message_code="browser_unavailable")
    return public_state(settings, value)


def pause(settings):
    if settings.browser_url:
        library.set_setting(BINDING_KEY, "disabled")
        update_state(
            state="disabled",
            requested=None,
            next_check=0,
            message_code=None,
            login_pending=False,
            interaction_id=None,
            check_only=False,
        )


def store_manual_credential(settings, cookie):
    # Serialize the user's explicit choice against browser adoption in other processes.
    with db.atomic("IMMEDIATE"):
        result = accounts.store_credential(settings, cookie)
        pause(settings)
        return result


async def disconnect(settings, client_factory=BrowserClient):
    with db.atomic("IMMEDIATE"):
        pause(settings)
        accounts.disconnect()
    if settings.browser_url:
        with contextlib.suppress(DomainError):
            await client_factory(settings).request("POST", "/disconnect")


def request_reauth(settings):
    if not settings.browser_url:
        raise DomainError("browser_disabled", "로그인 브라우저가 연결되지 않았습니다.", 409)
    if cached().get("login_pending"):
        raise DomainError("login_in_progress", "로그인을 마친 뒤 로그인 완료를 눌러 주세요.", 409)
    library.set_setting(BINDING_KEY, "enabled")
    value = update_state(
        state="reauthenticating",
        requested=str(uuid.uuid4()),
        check_only=False,
        next_check=0,
        message_code=None,
    )
    return public_state(settings, value)


async def open_browser(settings, client_factory=BrowserClient, *, login=False):
    if not settings.browser_url:
        raise DomainError("browser_disabled", "로그인 브라우저가 연결되지 않았습니다.", 409)
    if shared_retry_at() > now():
        raise DomainError("rate_limited", "Pixiv 요청 대기 시간이 지난 뒤 다시 시도해 주세요.", 429)
    library.set_setting(BINDING_KEY, "enabled")
    update_state(
        state="awaiting_confirmation",
        login_pending=True,
        interaction_id=str(uuid.uuid4()),
        requested=None,
        check_only=False,
        next_check=now() + settings.browser_refresh_seconds,
        retry_at=0,
        message_code=None,
    )
    client = client_factory(settings)
    remote = await client.status()
    if not remote["cookie_available"]:
        try:
            cookie, _ = accounts.load_credential(settings)
        except DomainError as error:
            if error.code == "key_unavailable":
                raise
        else:
            await client.request("POST", "/seed", {"cookie": cookie})
            cookie = None
    await client.request("POST", "/login" if login else "/open")
    update_state(available=True)
    return await status(settings, client_factory)


async def complete_login(settings, client_factory=BrowserClient):
    if not settings.browser_url:
        raise DomainError("browser_disabled", "로그인 브라우저가 연결되지 않았습니다.", 409)
    value = cached()
    if not bound() or not value.get("login_pending"):
        raise DomainError("login_not_started", "로그인 브라우저를 먼저 열어 주세요.", 409)
    if value.get("requested"):
        return public_state(settings, value)
    if shared_retry_at() > now():
        raise DomainError("rate_limited", "Pixiv 요청 대기 시간이 지난 뒤 다시 시도해 주세요.", 429)
    remote = await client_factory(settings).request("POST", "/complete")
    with db.atomic("IMMEDIATE"):
        current = cached()
        if not bound() or current.get("interaction_id") != value.get("interaction_id"):
            return public_state(settings, current)
        if current.get("requested"):
            return public_state(settings, current)
        if remote.get("state") != "connected" or not remote.get("cookie_available"):
            value = update_state(
                state="awaiting_confirmation",
                login_pending=True,
                message_code="login_not_completed",
                available=True,
            )
        else:
            if type(remote.get("login_generation")) is not int:
                raise DomainError(
                    "browser_unavailable", "로그인 브라우저 상태를 다시 확인해 주세요.", 503
                )
            value = update_state(
                state="reauthenticating",
                requested=str(uuid.uuid4()),
                check_only=True,
                completion_generation=remote.get("login_generation"),
                login_pending=True,
                retry_at=0,
                message_code=None,
                available=True,
            )
    return public_state(settings, value)


def claim():
    owner = str(uuid.uuid4())
    with db.atomic("IMMEDIATE"):
        Lease.insert(
            key=LEASE_KEY, owner=owner, expires_at=now() + 120
        ).on_conflict_ignore().execute()
        changed = (
            Lease.update(owner=owner, expires_at=now() + 120)
            .where((Lease.key == LEASE_KEY) & (Lease.expires_at < now()))
            .execute()
        )
        row = Lease.get_by_id(LEASE_KEY)
        return owner if changed or row.owner == owner else None


def release(owner):
    Lease.delete().where((Lease.key == LEASE_KEY) & (Lease.owner == owner)).execute()


def current_snapshot(settings):
    row = Credential.get_or_none(Credential.account == 1)
    expected = row.ciphertext if row else None
    cookie = None
    if row and row.kind == "pixiv_cookie":
        cookie, _ = accounts.load_credential(settings)
    return cookie, expected


def shared_retry_at():
    try:
        return int(library.get_setting("pixiv_retry_at", "0"))
    except (ValueError, TypeError):
        return 0


def record_cooldown(until):
    if type(until) is int and until > now():
        with db.atomic("IMMEDIATE"):
            library.set_setting("pixiv_retry_at", str(max(until, shared_retry_at())))


def generation(value):
    # Keep only an irreversible generation marker in non-secret worker state.
    return hashlib.sha256(value.encode()).hexdigest() if value is not None else None


def operation_current(value):
    current = cached()
    return (
        bound()
        and current.get("interaction_id") == value.get("interaction_id")
        and current.get("requested") == value.get("requested")
        and bool(current.get("login_pending")) == bool(value.get("login_pending"))
    )


def worker_state(value, **changes):
    # A new login window/manual choice always wins over an in-flight worker result.
    with db.atomic("IMMEDIATE"):
        if not operation_current(value):
            return None
        return update_state(**changes)


async def run_once(settings, client_factory=BrowserClient, adapter_factory=CookiePixivAdapter):
    if not settings.browser_url or not bound():
        return
    value = cached()
    confirm = bool(value.get("requested")) and value.get("check_only") is True
    if value.get("login_pending") and not confirm:
        return
    cooldown = shared_retry_at()
    if cooldown > now():
        worker_state(value, state="error", message_code="rate_limited", retry_at=cooldown)
        return
    account = PixivAccount.get_or_none(PixivAccount.id == 1)
    # Cookie changes in the browser are checked every loop; navigation/validation are paced.
    force = bool(value.get("requested"))
    expired = account is not None and account.status == "auth_expired"
    if now() < value.get("retry_at", 0) and not force:
        return
    owner = claim()
    if not owner:
        return
    expected = None
    navigate = False
    try:
        async with asyncio.timeout(90):
            existing, expected = current_snapshot(settings)
            client = client_factory(settings)
            remote = await client.status()
            if remote.get("login_pending") and not confirm:
                worker_state(
                    value,
                    state="awaiting_confirmation",
                    login_pending=True,
                    requested=None,
                    available=True,
                )
                return
            if remote.get("retry_at", 0) > now():
                record_cooldown(remote["retry_at"])
                worker_state(
                    value, state="error", message_code="rate_limited", retry_at=remote["retry_at"]
                )
                return
            if not confirm and not remote["cookie_available"] and existing:
                await client.request("POST", "/seed", {"cookie": existing})
                remote = await client.status()
            due = now() >= value.get("next_check", 0)
            restored_unvisited = (
                remote["state"] == "needs_login" and remote.get("last_checked") is None
            )
            # A previous needs_login result has a navigation cooldown, while a browser
            # login completing still gets picked up by the credential poll below.
            navigate = not confirm and (
                force
                or (
                    (due or restored_unvisited or (expired and value.get("state") != "needs_login"))
                    and not remote.get("viewing", False)
                )
            )
            if navigate:
                worker_state(value, state="reauthenticating", available=True)
                await client.request("POST", "/reauth")
                remote = await client.status()
                if remote.get("retry_at", 0) > now():
                    record_cooldown(remote["retry_at"])
                    worker_state(
                        value,
                        state="error",
                        message_code="rate_limited",
                        requested=None,
                        retry_at=remote["retry_at"],
                    )
                    return
            candidate = await client.credential()
            if not operation_current(value):
                return
            if not candidate:
                worker_state(
                    value,
                    state=(
                        "awaiting_confirmation"
                        if confirm
                        else remote["state"]
                        if remote["state"] != "connected"
                        else "needs_login"
                    ),
                    available=True,
                    requested=None,
                    last_checked=now(),
                    next_check=(
                        now() + settings.browser_refresh_seconds
                        if navigate
                        else value.get("next_check", now())
                    ),
                    message_code=(
                        "login_not_completed"
                        if confirm
                        else "login_required"
                        if remote["state"] == "needs_login"
                        else None
                    ),
                    retry_at=0,
                )
                return
            if not re.fullmatch(COOKIE_PATTERN, candidate):
                raise DomainError("invalid_cookie", "로그인 상태를 확인해 주세요.", 409)
            unchanged = existing is not None and secrets.compare_digest(existing, candidate)
            verified_generation = value.get("verified_candidate") == generation(
                candidate
            ) and value.get("verified_credential") == generation(expected)
            if (
                (unchanged or verified_generation)
                and account
                and account.status == "connected"
                and not navigate
                and not confirm
            ):
                worker_state(
                    value, state="connected", available=True, message_code=None, retry_at=0
                )
                jobs.resume_waiting_auth()
                return
            if not navigate and remote["state"] != "connected":
                return
            worker_state(value, state="reauthenticating", available=True)
            rotation = [candidate]

            async def on_cookie(cookie, pixiv_id):
                rotation[0] = cookie

            def on_retry(error, delay):
                if not operation_current(value):
                    raise DomainError("credential_changed", "로그인 요청이 변경되었습니다.", 409)
                if error.code == "rate_limited":
                    record_cooldown(now() + math.ceil(delay))
                    worker_state(
                        value,
                        state="error",
                        message_code="rate_limited",
                        retry_at=shared_retry_at(),
                    )

            async with adapter_factory(candidate, interval=1.0) as adapter:
                adapter.on_cookie = on_cookie
                adapter.on_retry = on_retry
                pixiv_id = await adapter.validate()
            # User disconnection/manual replacement during network validation wins.
            with db.atomic("IMMEDIATE"):
                if not operation_current(value):
                    return
                adopted = accounts.adopt_verified_cookie(settings, rotation[0], pixiv_id, expected)
            jobs.resume_waiting_auth()
            if confirm:
                if not operation_current(value):
                    return
                await client.request(
                    "POST", "/resume", {"generation": value.get("completion_generation")}
                )
            worker_state(
                value,
                state="connected",
                login_pending=False,
                check_only=False,
                available=True,
                requested=None,
                last_checked=now(),
                last_synced=now(),
                next_check=now() + settings.browser_refresh_seconds,
                retry_at=0,
                message_code=None,
                verified_candidate=generation(candidate),
                verified_credential=generation(adopted),
            )
    except AdapterError as error:
        if not operation_current(value):
            return
        if error.code == "rate_limited":
            record_cooldown(now() + math.ceil(max(60, error.retry_after or 0)))
        if error.code == "auth_expired":
            # A failed new browser candidate must not invalidate a still-current,
            # different manual cookie. Existing expired credentials stay waiting.
            worker_state(
                value,
                state="needs_login",
                available=True,
                requested=None,
                last_checked=now(),
                next_check=(
                    now() + settings.browser_refresh_seconds
                    if navigate
                    else value.get("next_check", now())
                ),
                message_code="login_required",
                retry_at=now() + 30,
            )
        else:
            worker_state(
                value, state="error", message_code=error.code, requested=None, retry_at=now() + 60
            )
    except DomainError as error:
        if not operation_current(value):
            return
        code = error.code
        if code == "credential_changed":
            # A newer verified generation or explicit user operation won the CAS.
            return
        worker_state(
            value,
            state="unavailable" if code == "browser_unavailable" else "error",
            available=code != "browser_unavailable",
            message_code=code,
            requested=None,
            retry_at=now() + 60,
        )
    except TimeoutError:
        if not operation_current(value):
            return
        cooldown = shared_retry_at()
        limited = cooldown > now()
        worker_state(
            value,
            state="error" if limited else "unavailable",
            available=limited,
            requested=None,
            message_code="rate_limited" if limited else "browser_unavailable",
            retry_at=max(now() + 60, cooldown),
        )
    finally:
        release(owner)


async def loop(ctx):
    while True:
        try:
            await run_once(ctx["settings"])
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never log browser responses, URLs, cookies or raw exception messages.
            update_state(state="error", message_code="browser_unavailable", retry_at=now() + 60)
        await asyncio.sleep(5)


def authorize_view(settings, token, origin, protocols):
    if not settings.browser_url or not bound():
        raise DomainError("browser_disabled", "로그인 브라우저가 연결되지 않았습니다.", 409)
    if not settings.allows_origin(origin):
        raise DomainError("origin_rejected", "요청 출처가 다릅니다.", 403)
    session = auth.require_session(token)
    csrf = next(
        (p.removeprefix("pixchive-csrf.") for p in protocols if p.startswith("pixchive-csrf.")), ""
    )
    if "binary" not in protocols or not secrets.compare_digest(csrf, session.csrf):
        raise DomainError("csrf_failed", "페이지를 새로고침한 뒤 다시 시도해 주세요.", 403)
    return session


async def proxy_view(websocket, settings, token, client_factory=BrowserClient):
    protocols = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    try:
        authorize_view(settings, token, websocket.headers.get("origin"), protocols)
    except DomainError:
        await websocket.close(code=1008)
        return
    try:
        async with client_factory(settings).view() as upstream:
            await websocket.accept(subprotocol="binary")

            async def inbound():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    data = message.get("bytes")
                    if data is None or len(data) > 8 * 1024 * 1024:
                        raise ValueError
                    await upstream.send(data)

            async def outbound():
                async for data in upstream:
                    if not isinstance(data, bytes):
                        raise ValueError
                    await websocket.send_bytes(data)

            async def liveness():
                while True:
                    await asyncio.sleep(VIEW_CHECK_SECONDS)
                    authorize_view(settings, token, websocket.headers.get("origin"), protocols)

            tasks = [asyncio.create_task(fn()) for fn in (inbound, outbound, liveness)]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except DomainError:
        with contextlib.suppress(Exception):
            await websocket.close(code=1008)
    except Exception:
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
