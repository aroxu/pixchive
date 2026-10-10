"""Immutable ZIP/timing preservation and cancellable, local-only video derivation."""

import asyncio
import errno
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import sys
import zipfile
from pathlib import Path

from peewee import SQL
from PIL import Image
from pydantic import TypeAdapter, ValidationError

from app.adapters.pixiv.contracts import UgoiraFrame
from app.core.db import db
from app.core.errors import DomainError
from app.models.entities import Job, JobDependency, Tombstone, UgoiraBundle
from app.services import jobs, storage, storage_budget, tempfiles
from app.services.execution import Interrupted, disk_operation, settle, watch

MAX_FRAMES = 5000
MAX_UNCOMPRESSED = 1024 * 1024 * 1024
MAX_FRAME_BYTES = 20 * 1024 * 1024
MAX_FRAME_PIXELS = 25_000_000
MAX_TOTAL_PIXELS = 500_000_000
MAX_DURATION_MS = 600_000
MAX_VIDEO_BYTES = 200 * 1024 * 1024
PLAYBACK_FILENAME = "playback.mp4"


def invalid():
    return DomainError("invalid_ugoira", "우고이라 원본과 프레임 정보를 검증하지 못했습니다.")


def frames_json(frames) -> bytes:
    try:
        parsed = TypeAdapter(list[UgoiraFrame]).validate_python(frames)
        if not 1 <= len(parsed) <= MAX_FRAMES or sum(x.delay for x in parsed) > MAX_DURATION_MS:
            raise ValueError
        names = [x.file for x in parsed]
        if len(set(names)) != len(names) or not all(
            re.fullmatch(r"[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp|gif)", name) for name in names
        ):
            raise ValueError
        return json.dumps([frame.model_dump() for frame in parsed], separators=(",", ":")).encode()
    except (ValueError, ValidationError):
        raise invalid() from None


def inspect_zip(content: bytes, timing: bytes, directory: Path | None = None, check=None):
    """Never extract remote paths. Decode into generated PNG names with finite budgets."""
    frames = json.loads(timing)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if (
                len(infos) != len(frames)
                or {info.filename for info in infos} != {frame["file"] for frame in frames}
                or len({info.filename for info in infos}) != len(infos)
                or sum(info.file_size for info in infos) > MAX_UNCOMPRESSED
            ):
                raise invalid()
            for info in infos:
                if (
                    not 0 < info.file_size <= MAX_FRAME_BYTES
                    or info.file_size > max(1, info.compress_size) * 1000
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or info.flag_bits & 1
                    or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                ):
                    raise invalid()
            dimensions = None
            thumbnail = None
            pixels = 0
            manifest = []
            for index, frame in enumerate(frames):
                if check:
                    check()
                data = archive.read(frame["file"])
                with Image.open(io.BytesIO(data)) as image:
                    if image.width * image.height > MAX_FRAME_PIXELS:
                        raise invalid()
                    pixels += image.width * image.height
                    if pixels > MAX_TOTAL_PIXELS or image.format not in {
                        "JPEG",
                        "PNG",
                        "WEBP",
                        "GIF",
                    }:
                        raise invalid()
                    image.load()
                    if dimensions is None:
                        dimensions = image.size
                        preview = image.convert("RGB")
                        preview.thumbnail((640, 640))
                        output = io.BytesIO()
                        preview.save(output, "WEBP", quality=85)
                        thumbnail = output.getvalue()
                    elif image.size != dimensions:
                        raise invalid()
                    if directory:
                        image.convert("RGB").save(directory / f"frame{index:05d}.png", "PNG")
                if directory:
                    manifest.extend(
                        [
                            f"file frame{index:05d}.png",
                            "option framerate 1000",
                            f"duration {frame['delay'] / 1000:.3f}",
                        ]
                    )
            if directory:
                manifest.extend([f"file frame{len(frames) - 1:05d}.png", "option framerate 1000"])
                (directory / "frames.ffconcat").write_text(
                    "ffconcat version 1.0\n" + "\n".join(manifest) + "\n"
                )
            return dimensions, thumbnail, sum(frame["delay"] for frame in frames) / 1000
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EACCES, errno.EROFS):
            raise
        raise invalid() from None
    except (
        zipfile.BadZipFile,
        ValueError,
        KeyError,
        RuntimeError,
        Image.DecompressionBombError,
    ):
        raise invalid() from None


def store_original(
    settings,
    artwork_id,
    content,
    frames,
    *,
    guard=None,
    on_staged=None,
    parent_job=None,
    provenance=None,
):
    timing = frames_json(frames)
    dimensions, _, _ = inspect_zip(content, timing, check=guard)
    revision = hashlib.sha256(hashlib.sha256(content).digest() + timing).hexdigest()
    archive = storage.store_blob(
        settings,
        artwork_id,
        0,
        content,
        role="ugoira_zip",
        provenance=provenance,
        extension="zip",
        media_type="application/zip",
        width=dimensions[0],
        height=dimensions[1],
        revision=revision,
        guard=guard,
        on_staged=on_staged,
    )
    timing_asset = storage.store_blob(
        settings,
        artwork_id,
        0,
        timing,
        role="ugoira_timing",
        extension="json",
        media_type="application/json",
        revision=revision,
        guard=guard,
    )
    with db.atomic("IMMEDIATE"):
        if guard:
            guard()
        bundle, _ = UgoiraBundle.get_or_create(
            artwork=artwork_id,
            revision=revision,
            defaults={"archive": archive, "timing": timing_asset},
        )
        # The conversion job and its outbox are committed with the source bundle.
        child = jobs.enqueue_conversion(bundle.id, "auto")
        if parent_job:
            JobDependency.get_or_create(parent=parent_job, child=child)
    return archive


def require_bundle(bundle_id):
    bundle = UgoiraBundle.get_or_none(UgoiraBundle.id == bundle_id)
    if not bundle or Tombstone.get_or_none(Tombstone.artwork_id == bundle.artwork_id):
        raise DomainError("not_found", "우고이라 원본을 찾을 수 없습니다.", 404)
    return bundle


def latest_bundle(artwork_id):
    return (
        UgoiraBundle.select()
        .where(UgoiraBundle.artwork == artwork_id)
        .order_by(SQL("rowid DESC"))
        .first()
    )


def request_conversion(artwork_id, key):
    from app.services.library import require_artwork

    require_artwork(artwork_id)
    bundle = latest_bundle(artwork_id)
    if not bundle:
        raise DomainError("source_missing", "우고이라 원본을 먼저 보관해 주세요.", 409)
    return jobs.enqueue_conversion(bundle.id, key)


def check_job(job_id, owner):
    job = jobs.owned(job_id, owner)
    if not job or job.state != "running":
        raise Interrupted


def estimate_space(settings, bundle_id, job_id, owner):
    """Budget decoded RGB PNGs, the MP4 and its temporary storage copy."""
    with db.connection_context():
        check_job(job_id, owner)
        bundle = require_bundle(bundle_id)
        if storage.inspect_asset(settings, bundle.timing) != "ready":
            raise DomainError("source_missing", "우고이라 원본 파일을 먼저 복구해 주세요.")
        timing = frames_json(
            json.loads(storage.safe_path(settings, bundle.timing.path).read_bytes())
        )
        count = len(json.loads(timing))
        width, height = bundle.archive.width, bundle.archive.height
        pixels = width * height
        if not (width > 0 and height > 0 and pixels <= MAX_FRAME_PIXELS):
            raise invalid()
        total_pixels = pixels * count
        if total_pixels > MAX_TOTAL_PIXELS:
            raise invalid()
        check_job(job_id, owner)
        return (
            total_pixels * 5
            + count * 64 * 1024
            + MAX_VIDEO_BYTES * 2
            + storage_budget.PREVIEW_BYTES
        )


def prepare(settings, bundle_id, directory, job_id, owner):
    with db.connection_context():
        bundle = require_bundle(bundle_id)

        def check():
            check_job(job_id, owner)

        for asset in (bundle.archive, bundle.timing):
            if storage.inspect_asset(settings, asset) != "ready":
                raise DomainError("source_missing", "우고이라 원본 파일을 먼저 복구해 주세요.")
        timing = frames_json(
            json.loads(storage.safe_path(settings, bundle.timing.path).read_bytes())
        )
        dimensions, thumbnail, duration = inspect_zip(
            storage.safe_path(settings, bundle.archive.path).read_bytes(),
            timing,
            directory,
            check,
        )
        asset = storage.store_image(
            settings,
            bundle.artwork_id,
            0,
            thumbnail,
            role="thumbnail",
            guard=check,
        )
        with db.atomic("IMMEDIATE"):
            check()
            UgoiraBundle.update(thumbnail=asset).where(UgoiraBundle.id == bundle_id).execute()
        return dimensions, duration


async def process(*command, timeout=600):
    if not shutil.which(command[0]):
        raise DomainError("ffmpeg_missing", "서버의 영상 변환 도구를 확인해 주세요.")
    try:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.core.child_process",
            str(os.getpid()),
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "LC_ALL": "C"},
            pass_fds=tempfiles.active_fds(),
        )
    except FileNotFoundError:
        raise DomainError("ffmpeg_missing", "서버의 영상 변환 도구를 확인해 주세요.") from None
    try:
        async with asyncio.timeout(timeout):
            output, errors = await child.communicate()
        if child.returncode:
            if any(
                marker in errors.lower()
                for marker in (b"no space left on device", b"disk quota exceeded")
            ):
                raise OSError(errno.ENOSPC, "storage_full")
            raise DomainError("conversion_failed", "재생 파일을 만들지 못했습니다.")
        return output
    finally:
        if child.returncode is None:
            child.kill()
            await settle(child.wait())


async def encode(directory: Path, duration: float):
    """Derive x264 MP4 playback directly from timed frames; never create a GIF."""
    playback = directory / PLAYBACK_FILENAME
    await process(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-protocol_whitelist",
        "file",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(directory / "frames.ffconcat"),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:v",
        "libx264",
        # Reordered B-frames can shorten MP4 duration with variable packet durations.
        "-bf",
        "0",
        "-threads",
        "2",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "vfr",
        "-enc_time_base",
        "1:1000",
        "-video_track_timescale",
        "1000",
        "-movflags",
        "+faststart",
        "-fs",
        str(MAX_VIDEO_BYTES),
        "-f",
        "mp4",
        str(playback),
    )
    if playback.stat().st_size > MAX_VIDEO_BYTES:
        raise DomainError("conversion_limit", "재생 파일이 영상 변환 크기 제한을 넘었습니다.")
    output = await process(
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(playback),
        timeout=30,
    )
    try:
        metadata = json.loads(output)
        streams = metadata["streams"]
        if len(streams) != 1:
            raise ValueError
        video = streams[0]
        actual_duration = float(metadata["format"]["duration"])
        if (
            video["codec_type"] != "video"
            or video["codec_name"] != "h264"
            or video["pix_fmt"] != "yuv420p"
            or "mp4" not in metadata["format"]["format_name"].split(",")
            or not math.isfinite(actual_duration)
            or abs(actual_duration - duration) > 0.01
        ):
            raise ValueError
        return video["width"], video["height"]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise DomainError("conversion_failed", "재생 파일을 검증하지 못했습니다.") from None


def save_video(settings, bundle_id, path, dimensions, job_id, owner):
    with db.connection_context():
        bundle = require_bundle(bundle_id)

        def check():
            check_job(job_id, owner)

        if path.stat().st_size > MAX_VIDEO_BYTES:
            raise DomainError("conversion_limit", "재생 파일이 영상 변환 크기 제한을 넘었습니다.")
        asset = storage.store_blob(
            settings,
            bundle.artwork_id,
            0,
            path.read_bytes(),
            role="video",
            extension="mp4",
            media_type="video/mp4",
            width=dimensions[0],
            height=dimensions[1],
            guard=check,
        )
        with db.atomic("IMMEDIATE"):
            check()
            UgoiraBundle.update(video=asset, state="ready", error_code=None).where(
                UgoiraBundle.id == bundle_id
            ).execute()


async def execute(ctx, job_id, owner):
    bundle_id = jobs.require_job(job_id).scope
    settings = ctx["settings"]
    try:
        bundle = require_bundle(bundle_id)
        check_job(job_id, owner)
        UgoiraBundle.update(state="running", error_code=None).where(
            UgoiraBundle.id == bundle_id
        ).execute()
        Job.update(phase="converting", total=1, processed=0, failed=0).where(
            Job.id == job_id
        ).execute()
        needed = await disk_operation(estimate_space, settings, bundle.id, job_id, owner)
        with storage_budget.hold(settings, job_id, owner, needed, lambda: check_job(job_id, owner)):
            with tempfiles.workspace(
                settings, "ugoira", lambda: check_job(job_id, owner)
            ) as working:
                path = working.path
                _, duration = await disk_operation(
                    prepare, settings, bundle.id, path, job_id, owner
                )
                dimensions = await watch(
                    ctx.get("encode", encode)(path, duration),
                    lambda: check_job(job_id, owner),
                )
                await disk_operation(
                    save_video,
                    settings,
                    bundle.id,
                    path / PLAYBACK_FILENAME,
                    dimensions,
                    job_id,
                    owner,
                )
        Job.update(phase="finished", processed=1).where(Job.id == job_id).execute()
        jobs.finish(job_id, owner, "succeeded")
    except Interrupted:
        if jobs.owned(job_id, owner):
            UgoiraBundle.update(state="pending").where(UgoiraBundle.id == bundle_id).execute()
        jobs.finish(job_id, owner, "succeeded")
    except OSError as exc:
        full = exc.errno in (errno.ENOSPC, errno.EDQUOT)
        code = "storage_full" if full else "storage_error"
        if jobs.owned(job_id, owner):
            UgoiraBundle.update(state="failed", error_code=code).where(
                UgoiraBundle.id == bundle_id
            ).execute()
        jobs.finish(job_id, owner, "waiting_storage" if full else "failed", code)
    except (DomainError, TimeoutError) as exc:
        code = exc.code if isinstance(exc, DomainError) else "conversion_timeout"
        if jobs.owned(job_id, owner):
            UgoiraBundle.update(state="failed", error_code=code).where(
                UgoiraBundle.id == bundle_id
            ).execute()
            Job.update(failed=1).where(Job.id == job_id).execute()
        jobs.finish(job_id, owner, "failed", code)
