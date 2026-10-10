import asyncio
import io
import json
import stat
import zipfile

import pytest
from PIL import Image

from app.core.errors import DomainError
from app.models.entities import Artwork, Asset, Job, JobDependency, UgoiraBundle
from app.services import accounts, jobs, library, storage, ugoira
from app.worker import run_job
from tests.test_collection import BookmarkPage, FakeAdapter, connected, context
from tests.ugoira_fixtures import animation, zip_bytes


async def test_variable_delays_preserve_container_duration_and_frame_timestamps(tmp_path):
    delays = [70] * 18 + [60] * 54 + [60, 60, 60, 70, 60, 60, 60, 100]
    frames = []
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, delay in enumerate(delays):
            name = f"{index:06d}.png"
            frame = io.BytesIO()
            Image.new("RGB", (64, 48), (index * 3, 130, 160)).save(frame, "PNG")
            archive.writestr(name, frame.getvalue())
            frames.append({"file": name, "delay": delay})
    dimensions, _, duration = ugoira.inspect_zip(
        content.getvalue(), ugoira.frames_json(frames), tmp_path
    )
    assert await ugoira.encode(tmp_path, duration) == dimensions
    metadata = json.loads(
        await ugoira.process(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_packets",
            "-show_format",
            "-of",
            "json",
            str(tmp_path / ugoira.PLAYBACK_FILENAME),
        )
    )
    expected = [0]
    for delay in delays:
        expected.append(expected[-1] + delay)
    timestamps = sorted(round(float(packet["pts_time"]) * 1000) for packet in metadata["packets"])
    assert timestamps == expected
    assert abs(float(metadata["format"]["duration"]) - duration) <= 0.002


def store(settings):
    remote = animation()
    library.ingest(remote, "public")
    ugoira.store_original(
        settings, remote.id, zip_bytes(), [f.model_dump() for f in remote.ugoira_frames]
    )
    return UgoiraBundle.get(), Job.get(Job.kind == "convert")


async def test_zip_and_timing_preserved_video_duration_and_local_access(admin, settings):
    bundle, job = store(settings)
    assert Artwork.get().archive_status == "complete"
    assert bundle.state == "pending"
    assert storage.safe_path(settings, bundle.archive.path).read_bytes() == zip_bytes()
    original_ids = {x.id for x in Asset.select()}
    await run_job({"settings": settings}, job.id)
    assert Job.get_by_id(job.id).state == "succeeded", Job.get_by_id(job.id).error_code
    bundle = UgoiraBundle.get()
    assert bundle.state == "ready" and bundle.video and bundle.thumbnail
    detail = admin.get("/api/artworks/124").json()
    assert detail["playback_url"] and detail["archive_url"] and detail["timing_url"]
    assert detail["pages"] == []  # ZIP is never served as an image page.
    assert admin.get(detail["archive_url"]).content == zip_bytes()
    assert admin.get(detail["playback_url"], headers={"Range": "bytes=0-63"}).status_code == 206
    assert len(admin.get(detail["playback_url"]).content) > 0
    assert sum(frame["delay"] for frame in admin.get(detail["timing_url"]).json()) == 400
    assert original_ids.issubset({x.id for x in Asset.select()})
    media_url = detail["playback_url"]
    admin.delete("/api/session")
    assert admin.get(media_url).status_code == 401


async def test_collection_queues_conversion_and_disconnect_does_not_block_it(client, settings):
    connected(settings)
    page = BookmarkPage(items=[animation()], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))

    async def fetch(url):
        return zip_bytes()

    ctx["download"] = fetch
    job = jobs.enqueue_account("sync", "one", "public")
    await run_job(ctx, job.id)
    assert Job.get_by_id(job.id).state == "waiting_children"
    bundle = UgoiraBundle.get()
    assert bundle.state == "pending"
    accounts.disconnect()
    conversion = Job.get(Job.kind == "convert")
    await run_job({"settings": settings}, conversion.id)
    assert Job.get_by_id(conversion.id).state == "succeeded"
    assert Job.get_by_id(job.id).state == "succeeded"


async def test_conversion_failure_preserves_original_and_retry_does_not_download(admin, settings):
    bundle, job = store(settings)

    async def failed(*args):
        raise DomainError("conversion_failed", "synthetic")

    await run_job({"settings": settings, "encode": failed}, job.id)
    assert Artwork.get().archive_status == "complete"
    assert UgoiraBundle.get().state == "failed"
    assert Job.get_by_id(job.id).error_code == "conversion_failed"
    original = storage.safe_path(settings, bundle.archive.path).read_bytes()
    jobs.action(job.id, "retry")
    await run_job({"settings": settings}, job.id)
    assert Job.get_by_id(job.id).state == "succeeded"
    assert storage.safe_path(settings, bundle.archive.path).read_bytes() == original
    assert Asset.select().where(Asset.role == "ugoira_zip").count() == 1


async def test_conversion_cancel_waits_for_running_process_and_keeps_sources(client, settings):
    bundle, job = store(settings)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    task = asyncio.create_task(run_job({"settings": settings, "encode": blocked}, job.id))
    await asyncio.wait_for(started.wait(), 5)
    jobs.action(job.id, "cancel")
    await asyncio.wait_for(task, 5)
    assert stopped.is_set()
    assert Job.get_by_id(job.id).state == "cancelled"
    assert Artwork.get().archive_status == "complete"
    assert not list((settings.data_dir / "tmp").glob("ugoira-*"))


async def test_rescan_reuses_successful_manual_conversion_after_auto_failure(client, settings):
    connected(settings)
    page = BookmarkPage(items=[animation()], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))

    async def fetch(url):
        return zip_bytes()

    async def failed(*args):
        raise DomainError("conversion_failed", "synthetic")

    ctx["download"] = fetch
    first = jobs.enqueue_account("sync", "first", "public")
    await run_job(ctx, first.id)
    automatic = Job.get(Job.kind == "convert")
    await run_job({"settings": settings, "encode": failed}, automatic.id)
    assert Job.get_by_id(first.id).state == "partial"
    manual = ugoira.request_conversion("124", "manual-retry")
    assert jobs.enqueue_conversion(UgoiraBundle.get().id, "auto").id == manual.id
    await run_job({"settings": settings}, manual.id)

    async def unexpected_download(url):
        pytest.fail("A rescan must reuse the verified original")

    ctx["download"] = unexpected_download
    second = jobs.enqueue_account("sync", "second", "public", mode="full")
    await run_job(ctx, second.id)
    assert Job.get_by_id(second.id).state == "succeeded"
    # The unchanged ZIP/timing source is not queued again, so no duplicate conversion.
    assert not JobDependency.select().where(JobDependency.parent == second).exists()
    assert Job.select().where(Job.kind == "convert").count() == 2


async def test_missing_encoder_is_reported_without_running_child(monkeypatch):
    monkeypatch.setattr(ugoira.shutil, "which", lambda command: None)
    with pytest.raises(DomainError, match="ffmpeg_missing"):
        await ugoira.process("ffmpeg", "-version")


@pytest.mark.parametrize(
    "names",
    [
        ("../outside.png", "000001.png"),
        ("/outside.png", "000001.png"),
        ("000000.png", "000000.png"),
        ("000000.png", "extra.png"),
    ],
)
def test_zip_paths_duplicates_and_frame_mismatch_are_rejected(client, settings, names):
    remote = animation()
    library.ingest(remote, "public")
    with pytest.raises(DomainError, match="invalid_ugoira"):
        ugoira.store_original(
            settings, remote.id, zip_bytes(names), [f.model_dump() for f in remote.ugoira_frames]
        )
    assert Asset.select().count() == 0


def test_symlink_compression_bomb_and_timing_limits(client, settings):
    frames = [{"file": "000000.png", "delay": 100}]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        info = zipfile.ZipInfo("000000.png")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b"/etc/passwd")
    with pytest.raises(DomainError):
        ugoira.inspect_zip(output.getvalue(), ugoira.frames_json(frames))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("000000.png", b"a" * 2_000_000)
    with pytest.raises(DomainError):
        ugoira.inspect_zip(output.getvalue(), ugoira.frames_json(frames))
    for delay in (0, -1, 60001):
        with pytest.raises(DomainError):
            ugoira.frames_json([{"file": "000000.png", "delay": delay}])


def test_changed_timing_produces_new_bundle_and_keeps_original(client, settings):
    bundle, _ = store(settings)
    frames = json.loads(storage.safe_path(settings, bundle.timing.path).read_bytes())
    frames[0]["delay"] = 250
    ugoira.store_original(settings, "124", zip_bytes(), frames)
    assert UgoiraBundle.select().count() == 2
    assert Asset.select().where(Asset.role == "ugoira_zip").count() == 2
    assert storage.safe_path(settings, bundle.timing.path).is_file()


@pytest.mark.parametrize("command,expected", [("pause", "paused"), ("cancel", "cancelled")])
async def test_parent_waits_for_conversion_acknowledgement(client, settings, command, expected):
    connected(settings)
    page = BookmarkPage(items=[animation()], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))

    async def fetch(url):
        return zip_bytes()

    ctx["download"] = fetch
    parent = jobs.enqueue_account("sync", "one", "public")
    await run_job(ctx, parent.id)
    child = Job.get(Job.kind == "convert")
    started, stopped = asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    task = asyncio.create_task(run_job({"settings": settings, "encode": blocked}, child.id))
    await asyncio.wait_for(started.wait(), 5)
    assert jobs.action(parent.id, command).state in ("pausing", "cancelling")
    await asyncio.wait_for(task, 5)
    assert stopped.is_set()
    assert Job.get_by_id(parent.id).state == expected
    assert Job.get_by_id(child.id).state == expected
    if command == "pause":
        jobs.action(parent.id, "resume")
        assert Job.get_by_id(child.id).state == "queued"
        await run_job(context(settings, FakeAdapter()), parent.id)
        await run_job({"settings": settings}, child.id)
        assert Job.get_by_id(parent.id).state == "succeeded"


async def test_conversion_disk_full_keeps_original_waiting_storage(client, settings):
    import errno

    _, job = store(settings)

    async def full(*args):
        raise OSError(errno.ENOSPC, "synthetic")

    await run_job({"settings": settings, "encode": full}, job.id)
    assert Job.get_by_id(job.id).state == "waiting_storage"
    assert UgoiraBundle.get().error_code == "storage_full"
    assert Artwork.get().archive_status == "complete"
