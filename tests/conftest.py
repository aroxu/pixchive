import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import db
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    key = tmp_path / "secrets" / "key"
    key.parent.mkdir()
    key.write_bytes(Fernet.generate_key())
    setup = key.parent / "setup"
    setup.write_text("synthetic-setup-token-for-tests")
    return Settings(
        data_dir=tmp_path / "data",
        key_file=key,
        setup_token_file=setup,
        public_url="http://testserver",
        frontend_dir=tmp_path / "frontend",
    )


@pytest.fixture
def client(settings):
    # TestClient runs the app in its own thread. Close the test thread's prior
    # connection before the next app lifespan rebinds the global database path.
    if not db.is_closed():
        db.close()
    with TestClient(create_app(settings)) as client:
        client.headers["Origin"] = "http://testserver"
        yield client
    if not db.is_closed():
        db.close()


@pytest.fixture
def admin(client):
    response = client.post(
        "/api/setup",
        json={
            "username": "admin",
            "password": "a-synthetic-long-password",
            "setup_token": "synthetic-setup-token-for-tests",
        },
    )
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client
