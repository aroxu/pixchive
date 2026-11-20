import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pixchive_browser.api import create_app

from .conftest import HOME, cookie
from .test_manual_login import assert_quiet, reset_calls, track_browser


def client_for(service, **kwargs):
    return TestClient(
        create_app(service=service, **kwargs),
        client=("127.0.0.1", 19000),
        raise_server_exceptions=True,
    )


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/status", None),
        ("GET", "/credential", None),
        ("POST", "/open", None),
        ("POST", "/login", None),
        ("POST", "/complete", None),
        ("POST", "/resume", {"generation": 0}),
        ("POST", "/reauth", None),
        ("POST", "/seed", {"cookie": "must-not-be-accepted"}),
        ("POST", "/disconnect", None),
    ],
)
def test_every_http_route_requires_control_authorization(service, method, path, payload):
    with client_for(service) as client:
        for headers in (
            {},
            {"Authorization": "Bearer " + "0" * 64},
            {"Authorization": "Basic secret"},
        ):
            response = client.request(method, path, json=payload, headers=headers)
            assert response.status_code == 401
            assert response.headers["Cache-Control"] == "no-store"
    assert service.factory.context.added == []


def test_duplicate_authorization_is_rejected(service, headers):
    header = headers["Authorization"]
    with client_for(service) as client:
        response = client.get(
            "/status", headers=[("Authorization", header), ("Authorization", header)]
        )
        assert response.status_code == 401


def test_api_status_seed_credential_reauth_disconnect_contract(service, headers):
    with client_for(service) as client:
        assert set(client.get("/status", headers=headers).json()) == {
            "state",
            "last_checked",
            "cookie_available",
            "viewing",
            "retry_at",
            "login_pending",
            "login_generation",
        }
        response = client.post("/seed", headers=headers, json={"cookie": "first-cookie"})
        assert response.status_code == 200
        assert response.json()["state"] == "connected"
        response = client.get("/credential", headers=headers)
        assert response.json() == {"cookie": "first-cookie"}
        assert response.headers["Cache-Control"] == "no-store"
        assert client.post("/seed", headers=headers, json={"cookie": "other"}).status_code == 409
        service.factory.context.pages[0].next_cookie = "new-cookie"
        assert client.post("/reauth", headers=headers).json()["state"] == "connected"
        assert client.get("/credential", headers=headers).json() == {"cookie": "new-cookie"}
        response = client.post("/disconnect", headers=headers)
        assert response.status_code == 200
        assert response.json()["state"] == "needs_login"
        assert client.get("/credential", headers=headers).json() == {"cookie": None}
        assert not service.settings.state_file.exists()


def test_api_manual_complete_and_resume_contract(service, headers, monkeypatch):
    # Isolate request calls: the supervisor may correctly poll as soon as resume clears pending.
    monkeypatch.setattr(service, "start", AsyncMock())
    with client_for(service) as client:
        assert client.get("/status", headers=headers).json()["login_pending"] is False
        assert client.post("/open", headers=headers).json()["login_pending"] is True
        context = service.factory.context
        page = context.pages[0]
        page.input_value = "unfinished"
        calls = track_browser(service, monkeypatch)
        response = client.post("/complete", headers=headers)
        assert response.status_code == 200
        assert response.json()["state"] == "needs_login"
        assert response.json()["login_pending"] is True
        generation = response.json()["login_generation"]
        assert page.input_value == "unfinished"
        assert client.get("/credential", headers=headers).json() == {"cookie": None}
        assert (
            client.post("/resume", headers=headers, json={"generation": generation}).status_code
            == 503
        )
        assert not service.settings.state_file.exists()
        reset_calls(calls)
        page.url = HOME
        context.cookie_data = [cookie("synthetic-explicit-cookie")]
        response = client.post("/complete", headers=headers)
        assert response.status_code == 200
        assert response.json()["state"] == "connected"
        assert response.json()["login_pending"] is True
        context.cookies.assert_awaited_once()
        page.evaluate.assert_awaited_once()
        context.storage_state.assert_awaited_once_with(indexed_db=True)
        calls["save"].assert_called_once()
        reset_calls(calls)
        assert client.get("/credential", headers=headers).json() == {
            "cookie": "synthetic-explicit-cookie"
        }
        assert client.post("/reauth", headers=headers).json()["login_pending"] is True
        response = client.post("/resume", headers=headers, json={"generation": generation})
        assert response.status_code == 200
        assert response.json()["login_pending"] is False
        assert response.headers["Cache-Control"] == "no-store"
        assert_quiet(calls)
        assert (
            client.post("/resume", headers=headers, json={"generation": generation}).status_code
            == 503
        )


@pytest.mark.parametrize("action", ["open", "login", "disconnect"])
def test_api_new_manual_action_or_disconnect_invalidates_resume(
    service, headers, monkeypatch, action
):
    with client_for(service) as client:
        assert (
            client.post("/seed", headers=headers, json={"cookie": "synthetic"}).status_code == 200
        )
        assert client.post("/open", headers=headers).json()["login_pending"] is True
        completed = client.post("/complete", headers=headers).json()
        assert completed["state"] == "connected"
        generation = completed["login_generation"]
        assert client.post(f"/{action}", headers=headers).status_code == 200
        calls = track_browser(service, monkeypatch)
        response = client.post("/resume", headers=headers, json={"generation": generation})
        assert response.status_code == 503
        assert response.json() == {"detail": "Browser unavailable"}
        assert response.headers["Cache-Control"] == "no-store"
        assert client.get("/status", headers=headers).json()["login_pending"] is (
            action != "disconnect"
        )
        assert_quiet(calls)


def test_api_resume_is_bound_to_the_completed_generation(service, headers, monkeypatch):
    monkeypatch.setattr(service, "start", AsyncMock())
    with client_for(service) as client:
        assert (
            client.post("/seed", headers=headers, json={"cookie": "synthetic"}).status_code == 200
        )
        opened = client.post("/open", headers=headers).json()
        old_completion = client.post("/complete", headers=headers).json()
        assert old_completion["login_generation"] == opened["login_generation"]
        newer = client.post("/open", headers=headers).json()
        assert newer["login_generation"] == old_completion["login_generation"] + 1
        current = client.post("/complete", headers=headers).json()
        assert current["state"] == "connected"
        calls = track_browser(service, monkeypatch)
        response = client.post(
            "/resume", headers=headers, json={"generation": old_completion["login_generation"]}
        )
        assert response.status_code == 503
        assert response.json() == {"detail": "Browser unavailable"}
        assert client.get("/status", headers=headers).json()["login_pending"] is True
        response = client.post(
            "/resume", headers=headers, json={"generation": current["login_generation"]}
        )
        assert response.status_code == 200
        assert response.json()["login_pending"] is False
        assert_quiet(calls)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"generation": True},
        {"generation": 1.0},
        {"generation": "TOP-SECRET"},
        {"generation": -1},
        {"generation": None},
        {"generation": 1, "unexpected": "TOP-SECRET"},
    ],
)
def test_api_resume_requires_an_explicit_strict_generation(service, headers, monkeypatch, payload):
    with client_for(service) as client:
        assert client.post("/open", headers=headers).status_code == 200
        calls = track_browser(service, monkeypatch)
        response = client.post("/resume", headers=headers, json=payload)
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid request"}
        assert "TOP-SECRET" not in response.text
        assert service.status()["login_pending"] is True
        assert_quiet(calls)


@pytest.mark.parametrize("stage", ["cookies", "evaluate", "storage_state", "save"])
def test_api_completion_failure_is_generic_and_keeps_manual_control(
    service, headers, monkeypatch, caplog, stage
):
    with client_for(service) as client:
        assert client.post("/open", headers=headers).status_code == 200
        context = service.factory.context
        page = context.pages[0]
        page.url = HOME
        context.cookie_data = [cookie("synthetic-cookie")]
        target = service.store if stage == "save" else page if stage == "evaluate" else context

        def fail(*_args, **_kwargs):
            raise RuntimeError("PHPSESSID=TOP-SECRET")

        monkeypatch.setattr(target, stage, fail)
        response = client.post("/complete", headers=headers)
        assert response.status_code == 503
        assert response.json() == {"detail": "Browser unavailable"}
        assert "TOP-SECRET" not in response.text + caplog.text
        assert client.get("/status", headers=headers).json()["login_pending"] is True
        assert client.get("/credential", headers=headers).json() == {"cookie": None}
        generation = service.status()["login_generation"]
        assert (
            client.post("/resume", headers=headers, json={"generation": generation}).status_code
            == 503
        )
        assert not service.settings.state_file.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"cookie": "TOP-SECRET\nCOOKIE"},
        {"cookie": 12345},
        {"cookie": ""},
        {"cookie": "TOP-SECRET", "unexpected": "TOP-SECRET"},
        {"not-cookie": "TOP-SECRET"},
    ],
)
def test_invalid_json_never_echoes_cookie_in_validation_errors(service, headers, payload, caplog):
    with client_for(service) as client:
        response = client.post("/seed", headers=headers, json=payload)
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid request"}
        assert "TOP-SECRET" not in response.text + caplog.text


def test_body_size_and_malformed_json_errors_are_sanitized(service, headers):
    with client_for(service) as client:
        assert client.post("/seed", headers=headers, content=b"x" * 8193).status_code == 413
        response = client.post("/seed", headers=headers, content=b'{"cookie":"SECRET')
        assert response.status_code == 422
        assert "SECRET" not in response.text


def test_public_peer_cannot_read_credential_even_with_control_token(service, headers):
    with TestClient(create_app(service=service), client=("8.8.8.8", 12345)) as client:
        assert client.get("/credential", headers=headers).status_code == 403


def test_exception_and_boot_failure_are_sanitized_and_retryable(service, headers, caplog, capsys):
    service.factory.failure = RuntimeError("PHPSESSID=TOP-SECRET")
    with client_for(service) as client:
        response = client.post("/reauth", headers=headers)
        assert response.status_code == 503
        assert response.json() == {"detail": "Browser unavailable"}
        service.factory.failure = None
        response = client.post("/seed", headers=headers, json={"cookie": "recovered-cookie"})
        assert response.json()["state"] == "connected"
        assert "TOP-SECRET" not in response.text + caplog.text + str(capsys.readouterr())


def test_websocket_authorization_is_checked_before_connecting_to_vnc(service):
    async def never_connect():
        pytest.fail("Unauthenticated WebSocket reached VNC")

    with client_for(service, connector=never_connect) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/view", subprotocols=["binary"]):
                pytest.fail("Unauthenticated WebSocket accepted")
        assert error.value.code == 1008


@pytest.mark.parametrize("manual_pending", [False, True])
def test_binary_websocket_forwards_first_rfb_banner_and_releases_view_count(
    service, headers, monkeypatch, manual_pending
):
    class Writer:
        def __init__(self, reader):
            self.reader = reader
            self.closed = False
            self.messages = []

        def write(self, data):
            self.messages.append(data)
            self.reader.feed_data(b"server-reply")

        async def drain(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    writers = []

    async def connect():
        reader = asyncio.StreamReader()
        reader.feed_data(b"RFB 003.008\n")
        writer = Writer(reader)
        writers.append(writer)
        return reader, writer

    with client_for(service, connector=connect) as client:
        if manual_pending:
            assert client.post("/open", headers=headers).json()["login_pending"] is True
            service.factory.context.pages[0].input_value = "unfinished-login"
            calls = track_browser(service, monkeypatch)
        for _ in range(2):
            with client.websocket_connect(
                "/view", headers=headers, subprotocols=["binary"]
            ) as websocket:
                assert websocket.accepted_subprotocol == "binary"
                assert websocket.receive_bytes() == b"RFB 003.008\n"
                assert service.viewers == 1
                assert client.get("/status", headers=headers).json()["viewing"] is True
                websocket.send_bytes(b"client-message")
                assert websocket.receive_bytes() == b"server-reply"
            status = client.get("/status", headers=headers).json()
            assert status["viewing"] is False
            assert status["login_pending"] is manual_pending
            if manual_pending:
                assert client.post("/open", headers=headers).json()["login_pending"] is True
                assert client.post("/reauth", headers=headers).json()["login_pending"] is True
                assert service.factory.context.pages[0].input_value == "unfinished-login"
                assert_quiet(calls)
    if manual_pending:
        assert_quiet(calls)
    assert all(writer.closed and writer.messages == [b"client-message"] for writer in writers)
    assert service.viewers == 0
