import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pixchive_browser.api import Status
from pixchive_browser.config import HOME, LOGIN
from pixchive_browser.service import BrowserService, ProfileOccupied, Unavailable

from .conftest import FakeFactory, connect, cookie


def track_browser(service, monkeypatch):
    calls = {}
    targets = [
        (
            service.factory.context,
            ("cookies", "storage_state", "set_storage_state", "add_cookies", "new_page"),
        ),
        (service.factory, ("start", "reset")),
        *((page, ("goto", "evaluate", "bring_to_front")) for page in service.factory.context.pages),
    ]
    for index, (target, names) in enumerate(targets):
        for name in names:
            spy = AsyncMock(wraps=getattr(target, name))
            monkeypatch.setattr(target, name, spy)
            calls[f"{index}.{name}"] = spy
    save = Mock(wraps=service.store.save)
    monkeypatch.setattr(service.store, "save", save)
    calls["save"] = save
    return calls


def assert_quiet(calls):
    for call in calls.values():
        call.assert_not_called()


def reset_calls(calls):
    for call in calls.values():
        call.reset_mock()


def test_login_pending_defaults_false(service):
    status = service.status()
    assert status["login_pending"] is False
    assert status["login_generation"] == 0
    del status["login_pending"]
    del status["login_generation"]
    assert Status.model_validate(status).login_pending is False
    assert Status.model_validate(status).login_generation == 0


async def test_first_open_visits_login_once_without_inspecting_or_saving(service, monkeypatch):
    calls = track_browser(service, monkeypatch)
    page = service.factory.context.pages[0]
    assert (await service.open())["login_pending"] is True
    assert page.visits == [LOGIN]
    page.goto.assert_awaited_once_with(LOGIN, wait_until="domcontentloaded")
    page.evaluate.assert_not_called()
    service.factory.context.cookies.assert_not_called()
    service.factory.context.storage_state.assert_not_called()
    calls["save"].assert_not_called()
    reset_calls(calls)
    await service.open()
    await service.close()
    assert_quiet(calls)


@pytest.mark.parametrize("connected", [False, True])
@pytest.mark.parametrize("managed", [False, True])
async def test_manual_input_and_popups_survive_many_ticks_and_view_reconnects(
    settings, monkeypatch, connected, managed
):
    service = BrowserService(
        replace(settings, reauth_seconds=0, save_seconds=0, managed_navigation=managed),
        factory=FakeFactory(),
    )
    if connected:
        await connect(service)
    await service.open()
    context = service.factory.context
    page = context.pages[0]
    page.input_value = "unfinished-login-input"
    popup = await context.new_page()
    popup.url = "https://identity.example.test/challenge"
    popup.input_value = "unfinished-popup-input"
    blank = await context.new_page()
    blank.input_value = "untouched"
    context.origins = [{"origin": "https://accounts.pixiv.net", "localStorage": []}]
    profile = copy.deepcopy((context.cookie_data, context.origins))
    before = service.settings.state_file.read_bytes() if connected else None
    pages = list(context.pages)
    calls = track_browser(service, monkeypatch)
    for _ in range(5):
        async with service.view():
            assert service.status()["viewing"] is True
            await service.open()
            for _ in range(20):
                await service.tick()
        assert service.status()["viewing"] is False
        for _ in range(20):
            await service.tick()
        assert (await service.reauth())["login_pending"] is True
        with pytest.raises(ProfileOccupied):
            await service.seed("must-not-replace-input")
    assert service.status()["login_pending"] is True
    assert context.pages == pages
    assert [item.input_value for item in pages] == [
        "unfinished-login-input",
        "unfinished-popup-input",
        "untouched",
    ]
    assert (context.cookie_data, context.origins) == profile
    if before is None:
        assert not service.settings.state_file.exists()
    else:
        assert service.settings.state_file.read_bytes() == before
    await service.close()
    assert_quiet(calls)


@pytest.mark.parametrize(
    "url", [HOME, LOGIN, "https://accounts.pixiv.net/mfa", "https://identity.example.test/oauth"]
)
async def test_open_keeps_any_existing_nonblank_live_page(service, monkeypatch, url):
    await connect(service)
    context = service.factory.context
    page = context.pages[0]
    page.url = url
    blank = await context.new_page()
    closed = await context.new_page()
    closed.closed = True
    calls = track_browser(service, monkeypatch)
    result = await service.open()
    assert result["login_pending"] is True
    assert result["state"] == "connected"
    assert page.url == url and blank.url == "about:blank"
    assert_quiet(calls)
    await service.close()


@pytest.mark.parametrize("action", ["open", "login"])
async def test_manual_claim_invalidates_completion_before_waiting_for_lock(
    service, monkeypatch, action
):
    await connect(service)
    await service.open()
    await service.complete()
    assert service._completion_ready
    calls = track_browser(service, monkeypatch)
    async with service.lock:
        request = asyncio.create_task(getattr(service, action)())
        await asyncio.sleep(0)
        assert service.status()["login_pending"] is True
        assert service._completion_ready is False
        await asyncio.wait_for(service.tick(), timeout=0.1)
        assert (await asyncio.wait_for(service.reauth(), timeout=0.1))["login_pending"] is True
        with pytest.raises(ProfileOccupied):
            await asyncio.wait_for(service.seed("occupied"), timeout=0.1)
        assert_quiet(calls)
        # Older work holding the lock must not publish readiness for this new interaction.
        service._completion_ready = True
    await request
    assert service._completion_ready is False
    reset_calls(calls)
    with pytest.raises(Unavailable, match="^Browser unavailable$"):
        await service.resume(service.status()["login_generation"])
    assert_quiet(calls)
    await service.close()


async def test_complete_inspects_and_encrypts_once_then_resume_only_changes_flags(
    service, monkeypatch
):
    await service.open()
    context = service.factory.context
    page = context.pages[0]
    page.url = HOME
    context.cookie_data = [cookie("explicit-manual-cookie")]
    context.origins = [
        {"origin": HOME.rstrip("/"), "localStorage": [{"name": "sso", "value": "sentinel"}]}
    ]
    service._navigation_failed = True  # The user has recovered a previous failed navigation.
    calls = track_browser(service, monkeypatch)
    result = await service.complete()
    assert result["state"] == "connected"
    assert result["login_pending"] is True
    assert result["cookie_available"] is True
    assert service.credential() == "explicit-manual-cookie"
    context.cookies.assert_awaited_once_with([HOME])
    page.evaluate.assert_awaited_once()
    context.storage_state.assert_awaited_once_with(indexed_db=True)
    calls["save"].assert_called_once()
    page.goto.assert_not_called()
    page.bring_to_front.assert_not_called()
    context.new_page.assert_not_called()
    assert service.store.load() == {"cookies": context.cookie_data, "origins": context.origins}
    assert b"explicit-manual-cookie" not in service.settings.state_file.read_bytes()
    reset_calls(calls)
    for _ in range(100):
        await service.tick()
    async with service.view():
        assert (await service.reauth())["login_pending"] is True
    assert service.status()["login_pending"] is True
    assert_quiet(calls)
    result = await service.resume(service.status()["login_generation"])
    assert result["state"] == "connected"
    assert result["login_pending"] is False
    assert service._completion_ready is False
    assert_quiet(calls)
    with pytest.raises(Unavailable):
        await service.resume(service.status()["login_generation"])
    assert_quiet(calls)
    await service.close()


async def test_completed_pending_login_is_not_snapshotted_again_on_shutdown(service, monkeypatch):
    await connect(service)
    await service.open()
    calls = track_browser(service, monkeypatch)
    assert (await service.complete())["login_pending"] is True
    encrypted = service.settings.state_file.read_bytes()
    await service.close()
    service.factory.context.cookies.assert_awaited_once()
    service.factory.context.pages[0].evaluate.assert_awaited_once()
    service.factory.context.storage_state.assert_awaited_once_with(indexed_db=True)
    calls["save"].assert_called_once()
    assert service.settings.state_file.read_bytes() == encrypted


@pytest.mark.parametrize("action", ["open", "login"])
async def test_stale_resume_cannot_release_a_newly_completed_interaction(
    service, monkeypatch, action
):
    await connect(service)
    opened = await service.open()
    old_completion = await service.complete()
    assert old_completion["login_generation"] == opened["login_generation"]
    newer = await getattr(service, action)()
    assert newer["login_generation"] == old_completion["login_generation"] + 1
    service.factory.context.pages[0].url = HOME  # Synthetic new user login finishes.
    current = await service.complete()
    assert current["state"] == "connected"
    calls = track_browser(service, monkeypatch)
    with pytest.raises(Unavailable, match="^Browser unavailable$"):
        await service.resume(old_completion["login_generation"])
    assert service.status()["login_pending"] is True
    assert service._completion_ready is True  # A stale request cannot consume the current result.
    result = await service.resume(current["login_generation"])
    assert result["login_pending"] is False
    assert result["login_generation"] == current["login_generation"]
    assert_quiet(calls)
    await service.close()


@pytest.mark.parametrize(
    "outcome", ["form", "challenge", "guest", "unknown", "no_cookie", "blank", "closed"]
)
async def test_failed_complete_preserves_profile_input_and_pending(service, monkeypatch, outcome):
    await connect(service)
    await service.login()
    context = service.factory.context
    page = context.pages[0]
    page.input_value = "still-typing"
    page.url = HOME
    page.snapshot = {"identity_known": True, "authenticated": True}
    if outcome in {"form", "challenge"}:
        page.snapshot["login_form" if outcome == "form" else "challenge"] = True
    elif outcome == "guest":
        page.snapshot["authenticated"] = False
    elif outcome == "unknown":
        page.snapshot = {}
    elif outcome == "no_cookie":
        context.cookie_data = []
    elif outcome == "blank":
        page.url = "about:blank"
    else:
        page.closed = True
    profile = copy.deepcopy((context.cookie_data, context.origins))
    encrypted = service.settings.state_file.read_bytes()
    calls = track_browser(service, monkeypatch)
    result = await service.complete()
    assert result["state"] == "needs_login"
    assert result["login_pending"] is True
    assert result["cookie_available"] is False
    assert service.credential() is None
    assert page.input_value == "still-typing"
    assert (context.cookie_data, context.origins) == profile
    assert service.settings.state_file.read_bytes() == encrypted
    assert len(context.pages) == 1
    context.cookies.assert_awaited_once_with([HOME])
    assert page.evaluate.await_count == (0 if outcome in {"blank", "closed"} else 1)
    context.storage_state.assert_not_called()
    context.set_storage_state.assert_not_called()
    context.new_page.assert_not_called()
    page.goto.assert_not_called()
    calls["save"].assert_not_called()
    reset_calls(calls)
    with pytest.raises(Unavailable):
        await service.resume(service.status()["login_generation"])
    for _ in range(100):
        await service.tick()
    await service.close()
    assert_quiet(calls)


@pytest.mark.parametrize("stage", ["fresh", "pending", "reopened", "relogin", "disconnected"])
async def test_resume_requires_current_successful_explicit_completion(service, monkeypatch, stage):
    if stage != "fresh":
        await connect(service)
        await service.open()
        if stage != "pending":
            await service.complete()
            await getattr(
                service,
                {"reopened": "open", "relogin": "login", "disconnected": "disconnect"}[stage],
            )()
    calls = track_browser(service, monkeypatch)
    with pytest.raises(Unavailable, match="^Browser unavailable$"):
        await service.resume(service.status()["login_generation"])
    assert service.status()["login_pending"] is (stage not in {"fresh", "disconnected"})
    assert service._completion_ready is False
    assert_quiet(calls)
    await service.close()


@pytest.mark.parametrize("operation", ["tick", "reauth", "seed"])
async def test_queued_background_operations_honor_new_manual_claim(service, monkeypatch, operation):
    await connect(service)
    calls = track_browser(service, monkeypatch)
    async with service.lock:
        args = ("must-not-seed",) if operation == "seed" else ()
        background = asyncio.create_task(getattr(service, operation)(*args))
        await asyncio.sleep(0)
        manual = asyncio.create_task(service.open())
        await asyncio.sleep(0)
        assert service.status()["login_pending"] is True
    if operation == "seed":
        with pytest.raises(ProfileOccupied):
            await background
    else:
        await background
    await manual
    assert_quiet(calls)
    await service.close()


@pytest.mark.parametrize("stage", ["evaluate", "storage_state"])
async def test_new_open_during_complete_cannot_enable_resume(service, monkeypatch, stage):
    await connect(service)
    await service.open()
    target = (
        service.factory.context if stage == "storage_state" else service.factory.context.pages[0]
    )
    original = getattr(target, stage)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(target, stage, blocked)
    completion = asyncio.create_task(service.complete())
    await asyncio.wait_for(started.wait(), timeout=1)
    manual = asyncio.create_task(service.open())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(Unavailable):
        await completion
    await manual
    with pytest.raises(Unavailable):
        await service.resume(service.status()["login_generation"])
    assert service.status()["login_pending"] is True
    assert service._completion_ready is False
    await service.close()


async def test_manual_claim_stops_inflight_tick_after_cookie_read(service, monkeypatch):
    await connect(service)
    calls = track_browser(service, monkeypatch)
    context = service.factory.context
    cookies = context.cookies
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked(*args):
        started.set()
        await release.wait()
        return await cookies(*args)

    monkeypatch.setattr(context, "cookies", blocked)
    tick = asyncio.create_task(service.tick())
    await asyncio.wait_for(started.wait(), timeout=1)
    manual = asyncio.create_task(service.open())
    await asyncio.sleep(0)
    release.set()
    await tick
    await manual
    cookies.assert_awaited_once()
    cookies.reset_mock()
    assert_quiet(calls)
    await service.close()


async def test_pending_boot_failure_does_not_trigger_background_restart(service, monkeypatch):
    service.factory.failure = RuntimeError("synthetic boot failure")
    with pytest.raises(Unavailable):
        await service.open()
    service.factory.failure = None
    calls = track_browser(service, monkeypatch)
    for _ in range(100):
        await service.tick()
    assert (await service.reauth())["login_pending"] is True
    with pytest.raises(ProfileOccupied):
        await service.seed("not-allowed")
    with pytest.raises(Unavailable):
        await service.resume(service.status()["login_generation"])
    await service.close()
    assert_quiet(calls)


async def test_automatic_bounded_sso_reauth_still_works_after_resume(settings, monkeypatch):
    service = BrowserService(replace(settings, reauth_seconds=0), factory=FakeFactory())
    await connect(service)
    await service.open()
    await service.complete()
    await service.resume(service.status()["login_generation"])
    context = service.factory.context
    sso = cookie("remaining-sso", name="account-sso", domain="accounts.pixiv.net")
    context.cookie_data = [sso]
    context.origins = [{"origin": "https://accounts.pixiv.net", "localStorage": []}]
    page = context.pages[0]
    page.visits.clear()

    async def navigate(url, **_options):
        page.visits.append(url)
        page.url = HOME
        page.snapshot = {"identity_known": True, "authenticated": url == LOGIN}
        if url == LOGIN:
            context.cookie_data.append(cookie("automatic-cookie-after-resume"))
        return SimpleNamespace(status=200)

    monkeypatch.setattr(page, "goto", navigate)
    await service.tick()
    assert page.visits == [HOME, LOGIN]
    assert service.credential() == "automatic-cookie-after-resume"
    assert service.status()["login_pending"] is False
    assert context.cookie_data[0] == sso
    assert service.store.load()["cookies"] == context.cookie_data
    await service.close()
