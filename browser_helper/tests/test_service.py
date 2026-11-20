import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pixchive_browser.config import HOME, LOGIN
from pixchive_browser.identity import classify, session_cookie
from pixchive_browser.service import BrowserService, ProfileOccupied, Unavailable, retry_deadline

from .conftest import FakeFactory, connect, cookie


async def test_managed_browser_leaves_navigation_timing_to_account_worker(settings):
    service = BrowserService(
        replace(settings, reauth_seconds=0, managed_navigation=True), factory=FakeFactory()
    )
    await connect(service)
    page = service.factory.context.pages[0]
    before = len(page.visits)
    await service.tick()
    assert len(page.visits) == before
    await service.reauth()
    assert len(page.visits) == before + 1
    await service.close()


async def test_explicit_login_opens_official_form_without_erasing_existing_session(service):
    await connect(service)
    before = list(service.factory.context.cookie_data)
    assert (await service.login())["state"] == "needs_login"
    assert service.factory.context.pages[0].visits[-1] == LOGIN
    assert service.factory.context.cookie_data == before
    await service.close()


@pytest.mark.parametrize("value", ["120", "Tue, 27 Oct 2026 00:02:00 GMT", None, "bad"])
def test_retry_after_deadline_is_not_shortened(monkeypatch, value):
    monkeypatch.setattr("pixchive_browser.service.time.time", lambda: 1793059200)
    monkeypatch.setattr("pixchive_browser.service.random.uniform", lambda *_: 1)
    expected = 1793059321 if value in {"120", "Tue, 27 Oct 2026 00:02:00 GMT"} else 1793059261
    assert retry_deadline(value) == expected


@pytest.mark.parametrize(
    "snapshot,expected",
    [
        ({"url": HOME, "identity_known": True, "authenticated": True}, "connected"),
        ({"url": HOME, "identity_known": True, "authenticated": False}, "needs_login"),
        ({"url": HOME}, "error"),
        ({"url": LOGIN, "identity_known": True, "authenticated": True}, "needs_login"),
        (
            {"url": HOME, "identity_known": True, "authenticated": True, "challenge": True},
            "needs_login",
        ),
        (
            {"url": HOME, "identity_known": True, "authenticated": True, "login_form": True},
            "needs_login",
        ),
        (
            {
                "url": "https://www.pixiv.net/auth/mfa",
                "identity_known": True,
                "authenticated": True,
            },
            "needs_login",
        ),
        ({"url": "https://www.pixiv.net.evil.test", "authenticated": True}, "needs_login"),
    ],
)
def test_identity_evidence_and_interactive_login(snapshot, expected):
    assert classify(snapshot, "test-cookie") == expected


def test_cookie_presence_never_proves_login():
    assert classify({"url": HOME}, "test-cookie") != "connected"
    assert (
        classify({"url": HOME, "identity_known": True, "authenticated": True}, None) != "connected"
    )
    assert (
        classify({"url": HOME, "identity_known": True, "authenticated": True}, "cookie", 429)
        == "error"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"domain": "accounts.pixiv.net"},
        {"path": "/login"},
        {"secure": False},
        {"expires": 1},
        {"value": "secret\ninvalid"},
        {"domain": "www.pixiv.net.evil.test"},
    ],
)
def test_only_unexpired_applicable_session_cookie_is_exported(overrides):
    assert session_cookie([cookie(**overrides)]) is None


def test_conflicting_cookies_are_not_arbitrarily_selected():
    assert session_cookie([cookie("account-a"), cookie("account-b", domain=".pixiv.net")]) is None


async def test_seed_sets_only_first_cookie_and_visits_real_home(service):
    await connect(service)
    assert service.factory.context.added == [
        {
            "name": "PHPSESSID",
            "value": "101_fake-session",
            "domain": "www.pixiv.net",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ]
    assert service.credential() == "101_fake-session"
    with pytest.raises(ProfileOccupied):
        await service.seed("never-overwrite")
    assert service.credential() == "101_fake-session"
    await service.close()


async def test_reauth_recovers_rotated_cookie_through_browser_home(service):
    await connect(service, "old-cookie")
    page = service.factory.context.pages[0]
    page.next_cookie = "renewed-cookie"
    result = await service.reauth()
    assert result["state"] == "connected"
    assert page.visits[-1] == HOME
    assert service.credential() == "renewed-cookie"
    assert service.store.load()["cookies"][0]["value"] == "renewed-cookie"
    assert b"renewed-cookie" not in service.settings.state_file.read_bytes()
    await service.close()


async def test_restore_uses_set_storage_state_with_indexeddb(settings):
    first = BrowserService(settings, factory=FakeFactory())
    await connect(first)
    first.factory.context.origins = [
        {
            "origin": "https://accounts.pixiv.net",
            "localStorage": [{"name": "sso", "value": "test"}],
            "indexedDB": [{"name": "auth", "version": 1, "stores": []}],
        }
    ]
    await first.close()
    second = BrowserService(settings, factory=FakeFactory())
    await second.reauth()
    assert len(second.factory.context.restorations) == 1
    assert second.factory.context.restorations[0]["origins"][0]["indexedDB"][0]["name"] == "auth"
    assert all(
        options == {"indexed_db": True} for options in second.factory.context.storage_options
    )
    with pytest.raises(ProfileOccupied):
        await second.seed("do-not-overwrite-restored-login")
    await second.close()


async def test_captcha_mfa_and_login_do_not_report_success(service):
    await connect(service)
    page = service.factory.context.pages[0]
    page.snapshot["challenge"] = True
    assert (await service.reauth())["state"] == "needs_login"
    assert service.credential() is None
    assert not service.status()["cookie_available"]
    page.snapshot = {"identity_known": True, "authenticated": False, "login_form": True}
    assert (await service.open())["state"] == "needs_login"
    assert page.url == HOME
    visits = len(page.visits)
    await service.open()
    assert len(page.visits) == visits  # Even a form on HOME is not reset.
    await service.login()
    assert page.url == LOGIN
    await service.close()


async def test_open_connected_profile_preserves_page(service):
    await connect(service)
    page = service.factory.context.pages[0]
    visits = list(page.visits)
    assert (await service.open())["state"] == "connected"
    assert page.visits == visits
    await service.close()


async def test_background_does_not_navigate_while_view_is_connected(settings):
    service = BrowserService(replace(settings, reauth_seconds=0), factory=FakeFactory())
    await connect(service)
    page = service.factory.context.pages[0]
    async with service.view():
        visits = len(page.visits)
        page.snapshot["login_form"] = True
        await service.tick()
        assert len(page.visits) == visits
        assert service.viewers == 1
        assert service.status()["viewing"] is True
        page.snapshot["login_form"] = False
        await service.reauth()  # Explicit requests are allowed during a viewing session.
        assert len(page.visits) == visits + 1
        await service.tick()
        assert len(page.visits) == visits + 1
    assert service.viewers == 0
    assert service.status()["viewing"] is False
    await service.tick()
    assert len(page.visits) == visits + 2
    await service.close()


async def test_explicit_completion_precedes_passive_cookie_rotation(service):
    await service.open()
    context = service.factory.context
    page = context.pages[0]
    page.url = HOME
    context.cookie_data = [cookie("from-interactive-login")]
    await service.tick()
    assert service.credential() is None
    assert (await service.complete())["login_pending"] is True
    assert service.credential() == "from-interactive-login"
    await service.resume(service.status()["login_generation"])
    context.cookie_data = [cookie("changed-without-navigation")]
    await service.tick()
    assert service.credential() == "changed-without-navigation"
    assert service.store.load()["cookies"][0]["value"] == "changed-without-navigation"
    await service.close()


async def test_disconnect_erases_state_and_stays_disconnected(service):
    await connect(service)
    session = service.session
    result = await service.disconnect()
    assert result["state"] == "needs_login"
    assert not result["cookie_available"]
    assert session.reset and session.closed
    assert session.context.restorations[-1] == {"cookies": [], "origins": []}
    assert not service.settings.state_file.exists()
    await service.tick()
    assert len(service.factory.sessions) == 1
    assert not service.settings.state_file.exists()
    await service.seed("new-login-after-explicit-reset")
    assert service.credential() == "new-login-after-explicit-reset"
    await service.close()


async def test_corrupt_snapshot_is_not_silently_replaced(service):
    service.settings.state_file.parent.mkdir()
    service.settings.state_file.write_bytes(b"invalid-ciphertext")
    with pytest.raises(Unavailable):
        await service.reauth()
    assert service.status()["state"] == "error"
    assert not service.factory.sessions
    assert service.settings.state_file.read_bytes() == b"invalid-ciphertext"
    await service.close()


async def test_boot_error_can_retry_without_exception_details(service, caplog, capsys):
    service.factory.failure = RuntimeError("PHPSESSID=do-not-log-this")
    with pytest.raises(Unavailable, match="^Browser unavailable$"):
        await service.reauth()
    service.factory.failure = None
    await service.seed("recovered")
    assert service.status()["state"] == "connected"
    assert "do-not-log-this" not in caplog.text + str(capsys.readouterr())
    await service.close()


async def test_navigation_failure_does_not_return_old_credentials(service, caplog):
    await connect(service)
    service.factory.context.pages[0].navigation_error = RuntimeError("PHPSESSID=do-not-log")
    assert (await service.reauth())["state"] == "error"
    assert service.credential() is None
    async with service.lock:
        await service._poll_locked()
    assert service.credential() is None  # Stale logged-in DOM cannot undo a failed visit.
    assert "do-not-log" not in caplog.text
    await service.close()


async def test_supervisor_boot_retry_and_shutdown_do_not_leave_tasks(service):
    await service.start()
    for _ in range(100):
        if service.state != "starting":
            break
        await asyncio.sleep(0.005)
    assert service.state == "needs_login"
    assert service.last_checked is None
    await service.close()
    assert service._supervisor.done()
    assert not service._disk_tasks


async def test_profile_operations_are_serialized(service):
    await connect(service)
    context = service.factory.context
    original = context.pages[0].goto
    active = 0
    maximum = 0

    async def tracked(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        try:
            return await original(*args, **kwargs)
        finally:
            active -= 1

    context.pages[0].goto = tracked
    await asyncio.gather(service.reauth(), service.open(), service.reauth())
    assert maximum == 1
    assert service.last_checked <= int(time.time())
    await service.close()


async def test_vnc_user_navigation_prevents_seed_even_before_cookies_are_set(service):
    async with service.lock:
        await service._ensure_locked()
    service.factory.context.pages[0].url = LOGIN
    with pytest.raises(ProfileOccupied):
        await service.seed("never-overwrite-active-form")
    assert not service.factory.context.added
    await service.tick()
    assert not service._seed_eligible
    assert service.settings.state_file.exists()
    await service.close()


async def test_ssostate_without_php_remains_eligible_for_automatic_home_recovery(settings):
    service = BrowserService(replace(settings, reauth_seconds=0), factory=FakeFactory())
    await connect(service)
    service.factory.context.cookie_data = [
        cookie(
            "remaining-sso",
            name="account-sso",
            domain="accounts.pixiv.net",
        )
    ]
    service.factory.context.origins = [
        {
            "origin": "https://accounts.pixiv.net",
            "localStorage": [{"name": "sso", "value": "existing"}],
        }
    ]
    page = service.factory.context.pages[0]
    async with service.lock:
        await service._poll_locked()
    assert service.state == "needs_login"
    assert service.credential() is None
    page.next_cookie = "new-php-from-existing-sso"
    await service.tick()
    assert page.visits[-1] == HOME
    assert service.credential() == "new-php-from-existing-sso"
    await service.close()


@pytest.mark.parametrize("entry", ["reauth", "tick", "restore"])
async def test_guest_home_uses_official_login_once_with_preserved_sso(settings, monkeypatch, entry):
    service = BrowserService(settings, factory=FakeFactory())
    sso_cookie = cookie("remaining-sso", name="account-sso", domain="accounts.pixiv.net")
    origins = [
        {
            "origin": "https://accounts.pixiv.net",
            "localStorage": [{"name": "sso", "value": "existing"}],
            "indexedDB": [{"name": "auth", "version": 1, "stores": []}],
        }
    ]
    service.store.save({"cookies": [sso_cookie], "origins": origins})
    context = service.factory.context
    page = context.pages[0]

    async def navigate(url, **_options):
        page.visits.append(url)
        assert context.origins == origins
        assert context.cookie_data == [sso_cookie]
        page.url = HOME
        page.snapshot = {"identity_known": True, "authenticated": url == LOGIN}
        if url == LOGIN:
            # Only the official login's normal SSO transition issues a new PHP cookie.
            context.cookie_data.append(cookie("new-php-from-existing-sso"))
        return SimpleNamespace(status=200)

    monkeypatch.setattr(page, "goto", navigate)
    try:
        if entry == "restore":
            await service.tick()  # Restored startup also uses the shared reauth path.
        else:
            async with service.lock:
                await service._ensure_locked(visit=False)
            if entry == "tick":
                service._last_visit = time.monotonic() - settings.reauth_seconds - 1
                await service.tick()
            else:
                async with service.view():
                    assert (await service.reauth())["viewing"]
        assert page.visits == [HOME, LOGIN]
        assert service.state == "connected"
        assert service.credential() == "new-php-from-existing-sso"
        assert service.store.load() == {"cookies": context.cookie_data, "origins": origins}
        assert not context.added  # No seed, cookie clearing or identity replacement.
    finally:
        await service.close()


@pytest.mark.parametrize(
    "url,snapshot,status,expected",
    [
        (HOME, {"challenge": True}, 200, "needs_login"),
        (HOME, {"login_form": True}, 200, "needs_login"),
        (HOME, {"identity_known": False}, 200, "error"),
        (HOME, {"authenticated": True}, 200, "connected"),
        (HOME, {}, 429, "error"),
        (HOME, {"challenge": True}, 429, "needs_login"),
        (HOME, {}, 500, "error"),
        (LOGIN, {}, 200, "needs_login"),
        ("https://accounts.pixiv.net/mfa", {}, 200, "needs_login"),
        ("https://www.pixiv.net/auth/mfa", {}, 200, "needs_login"),
        ("https://www.pixiv.net/login", {}, 200, "needs_login"),
        ("https://www.pixiv.net/mfa", {}, 200, "needs_login"),
        ("http://www.pixiv.net/", {}, 200, "needs_login"),
        ("https://www.pixiv.net.evil.test/", {}, 200, "needs_login"),
        ("https://www.pixiv.net:8443/", {}, 200, "needs_login"),
    ],
)
async def test_reauth_never_follows_up_without_safe_guest_home(
    service, monkeypatch, url, snapshot, status, expected
):
    await connect(service)
    context = service.factory.context
    page = context.pages[0]
    page.visits.clear()
    before = list(context.cookie_data)

    async def navigate(target, **_options):
        page.visits.append(target)
        page.url = url
        page.snapshot = {"identity_known": True, "authenticated": False, **snapshot}
        return SimpleNamespace(status=status, headers={"retry-after": "120"})

    monkeypatch.setattr(page, "goto", navigate)
    try:
        assert (await service.reauth())["state"] == expected
        assert page.visits == [HOME]
        assert context.cookie_data == before
        if status == 429:
            assert service.retry_at >= int(time.time()) + 120
    finally:
        await service.close()


@pytest.mark.parametrize("outcome", ["form", "challenge", "guest"])
async def test_login_fallback_is_bounded_when_sso_needs_user_input(service, monkeypatch, outcome):
    await connect(service)
    context = service.factory.context
    page = context.pages[0]
    page.visits.clear()
    before = list(context.cookie_data)

    async def navigate(target, **_options):
        page.visits.append(target)
        page.url = LOGIN if target == LOGIN and outcome != "guest" else HOME
        page.snapshot = {"identity_known": True, "authenticated": False}
        if target == LOGIN and outcome != "guest":
            page.snapshot["login_form" if outcome == "form" else "challenge"] = True
        return SimpleNamespace(status=200)

    monkeypatch.setattr(page, "goto", navigate)
    try:
        assert (await service.reauth())["state"] == "needs_login"
        assert page.visits == [HOME, LOGIN]
        assert service.credential() is None
        assert context.cookie_data == before
    finally:
        await service.close()


async def test_login_fallback_429_preserves_shared_navigation_cooldown(service, monkeypatch):
    await connect(service)
    page = service.factory.context.pages[0]
    page.visits.clear()

    async def navigate(target, **_options):
        page.visits.append(target)
        page.url = target
        page.snapshot = {"identity_known": True, "authenticated": False}
        return SimpleNamespace(
            status=429 if target == LOGIN else 200, headers={"retry-after": "120"}
        )

    monkeypatch.setattr(page, "goto", navigate)
    try:
        await service.reauth()
        assert page.visits == [HOME, LOGIN]
        deadline = service.retry_at
        assert deadline >= int(time.time()) + 120
        await service.reauth()
        await service.login()
        await service.open()
        service._last_visit = 0
        await service.tick()
        assert page.visits == [HOME, LOGIN]
        assert service.retry_at == deadline
        assert service.credential() is None
    finally:
        await service.close()


@pytest.mark.parametrize("guard", ["viewing", "managed"])
async def test_guest_sso_does_not_override_background_navigation_guards(settings, guard):
    service = BrowserService(
        replace(settings, managed_navigation=guard == "managed", reauth_seconds=0),
        factory=FakeFactory(),
    )
    service.store.save(
        {"cookies": [cookie("sso", name="account-sso", domain="accounts.pixiv.net")], "origins": []}
    )
    page = service.factory.context.pages[0]
    page.snapshot = {"identity_known": True, "authenticated": False}
    try:
        if guard == "viewing":
            async with service.view():
                await service.tick()  # Restore cannot navigate underneath the viewer.
                await service.tick()
        else:
            await service.tick()
            await service.tick()
        assert page.visits == []
        assert service.state == "needs_login"
        if guard == "managed":
            await service.reauth()  # Account worker's explicit request still gets the fallback.
            assert page.visits == [HOME, LOGIN]
    finally:
        await service.close()
