"""Bounded blob batches retain ownership without retaining unused payload bytes."""

import asyncio
import contextlib
import errno
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from app.core.db import db
from app.core.errors import DomainError
from app.models.entities import Asset, Job, UgoiraBundle
from app.services import library, storage, tempfiles, ugoira
from app.services.execution import Interrupted, disk_operation
from app.worker import run_job
from tests.test_library import remote
from tests.ugoira_fixtures import animation, zip_bytes


def png(color="teal"):
    output = io.BytesIO()
    Image.new("RGB", (32, 48), color).save(output, "PNG")
    return output.getvalue()


@pytest.fixture
def artwork(client):
    library.ingest(remote(), "public")
    return "123"


@pytest.fixture
def creations(monkeypatch):
    created = []
    workspace = tempfiles.workspace

    @contextmanager
    def tracked(settings, purpose, guard=None):
        with workspace(settings, purpose, guard) as working:
            created.append((purpose, working))
            yield working

    monkeypatch.setattr(tempfiles, "workspace", tracked)
    return created


def metadata_only(working):
    assert {entry.name for entry in working.path.iterdir()} == {
        tempfiles.MANIFEST,
        tempfiles.OWNER_LOCK,
    }


def assert_closed(fd):
    with pytest.raises(OSError) as error:
        os.fstat(fd)
    assert error.value.errno == errno.EBADF


def connected(fn, *args, **kwargs):
    with db.connection_context():
        return fn(*args, **kwargs)


@pytest.mark.parametrize("use_cache", [False, True])
async def test_real_images_and_thumbnails_share_one_caller_owned_workspace(
    artwork, settings, creations, use_cache
):
    if use_cache:
        settings = replace(settings, cache_dir=settings.data_dir.parent / "cache")
    before = tempfiles.active_fds()
    guards = []

    def check():
        guards.append(True)

    with tempfiles.reuse_blobs(settings, guard=check) as working:
        owners = [working]
        if use_cache:
            with tempfiles.publication_workspace(settings) as publication:
                owners.append(publication)
        assert tempfiles.active_fds() == (*before, *(owner.lock_fd for owner in owners))
        for color in ("red", "green", "blue"):
            source = await disk_operation(
                connected, storage.store_image, settings, artwork, 0, png(color), guard=check
            )
            thumbnail = await disk_operation(
                connected, storage.make_thumbnail, settings, artwork, source, check
            )
            assert source.role == "original" and thumbnail.role == "thumbnail"
            assert Asset.get_by_id(source.id).thumbnail_id == thumbnail.id
            assert storage.inspect_asset(settings, source) == "ready"
            assert storage.inspect_asset(settings, thumbnail) == "ready"
            metadata_only(working)
            for owner in owners:
                metadata_only(owner)
            assert tempfiles.reap(settings)["active"] == len(owners)
        assert [purpose for purpose, _ in creations] == ["blob"] * len(owners)
    assert guards
    assert tempfiles.active_fds() == before
    for owner in owners:
        assert_closed(owner.lock_fd)
    assert list(tempfiles.root(settings).iterdir()) == []
    assert Asset.select().count() == 6


def test_duplicates_use_unique_temporary_files_without_retaining_bytes(
    artwork, settings, monkeypatch
):
    opened = []
    original_open = Path.open

    def track(path, mode="r", *args, **kwargs):
        if mode == "xb" and path.suffix == ".part":
            opened.append(path)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", track)
    with tempfiles.reuse_blobs(settings) as working:
        identifiers = {storage.store_image(settings, artwork, 0, png()).id for _ in range(12)}
        metadata_only(working)
        assert len(identifiers) == 1
        assert len(opened) == len(set(opened)) == 12
        assert {path.parent for path in opened} == {working.path}
    assert Asset.select().count() == 1


def test_rejected_record_and_unknown_destination_leave_no_temporary_bytes(artwork, settings):
    with tempfiles.reuse_blobs(settings) as working:
        first = storage.store_image(settings, artwork, 0, png())
        with pytest.raises(DomainError, match="integrity_error"):
            storage.store_image(settings, artwork, 0, png("red"), revision=first.revision)
        metadata_only(working)
        assert storage.safe_path(settings, first.path).read_bytes() == png()

        content = png("blue")
        sha = hashlib.sha256(content).hexdigest()
        destination = storage.safe_path(settings, f"media/456/{artwork}/original/{sha}/p0000.png")
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"unrecorded bytes")
        with pytest.raises(DomainError, match="integrity_error"):
            storage.store_image(settings, artwork, 0, content)
        metadata_only(working)
        assert destination.read_bytes() == b"unrecorded bytes"
        assert Asset.select().count() == 1


def test_guard_after_staging_removes_unused_payload_and_keeps_staged_record(artwork, settings):
    staged = []

    def check():
        if staged:
            raise Interrupted()

    with tempfiles.reuse_blobs(settings, guard=check) as working:
        with pytest.raises(Interrupted):
            storage.store_image(settings, artwork, 0, png(), guard=check, on_staged=staged.append)
        metadata_only(working)
        asset = Asset.get_by_id(staged[0].id)
        assert asset.state == "staged"
        assert not storage.safe_path(settings, asset.path).exists()
    assert not working.path.exists()


def test_failed_payload_fsync_closes_file_before_unlink(artwork, settings, monkeypatch):
    fsync = os.fsync
    unlink = Path.unlink
    descriptors = []

    def fail_payload(fd):
        if os.readlink(f"/proc/self/fd/{fd}").endswith(".part"):
            descriptors.append(fd)
            raise OSError(errno.ENOSPC, "synthetic full storage")
        return fsync(fd)

    def nfs_unlink(path, *args, **kwargs):
        if path.suffix == ".part":
            assert_closed(descriptors[-1])
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", fail_payload)
    monkeypatch.setattr(Path, "unlink", nfs_unlink)
    with tempfiles.reuse_blobs(settings) as working:
        with pytest.raises(OSError, match="synthetic full storage"):
            storage.store_image(settings, artwork, 0, png())
        metadata_only(working)
        assert Asset.select().count() == 0
    assert descriptors


def test_atomic_move_crash_and_repair_keep_existing_verification(artwork, settings):
    def crash():
        raise RuntimeError("synthetic crash after move")

    with tempfiles.reuse_blobs(settings) as working:
        with pytest.raises(RuntimeError, match="synthetic crash after move"):
            storage.store_image(settings, artwork, 0, png(), after_move=crash)
        metadata_only(working)
        asset = Asset.get()
        assert asset.state == "staged"
        assert storage.inspect_asset(settings, asset) == "ready"
        storage.reconcile(settings)
        assert Asset.get_by_id(asset.id).state == "ready"
        path = storage.safe_path(settings, asset.path)
        path.write_bytes(b"!" * asset.size)
        assert storage.verify_asset(settings, asset) == "corrupt"
        repaired = storage.store_image(settings, artwork, 0, png())
        assert repaired.id == asset.id and repaired.state == "ready"
        assert storage.inspect_asset(settings, repaired) == "ready"
        metadata_only(working)


async def test_concurrent_real_stores_serialize_before_opening_payloads(
    artwork, settings, monkeypatch
):
    staged, release, attempted, second_opened = (threading.Event() for _ in range(4))
    opened = []
    original_open = Path.open

    def track(path, mode="r", *args, **kwargs):
        if mode == "xb" and path.suffix == ".part":
            opened.append(path)
            if len(opened) == 2:
                second_opened.set()
        return original_open(path, mode, *args, **kwargs)

    def hold(asset):
        staged.set()
        assert release.wait(5)

    monkeypatch.setattr(Path, "open", track)
    with tempfiles.reuse_blobs(settings) as working:
        first = asyncio.create_task(
            disk_operation(
                connected, storage.store_image, settings, artwork, 0, png(), on_staged=hold
            )
        )
        second = None
        try:
            assert await asyncio.to_thread(staged.wait, 3)
            second = asyncio.create_task(
                disk_operation(
                    connected,
                    storage.store_image,
                    settings,
                    artwork,
                    0,
                    png("red"),
                    guard=attempted.set,
                )
            )
            assert await asyncio.to_thread(attempted.wait, 3)
            assert not await asyncio.to_thread(second_opened.wait, 0.15)
        finally:
            release.set()
            results = await asyncio.gather(
                *[task for task in (first, second) if task is not None], return_exceptions=True
            )
        assert len(results) == 2 and all(isinstance(result, Asset) for result in results)
        assert second_opened.is_set()
        assert len(opened) == 2 and opened[0] != opened[1]
        metadata_only(working)


async def test_waiting_borrower_can_be_interrupted(settings):
    settings.data_dir.mkdir()
    entered, release = threading.Event(), threading.Event()
    checks = []

    def hold():
        with tempfiles.blob_workspace(settings):
            entered.set()
            assert release.wait(5)

    def check():
        checks.append(True)
        if len(checks) == 2:
            raise Interrupted()

    def borrow():
        with tempfiles.blob_workspace(settings, check):
            pytest.fail("The interrupted borrower must not enter")

    with tempfiles.reuse_blobs(settings) as working:
        holder = asyncio.create_task(disk_operation(hold))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            with pytest.raises(Interrupted):
                await asyncio.wait_for(disk_operation(borrow), 2)
            metadata_only(working)
        finally:
            release.set()
            await holder
    assert len(checks) == 2


@pytest.mark.parametrize("use_cache", [False, True])
async def test_repeated_cancel_settles_disk_write_before_scope_cleanup(
    artwork, settings, creations, monkeypatch, use_cache
):
    if use_cache:
        settings = replace(settings, cache_dir=settings.data_dir.parent / "cache")
    staged, release, attempted = (threading.Event() for _ in range(3))
    completed = []
    workspaces = []
    remove = tempfiles.remove

    def hold(asset):
        staged.set()
        assert release.wait(5)

    def write(color, **kwargs):
        try:
            return connected(storage.store_image, settings, artwork, 0, png(color), **kwargs)
        finally:
            completed.append(color)

    def settled_remove(path):
        assert sorted(completed) == ["red", "teal"]
        owners = {owner.path.name.rsplit("-", 1)[1]: owner.lock_fd for _, owner in creations}
        assert_closed(owners[path.name.rsplit("-", 1)[1]])
        return remove(path)

    async def batch():
        with tempfiles.reuse_blobs(settings) as working:
            workspaces.append(working)
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(disk_operation(write, "teal", on_staged=hold))
                assert await asyncio.to_thread(staged.wait, 3)
                tasks.create_task(disk_operation(write, "red", guard=attempted.set))
                await asyncio.Event().wait()

    monkeypatch.setattr(tempfiles, "remove", settled_remove)
    task = asyncio.create_task(batch())
    try:
        assert await asyncio.to_thread(attempted.wait, 3)
        working = workspaces[0]
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not completed
        assert working.path.is_dir()
        os.fstat(working.lock_fd)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert sorted(completed) == ["red", "teal"]
    assert not working.path.exists()
    assert_closed(working.lock_fd)
    assert len(creations) == (2 if use_cache else 1)
    for _, owner in creations:
        assert_closed(owner.lock_fd)
        assert not owner.path.exists()
    assert Asset.select().count() == 2
    assert all(storage.inspect_asset(settings, asset) == "ready" for asset in Asset.select())


@pytest.mark.parametrize("failure", [RuntimeError, Interrupted])
def test_scope_failure_resets_context_and_closes_owner_before_cleanup(
    settings, monkeypatch, failure
):
    settings.data_dir.mkdir()
    remove = tempfiles.remove
    before = tempfiles.active_fds()
    removed = []

    def nfs_remove(path):
        assert_closed(working.lock_fd)
        removed.append(path)
        return remove(path)

    monkeypatch.setattr(tempfiles, "remove", nfs_remove)
    with pytest.raises(failure):
        with tempfiles.reuse_blobs(settings) as working:
            (working.path / "abandoned.part").write_bytes(b"unfinished bytes")
            raise failure()
    assert len(removed) == 1
    assert list(tempfiles.root(settings).iterdir()) == []
    assert tempfiles.active_fds() == before


def test_nested_roots_settings_and_other_purposes_do_not_share(settings, creations):
    settings.data_dir.mkdir()
    other = replace(settings, data_dir=settings.data_dir.parent / "other")
    other.data_dir.mkdir()
    changed_settings = replace(settings, public_url="http://another-server")
    before = tempfiles.active_fds()
    with tempfiles.reuse_blobs(settings) as outer:
        with tempfiles.blob_workspace(settings) as borrowed:
            assert borrowed is outer
        with tempfiles.reuse_blobs(settings) as inner:
            assert inner.path != outer.path
            assert tempfiles.active_fds() == (*before, outer.lock_fd, inner.lock_fd)
            with tempfiles.blob_workspace(settings) as borrowed:
                assert borrowed is inner
        with tempfiles.blob_workspace(settings) as borrowed:
            assert borrowed is outer
        for purpose in ("blob", "avatar", "ugoira"):
            with tempfiles.workspace(settings, purpose) as separate:
                assert separate.path != outer.path
                assert tempfiles.active_fds() == (*before, outer.lock_fd, separate.lock_fd)
        for distinct in (other, changed_settings):
            with tempfiles.blob_workspace(distinct) as separate:
                assert separate.path != outer.path
                assert separate.path.parent == tempfiles.root(distinct)
        with tempfiles.reuse_blobs(other) as separate:
            with tempfiles.blob_workspace(settings) as unmatched:
                assert unmatched.path not in (outer.path, separate.path)
            with tempfiles.blob_workspace(other) as borrowed:
                assert borrowed is separate
        with tempfiles.blob_workspace(settings) as borrowed:
            assert borrowed is outer
    assert len({working.path for _, working in creations}) == len(creations)
    assert tempfiles.active_fds() == before
    assert list(tempfiles.root(settings).iterdir()) == []
    assert list(tempfiles.root(other).iterdir()) == []


async def test_active_owner_is_copied_to_threads_and_inherited_by_children(settings):
    settings.data_dir.mkdir()

    def child_inherits():
        with tempfiles.blob_workspace(settings) as borrowed:
            assert borrowed.lock_fd in tempfiles.active_fds()
            output = subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import os, sys; [os.fstat(int(fd)) for fd in sys.argv[1:]]; print('owned')",
                    *map(str, tempfiles.active_fds()),
                ],
                pass_fds=tempfiles.active_fds(),
                timeout=3,
            )
            assert output.strip() == b"owned"
            return borrowed

    with tempfiles.reuse_blobs(settings) as working:
        assert await disk_operation(child_inherits) is working
        assert tempfiles.reap(settings)["active"] == 1
    assert_closed(working.lock_fd)


def test_stale_context_cannot_borrow_a_closed_scope(settings):
    settings.data_dir.mkdir()
    with tempfiles.reuse_blobs(settings) as working:
        stale = copy_context()

    def late_borrow():
        with tempfiles.blob_workspace(settings):
            pytest.fail("A closed scope must not be reused")

    with pytest.raises(RuntimeError, match="scope has already ended"):
        stale.run(late_borrow)
    assert not working.path.exists()
    assert list(tempfiles.root(settings).iterdir()) == []


def test_default_stores_and_avatars_keep_individual_lifecycles(artwork, settings, creations):
    original = storage.store_image(settings, artwork, 0, png())
    storage.make_thumbnail(settings, artwork, original)
    assert [purpose for purpose, _ in creations] == ["blob", "blob"]
    assert list(tempfiles.root(settings).iterdir()) == []
    with tempfiles.reuse_blobs(settings) as working:
        for color in ("red", "blue"):
            relative, _ = storage.store_avatar(settings, "456", png(color))
            assert storage.avatar_file(settings, relative).read_bytes() == png(color)
            metadata_only(working)
    assert [purpose for purpose, _ in creations] == ["blob", "blob", "blob", "avatar", "avatar"]


def test_cache_to_nas_publication_uses_only_same_filesystem_renames(
    artwork, settings, creations, monkeypatch
):
    # /dev/shm is a separate tmpfs in this isolated Docker container. A direct
    # cache-to-data os.replace would actually fail with EXDEV, without mocking.
    cache = Path(tempfile.mkdtemp(prefix="pixchive-cache-", dir="/dev/shm"))
    cached = replace(settings, cache_dir=cache)
    rename = os.replace
    moves = []

    def same_filesystem(source, destination):
        source, destination = Path(source), Path(destination)
        assert source.is_relative_to(settings.data_dir / "tmp")
        assert source.stat().st_dev == destination.parent.stat().st_dev
        assert not db.in_transaction()
        moves.append((source, destination))
        return rename(source, destination)

    monkeypatch.setattr(os, "replace", same_filesystem)
    try:
        assert cache.stat().st_dev != settings.data_dir.stat().st_dev
        with tempfiles.reuse_blobs(cached) as working:
            assert working.path.is_relative_to(cache)
            for color in ("teal", "red"):
                original = storage.store_image(cached, artwork, 0, png(color))
                storage.make_thumbnail(cached, artwork, original)
            relative, _ = storage.store_avatar(cached, "456", png("blue"))
            assert storage.avatar_file(cached, relative).read_bytes() == png("blue")
            assert tempfiles.reap(cached)["active"] == 2
            metadata_only(working)
            with tempfiles.publication_workspace(cached) as publication:
                metadata_only(publication)
        assert len(moves) == 5
        assert [purpose for purpose, _ in creations] == ["blob", "blob", "avatar"]
        assert list(tempfiles.root(cached).iterdir()) == []
        assert list(tempfiles.root(settings).iterdir()) == []
        assert all(storage.inspect_asset(cached, asset) == "ready" for asset in Asset.select())
    finally:
        shutil.rmtree(cache)


async def test_cache_publication_without_reuse_does_not_nest_the_media_lock(
    artwork, settings, creations
):
    cached = replace(settings, cache_dir=settings.data_dir.parent / "cache")
    asset = await disk_operation(connected, storage.store_image, cached, artwork, 0, png())
    relative, _ = await disk_operation(storage.store_avatar, cached, "456", png())
    assert storage.inspect_asset(cached, asset) == "ready"
    assert storage.avatar_file(cached, relative).read_bytes() == png()
    assert [purpose for purpose, _ in creations] == ["blob", "blob", "avatar", "blob"]
    assert list(tempfiles.root(cached).iterdir()) == []
    assert list(tempfiles.root(settings).iterdir()) == []


async def test_ugoira_uses_cache_for_work_and_nas_for_all_final_files(client, settings):
    cache = Path(tempfile.mkdtemp(prefix="pixchive-ugoira-cache-", dir="/dev/shm"))
    cached = replace(settings, cache_dir=cache, min_free_bytes=0)
    remote_animation = animation()
    encoded = []

    async def encode(path, duration):
        assert path.parent == tempfiles.root(cached)
        assert path.name.startswith("work-ugoira-")
        assert path.stat().st_dev != settings.data_dir.stat().st_dev
        assert tempfiles.reap(cached)["active"] == 1
        encoded.append(path)
        return await ugoira.encode(path, duration)

    try:
        library.ingest(remote_animation, "public")
        with tempfiles.reuse_blobs(cached):
            ugoira.store_original(
                cached,
                remote_animation.id,
                zip_bytes(),
                [frame.model_dump() for frame in remote_animation.ugoira_frames],
            )
        job = Job.get(Job.kind == "convert")
        await run_job({"settings": cached, "encode": encode}, job.id)
        result = Job.get_by_id(job.id)
        assert result.state == "succeeded", result.error_code
        bundle = UgoiraBundle.get()
        assert bundle.state == "ready"
        assert bundle.video.media_type == "video/mp4"
        assert storage.safe_path(cached, bundle.archive.path).read_bytes() == zip_bytes()
        assert encoded and not encoded[0].exists()
        for asset in Asset.select():
            assert storage.safe_path(cached, asset.path).is_relative_to(settings.data_dir / "media")
            assert storage.inspect_asset(cached, asset) == "ready"
        assert list(tempfiles.root(cached).iterdir()) == []
        assert list(tempfiles.root(settings).iterdir()) == []
    finally:
        shutil.rmtree(cache)


@pytest.mark.parametrize("damage", ["same_size", "truncated", "rename_failure"])
def test_failed_nas_publication_preserves_old_bytes_and_cleans_both_parts(
    artwork, settings, monkeypatch, damage
):
    original = storage.store_image(settings, artwork, 0, png())
    destination = storage.safe_path(settings, original.path)
    old_bytes = b"?" * original.size
    destination.write_bytes(old_bytes)
    cached = replace(settings, cache_dir=settings.data_dir.parent / "cache")
    fsync, rename = os.fsync, os.replace
    damaged = []

    def corrupt_nas(fd):
        fsync(fd)
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if path.suffix == ".part" and path.is_relative_to(settings.data_dir / "tmp"):
            assert not db.in_transaction()
            if damage == "same_size":
                os.pwrite(fd, b"!", 0)
            elif damage == "truncated":
                os.ftruncate(fd, original.size - 1)
            damaged.append(path)

    def fail_rename(source, target):
        if Path(target) == destination:
            assert not db.in_transaction()
            assert Path(source).read_bytes() == png()
            raise OSError(errno.EIO, "synthetic prepublish failure")
        return rename(source, target)

    with tempfiles.reuse_blobs(cached) as working:
        with tempfiles.publication_workspace(cached) as publication:
            pass
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", corrupt_nas)
            if damage == "rename_failure":
                patch.setattr(os, "replace", fail_rename)
            expected_error = OSError if damage == "rename_failure" else DomainError
            with pytest.raises(expected_error):
                storage.store_image(cached, artwork, 0, png())
        assert damaged
        assert destination.read_bytes() == old_bytes
        assert Asset.get_by_id(original.id).state == "staged"
        metadata_only(working)
        metadata_only(publication)
        repaired = storage.store_image(cached, artwork, 0, png())
        assert repaired.id == original.id and repaired.state == "ready"
        assert destination.read_bytes() == png()
        metadata_only(working)
        metadata_only(publication)
    assert list(tempfiles.root(cached).iterdir()) == []
    assert list(tempfiles.root(settings).iterdir()) == []


def test_guard_can_reject_verified_nas_copy_before_atomic_publish(artwork, settings, monkeypatch):
    cached = replace(settings, cache_dir=settings.data_dir.parent / "cache")
    verified = []
    hash_file = storage.hash_file

    def hash_and_mark(path):
        result = hash_file(path)
        if path.suffix == ".part" and path.is_relative_to(settings.data_dir / "tmp"):
            assert not db.in_transaction()
            verified.append(path)
        return result

    def check():
        if verified:
            raise Interrupted()

    monkeypatch.setattr(storage, "hash_file", hash_and_mark)
    with tempfiles.reuse_blobs(cached, guard=check) as working:
        with tempfiles.publication_workspace(cached) as publication:
            pass
        with pytest.raises(Interrupted):
            storage.store_image(cached, artwork, 0, png(), guard=check)
        assert verified
        metadata_only(working)
        metadata_only(publication)
        asset = Asset.get()
        assert asset.state == "staged"
        assert not storage.safe_path(cached, asset.path).exists()


def test_corrupt_cached_bytes_are_rejected_before_nas_copy(artwork, settings, monkeypatch):
    cached = replace(settings, cache_dir=settings.data_dir.parent / "cache")
    fsync = os.fsync

    def corrupt_cache(fd):
        fsync(fd)
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if path.suffix == ".part" and path.is_relative_to(cached.cache_dir):
            os.pwrite(fd, b"!", 0)

    monkeypatch.setattr(os, "fsync", corrupt_cache)
    with tempfiles.reuse_blobs(cached) as working:
        with tempfiles.publication_workspace(cached) as publication:
            pass
        with pytest.raises(DomainError, match="integrity_error"):
            storage.store_image(cached, artwork, 0, png())
        metadata_only(working)
        metadata_only(publication)
        assert not storage.safe_path(cached, Asset.get().path).exists()


def test_avatar_copy_corruption_is_rejected_without_publishing(artwork, settings, monkeypatch):
    cached = replace(settings, cache_dir=settings.data_dir.parent / "cache")
    fsync = os.fsync

    def corrupt_nas(fd):
        fsync(fd)
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if path.suffix == ".part" and path.is_relative_to(settings.data_dir / "tmp"):
            os.pwrite(fd, b"!", 0)

    monkeypatch.setattr(os, "fsync", corrupt_nas)
    with pytest.raises(DomainError, match="integrity_error"):
        storage.store_avatar(cached, "456", png())
    assert list(tempfiles.root(cached).iterdir()) == []
    assert list(tempfiles.root(settings).iterdir()) == []
    assert list((settings.data_dir / "avatars").rglob("*.png")) == []


def abandoned_workspace(directory, purpose, nonce):
    path = directory / f"work-{purpose}-{nonce}"
    path.mkdir()
    (path / tempfiles.OWNER_LOCK).touch()
    (path / tempfiles.MANIFEST).write_text(
        json.dumps({"format": tempfiles.FORMAT, "purpose": purpose, "nonce": nonce})
    )
    (path / "unfinished.part").write_bytes(b"abandoned bytes")
    return path


def test_reaper_visits_cache_and_old_nas_preserving_unknown_and_live_work(settings):
    settings.data_dir.mkdir()
    cached = replace(settings, cache_dir=settings.data_dir.parent / "cache")
    roots = [tempfiles.root(cached, create=True), tempfiles.root(settings, create=True)]
    unknown = []
    for index, directory in enumerate(roots):
        abandoned_workspace(directory, "blob", str(index + 1) * 32)
        abandoned_workspace(directory, "ugoira", str(index + 3) * 32)
        (directory / f".creating-avatar-{str(index + 5) * 32}").mkdir()
        (directory / f".cleaning-blob-{str(index + 7) * 32}").mkdir()
        legacy = directory / "legacy.part"
        legacy.write_bytes(b"unmanaged bytes")
        malformed = directory / f"work-blob-{'a' * 32}"
        malformed.mkdir()
        (malformed / tempfiles.OWNER_LOCK).touch()
        unknown.extend([legacy, malformed])
    with tempfiles.reuse_blobs(cached):
        assert tempfiles.reap(cached) == {"removed": 8, "failed": 0, "active": 2, "unknown": 4}
    assert all(path.exists() for path in unknown)
    assert {path for directory in roots for path in directory.iterdir()} == set(unknown)


def test_reaper_limit_is_shared_across_cache_and_nas(settings):
    settings.data_dir.mkdir()
    cached = replace(settings, cache_dir=settings.data_dir.parent / "cache")
    paths = [
        abandoned_workspace(tempfiles.root(cached, create=True), "blob", "1" * 32),
        abandoned_workspace(tempfiles.root(settings, create=True), "blob", "2" * 32),
    ]
    assert tempfiles.reap(cached, limit=1)["removed"] == 1
    assert sum(path.exists() for path in paths) == 1
    assert tempfiles.reap(cached, limit=1)["removed"] == 1
    assert not any(path.exists() for path in paths)


@pytest.mark.parametrize("unsafe", ["relative", "traversal", "cache_symlink", "tmp_symlink"])
def test_unsafe_cache_paths_are_rejected(settings, tmp_path, unsafe):
    base = tmp_path / "cache"
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_bytes(b"unrelated bytes")
    if unsafe == "relative":
        base = Path("relative-cache")
    elif unsafe == "traversal":
        base = tmp_path / "child" / ".." / "outside"
    elif unsafe == "cache_symlink":
        base.symlink_to(outside, target_is_directory=True)
    else:
        base.mkdir()
        (base / "tmp").symlink_to(outside, target_is_directory=True)
    cached = replace(settings, cache_dir=base)
    with pytest.raises(DomainError, match="invalid_path"):
        tempfiles.root(cached, create=True)
    with pytest.raises(DomainError, match="invalid_path"):
        tempfiles.reap(cached)
    assert marker.read_bytes() == b"unrelated bytes"
