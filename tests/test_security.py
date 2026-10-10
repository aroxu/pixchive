from dataclasses import replace

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings, normalize_origin
from app.models.entities import Credential, Session
from app.services.fixtures import seed

TRUSTED_ORIGINS = tuple(
    f"{scheme}://{host}{suffix}"
    for host in ("localhost", "archive-server", "archive-server.example.net")
    for scheme in ("http", "https")
    for suffix in ("", ":8000")
)


@pytest.mark.parametrize("origin", TRUSTED_ORIGINS)
def test_explicit_trusted_origins_still_require_session_and_csrf(admin, settings, origin):
    admin.app.state.settings = replace(settings, allowed_origins=TRUSTED_ORIGINS)
    rejected = admin.patch(
        "/api/settings",
        json={"show_sensitive": False},
        headers={"Origin": origin, "X-CSRF-Token": "wrong"},
    )
    assert rejected.status_code == 403 and rejected.json()["code"] == "csrf_failed"
    accepted = admin.patch(
        "/api/settings", json={"show_sensitive": False}, headers={"Origin": origin}
    )
    assert accepted.status_code == 200
    admin.cookies.clear()
    assert (
        admin.patch(
            "/api/settings", json={"show_sensitive": False}, headers={"Origin": origin}
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "",
        "http://evil.invalid",
        "http://localhost:8001",
        "http://archive-server.evil.invalid:8000",
        "http://archive-server:8001",
        "http://archive-server.example.net.evil.invalid",
        "http://archive-server.example.net:8001",
        "http://archive-server:8000/path",
        "http://archive-server:8000?query=1",
        "http://archive-server:8000#fragment",
        "http://archive-server:8000@evil.invalid",
        "http://archive-server:",
        "http://archive-server:65536",
        "http://archive-server:bad",
        "http://archive-server:8000,http://evil.invalid",
        "file://archive-server",
    ],
)
def test_trusted_origin_list_does_not_accept_other_ports_suffixes_or_invalid_urls(
    admin, settings, origin
):
    admin.app.state.settings = replace(settings, allowed_origins=TRUSTED_ORIGINS)
    response = admin.patch(
        "/api/settings", json={"show_sensitive": False}, headers={"Origin": origin}
    )
    assert response.status_code == 403 and response.json()["code"] == "origin_rejected"


def test_missing_origin_is_rejected_even_when_other_origins_are_configured(admin, settings):
    admin.app.state.settings = replace(settings, allowed_origins=TRUSTED_ORIGINS)
    del admin.headers["Origin"]
    assert admin.patch("/api/settings", json={"show_sensitive": False}).status_code == 403


def test_origin_config_normalizes_default_ports_and_preserves_explicit_port(monkeypatch):
    monkeypatch.setenv("PIXCHIVE_PUBLIC_URL", "http://localhost:80/")
    monkeypatch.setenv(
        "PIXCHIVE_ALLOWED_ORIGINS",
        "https://ARCHIVE-SERVER:443/, http://archive-server:8000, https://archive-server",
    )
    settings = Settings.from_env()
    assert settings.public_url == "http://localhost"
    assert settings.allowed_origins == ("https://archive-server", "http://archive-server:8000")
    assert settings.allows_origin("https://archive-server:443")
    assert not settings.allows_origin(None)
    assert normalize_origin("http://[::1]:8000") == "http://[::1]:8000"
    monkeypatch.setenv("PIXCHIVE_ALLOWED_ORIGINS", "http://archive-server:8000/path")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_https_alias_login_sets_secure_cookie(admin, settings):
    admin.app.state.settings = replace(settings, allowed_origins=TRUSTED_ORIGINS)
    response = admin.post(
        "/api/session",
        headers={"Origin": "https://archive-server.example.net"},
        json={"username": "admin", "password": "a-synthetic-long-password"},
    )
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/artworks",
        "/api/artworks/900000000",
        "/api/artists",
        "/api/tags",
        "/api/jobs",
        "/api/settings",
        "/api/accounts",
        "/api/media/unknown",
        "/api/system/status",
    ],
)
def test_private_routes_require_login(client, path):
    assert client.get(path).status_code == 401


def test_setup_is_one_time_and_requires_secret(admin):
    response = admin.post(
        "/api/setup",
        json={
            "username": "other",
            "password": "other-password-long",
            "setup_token": "synthetic-setup-token-for-tests",
        },
    )
    assert response.status_code == 409


def test_setup_refuses_wrong_secret(client):
    assert (
        client.post(
            "/api/setup",
            json={
                "username": "admin",
                "password": "long-password-here",
                "setup_token": "incorrect-secret-token",
            },
        ).status_code
        == 403
    )
    assert client.get("/api/session").json()["setup_required"]


def test_csrf_and_origin_required(admin):
    assert (
        admin.patch(
            "/api/settings",
            json={"show_sensitive": True},
            headers={"Origin": "https://evil.invalid"},
        ).status_code
        == 403
    )
    assert (
        admin.patch(
            "/api/settings", json={"show_sensitive": True}, headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    assert admin.patch("/api/settings", json={"show_sensitive": True}).status_code == 200


def test_validation_never_echoes_secret(admin):
    secret = "sensitive-session-cookie"
    result = admin.put(
        "/api/accounts/1/credential",
        json={
            "phpsessid": secret,
            "extra": secret,
        },
    )
    assert result.status_code == 422
    assert secret not in result.text


def test_credentials_encrypted_and_disconnect_preserves_archive(admin, settings):
    seed(settings)
    token = "synthetic-pixiv-session-cookie"
    result = admin.put("/api/accounts/1/credential", json={"phpsessid": token})
    assert result.status_code == 200
    assert result.json()["status"] == "unverified"
    assert result.json()["validated_at"] is None
    assert result.json()["credential_kind"] == "pixiv_cookie"
    assert token not in result.text
    ciphertext = Credential.get().ciphertext
    assert ciphertext != token
    assert Fernet(settings.key_file.read_bytes()).decrypt(ciphertext.encode()).decode() == token
    assert token not in admin.get("/api/accounts").text
    assert admin.delete("/api/accounts/1/credential").status_code == 204
    assert admin.get("/api/artworks").json()["total"] == 6
    assert Credential.select().count() == 0


@pytest.mark.parametrize(
    "value",
    [
        "PHPSESSID=synthetic-cookie-value",
        "session-cookie; another=secret",
        "session-cookie\r\nX-Secret: injected",
        " short-cookie ",
        "한글-cookie-value",
    ],
)
def test_cookie_input_rejects_headers_and_never_echoes_value(admin, value):
    result = admin.put("/api/accounts/1/credential", json={"phpsessid": value})
    assert result.status_code == 422
    assert value not in result.text
    assert Credential.select().count() == 0


def test_legacy_credential_cannot_be_sent_as_cookie(admin, settings):
    from app.core.errors import DomainError
    from app.models.entities import PixivAccount
    from app.services import accounts

    legacy = "previous-refresh-secret"
    PixivAccount.create(id=1, status="cookie_required")
    Credential.create(
        account=1,
        kind="refresh_token",
        ciphertext=Fernet(settings.key_file.read_bytes()).encrypt(legacy.encode()).decode(),
    )
    with pytest.raises(DomainError, match="cookie_required"):
        accounts.load_credential(settings)
    response = admin.post("/api/accounts/1/validate", headers={"Idempotency-Key": "old"})
    assert response.status_code == 409 and legacy not in response.text
    assert (
        admin.put("/api/accounts/1/credential", json={"refresh_token": legacy}).status_code == 422
    )
    result = admin.put(
        "/api/accounts/1/credential", json={"phpsessid": "replacement-session-cookie"}
    )
    assert result.status_code == 200 and result.json()["status"] == "unverified"
    assert accounts.load_credential(settings)[0] == "replacement-session-cookie"


def test_logout_revokes_media_and_session(admin, settings):
    seed(settings)
    url = admin.get("/api/artworks").json()["items"][0]["thumbnail_url"]
    assert admin.get(url).status_code == 200
    raw = admin.cookies.get("pixchive_session")
    assert Session.get().id != raw
    assert admin.delete("/api/session").status_code == 204
    assert admin.get(url).status_code == 401
    assert not admin.get("/api/session").json()["authenticated"]
    response = admin.post(
        "/api/session", json={"username": "admin", "password": "a-synthetic-long-password"}
    )
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]


def test_request_limit_and_login_throttle(client):
    assert client.post("/api/setup", content=b"x" * 32769).status_code == 413
    for _ in range(20):
        assert (
            client.post(
                "/api/session",
                json={
                    "username": "no-admin",
                    "password": "not-a-real-password",
                },
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/session", json={"username": "no-admin", "password": "not-a-real-password"}
        ).status_code
        == 429
    )


def test_unknown_api_is_not_spa(client):
    assert client.get("/api/nonexistent").status_code == 404


def test_password_whitespace_is_not_silently_changed(client):
    password = "  preserve-password-spaces  "
    response = client.post(
        "/api/setup",
        json={
            "username": "admin",
            "password": password,
            "setup_token": "synthetic-setup-token-for-tests",
        },
    )
    assert response.status_code == 201
    assert (
        client.post(
            "/api/session",
            json={
                "username": "admin",
                "password": password.strip(),
            },
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/session",
            json={
                "username": "admin",
                "password": password,
            },
        ).status_code
        == 200
    )
