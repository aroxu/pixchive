"""Synthetic session rotation contracts; no real credentials, services, or Pixiv calls."""

import asyncio
import io
import json

import httpx
import pytest
from cryptography.fernet import Fernet

from app.adapters.pixiv import bridge as bridge_module
from app.adapters.pixiv import live as live_module
from app.adapters.pixiv.contracts import AdapterError
from app.adapters.pixiv.live import CookiePixivAdapter
from app.adapters.pixiv.transport import retry
from app.adapters.pixiv.web import WebBridge
from app.core.db import db
from app.core.errors import DomainError
from app.models.entities import (
    ArtworkTag,
    Asset,
    CollectionRun,
    Credential,
    DownloadTask,
    Job,
    PixivAccount,
)
from app.services import accounts, jobs, library, storage
from app.services.fixtures import image_bytes
from app.worker import run_job
from tests.test_collection import BookmarkPage, FakeAdapter, context, work
from tests.test_pixiv_web import identity, router

OLD = "42_synthetic_original_session"
NEW = "42_synthetic_rotated_session"
FINAL = "42_synthetic_final_session"
EXTERNAL = "42_synthetic_browser_session"


def connect(settings, cookie=OLD):
    return accounts.adopt_verified_cookie(settings, cookie, "42", None)


def account_snapshot():
    account = PixivAccount.get()
    return account.pixiv_id, account.status, account.validated_at


def test_verified_adoption_initial_and_same_cookie_do_not_reencrypt(client, settings, monkeypatch):
    first = connect(settings)
    assert first != OLD and accounts.load_credential(settings) == (OLD, first)
    assert account_snapshot()[:2] == ("42", "connected")
    assert account_snapshot()[2] is not None
    PixivAccount.update(status="auth_expired", validated_at=None).execute()

    def must_not_encrypt(*args):
        pytest.fail("An unchanged cookie must preserve its existing ciphertext")

    monkeypatch.setattr(Fernet, "encrypt", must_not_encrypt)
    monkeypatch.setattr(accounts, "now", lambda: 123456)
    assert accounts.adopt_verified_cookie(settings, OLD, "42", first) == first
    assert account_snapshot() == ("42", "connected", 123456)
    assert accounts.load_credential(settings) == (OLD, first)


@pytest.mark.parametrize("cookie", [None, 42, "short", OLD + "\n", "PHPSESSID=" + OLD])
def test_adoption_rejects_invalid_candidate_before_writing(client, settings, cookie):
    with pytest.raises(DomainError, match="invalid_cookie"):
        accounts.adopt_verified_cookie(settings, cookie, "42", None)
    assert Credential.select().count() == PixivAccount.select().count() == 0


@pytest.mark.parametrize("pixiv_id", [None, 42, True, "", "0", "-1", "4.2", "9" * 21, "42\n"])
def test_adoption_requires_validated_identifier(client, settings, pixiv_id):
    with pytest.raises(DomainError, match="invalid_pixiv_id"):
        accounts.adopt_verified_cookie(settings, NEW, pixiv_id, None)
    assert Credential.select().count() == PixivAccount.select().count() == 0


def test_adoption_rejects_stale_and_other_account_without_replacing(client, settings):
    original = connect(settings)
    before = account_snapshot()
    for candidate, pixiv_id, expected, code in (
        (NEW, "42", None, "credential_changed"),
        (NEW, "42", "stale-ciphertext", "credential_changed"),
        (NEW, "43", original, "account_mismatch"),
        (OLD, "43", original, "account_mismatch"),
    ):
        with pytest.raises(DomainError, match=code):
            accounts.adopt_verified_cookie(settings, candidate, pixiv_id, expected)
        assert accounts.load_credential(settings) == (OLD, original)
        assert account_snapshot() == before
    accounts.disconnect()
    with pytest.raises(DomainError, match="credential_changed"):
        accounts.adopt_verified_cookie(settings, NEW, "42", original)
    assert Credential.select().count() == 0
    with pytest.raises(DomainError, match="account_mismatch"):
        accounts.adopt_verified_cookie(settings, NEW, "43", None)
    assert account_snapshot()[:2] == ("42", "disconnected")
    assert accounts.adopt_verified_cookie(settings, NEW, "42", None)


@pytest.mark.parametrize("already_stored", [False, True])
def test_adoption_rechecks_absence_and_ciphertext_after_encrypt(
    client, settings, monkeypatch, already_stored
):
    expected = connect(settings) if already_stored else None
    cipher = Fernet(settings.key_file.read_bytes().strip())
    external_ciphertext = cipher.encrypt(EXTERNAL.encode()).decode()
    original_encrypt = Fernet.encrypt

    def encrypt_and_replace(self, data):
        assert not db.in_transaction(), "Key operations must precede the write transaction"
        encrypted = original_encrypt(self, data)
        account, _ = PixivAccount.get_or_create(id=1)
        Credential.insert(
            account=account, ciphertext=external_ciphertext, kind="pixiv_cookie"
        ).on_conflict(
            conflict_target=[Credential.account],
            update={Credential.ciphertext: external_ciphertext},
        ).execute()
        return encrypted

    monkeypatch.setattr(Fernet, "encrypt", encrypt_and_replace)
    with pytest.raises(DomainError, match="credential_changed"):
        accounts.adopt_verified_cookie(settings, NEW, "42", expected)
    assert accounts.load_credential(settings) == (EXTERNAL, external_ciphertext)


def test_adoption_key_failure_and_manual_store_keep_existing_meaning(client, settings):
    expected = connect(settings)
    settings.key_file.write_bytes(b"not-a-fernet-key")
    with pytest.raises(DomainError, match="key_unavailable"):
        accounts.adopt_verified_cookie(settings, NEW, "42", expected)
    assert Credential.get().ciphertext == expected
    assert account_snapshot()[:2] == ("42", "connected")
    settings.key_file.write_bytes(Fernet.generate_key())
    accounts.store_credential(settings, NEW)
    assert account_snapshot() == ("42", "unverified", None)
    accounts.disconnect()
    assert Credential.select().count() == 0
    assert account_snapshot() == ("42", "disconnected", None)


@pytest.mark.parametrize("domain", ["", "www.pixiv.net", ".www.pixiv.net", ".pixiv.net"])
def test_web_rotation_updates_next_header_and_is_reported_once(domain):
    sent = []

    def response(request):
        sent.append(request.headers["cookie"])
        suffix = f"; Domain={domain}" if domain else ""
        return httpx.Response(
            200,
            text=identity(),
            headers=[("set-cookie", f"PHPSESSID={NEW}; Path=/; Secure; HttpOnly{suffix}")],
        )

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        bridge = WebBridge(OLD, client)
        assert bridge.validate() == "42"
        assert bridge.cookie == NEW and bridge.take_cookie_rotation() == NEW
        assert bridge.validate() == "42"
        assert bridge.take_cookie_rotation() is None
        assert sent == [f"PHPSESSID={OLD}", f"PHPSESSID={NEW}"]


@pytest.mark.parametrize(
    "header",
    [
        "PHPSESSID=; Path=/",
        "PHPSESSID=deleted; Path=/",
        f"PHPSESSID={NEW}; Path=/; Max-Age=0",
        f"PHPSESSID={NEW}; Path=/; Max-Age=-1",
        f"PHPSESSID={NEW}; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
        f"PHPSESSID={NEW}; Path=/; Expires=not-a-date",
        f"PHPSESSID={NEW}; Path=/; Domain=accounts.pixiv.net",
        f"PHPSESSID={NEW}; Path=/; Domain=www.pixiv.net.example.org",
        f"PHPSESSID={NEW}; Path=/; Domain=evilpixiv.net",
        f"PHPSESSID={NEW}; Path=/; Domain=.net",
        f"PHPSESSID={NEW}; Path=/; Domain=.",
        f"PHPSESSID={NEW}; Path=/; Domain=..pixiv.net",
        f"PHPSESSID={NEW}; Path=/ajax",
        "PHPSESSID=not%20a%20valid%20cookie; Path=/",
        f"OTHER_SESSION={NEW}; Path=/",
    ],
)
def test_web_ignores_wrong_scope_deleted_or_malformed_cookie(header):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=identity(), headers={"set-cookie": header})
        )
    ) as client:
        bridge = WebBridge(OLD, client)
        assert bridge.validate() == "42"
        assert bridge.cookie == OLD and bridge.take_cookie_rotation() is None


def test_cookie_expiry_precedence_and_multiple_headers():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=identity(),
                headers=[
                    ("set-cookie", "unrelated=one; Path=/"),
                    (
                        "set-cookie",
                        f"PHPSESSID={NEW}; Path=/; Max-Age=120; "
                        "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
                    ),
                    ("set-cookie", f"PHPSESSID={EXTERNAL}; Path=/; Domain=other.example"),
                ],
            )
        )
    ) as client:
        bridge = WebBridge(OLD, client)
        bridge.validate()
        assert bridge.take_cookie_rotation() == NEW


@pytest.mark.parametrize("status", [302, 401, 403, 429, 503])
def test_web_observes_rotation_on_redirects_and_errors(status):
    sent = []

    def response(request):
        sent.append(request.headers["cookie"])
        if request.url.path == "/en/":
            return httpx.Response(200, text=identity())
        return httpx.Response(
            status,
            headers={"set-cookie": f"PHPSESSID={NEW}; Path=/", "location": "/en/"},
        )

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        bridge = WebBridge(OLD, client)
        if status == 302:
            bridge.validate()
            assert sent[-1] == f"PHPSESSID={NEW}"
        else:
            with pytest.raises(AdapterError):
                bridge.validate()
        assert bridge.cookie == NEW and bridge.take_cookie_rotation() == NEW


def test_pending_rotation_that_returns_to_original_is_not_reported():
    def response(request):
        if request.url.path == "/":
            return httpx.Response(
                302, headers={"set-cookie": f"PHPSESSID={NEW}; Path=/", "location": "/en/"}
            )
        assert request.headers["cookie"] == f"PHPSESSID={NEW}"
        return httpx.Response(
            200, text=identity(), headers={"set-cookie": f"PHPSESSID={OLD}; Path=/"}
        )

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        bridge = WebBridge(OLD, client)
        bridge.validate()
        assert bridge.cookie == OLD and bridge.take_cookie_rotation() is None


def test_bridge_private_rotation_does_not_enter_artwork_or_tags(monkeypatch):
    def response(request):
        if request.url.path == "/":
            return httpx.Response(200, text=identity())
        result = router(request)
        if request.url.path == "/ajax/illust/123":
            result.headers["set-cookie"] = f"PHPSESSID={NEW}; Path=/; Domain=.pixiv.net"
        return result

    commands = [
        {"op": "open", "cookie": OLD},
        {"op": "validate"},
        {"op": "artwork", "artwork_id": "123"},
        {"op": "artwork", "artwork_id": "123"},
    ]
    output = io.StringIO()
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        bridge = WebBridge(OLD, client)
        monkeypatch.setattr(bridge_module, "WebBridge", lambda *args, **kwargs: bridge)
        monkeypatch.setattr(
            bridge_module.sys, "stdin", io.StringIO("\n".join(map(json.dumps, commands)))
        )
        monkeypatch.setattr(bridge_module.sys, "stdout", output)
        # The child disables logging globally; restore it in this in-process test.
        monkeypatch.setattr(bridge_module.logging, "disable", lambda *args: None)
        bridge_module.main()
    outputs = list(map(json.loads, output.getvalue().splitlines()))
    assert outputs[0] == {"result": True} and outputs[1] == {"result": "42"}
    assert outputs[2]["cookie_rotation"] == NEW
    assert "cookie_rotation" not in outputs[3]
    artwork = outputs[2]["result"]
    assert artwork["work_tags"] == [{"name": "風景", "translations": {"en": "landscape"}}]
    assert [page["source"] for page in artwork["pages"]] == ["pixiv_pages_original"] * 2
    assert NEW not in json.dumps(artwork) and OLD not in json.dumps(artwork)
    assert "not-exported" not in output.getvalue()


class ScriptedProcess:
    """Exercise the actual parent JSON-lines parser without creating a network child."""

    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.commands = []
        self.stdin = self
        self.stdout = asyncio.StreamReader()
        self.returncode = None

    def write(self, data):
        self.commands.append(json.loads(data))
        self.stdout.feed_data(json.dumps(next(self.outputs)).encode() + b"\n")

    async def drain(self):
        pass


def scripted_adapter(outputs, on_cookie):
    adapter = CookiePixivAdapter(OLD)
    adapter.process = ScriptedProcess(outputs)
    adapter.on_cookie = on_cookie
    return adapter


async def test_parent_verifies_final_rotation_without_recursive_callback():
    received = []

    async def rotated(cookie, pixiv_id):
        received.append((cookie, pixiv_id))

    adapter = scripted_adapter(
        [
            {"result": "42", "cookie_rotation": NEW},
            {"result": "42", "cookie_rotation": FINAL},
            {"result": "42"},
        ],
        rotated,
    )
    assert await adapter.validate() == "42"
    assert received == [(FINAL, "42")]
    assert adapter.process.commands == [{"op": "validate"}] * 3
    assert adapter._pending_cookie is None


async def test_parent_strips_rotation_from_returned_metadata_and_keeps_original_error():
    received = []

    async def rotated(cookie, pixiv_id):
        received.append((cookie, pixiv_id))

    metadata = work().model_dump(mode="json")
    adapter = scripted_adapter(
        [
            {"result": "42"},
            {"result": metadata, "cookie_rotation": NEW},
            {"result": "42"},
            {"error": "unavailable", "cookie_rotation": FINAL},
            {"result": "42"},
            {"result": "42"},
        ],
        rotated,
    )
    assert await adapter.validate() == "42"
    assert (await adapter.artwork("123")).model_dump(mode="json") == metadata
    with pytest.raises(AdapterError, match="unavailable"):
        await adapter.artwork("123")
    assert received == [(NEW, "42")]
    assert await adapter.validate() == "42"
    assert received == [(NEW, "42"), (FINAL, "42")]


@pytest.mark.parametrize("verification", [{"result": "43"}, {"error": "auth_expired"}])
async def test_parent_rejects_other_account_or_expired_rotated_cookie(verification):
    received = []

    async def rotated(cookie, pixiv_id):
        received.append((cookie, pixiv_id))

    adapter = scripted_adapter([{"result": "42", "cookie_rotation": NEW}, verification], rotated)
    with pytest.raises(AdapterError, match="auth_expired"):
        await adapter.validate()
    assert received == [] and len(adapter.process.commands) == 2


async def test_parent_bounds_rotation_during_validation_and_requires_initial_identity():
    received = []

    async def rotated(cookie, pixiv_id):
        received.append((cookie, pixiv_id))

    adapter = scripted_adapter(
        [{"result": "42", "cookie_rotation": OLD + str(i)} for i in range(5)], rotated
    )
    with pytest.raises(AdapterError, match="auth_expired"):
        await adapter.validate()
    assert received == [] and len(adapter.process.commands) == 5
    adapter = scripted_adapter(
        [{"result": work().model_dump(mode="json"), "cookie_rotation": NEW}], rotated
    )
    with pytest.raises(AdapterError, match="auth_expired"):
        await adapter.artwork("123")
    assert received == [] and len(adapter.process.commands) == 1


@pytest.mark.parametrize("error", ["network", "rate_limited"])
async def test_initial_transient_error_with_rotation_keeps_error_and_retry_policy(
    monkeypatch, error
):
    monkeypatch.setattr("app.adapters.pixiv.transport.random.uniform", lambda low, high: 0.5)
    received, delays = [], []

    async def rotated(cookie, pixiv_id):
        received.append((cookie, pixiv_id))

    async def sleep(delay):
        delays.append(delay)

    async def immediate_retry(operation, **kwargs):
        return await retry(operation, sleep=sleep, **kwargs)

    monkeypatch.setattr(live_module, "retry", immediate_retry)
    adapter = scripted_adapter(
        [
            {"error": error, "retry_after": 3, "cookie_rotation": NEW},
            {"result": "42"},
            {"result": "42"},
        ],
        rotated,
    )
    assert await adapter.validate() == "42"
    assert received == [(NEW, "42")] and delays == [3.5]


async def test_initial_identity_survives_rotation_verification_retry(monkeypatch):
    received = []

    async def rotated(cookie, pixiv_id):
        received.append((cookie, pixiv_id))

    async def sleep(delay):
        pass

    async def immediate_retry(operation, **kwargs):
        return await retry(operation, sleep=sleep, **kwargs)

    monkeypatch.setattr(live_module, "retry", immediate_retry)
    adapter = scripted_adapter(
        [
            {"result": "42", "cookie_rotation": NEW},
            {"error": "network"},
            {"result": "43"},
        ],
        rotated,
    )
    with pytest.raises(AdapterError, match="auth_expired"):
        await adapter.validate()
    assert received == [] and len(adapter.process.commands) == 3


@pytest.mark.parametrize("error", ["network", "rate_limited"])
async def test_validated_rotation_respects_retry_delay_before_identity_request(monkeypatch, error):
    monkeypatch.setattr("app.adapters.pixiv.transport.random.uniform", lambda low, high: 0.5)
    received, delays = [], []

    async def rotated(cookie, pixiv_id):
        received.append((cookie, pixiv_id))

    async def sleep(delay):
        # Only the original request was sent; validation must wait for backoff.
        assert [command["op"] for command in adapter.process.commands] == ["validate", "artwork"]
        delays.append(delay)

    async def immediate_retry(operation, **kwargs):
        return await retry(operation, sleep=sleep, **kwargs)

    monkeypatch.setattr(live_module, "retry", immediate_retry)
    metadata = work().model_dump(mode="json")
    adapter = scripted_adapter(
        [
            {"result": "42"},
            {"error": error, "retry_after": 5, "cookie_rotation": NEW},
            {"result": metadata},
            {"result": "42"},
        ],
        rotated,
    )
    assert await adapter.validate() == "42"
    assert (await adapter.artwork("123")).model_dump(mode="json") == metadata
    assert received == [(NEW, "42")] and delays == [5.5]


@pytest.mark.parametrize("rotate_at", ["validate", "listing", "artwork", "verify"])
async def test_collection_own_rotation_updates_all_guards_and_keeps_secrets_private(
    admin, settings, rotate_at, caplog
):
    original = connect(settings)
    item = work()
    library.ingest(item, "public")
    library.edit_tags([item.id], "로컬 유지", "add")

    class RotatingAdapter(FakeAdapter):
        async def rotate(self, operation):
            if rotate_at == operation:
                await self.on_cookie(NEW, "42")

        async def validate(self):
            await self.rotate("validate")
            return await super().validate()

        async def listing(self, *args):
            await self.rotate("listing")
            return await super().listing(*args)

        async def artwork(self, *args):
            await self.rotate("artwork")
            return await super().artwork(*args)

        async def verify(self, *args):
            await self.rotate("verify")
            return await super().verify(*args)

    adapter = RotatingAdapter(
        {"public": [BookmarkPage(items=[item], next_cursor=None, complete=True)]}
    )
    job = jobs.enqueue_account("sync", "own-rotation", "public", mode="full")
    fetched = []
    await run_job(context(settings, adapter, fetched), job.id)
    result = Job.get_by_id(job.id)
    assert (result.state, result.failed, result.processed) == ("succeeded", 0, 1)
    cookie, ciphertext = accounts.load_credential(settings)
    assert cookie == NEW and ciphertext != original
    assert account_snapshot()[:2] == ("42", "connected")
    asset = Asset.get(Asset.role == "original")
    assert storage.safe_path(settings, asset.path).read_bytes() == image_bytes(0)
    assert len(fetched) == 1
    assert {tag.source for tag in ArtworkTag.select()} == {"local", "pixiv_work"}
    assert {source for tag in library.detail(item.id)["tags"] for source in tag["sources"]} == {
        "local",
        "pixiv_work",
        "pixiv_bookmark",
    }
    for path in (
        "/api/accounts",
        f"/api/jobs/{job.id}",
        f"/api/jobs/{job.id}/events",
        "/api/artworks/123",
        "/api/export/metadata",
    ):
        response = admin.get(path)
        assert response.status_code == 200
        for secret in (OLD, NEW, original, ciphertext, "cookie_rotation"):
            assert secret not in response.text
    assert OLD not in caplog.text and NEW not in caplog.text


async def test_collection_external_rotation_preserves_checkpoint_until_resume(client, settings):
    original = connect(settings)
    object.__setattr__(settings, "browser_url", "http://synthetic-browser:3000")
    pages = [
        BookmarkPage(items=[work()], next_cursor="8"),
        BookmarkPage(items=[work("124")], next_cursor=None, complete=True),
    ]

    class ExternalRotation(FakeAdapter):
        async def listing(self, account_id, scope, cursor=None):
            if cursor == "8":
                accounts.adopt_verified_cookie(settings, EXTERNAL, "42", original)
                # The old job must neither adopt its own late rotation nor move its
                # guard to the independently replaced browser credential.
                await self.on_cookie(NEW, "42")
            return await super().listing(account_id, scope, cursor)

    job = jobs.enqueue_account("sync", "external-rotation", "public")
    fetched = []
    await run_job(context(settings, ExternalRotation({"public": pages}), fetched), job.id)
    result = Job.get_by_id(job.id)
    assert (result.state, result.error_code) == ("waiting_auth", "credential_changed")
    assert CollectionRun.get().cursor == "8"
    assert DownloadTask.get().state == "ready" and len(fetched) == 1
    external_cookie, external_ciphertext = accounts.load_credential(settings)
    assert external_cookie == EXTERNAL and external_ciphertext != original
    assert account_snapshot()[:2] == ("42", "connected")
    jobs.resume_waiting_auth()
    resumed = FakeAdapter({"public": [pages[1]]})
    await run_job(context(settings, resumed, fetched), job.id)
    assert Job.get_by_id(job.id).state == "succeeded"
    assert resumed.cursors == [("public", "8")] and len(fetched) == 2
    assert accounts.load_credential(settings) == (EXTERNAL, external_ciphertext)


@pytest.mark.parametrize("browser_enabled", [False, True])
async def test_collection_external_change_during_download_is_never_success(
    client, settings, browser_enabled
):
    original = connect(settings)
    if browser_enabled:
        object.__setattr__(settings, "browser_url", "http://synthetic-browser:3000")
    job = jobs.enqueue_account("sync", "external-download", "public")
    adapter = FakeAdapter(
        {"public": [BookmarkPage(items=[work()], next_cursor=None, complete=True)]}
    )

    async def fetch(url):
        accounts.adopt_verified_cookie(settings, EXTERNAL, "42", original)
        return image_bytes(0)

    ctx = context(settings, adapter)
    ctx["download"] = fetch
    await run_job(ctx, job.id)
    result = Job.get_by_id(job.id)
    assert (result.state, result.error_code) == ("waiting_auth", "credential_changed")
    assert accounts.load_credential(settings)[0] == EXTERNAL
    assert DownloadTask.get().state == "pending"
    assert Asset.select().count() == 0


async def test_collection_late_mismatch_cannot_replace_current_account_cookie(client, settings):
    original = connect(settings)

    class MismatchedRotation(FakeAdapter):
        async def listing(self, *args):
            await self.on_cookie(NEW, "43")
            return await super().listing(*args)

    job = jobs.enqueue_account("sync", "mismatch-rotation", "public")
    await run_job(context(settings, MismatchedRotation()), job.id)
    assert Job.get_by_id(job.id).state == "waiting_auth"
    assert Job.get_by_id(job.id).error_code == "account_mismatch"
    assert accounts.load_credential(settings) == (OLD, original)
    assert account_snapshot()[:2] == ("42", "connected")
