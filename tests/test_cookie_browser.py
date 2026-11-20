import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from fastapi.websockets import WebSocketDisconnect

from app.adapters.pixiv.contracts import AdapterError
from app.core.errors import DomainError
from app.core.time import now
from app.models.entities import Credential, Job, PixivAccount, Session
from app.services import accounts, auth, browser_auth, jobs, library
from app.services.browser_client import BrowserClient

OLD = "12345_" + "a" * 40
NEW = "12345_" + "b" * 40
MANUAL = "12345_" + "c" * 40


class FakeBrowser:
    def __init__(self, cookie=NEW, state="connected"):
        self.cookie = cookie
        self.state = state
        self.operations = []

    async def status(self):
        return {"state": self.state, "cookie_available": bool(self.cookie), "last_checked": now()}

    async def request(self, method, path, payload=None):
        self.operations.append((method, path))
        if path == "/seed" and not self.cookie:
            self.cookie = payload["cookie"]
        return await self.status()

    async def credential(self):
        return self.cookie if self.state == "connected" else None


class FakeAdapter:
    def __init__(self, account="12345", error=None, hook=None):
        self.account = account
        self.error = error
        self.hook = hook
        self.calls = 0
        self.on_cookie = None
        self.on_retry = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def validate(self):
        self.calls += 1
        if self.hook:
            await self.hook()
        if self.error:
            raise self.error
        return self.account


class ManualBrowser(FakeBrowser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.login_pending = False
        self.login_generation = 0
        self.inspections = 0
        self.credential_reads = 0

    async def status(self):
        return {
            **await super().status(),
            "login_pending": self.login_pending,
            "login_generation": self.login_generation,
        }

    async def request(self, method, path, payload=None):
        if path in {"/open", "/login"}:
            self.login_generation += 1
        if path in {"/open", "/login", "/complete"}:
            self.login_pending = True
        if path == "/complete":
            self.inspections += 1
        if path == "/resume":
            assert payload == {"generation": self.login_generation}
            self.login_pending = False
        return await super().request(method, path, payload)

    async def credential(self):
        self.credential_reads += 1
        return await super().credential()


def configured(admin, settings):
    result = replace(settings, browser_url="http://browser-helper:8765")
    admin.app.state.settings = result
    accounts.store_credential(result, OLD)
    token = Credential.get_by_id(1).ciphertext
    accounts.validated(token, "12345")
    return result, token


def waiting_job(token):
    job = jobs.enqueue_account("sync", "synthetic-browser-sync", "public", mode="normal")
    owner = jobs.claim(job.id)
    jobs.finish(job.id, owner, "waiting_auth", "auth_expired")
    accounts.authentication_failed(token)
    return job.id


def test_browser_routes_preserve_session_origin_and_csrf_boundaries(admin, settings):
    result = admin.get("/api/accounts/1/browser")
    assert result.status_code == 200
    assert result.json()["state"] == "disabled" and result.json()["enabled"] is False
    assert admin.post("/api/accounts/1/browser/reauth").status_code == 409
    configured(admin, settings)
    assert (
        admin.post("/api/accounts/1/browser/reauth", headers={"X-CSRF-Token": "wrong"}).status_code
        == 403
    )
    assert (
        admin.post(
            "/api/accounts/1/browser/open", headers={"Origin": "https://untrusted.invalid"}
        ).status_code
        == 403
    )
    result = admin.post("/api/accounts/1/browser/reauth")
    assert result.status_code == 202 and result.json()["state"] == "reauthenticating"
    assert OLD not in result.text and "ciphertext" not in result.text
    admin.cookies.clear()
    assert admin.get("/api/accounts/1/browser").status_code == 401


@pytest.mark.asyncio
async def test_expired_session_reauth_adopts_new_cookie_and_resumes_same_job(admin, settings):
    settings, old = configured(admin, settings)
    job_id = waiting_job(old)
    remote, adapter = FakeBrowser(), FakeAdapter()
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    assert ("POST", "/reauth") in remote.operations
    assert accounts.load_credential(settings)[0] == NEW
    assert Credential.get_by_id(1).ciphertext != old
    assert Job.get_by_id(job_id).state == "queued"
    assert PixivAccount.get_by_id(1).status == "connected"
    view = browser_auth.public_state(settings)
    assert view["state"] == "connected" and view["auto_reauth"] is True
    assert "cookie" not in view and "ciphertext" not in view
    assert OLD not in str(view) and NEW not in str(view)


@pytest.mark.asyncio
async def test_restarted_unvisited_browser_recovers_before_cached_refresh_deadline(admin, settings):
    settings, _ = configured(admin, settings)
    deadline = now() + settings.browser_refresh_seconds
    browser_auth.update_state(state="connected", next_check=deadline)

    class RestoredBrowser(FakeBrowser):
        viewing = True

        async def status(self):
            connected = self.state == "connected"
            return {
                "state": self.state,
                "cookie_available": connected,
                "last_checked": now() if connected else None,
                "viewing": self.viewing,
            }

        async def request(self, method, path, payload=None):
            if path == "/reauth":
                self.state = "connected"
            return await super().request(method, path, payload)

    remote = RestoredBrowser(NEW, "needs_login")
    adapter = FakeAdapter()
    # Restored cookies exist, but no page has visited Pixiv yet. Preserve user input.
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    assert ("POST", "/reauth") not in remote.operations
    assert adapter.calls == 0 and browser_auth.cached()["next_check"] == deadline
    remote.viewing = False
    # A restart must recover immediately rather than display a false login prompt for 10 min.
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    assert remote.operations.count(("POST", "/reauth")) == 1
    assert adapter.calls == 1 and accounts.load_credential(settings)[0] == NEW
    assert browser_auth.public_state(settings)["state"] == "connected"


@pytest.mark.asyncio
async def test_manual_login_pauses_worker_until_explicit_completion(admin, settings, monkeypatch):
    settings, old = configured(admin, settings)
    job_id = waiting_job(old)
    remote = ManualBrowser()
    await browser_auth.open_browser(settings, lambda _: remote)
    assert browser_auth.public_state(settings)["state"] == "awaiting_confirmation"
    assert browser_auth.public_state(settings)["login_pending"] is True

    def must_not_connect(_):
        pytest.fail("No browser calls or account checks before the user confirms")

    started = now()
    for elapsed in (0, 5, 30, 600, 3600):
        monkeypatch.setattr(browser_auth, "now", lambda elapsed=elapsed: started + elapsed)
        await browser_auth.run_once(settings, must_not_connect)
    assert remote.operations == [("POST", "/open")]
    assert remote.inspections == remote.credential_reads == 0
    assert accounts.load_credential(settings) == (OLD, old)
    assert Job.get_by_id(job_id).state == "waiting_auth"
    with pytest.raises(DomainError, match="login_in_progress"):
        browser_auth.request_reauth(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [OLD, NEW])
async def test_completion_validates_once_without_navigation_and_resumes_auto(
    admin, settings, candidate
):
    settings, old = configured(admin, settings)
    job_id = waiting_job(old)
    remote, adapter = ManualBrowser(candidate), FakeAdapter()
    await browser_auth.open_browser(settings, lambda _: remote)
    result = await browser_auth.complete_login(settings, lambda _: remote)
    assert result["state"] == "reauthenticating" and result["login_pending"]
    repeated = await browser_auth.complete_login(settings, lambda _: remote)
    assert repeated == result and remote.inspections == 1
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    assert remote.operations == [("POST", "/open"), ("POST", "/complete"), ("POST", "/resume")]
    assert adapter.calls == 1 and accounts.load_credential(settings)[0] == candidate
    assert Job.get_by_id(job_id).state == "queued"
    assert browser_auth.public_state(settings)["state"] == "connected"
    assert browser_auth.public_state(settings)["login_pending"] is False
    assert remote.login_pending is False
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_completion_before_login_preserves_pending_input_and_credential(admin, settings):
    settings, old = configured(admin, settings)
    remote = ManualBrowser(None, "needs_login")
    await browser_auth.open_browser(settings, lambda _: remote)
    result = await browser_auth.complete_login(settings, lambda _: remote)
    assert result["state"] == "awaiting_confirmation"
    assert result["message_code"] == "login_not_completed" and result["login_pending"]
    assert browser_auth.cached().get("requested") is None
    assert remote.inspections == 1 and remote.login_pending
    assert accounts.load_credential(settings) == (OLD, old)
    before = list(remote.operations)

    def must_not_connect(_):
        pytest.fail("A failed manual completion must remain quiet")

    await browser_auth.run_once(settings, must_not_connect)
    assert remote.operations == before


@pytest.mark.asyncio
async def test_helper_pending_mode_blocks_unsolicited_cookie_import(admin, settings):
    settings, old = configured(admin, settings)
    remote, adapter = ManualBrowser(), FakeAdapter()
    remote.login_pending = True
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    assert browser_auth.public_state(settings)["login_pending"]
    assert browser_auth.public_state(settings)["state"] == "awaiting_confirmation"
    assert remote.credential_reads == adapter.calls == 0 and not remote.operations
    assert accounts.load_credential(settings) == (OLD, old)


@pytest.mark.asyncio
@pytest.mark.parametrize("confirming", [False, True])
async def test_new_login_interaction_wins_over_in_flight_verification(admin, settings, confirming):
    settings, old = configured(admin, settings)
    remote = ManualBrowser()
    if confirming:
        await browser_auth.open_browser(settings, lambda _: remote)
        await browser_auth.complete_login(settings, lambda _: remote)

    async def new_login():
        await browser_auth.open_browser(settings, lambda _: remote)

    await browser_auth.run_once(
        settings, lambda _: remote, lambda *a, **k: FakeAdapter(hook=new_login)
    )
    assert accounts.load_credential(settings) == (OLD, old)
    assert browser_auth.public_state(settings)["state"] == "awaiting_confirmation"
    assert browser_auth.public_state(settings)["login_pending"]
    assert ("POST", "/resume") not in remote.operations


@pytest.mark.asyncio
async def test_confirmed_different_account_does_not_resume_browser_or_replace_cookie(
    admin, settings
):
    settings, old = configured(admin, settings)
    remote = ManualBrowser()
    await browser_auth.open_browser(settings, lambda _: remote)
    await browser_auth.complete_login(settings, lambda _: remote)
    await browser_auth.run_once(
        settings, lambda _: remote, lambda *a, **k: FakeAdapter(account="54321")
    )
    assert accounts.load_credential(settings) == (OLD, old)
    assert browser_auth.public_state(settings)["message_code"] == "account_mismatch"
    assert browser_auth.public_state(settings)["login_pending"]
    assert ("POST", "/resume") not in remote.operations


def test_login_completion_requires_admin_csrf_and_exact_origin(admin, settings):
    configured(admin, settings)
    assert admin.post("/api/accounts/1/browser/complete").status_code == 409
    assert (
        admin.post(
            "/api/accounts/1/browser/complete", headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    assert (
        admin.post(
            "/api/accounts/1/browser/complete", headers={"Origin": "https://untrusted.invalid"}
        ).status_code
        == 403
    )
    admin.cookies.clear()
    assert admin.post("/api/accounts/1/browser/complete").status_code == 401


@pytest.mark.asyncio
async def test_polling_same_cookie_does_not_rewrite_or_repeat_validation(admin, settings):
    settings, old = configured(admin, settings)
    remote, adapter = FakeBrowser(OLD), FakeAdapter()
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    assert Credential.get_by_id(1).ciphertext == old
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_manual_disconnect_during_browser_validation_is_not_undone(admin, settings):
    settings, _ = configured(admin, settings)

    async def disconnect():
        accounts.disconnect()
        browser_auth.pause(settings)

    await browser_auth.run_once(
        settings, lambda _: FakeBrowser(), lambda *a, **k: FakeAdapter(hook=disconnect)
    )
    assert Credential.select().count() == 0
    assert browser_auth.public_state(settings)["auto_reauth"] is False


@pytest.mark.asyncio
async def test_manual_cookie_replacement_during_validation_wins(admin, settings):
    settings, _ = configured(admin, settings)

    async def replace_cookie():
        accounts.store_credential(settings, MANUAL)
        browser_auth.pause(settings)

    await browser_auth.run_once(
        settings, lambda _: FakeBrowser(), lambda *a, **k: FakeAdapter(hook=replace_cookie)
    )
    assert accounts.load_credential(settings)[0] == MANUAL
    assert PixivAccount.get_by_id(1).status == "unverified"


@pytest.mark.asyncio
async def test_different_account_is_not_imported(admin, settings):
    settings, old = configured(admin, settings)
    await browser_auth.run_once(
        settings, lambda _: FakeBrowser(), lambda *a, **k: FakeAdapter(account="54321")
    )
    assert accounts.load_credential(settings) == (OLD, old)
    assert browser_auth.public_state(settings)["message_code"] == "account_mismatch"
    assert browser_auth.public_state(settings)["state"] == "error"


@pytest.mark.asyncio
async def test_cookie_presence_does_not_fake_success_and_retry_is_paced(admin, settings):
    settings, old = configured(admin, settings)
    job_id = waiting_job(old)
    remote = FakeBrowser()
    adapter = FakeAdapter(error=AdapterError("auth_expired"))
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    assert adapter.calls == 1
    assert browser_auth.public_state(settings)["state"] == "needs_login"
    assert accounts.load_credential(settings) == (OLD, old)
    assert Job.get_by_id(job_id).state == "waiting_auth"


@pytest.mark.asyncio
async def test_temporary_helper_failure_does_not_erase_valid_credential(admin, settings):
    settings, old = configured(admin, settings)

    class FailedBrowser(FakeBrowser):
        async def status(self):
            raise DomainError("browser_unavailable", "sanitized", 503)

    await browser_auth.run_once(settings, lambda _: FailedBrowser())
    assert accounts.load_credential(settings) == (OLD, old)
    assert browser_auth.public_state(settings)["state"] == "unavailable"
    assert PixivAccount.get_by_id(1).status == "connected"


@pytest.mark.asyncio
async def test_account_browser_validation_is_single_owner(admin, settings):
    settings, _ = configured(admin, settings)
    started, proceed = asyncio.Event(), asyncio.Event()

    async def block():
        started.set()
        await proceed.wait()

    remote, adapter = FakeBrowser(), FakeAdapter(hook=block)
    task = asyncio.create_task(
        browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    )
    await started.wait()
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    proceed.set()
    await task
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_open_only_seeds_empty_browser_and_requires_explicit_binding(admin, settings):
    settings, _ = configured(admin, settings)
    browser_auth.pause(settings)
    remote = FakeBrowser(None, "needs_login")
    await browser_auth.open_browser(settings, lambda _: remote)
    assert remote.operations == [("POST", "/seed"), ("POST", "/open")]
    remote.operations.clear()
    await browser_auth.open_browser(settings, lambda _: remote)
    assert remote.operations == [("POST", "/open")]
    assert browser_auth.bound()


def test_websocket_rejects_bad_origin_csrf_and_missing_session(admin, settings):
    configured(admin, settings)
    csrf = admin.headers["X-CSRF-Token"]
    for origin, protocols in (
        ("https://untrusted.invalid", ["binary", "pixchive-csrf." + csrf]),
        ("http://testserver", ["binary"]),
        ("http://testserver", ["binary", "pixchive-csrf.wrong"]),
    ):
        with pytest.raises(WebSocketDisconnect):
            with admin.websocket_connect(
                "/api/accounts/1/browser/view", headers={"Origin": origin}, subprotocols=protocols
            ):
                pytest.fail("unauthorized viewer connected")
    admin.cookies.clear()
    with pytest.raises(WebSocketDisconnect):
        with admin.websocket_connect(
            "/api/accounts/1/browser/view",
            headers={"Origin": "http://testserver"},
            subprotocols=["binary", "pixchive-csrf." + csrf],
        ):
            pytest.fail("logged-out viewer connected")


def test_websocket_binary_proxy_and_revoked_session_disconnect(admin, settings, monkeypatch):
    configured(admin, settings)
    monkeypatch.setattr(browser_auth, "VIEW_CHECK_SECONDS", 0.01)

    class Pipe:
        def __init__(self):
            self.messages = asyncio.Queue()

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.messages.get()

        async def send(self, data):
            await self.messages.put(data)

    @asynccontextmanager
    async def fake_view(self):
        yield Pipe()

    monkeypatch.setattr(BrowserClient, "view", fake_view)
    csrf = admin.headers["X-CSRF-Token"]
    token = admin.cookies.get("pixchive_session")
    with admin.websocket_connect(
        "/api/accounts/1/browser/view",
        headers={"Origin": "http://testserver"},
        subprotocols=["binary", "pixchive-csrf." + csrf],
    ) as socket:
        assert socket.accepted_subprotocol == "binary"
        socket.send_bytes(b"RFB 003.008\n")
        assert socket.receive_bytes() == b"RFB 003.008\n"
        Session.delete().where(Session.id == auth.digest(token)).execute()
        with pytest.raises(WebSocketDisconnect):
            socket.receive_bytes()


def test_browser_config_validates_origin_and_refresh_interval(monkeypatch):
    monkeypatch.setenv("PIXCHIVE_BROWSER_URL", "http://browser-helper:8765/")
    monkeypatch.setenv("PIXCHIVE_BROWSER_REFRESH_SECONDS", "120")
    from app.core.config import Settings

    assert Settings.from_env().browser_url == "http://browser-helper:8765"
    assert Settings.from_env().browser_refresh_seconds == 120
    monkeypatch.setenv("PIXCHIVE_BROWSER_REFRESH_SECONDS", "0")
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("PIXCHIVE_BROWSER_REFRESH_SECONDS", "120")
    monkeypatch.setenv("PIXCHIVE_BROWSER_URL", "http://user:password@browser-helper:8765")
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.asyncio
async def test_auto_navigation_does_not_interrupt_user_login_screen(admin, settings):
    settings, old = configured(admin, settings)
    waiting_job(old)

    class ViewedBrowser(FakeBrowser):
        async def status(self):
            return {**await super().status(), "viewing": True}

    remote = ViewedBrowser(None, "needs_login")
    await browser_auth.run_once(settings, lambda _: remote)
    assert ("POST", "/reauth") not in remote.operations
    assert browser_auth.public_state(settings)["state"] == "needs_login"


@pytest.mark.asyncio
async def test_user_requested_reauth_can_navigate_with_view_open(admin, settings):
    settings, _ = configured(admin, settings)

    class ViewedBrowser(FakeBrowser):
        async def status(self):
            return {**await super().status(), "viewing": True}

    remote = ViewedBrowser()
    browser_auth.request_reauth(settings)
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: FakeAdapter())
    assert ("POST", "/reauth") in remote.operations
    assert browser_auth.public_state(settings)["state"] == "connected"


@pytest.mark.asyncio
async def test_shared_pixiv_cooldown_blocks_automatic_and_manual_navigation(admin, settings):
    settings, _ = configured(admin, settings)
    until = now() + 120
    library.set_setting("pixiv_retry_at", str(until))
    browser_auth.request_reauth(settings)

    def must_not_connect(_):
        pytest.fail("cooldown must be checked before a browser request")

    await browser_auth.run_once(settings, must_not_connect)
    assert browser_auth.public_state(settings)["message_code"] == "rate_limited"
    with pytest.raises(DomainError, match="rate_limited"):
        await browser_auth.open_browser(settings, must_not_connect)


@pytest.mark.asyncio
async def test_browser_429_propagates_to_account_cooldown(admin, settings):
    settings, _ = configured(admin, settings)

    class RateLimitedBrowser(FakeBrowser):
        async def status(self):
            return {**await super().status(), "retry_at": now() + 180}

    await browser_auth.run_once(settings, lambda _: RateLimitedBrowser())
    assert browser_auth.shared_retry_at() >= now() + 179
    assert browser_auth.public_state(settings)["message_code"] == "rate_limited"


@pytest.mark.asyncio
async def test_missing_browser_cookie_poll_does_not_extend_reauth_deadline(
    admin, settings, monkeypatch
):
    settings, _ = configured(admin, settings)
    clock = [10000]
    monkeypatch.setattr(browser_auth, "now", lambda: clock[0])
    remote = FakeBrowser(None, "needs_login")
    await browser_auth.run_once(settings, lambda _: remote)
    deadline = browser_auth.cached()["next_check"]
    first_visits = remote.operations.count(("POST", "/reauth"))
    clock[0] += 5
    await browser_auth.run_once(settings, lambda _: remote)
    assert browser_auth.cached()["next_check"] == deadline
    clock[0] = deadline + 1
    await browser_auth.run_once(settings, lambda _: remote)
    assert remote.operations.count(("POST", "/reauth")) == first_visits + 1


@pytest.mark.asyncio
async def test_rejected_browser_cookie_retries_preserve_original_reauth_deadline(
    admin, settings, monkeypatch
):
    settings, old = configured(admin, settings)
    job_id = waiting_job(old)
    started = now()
    clock = [started]
    monkeypatch.setattr(browser_auth, "now", lambda: clock[0])
    remote = FakeBrowser()
    adapter = FakeAdapter(error=AdapterError("auth_expired"))

    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    assert adapter.calls == 1
    assert remote.operations.count(("POST", "/reauth")) == 1

    for elapsed in range(30, settings.browser_refresh_seconds, 30):
        clock[0] = started + elapsed
        await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
        assert remote.cookie == NEW
        assert remote.operations.count(("POST", "/reauth")) == 1
        assert accounts.load_credential(settings) == (OLD, old)
        assert Job.get_by_id(job_id).state == "waiting_auth"

    calls_before_refresh = adapter.calls
    adapter.error = None
    clock[0] = started + settings.browser_refresh_seconds + 1
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    assert remote.operations.count(("POST", "/reauth")) == 2
    assert adapter.calls == calls_before_refresh + 1
    assert accounts.load_credential(settings)[0] == NEW
    assert Job.get_by_id(job_id).state == "queued"


@pytest.mark.asyncio
async def test_validation_retry_cooldown_survives_timeout_and_blocks_early_requests(
    admin, settings, monkeypatch
):
    settings, old = configured(admin, settings)
    job_id = waiting_job(old)
    started = now()
    clock = [started]
    monkeypatch.setattr(browser_auth, "now", lambda: clock[0])
    remote = FakeBrowser()

    class TimedOutAdapter(FakeAdapter):
        async def validate(self):
            self.calls += 1
            if self.calls == 1:
                assert callable(self.on_retry), "Persist Retry-After before the adapter sleeps"
                self.on_retry(AdapterError("rate_limited", retry_after=180), 180)
                assert browser_auth.shared_retry_at() >= started + 180
                # The enclosing 90-second budget expires during the adapter's retry wait.
                clock[0] += 90
                raise TimeoutError
            return self.account

    adapter = TimedOutAdapter()
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    deadline = browser_auth.shared_retry_at()
    assert deadline >= started + 180
    assert accounts.load_credential(settings) == (OLD, old)
    assert Job.get_by_id(job_id).state == "waiting_auth"
    operations = list(remote.operations)

    def must_not_connect(_):
        pytest.fail("A validation timeout must not shorten the shared Pixiv cooldown")

    for elapsed in (95, 150, 179):
        clock[0] = started + elapsed
        if elapsed == 150:
            browser_auth.request_reauth(settings)
        await browser_auth.run_once(settings, must_not_connect, lambda *a, **k: adapter)
        assert browser_auth.shared_retry_at() == deadline
        assert adapter.calls == 1
        assert remote.operations == operations
    with pytest.raises(DomainError, match="rate_limited"):
        await browser_auth.open_browser(settings, must_not_connect)

    clock[0] = deadline + 1
    await browser_auth.run_once(settings, lambda _: remote, lambda *a, **k: adapter)
    assert adapter.calls == 2
    assert remote.operations.count(("POST", "/reauth")) == 2
    assert Job.get_by_id(job_id).state == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["refresh_due", "browser_cookie", "external_cookie", "external_generation"]
)
async def test_rotated_browser_cookie_is_paced_only_for_current_verified_pair(
    admin, settings, monkeypatch, change
):
    settings, _ = configured(admin, settings)
    started = now()
    clock = [started]
    monkeypatch.setattr(browser_auth, "now", lambda: clock[0])
    remote = FakeBrowser()
    rotated = "12345_" + "d" * 40
    candidates = []

    class RotatingAdapter(FakeAdapter):
        async def validate(self):
            pixiv_id = await super().validate()
            await self.on_cookie(rotated, pixiv_id)
            return pixiv_id

    adapter = RotatingAdapter()

    def make_adapter(cookie, **kwargs):
        candidates.append(cookie)
        return adapter

    await browser_auth.run_once(settings, lambda _: remote, make_adapter)
    adopted = accounts.load_credential(settings)
    assert adopted[0] == rotated
    assert candidates == [NEW]
    assert PixivAccount.get_by_id(1).status == "connected"

    for elapsed in (5, 30, 300, settings.browser_refresh_seconds - 1):
        clock[0] = started + elapsed
        await browser_auth.run_once(settings, lambda _: remote, make_adapter)
        assert remote.cookie == NEW
        assert adapter.calls == 1
        assert candidates == [NEW]
        assert accounts.load_credential(settings) == adopted
        assert remote.operations.count(("POST", "/reauth")) == 1

    if change == "refresh_due":
        clock[0] = started + settings.browser_refresh_seconds + 1
        await browser_auth.run_once(settings, lambda _: remote, make_adapter)
        assert adapter.calls == 2
        assert candidates == [NEW, NEW]
        assert remote.operations.count(("POST", "/reauth")) == 2
        return

    if change == "browser_cookie":
        remote.cookie = MANUAL
    else:
        replacement = MANUAL if change == "external_cookie" else rotated
        accounts.store_credential(settings, replacement)
        generation = Credential.get_by_id(1).ciphertext
        assert generation != adopted[1]
        accounts.validated(generation, "12345")
    # An unchanged browser candidate must not mask failure after either generation changes.
    current = accounts.load_credential(settings)
    adapter.error = AdapterError("auth_expired")
    await browser_auth.run_once(settings, lambda _: remote, make_adapter)
    assert adapter.calls == 2
    assert candidates == [NEW, MANUAL if change == "browser_cookie" else NEW]
    assert accounts.load_credential(settings) == current
    assert browser_auth.public_state(settings)["state"] == "needs_login"
    assert remote.operations.count(("POST", "/reauth")) == 1
