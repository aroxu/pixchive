import base64
import hashlib
import hmac
import json
import os
import tempfile
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet, InvalidToken
from starlette.datastructures import Headers

from pixchive_browser.config import CONTROL_LABEL, STORAGE_LABEL
from pixchive_browser.storage import Secrets, StateStore, filter_state, pixiv_domain, pixiv_origin

from .conftest import cookie


def test_root_only_source_key_can_be_replaced_by_private_derived_keys(settings):
    raw = settings.key_file.read_bytes()
    token = hmac.new(raw, CONTROL_LABEL, hashlib.sha256).hexdigest()
    key = base64.urlsafe_b64encode(hmac.new(raw, STORAGE_LABEL, hashlib.sha256).digest()).decode()
    derived = settings.key_file.parent / "derived.json"
    derived.write_text(json.dumps({"control_token": token, "storage_key": key}))
    secrets = Secrets(settings.key_file.parent / "unreadable-original", derived)
    assert secrets.authorized(Headers({"Authorization": "Bearer " + token}))
    plaintext = b"synthetic-login-state"
    assert Fernet(key.encode()).decrypt(secrets.cipher.encrypt(plaintext)) == plaintext


@pytest.mark.parametrize("token", ["bad", "z" * 64, 12, None])
def test_derived_key_file_rejects_invalid_control_token(settings, token):
    derived = settings.key_file.parent / "derived.json"
    derived.write_text(json.dumps({"control_token": token, "storage_key": "bad"}))
    with pytest.raises((ValueError, TypeError)):
        Secrets(settings.key_file, derived).load()


def test_ciphertext_and_domain_scoped_indexeddb_at_rest(settings):
    secret = Secrets(settings.key_file)
    store = StateStore(settings.state_file, secret)
    state = {
        "cookies": [cookie(), cookie("other-site-secret", domain="pixiv.net.evil.test")],
        "origins": [
            {
                "origin": "https://accounts.pixiv.net",
                "localStorage": [{"name": "sso", "value": "ls-secret"}],
                "indexedDB": [{"name": "idb-secret", "version": 1, "stores": []}],
                "opfs": "drop-this",
            },
            {
                "origin": "https://evil.test",
                "localStorage": [{"name": "bad", "value": "bad-secret"}],
            },
        ],
        "credentials": [{"privateKey": "drop-private-key"}],
    }
    store.save(state)
    encrypted = settings.state_file.read_bytes()
    assert encrypted.startswith(b"gAAAA")
    for plaintext in (
        b"fake-session",
        b"ls-secret",
        b"idb-secret",
        b"bad-secret",
        b"drop-private-key",
    ):
        assert plaintext not in encrypted
    assert store.load() == filter_state(state)
    assert len(list(settings.state_file.parent.iterdir())) == 1
    if os.name == "posix":
        assert settings.state_file.stat().st_mode & 0o777 == 0o600
    raw_key = settings.key_file.read_bytes()
    cipher = Fernet(
        base64.urlsafe_b64encode(hmac.new(raw_key, STORAGE_LABEL, hashlib.sha256).digest())
    )
    decoded = json.loads(cipher.decrypt(encrypted))
    assert len(decoded["cookies"]) == len(decoded["origins"]) == 1
    assert decoded["origins"][0]["indexedDB"][0]["name"] == "idb-secret"
    assert "opfs" not in decoded["origins"][0]
    assert "credentials" not in decoded


def test_raw_key_bytes_are_not_trimmed_for_control_token(settings):
    secrets = Secrets(settings.key_file)
    raw = settings.key_file.read_bytes()
    token = hmac.new(raw, CONTROL_LABEL, hashlib.sha256).hexdigest()
    trimmed = hmac.new(raw.strip(), CONTROL_LABEL, hashlib.sha256).hexdigest()
    assert secrets.authorized(Headers({"Authorization": f"Bearer {token}"}))
    assert not secrets.authorized(Headers({"Authorization": f"Bearer {trimmed}"}))


def test_atomic_replace_preserves_previous_snapshot_on_failure(settings, monkeypatch):
    store = StateStore(settings.state_file, Secrets(settings.key_file))
    store.save({"cookies": [cookie("old-cookie")], "origins": []})
    previous = settings.state_file.read_bytes()

    def fail(*_args):
        raise OSError("test replacement failed")

    monkeypatch.setattr("pixchive_browser.storage.os.replace", fail)
    with pytest.raises(OSError):
        store.save({"cookies": [cookie("new-cookie")], "origins": []})
    assert settings.state_file.read_bytes() == previous
    assert len(list(settings.state_file.parent.iterdir())) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor permissions")
@pytest.mark.parametrize("existing", [False, True])
def test_permissive_tempfile_is_private_before_writing(settings, monkeypatch, existing):
    store = StateStore(settings.state_file, Secrets(settings.key_file))
    if existing:
        store.save({"cookies": [cookie("old-cookie")], "origins": []})
    previous = settings.state_file.read_bytes() if existing else None
    mkstemp, fdopen = tempfile.mkstemp, os.fdopen
    writes = []

    def permissive_mkstemp(*args, **kwargs):
        descriptor, temporary = mkstemp(*args, **kwargs)
        os.fchmod(descriptor, 0o777)
        return descriptor, temporary

    @contextmanager
    def checked_fdopen(descriptor, mode):
        with fdopen(descriptor, mode) as destination:

            def checked_write(payload):
                assert os.fstat(descriptor).st_mode & 0o777 == 0o600
                assert os.fstat(descriptor).st_size == 0
                assert settings.state_file.exists() is existing
                if existing:
                    assert settings.state_file.read_bytes() == previous
                writes.append(payload)
                return destination.write(payload)

            yield SimpleNamespace(
                write=checked_write, flush=destination.flush, fileno=destination.fileno
            )

    state = {"cookies": [cookie("new-cookie")], "origins": []}
    with monkeypatch.context() as patches:
        patches.setattr("pixchive_browser.storage.tempfile.mkstemp", permissive_mkstemp)
        patches.setattr("pixchive_browser.storage.os.fdopen", checked_fdopen)
        store.save(state)

    assert len(writes) == 1
    assert b"new-cookie" not in writes[0]
    assert settings.state_file.stat().st_mode & 0o777 == 0o600
    assert store.load() == state
    assert list(settings.state_file.parent.iterdir()) == [settings.state_file]


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor permissions")
@pytest.mark.parametrize("existing", [False, True])
def test_permission_failure_closes_empty_tempfile_and_preserves_state(
    settings, monkeypatch, existing
):
    store = StateStore(settings.state_file, Secrets(settings.key_file))
    if existing:
        store.save({"cookies": [cookie("old-cookie")], "origins": []})
    previous = settings.state_file.read_bytes() if existing else None
    fingerprint = store.fingerprint
    descriptors = []

    def fail_permissions(descriptor, _mode):
        assert os.fstat(descriptor).st_size == 0
        descriptors.append(descriptor)
        raise PermissionError("synthetic permission failure")

    with monkeypatch.context() as patches:
        patches.setattr("pixchive_browser.storage.os.fchmod", fail_permissions)
        with pytest.raises(PermissionError, match="synthetic permission failure"):
            store.save({"cookies": [cookie("new-cookie")], "origins": []})

    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert store.fingerprint == fingerprint
    assert settings.state_file.exists() is existing
    if existing:
        assert settings.state_file.read_bytes() == previous
    assert list(settings.state_file.parent.iterdir()) == ([settings.state_file] if existing else [])


def test_save_without_posix_descriptor_permissions(settings, monkeypatch):
    platform_os = {**vars(os), "name": "nt"}
    platform_os.pop("fchmod", None)
    store = StateStore(settings.state_file, Secrets(settings.key_file))
    state = {"cookies": [cookie()], "origins": []}
    with monkeypatch.context() as patches:
        patches.setattr("pixchive_browser.storage.os", SimpleNamespace(**platform_os))
        store.save(state)
    assert store.load() == state


def test_corrupt_state_and_wrong_key_fail_closed(settings, tmp_path):
    store = StateStore(settings.state_file, Secrets(settings.key_file))
    store.save({"cookies": [cookie()], "origins": []})
    different_key = tmp_path / "different.key"
    different_key.write_bytes(Fernet.generate_key())
    with pytest.raises(InvalidToken):
        StateStore(settings.state_file, Secrets(different_key)).load()
    settings.state_file.write_bytes(b"not-ciphertext")
    with pytest.raises(InvalidToken):
        store.load()
    assert settings.state_file.read_bytes() == b"not-ciphertext"


def test_unchanged_snapshot_does_not_rewrite_and_delete_removes(settings):
    store = StateStore(settings.state_file, Secrets(settings.key_file))
    state = {"cookies": [cookie()], "origins": []}
    store.save(state)
    previous = settings.state_file.read_bytes()
    store.save(state)
    assert previous == settings.state_file.read_bytes()
    store.delete()
    assert not settings.state_file.exists()
    assert store.load() is None


@pytest.mark.parametrize(
    "domain", ["pixiv.net.evil.test", "evilpixiv.net", "pixiv.net/evil", "pixiv.net:443"]
)
def test_domain_lookalikes_are_excluded(domain):
    assert not pixiv_domain(domain)


@pytest.mark.parametrize(
    "origin",
    [
        "http://www.pixiv.net",
        "https://pixiv.net:444",
        "https://a@pixiv.net",
        "https://pixiv.net/path",
        "https://pixiv.net.evil.test",
        "https://pixiv.net#bad",
    ],
)
def test_non_pixiv_https_origins_are_excluded(origin):
    assert not pixiv_origin(origin)
