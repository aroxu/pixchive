"""Workspace ownership must end before unlink, including on NFS cleanup paths."""

import errno
import json
import os

import pytest

from app.services import tempfiles


def assert_closed(descriptor):
    with pytest.raises(OSError) as error:
        os.fstat(descriptor)
    assert error.value.errno == errno.EBADF


def test_workspace_closes_owner_before_removing_files(settings, monkeypatch):
    settings.data_dir.mkdir(parents=True)
    remove = tempfiles.remove
    descriptor = None
    removed = []

    def nfs_remove(path):
        assert_closed(descriptor)
        removed.append(path)
        return remove(path)

    monkeypatch.setattr(tempfiles, "remove", nfs_remove)
    with tempfiles.workspace(settings, "blob") as workspace:
        descriptor = workspace.lock_fd
        (workspace.path / "payload").write_bytes(b"private staged bytes")
        assert tempfiles.reap(settings)["active"] == 1
    assert len(removed) == 1
    assert list(tempfiles.root(settings).iterdir()) == []


def test_failed_workspace_creation_closes_owner_before_cleanup(settings, monkeypatch):
    settings.data_dir.mkdir(parents=True)
    real_open, remove = os.open, tempfiles.remove
    descriptor = None

    def tracked_open(path, *args, **kwargs):
        nonlocal descriptor
        fd = real_open(path, *args, **kwargs)
        if str(path).endswith(tempfiles.OWNER_LOCK):
            descriptor = fd
        return fd

    def fail_sync(path):
        raise OSError(errno.EIO, "synthetic durability failure")

    def nfs_remove(path):
        assert_closed(descriptor)
        return remove(path)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(tempfiles, "sync_directory", fail_sync)
    monkeypatch.setattr(tempfiles, "remove", nfs_remove)
    with pytest.raises(OSError, match="synthetic durability failure"):
        with tempfiles.workspace(settings, "blob"):
            pytest.fail("Incomplete workspace must not be published")
    assert list(tempfiles.root(settings).iterdir()) == []


def test_reaper_closes_abandoned_owner_before_removal(settings, monkeypatch):
    directory = tempfiles.root(settings, create=True)
    nonce = "a" * 32
    abandoned = directory / f"work-blob-{nonce}"
    abandoned.mkdir()
    (abandoned / tempfiles.OWNER_LOCK).touch()
    (abandoned / tempfiles.MANIFEST).write_text(
        json.dumps({"format": tempfiles.FORMAT, "purpose": "blob", "nonce": nonce})
    )
    real_open, remove = os.open, tempfiles.remove
    descriptor = None

    def tracked_open(path, *args, **kwargs):
        nonlocal descriptor
        fd = real_open(path, *args, **kwargs)
        if str(path).endswith(tempfiles.OWNER_LOCK):
            descriptor = fd
        return fd

    def nfs_remove(path):
        assert_closed(descriptor)
        return remove(path)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(tempfiles, "remove", nfs_remove)
    assert tempfiles.reap(settings) == {"removed": 1, "failed": 0, "active": 0, "unknown": 0}
    assert list(directory.iterdir()) == []
