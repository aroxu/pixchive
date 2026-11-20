"""A complete collection crosses a real cache/archive filesystem boundary."""

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from app.models.entities import Asset, Job, UgoiraBundle
from app.services import jobs, storage, tempfiles, ugoira
from app.services.fixtures import image_bytes
from app.worker import run_job
from tests.test_collection import BookmarkPage, FakeAdapter, connected, context, work
from tests.ugoira_fixtures import animation, zip_bytes


async def test_cached_collection_and_real_mp4_publish_only_into_archive(admin, settings):
    connected(settings)
    with tempfile.TemporaryDirectory(prefix="pixchive-cache-test-", dir="/dev/shm") as temporary:
        cache = Path(temporary)
        assert os.stat(cache).st_dev != os.stat(settings.data_dir).st_dev
        cached = replace(settings, cache_dir=cache)
        frames_seen = []

        async def encode(path, duration):
            assert path.is_relative_to(cache)
            assert list(path.glob("*.png"))
            frames_seen.append(path)
            assert tempfiles.reap(cached)["active"] >= 1
            return await ugoira.encode(path, duration)

        picture, animated = work(count=2), animation()
        page = BookmarkPage(items=[picture, animated], next_cursor=None, complete=True)
        ctx = context(cached, FakeAdapter({"public": [page]}))

        async def fetch(url):
            return zip_bytes() if url == animated.ugoira_zip_url else image_bytes(0)

        ctx["download"] = fetch
        parent = jobs.enqueue_account("sync", "local-cache-e2e", "public")
        await run_job(ctx, parent.id)
        assert Job.get_by_id(parent.id).state == "waiting_children"
        conversion = Job.get(Job.kind == "convert")
        await run_job({"settings": cached, "encode": encode}, conversion.id)
        assert Job.get_by_id(conversion.id).state == "succeeded"
        assert Job.get_by_id(parent.id).state == "succeeded"
        assert frames_seen and all(not path.exists() for path in frames_seen)
        assert list((cache / "tmp").iterdir()) == []
        assert list((settings.data_dir / "tmp").iterdir()) == []
        for asset in Asset.select():
            path = storage.safe_path(cached, asset.path)
            assert path.is_relative_to(settings.data_dir)
            assert not path.is_relative_to(cache)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == asset.sha256
        bundle = UgoiraBundle.get()
        video = storage.safe_path(cached, bundle.video.path)
        metadata = json.loads(
            await ugoira.process(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name:format=duration",
                "-of",
                "json",
                str(video),
            )
        )
        assert metadata["streams"][0]["codec_name"] == "h264"
        assert abs(float(metadata["format"]["duration"]) - 0.4) <= 0.002
        assert admin.get(f"/api/artworks/{animated.id}").json()["playback_url"]
