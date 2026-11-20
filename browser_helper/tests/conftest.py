import copy
import hashlib
import hmac
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet

from pixchive_browser.config import CONTROL_LABEL, HOME, LOGIN, Settings
from pixchive_browser.service import BrowserService


def cookie(value="101_fake-session", **overrides):
    return {
        "name": "PHPSESSID",
        "value": value,
        "domain": "www.pixiv.net",
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
        **overrides,
    }


class FakeResponse:
    status = 200


class FakePage:
    def __init__(self, context):
        self.context = context
        self.url = "about:blank"
        self.closed = False
        self.visits = []
        self.snapshot = {"identity_known": True, "authenticated": True}
        self.next_cookie = None
        self.navigation_error = None

    def is_closed(self):
        return self.closed

    async def goto(self, url, **_options):
        self.visits.append(url)
        if self.navigation_error:
            raise self.navigation_error
        self.url = url
        if self.next_cookie is not None:
            self.context.cookie_data = [cookie(self.next_cookie)]
            self.next_cookie = None
        return FakeResponse()

    async def bring_to_front(self):
        pass

    async def evaluate(self, _script):
        return {"url": self.url, **self.snapshot}


class FakeContext:
    def __init__(self):
        self.cookie_data = []
        self.origins = []
        self.pages = [FakePage(self)]
        self.restorations = []
        self.storage_options = []
        self.added = []

    async def cookies(self, _urls):
        return copy.deepcopy(self.cookie_data)

    async def storage_state(self, **options):
        assert "path" not in options
        self.storage_options.append(options)
        return copy.deepcopy({"cookies": self.cookie_data, "origins": self.origins})

    async def set_storage_state(self, storage_state):
        self.restorations.append(copy.deepcopy(storage_state))
        self.cookie_data = copy.deepcopy(storage_state["cookies"])
        self.origins = copy.deepcopy(storage_state["origins"])

    async def add_cookies(self, cookies):
        self.added.extend(copy.deepcopy(cookies))
        self.cookie_data.extend(copy.deepcopy(cookies))

    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page


class FakeSession:
    def __init__(self, context):
        self.context = context
        self.closed = False
        self.reset = False

    async def close(self, *, reset=False):
        self.closed = True
        self.reset = reset


class FakeFactory:
    def __init__(self, context=None):
        self.context = context or FakeContext()
        self.sessions = []
        self.failure = None
        self.resets = 0

    async def start(self):
        if self.failure:
            raise self.failure
        if self.sessions and self.sessions[-1].reset:
            self.context = FakeContext()
        session = FakeSession(self.context)
        self.sessions.append(session)
        return session

    async def reset(self):
        self.resets += 1


@pytest.fixture
def settings(tmp_path):
    key_file = tmp_path / "new-test.key"
    key_file.write_bytes(Fernet.generate_key() + b"\n")
    return Settings(
        key_file=key_file,
        state_file=tmp_path / "state" / "auth-state.enc",
        profile=tmp_path / "profile",
        poll_seconds=0.02,
        retry_seconds=0,
    )


@pytest.fixture
def service(settings):
    return BrowserService(settings, factory=FakeFactory())


@pytest.fixture
def headers(settings):
    token = hmac.new(settings.key_file.read_bytes(), CONTROL_LABEL, hashlib.sha256).hexdigest()
    return {"Authorization": f"Bearer {token}"}


async def connect(service, value="101_fake-session"):
    result = await service.seed(value)
    assert result["state"] == "connected"
    assert service.factory.context.pages[0].visits[-1] == HOME
    return result


__all__ = ["HOME", "LOGIN", "FakeFactory", "connect", "cookie", "replace"]
