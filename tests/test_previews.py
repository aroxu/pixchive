"""Deferred local-preview regression scenarios; no Pixiv credentials or adapter."""

import io

import pytest
from PIL import Image

from app.core.errors import DomainError
from app.models.entities import Asset, Credential, IntegrityIssue, Job
from app.services import jobs, library, preferences, previews, retention, storage
from app.worker import run_job
from tests.test_library import remote


def png(compression=6):
    output = io.BytesIO()
    Image.new("RGB", (200, 300), "teal").save(output, "PNG", compress_level=compression)
    return output.getvalue()


def original(settings, content=None):
    library.ingest(remote(), "public")
    return storage.store_image(settings, "123", 0, content or png())


async def test_preview_runs_without_cookie_or_download_permission_preserving_original(
    admin, settings
):
    source = original(settings)
    before = storage.safe_path(settings, source.path).read_bytes()
    preferences.update({"download_ratings": []})
    response = admin.post("/api/artworks/123/preview", headers={"Idempotency-Key": "one"})
    assert response.status_code == 202, response.text
    job = response.json()
    assert (job["kind"], job["artwork_id"], job["scope"]) == ("preview", "123", None)
    assert Credential.select().count() == 0

    await run_job({"settings": settings}, job["id"])

    result = Job.get_by_id(job["id"])
    assert (result.state, result.processed, result.failed) == ("succeeded", 1, 0)
    source = Asset.get_by_id(source.id)
    assert source.thumbnail.role == "thumbnail"
    assert storage.safe_path(settings, source.path).read_bytes() == before
    detail = admin.get("/api/artworks/123").json()
    assert detail["thumbnail_url"] == f"/api/media/{source.thumbnail_id}"
    assert admin.get(detail["thumbnail_url"]).status_code == 200
    assert admin.get("/api/jobs/history?kind=preview").json()["total"] == 1


async def test_preview_repeated_requests_and_regeneration_do_not_duplicate_assets(client, settings):
    source = original(settings)
    first = previews.request("123", "one")
    assert previews.request("123", "one").id == first.id
    assert previews.request("123", "two").id == first.id
    await run_job({"settings": settings}, first.id)
    thumbnail_id = Asset.get_by_id(source.id).thumbnail_id

    second = previews.request("123", "three")
    assert second.id != first.id
    await run_job({"settings": settings}, second.id)

    assert Asset.select().count() == 2
    assert Asset.get_by_id(source.id).thumbnail_id == thumbnail_id
    assert Job.get_by_id(second.id).state == "succeeded"


async def test_reused_thumbnail_links_to_each_original_revision(client, settings):
    first = original(settings, png(0))
    job = previews.request("123", "one")
    await run_job({"settings": settings}, job.id)
    thumbnail_id = Asset.get_by_id(first.id).thumbnail_id
    latest = storage.store_image(settings, "123", 0, png(9))
    assert latest.id != first.id

    job = previews.request("123", "two")
    await run_job({"settings": settings}, job.id)

    assert Asset.get_by_id(latest.id).thumbnail_id == thumbnail_id
    assert Asset.select().where(Asset.role == "thumbnail").count() == 1
    assert library.detail("123")["thumbnail_url"] == f"/api/media/{thumbnail_id}"
    assert storage.needs_thumbnail("123") is None


async def test_latest_corrupt_source_does_not_use_older_original(client, settings):
    older = original(settings, png(0))
    newer = storage.store_image(settings, "123", 0, png(9))
    path = storage.safe_path(settings, newer.path)
    content = bytearray(path.read_bytes())
    content[-1] ^= 1
    path.write_bytes(content)
    job = previews.request("123", "one")

    await run_job({"settings": settings}, job.id)

    result = Job.get_by_id(job.id)
    assert (result.state, result.processed, result.error_code) == ("failed", 0, "file_corrupt")
    assert Asset.get_by_id(newer.id).state == "corrupt"
    assert Asset.get_by_id(older.id).state == "ready"
    assert Asset.select().where(Asset.role == "thumbnail").count() == 0
    assert IntegrityIssue.get(IntegrityIssue.job == job).code == "file_corrupt"


async def test_missing_source_is_reported_and_pause_can_resume(client, settings):
    source = original(settings)
    job = previews.request("123", "one")
    assert jobs.action(job.id, "pause").state == "paused"
    assert previews.request("123", "two").id == job.id
    jobs.action(job.id, "resume")
    storage.safe_path(settings, source.path).unlink()

    await run_job({"settings": settings}, job.id)

    assert Job.get_by_id(job.id).error_code == "file_missing"
    assert Asset.get_by_id(source.id).state == "missing"


def test_local_deletion_cancels_preview_before_removing_sources(client, settings):
    original(settings)
    job = previews.request("123", "one")
    removal = retention.request_delete("123", "delete")

    assert Job.get_by_id(job.id).state == "cancelled"
    assert removal.lease_key == job.lease_key
    with pytest.raises(DomainError):
        previews.request("123", "two")


async def test_integrity_records_thumbnail_failure_instead_of_success(
    client, settings, monkeypatch
):
    source = original(settings)

    def failed(*args):
        raise DomainError("invalid_media", "synthetic")

    monkeypatch.setattr(previews, "build", failed)
    job = jobs.enqueue_integrity("one")
    await run_job({"settings": settings}, job.id)

    result = Job.get_by_id(job.id)
    assert (result.state, result.failed) == ("partial", 1)
    issue = IntegrityIssue.get(IntegrityIssue.job == job)
    assert (issue.asset_id, issue.code) == (source.id, "preview_failed")
    assert Asset.get_by_id(source.id).state == "ready"


def test_preview_creation_requires_app_session_and_csrf(admin, settings):
    original(settings)
    response = admin.post(
        "/api/artworks/123/preview",
        headers={"Idempotency-Key": "one", "X-CSRF-Token": "wrong"},
    )
    assert response.status_code == 403
    admin.delete("/api/session")
    response = admin.post("/api/artworks/123/preview", headers={"Idempotency-Key": "two"})
    assert response.status_code == 401
    assert Job.select().count() == 0
