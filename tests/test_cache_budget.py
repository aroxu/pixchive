"""Local scratch and archive space must each cover live storage reservations."""

import asyncio
import errno
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.config import Settings
from app.core.db import db, initialize
from app.core.time import now
from app.models.entities import Job, StorageReservation
from app.services import storage_budget
from app.services.execution import Interrupted


@pytest.fixture
def clean_settings_env(monkeypatch, tmp_path):
    for key in os.environ:
        if key.startswith("PIXCHIVE_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("PIXCHIVE_DATA_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("PIXCHIVE_KEY_FILE", str(tmp_path / "unused-key"))
    monkeypatch.setenv("PIXCHIVE_SETUP_TOKEN_FILE", str(tmp_path / "unused-setup-token"))


@pytest.mark.parametrize("value", [None, "", " \t "])
def test_from_env_defaults_to_no_cache(clean_settings_env, monkeypatch, tmp_path, value):
    if value is not None:
        monkeypatch.setenv("PIXCHIVE_CACHE_DIR", value)
    settings = Settings.from_env()
    assert settings.cache_dir is None
    assert settings.data_dir == tmp_path / "archive"
    assert not settings.data_dir.exists()
    assert (
        Settings(
            data_dir=settings.data_dir,
            key_file=settings.key_file,
            setup_token_file=settings.setup_token_file,
        ).cache_dir
        is None
    )


@pytest.mark.parametrize("relative", [False, True])
def test_from_env_accepts_configured_cache_path(
    clean_settings_env, monkeypatch, tmp_path, relative
):
    expected = Path("local scratch") if relative else tmp_path / "local scratch"
    monkeypatch.setenv("PIXCHIVE_CACHE_DIR", f"  {expected}  ")
    settings = Settings.from_env()
    assert settings.cache_dir == expected
    assert isinstance(settings.cache_dir, Path)
    assert settings.data_dir == tmp_path / "archive"
    assert not settings.data_dir.exists()
    assert not (tmp_path / "local scratch").exists()


@pytest.fixture
def budget_settings(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "archive",
        cache_dir=tmp_path / "cache",
        key_file=tmp_path / "unused-key",
        setup_token_file=tmp_path / "unused-setup-token",
        min_free_bytes=100,
    )
    settings.cache_dir.mkdir()
    initialize(settings.data_dir)
    with db.connection_context():
        db.create_tables([Job, StorageReservation])
        yield settings


@pytest.fixture
def disk_spaces(monkeypatch, budget_settings):
    calls = []

    def configure(archive_free, cache_free):
        values = {
            budget_settings.data_dir: archive_free,
            budget_settings.cache_dir: cache_free,
        }

        def disk_usage(path):
            path = Path(path)
            assert path in values, f"Unexpected storage path: {path}"
            calls.append(path)
            free = values[path]
            return SimpleNamespace(total=100_000, used=100_000 - free, free=free)

        monkeypatch.setattr(storage_budget.shutil, "disk_usage", disk_usage)
        return calls

    return configure


def running_job(name):
    return Job.create(
        kind="sync",
        state="running",
        idempotency_key=f"cache-budget:{name}",
        lease_key=f"cache-budget:{name}",
        owner=f"cache-budget-owner:{name}",
        lease_until=now() + 600,
    )


def assert_budget(job, *, free, additional, reserved=0):
    budget = storage_budget.view(Job.get_by_id(job.id).storage_budget)
    assert budget is not None
    assert budget == {
        "free_bytes": free,
        "additional_bytes": additional,
        "reserved_bytes": reserved,
        "reserve_bytes": 100,
        "checked_at": budget["checked_at"],
    }
    assert 0 < budget["checked_at"] <= now()


def reservation(job):
    return StorageReservation.get_or_none(StorageReservation.job == job.id)


@pytest.mark.parametrize("archive_free,cache_free", [(10_000, 149), (149, 10_000), (100, 100)])
def test_shortage_in_either_filesystem_blocks_before_writer_and_leaves_no_reservation(
    budget_settings, disk_spaces, tmp_path, archive_free, cache_free
):
    checked = disk_spaces(archive_free, cache_free)
    job = running_job("blocked")
    output = tmp_path / "must-not-be-written"
    writer = Mock(wraps=output.write_bytes)
    with pytest.raises(OSError) as error:
        with storage_budget.hold(budget_settings, job.id, job.owner, 50, lambda: None):
            writer(b"synthetic media")
    assert error.value.errno == errno.ENOSPC
    writer.assert_not_called()
    assert not output.exists()
    assert StorageReservation.select().count() == 0
    assert storage_budget.total() == 0
    assert checked == [budget_settings.data_dir, budget_settings.cache_dir]
    assert_budget(job, free=min(archive_free, cache_free), additional=50)


def test_unconfigured_cache_uses_only_archive_space(budget_settings, disk_spaces, tmp_path):
    checked = disk_spaces(150, 0)
    settings = replace(budget_settings, cache_dir=None)
    job = running_job("archive-only")
    output = tmp_path / "archive-only-output"
    with storage_budget.hold(settings, job.id, job.owner, 50, lambda: None):
        output.write_bytes(b"synthetic media")
        assert reservation(job).bytes == 50
        assert storage_budget.total() == 50
        assert_budget(job, free=150, additional=50)
    assert output.read_bytes() == b"synthetic media"
    assert checked == [settings.data_dir]
    assert not StorageReservation.select().exists()


@pytest.mark.parametrize("archive_free,cache_free", [(600, 1_000), (1_000, 600), (600, 600)])
def test_live_jobs_and_nested_holds_share_the_tighter_filesystem_budget(
    budget_settings, disk_spaces, tmp_path, archive_free, cache_free
):
    disk_spaces(archive_free, cache_free)
    other = running_job("other")
    current = running_job("current")
    check = Mock()
    with storage_budget.hold(budget_settings, other.id, other.owner, 100, check):
        assert storage_budget.total() == 100
        with storage_budget.hold(budget_settings, current.id, current.owner, 200, check):
            assert reservation(current).bytes == 200
            assert storage_budget.total() == 300
            assert_budget(current, free=600, additional=200, reserved=100)
            with storage_budget.hold(budget_settings, current.id, current.owner, 200, check):
                # 400 current + 100 other + 100 reserve exactly fills the limit.
                assert reservation(current).bytes == 400
                assert reservation(other).bytes == 100
                assert storage_budget.total() == 500
                assert not db.in_transaction()
                assert_budget(current, free=600, additional=400, reserved=100)
                (tmp_path / "allowed-output").write_bytes(b"synthetic media")
            assert reservation(current).bytes == 200
            assert reservation(other).bytes == 100
            assert storage_budget.total() == 300
        assert reservation(current) is None
        assert reservation(other).bytes == 100
        assert storage_budget.total() == 100
    assert not StorageReservation.select().exists()
    assert storage_budget.total() == 0


@pytest.mark.parametrize("archive_free,cache_free", [(600, 10_000), (10_000, 600)])
def test_other_live_job_can_exhaust_either_filesystem(
    budget_settings, disk_spaces, archive_free, cache_free
):
    disk_spaces(archive_free, cache_free)
    other = running_job("other")
    current = running_job("blocked-current")
    writer = Mock()
    with storage_budget.hold(budget_settings, other.id, other.owner, 300, lambda: None):
        with pytest.raises(OSError) as error:
            with storage_budget.hold(budget_settings, current.id, current.owner, 201, lambda: None):
                writer()
        assert error.value.errno == errno.ENOSPC
        writer.assert_not_called()
        assert reservation(current) is None
        assert reservation(other).bytes == 300
        assert storage_budget.total() == 300
        assert_budget(current, free=600, additional=201, reserved=300)
    assert not StorageReservation.select().exists()


@pytest.mark.parametrize("archive_free,cache_free", [(600, 10_000), (10_000, 600)])
def test_failed_nested_hold_preserves_outer_and_other_live_reservations(
    budget_settings, disk_spaces, archive_free, cache_free
):
    disk_spaces(archive_free, cache_free)
    other = running_job("other")
    current = running_job("nested-current")
    writer = Mock()
    with storage_budget.hold(budget_settings, other.id, other.owner, 100, lambda: None):
        with storage_budget.hold(budget_settings, current.id, current.owner, 200, lambda: None):
            with pytest.raises(OSError) as error:
                with storage_budget.hold(
                    budget_settings, current.id, current.owner, 201, lambda: None
                ):
                    writer()
            assert error.value.errno == errno.ENOSPC
            writer.assert_not_called()
            assert reservation(current).bytes == 200
            assert reservation(other).bytes == 100
            assert storage_budget.total() == 300
            assert_budget(current, free=600, additional=401, reserved=100)
            with storage_budget.hold(budget_settings, current.id, current.owner, 200, lambda: None):
                assert reservation(current).bytes == 400
                assert storage_budget.total() == 500
            assert reservation(current).bytes == 200
        assert reservation(current) is None
        assert reservation(other).bytes == 100
    assert not StorageReservation.select().exists()


@pytest.mark.parametrize("failure", [RuntimeError, Interrupted, asyncio.CancelledError])
def test_writer_failure_or_interruption_unwinds_nested_reservations(
    budget_settings, disk_spaces, failure
):
    disk_spaces(1_000, 600)
    other = running_job("other")
    current = running_job("interrupted")
    with storage_budget.hold(budget_settings, other.id, other.owner, 100, lambda: None):
        with pytest.raises(failure):
            with storage_budget.hold(budget_settings, current.id, current.owner, 200, lambda: None):
                with storage_budget.hold(
                    budget_settings, current.id, current.owner, 200, lambda: None
                ):
                    assert reservation(current).bytes == 400
                    raise failure()
        assert reservation(current) is None
        assert reservation(other).bytes == 100
        assert storage_budget.total() == 100
    assert not StorageReservation.select().exists()
