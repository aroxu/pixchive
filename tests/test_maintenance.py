import hashlib
import io
import json
import logging
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from peewee import SQL

from app.adapters.pixiv.contracts import AdapterError, RemoteTag
from app.core.db import db, initialize
from app.core.errors import DomainError
from app.core.redaction import RedactingFilter, redact
from app.core.time import now
from app.models.entities import (
    Artwork,
    ArtworkTag,
    Asset,
    Bookmark,
    BookmarkTag,
    CollectionItem,
    CollectionRun,
    Job,
    PixivAccount,
    Tag,
    Tombstone,
)
from app.services import accounts, backup, jobs, library, schedule, storage
from app.services.fixtures import image_bytes
from app.worker import run_job
from tests.test_collection import BookmarkPage, FakeAdapter, connected, context, work
from tests.test_library import remote


def single(*items, complete=True):
    return BookmarkPage(items=list(items), next_cursor=None, complete=complete)


def bookmark_tags(artwork_id):
    return sorted(
        row.tag.name
        for row in BookmarkTag.select(BookmarkTag, Tag)
        .join(Tag)
        .switch(BookmarkTag)
        .join(Bookmark)
        .where(Bookmark.artwork == artwork_id)
    )


def tag_sources(artwork_id):
    return sorted(
        (row.tag.name, row.source)
        for row in ArtworkTag.select(ArtworkTag, Tag)
        .join(Tag)
        .where(ArtworkTag.artwork == artwork_id)
    )


async def sync(settings, pages, key, *, mode="normal", scope="public", fetched=None):
    adapter = FakeAdapter(pages)
    job = jobs.enqueue_account("sync", key, scope, mode=mode)
    await run_job(context(settings, adapter, fetched), job.id)
    return adapter, Job.get_by_id(job.id)


async def test_refresh_policy_fetches_only_new_changed_or_stale_and_keeps_local(client, settings):
    connected(settings)
    fetched = []
    adapter, first = await sync(
        settings, {"public": [single(work("123"), work("124"))]}, "one", fetched=fetched
    )
    assert sorted(adapter.detail_calls) == ["123", "124"] and len(fetched) == 2
    library.edit_tags(["123"], "내 분류", "add")
    # Unchanged rows need no details or downloads; list-only bookmark tags still update.
    pages = {
        "public": [single(work("123", bookmark_tags=["새 북마크"]), work("124", title="새 제목"))]
    }
    adapter, second = await sync(settings, pages, "two", fetched=fetched)
    assert adapter.detail_calls == ["124"] and len(fetched) == 2
    scan = jobs.serialize(second)["scan"]
    assert (scan["discovered"], scan["unchanged"], scan["changed"]) == (2, 1, 1)
    assert bookmark_tags("123") == ["새 북마크"]
    assert Artwork.get_by_id("124").title == "새 제목"
    # Finished scans keep counts, not one row per bookmark.
    runs = CollectionRun.select().where(CollectionRun.job == second.id)
    assert not CollectionItem.select().where(CollectionItem.run.in_(runs)).exists()
    # Stale details are refreshed even when the list row looks the same.
    Artwork.update(metadata_fetched_at=now() - 40 * 86400).where(Artwork.id == "123").execute()
    adapter, _ = await sync(settings, pages, "three", fetched=fetched)
    assert adapter.detail_calls == ["123"]
    # A full refresh reads every artwork; remote tag changes keep the local tag.
    changed = work("123", work_tags=[RemoteTag(name="新しい")], bookmark_tags=["새 북마크"])
    adapter, full = await sync(
        settings, {"public": [single(changed, work("124", title="새 제목"))]}, "four", mode="full"
    )
    assert sorted(adapter.detail_calls) == ["123", "124"] and full.state == "succeeded"
    assert tag_sources("123") == [("新しい", "pixiv_work"), ("내 분류", "local")]
    assert schedule.full_refreshes()["public"] is not None
    assert schedule.full_refreshes()["private"] is None
    assert len(fetched) == 2


async def test_visibility_change_moves_bookmark_without_duplicates(client, settings):
    connected(settings)
    await sync(settings, {"public": [single(work())]}, "one")
    adapter, job = await sync(
        settings, {"public": [single()], "private": [single(work())]}, "two", scope="all"
    )
    assert job.state == "succeeded" and adapter.detail_calls == []
    assert Bookmark.select().count() == 1
    assert (Bookmark.get().visibility, Bookmark.get().status) == ("private", "seen")


async def test_validation_resumes_jobs_waiting_for_authentication(client, settings):
    connected(settings)
    job = jobs.enqueue_account("sync", "one", "public")
    await run_job(
        context(settings, FakeAdapter({"public": [AdapterError("auth_expired")]})), job.id
    )
    assert Job.get_by_id(job.id).state == "waiting_auth"
    validation = jobs.enqueue_account("validate", "again")
    await run_job(context(settings, FakeAdapter()), validation.id)
    assert PixivAccount.get().status == "connected"
    assert Job.get_by_id(job.id).state == "queued"


async def test_delete_is_permanent_tombstoned_and_restore_is_explicit(admin, settings):
    connected(settings)
    fetched = []
    page = single(work("123", count=2))
    await sync(settings, {"public": [page]}, "one", fetched=fetched)
    library.edit_note("123", "남길 메모")
    paths = [storage.safe_path(settings, a.path) for a in Asset.select()]
    assert len(paths) == 3 and all(path.is_file() for path in paths)  # two originals + preview
    preview = admin.get("/api/artworks/123/deletion").json()
    assert preview["file_count"] == 3 and preview["bytes"] > 0
    response = admin.post("/api/artworks/123/delete", headers={"Idempotency-Key": "delete-1"})
    assert response.status_code == 202
    assert admin.get("/api/artworks/123").status_code == 404
    assert (
        admin.post(
            f"/api/jobs/{response.json()['id']}/actions", json={"action": "cancel"}
        ).status_code
        == 409
    )
    await run_job({"settings": settings}, response.json()["id"])
    assert Job.get_by_id(response.json()["id"]).state == "succeeded"
    assert not any(path.exists() for path in paths)
    assert not (settings.data_dir / "media" / "456").exists()
    assert (Tombstone.get().state, Tombstone.get().file_count) == ("deleted", 3)
    assert Tombstone.get().freed_bytes == preview["bytes"]
    # A later full refresh sees the bookmark but never downloads the excluded work.
    _, again = await sync(settings, {"public": [page]}, "two", mode="full", fetched=fetched)
    assert again.state == "succeeded" and len(fetched) == 2
    assert jobs.serialize(again)["scan"]["excluded"] == 1
    listed = admin.get("/api/exclusions").json()
    assert listed["total"] == 1 and listed["items"][0]["title"] == "夜の海 한국어"
    restored = admin.post(
        "/api/artworks/123/restore", json={"download": True}, headers={"Idempotency-Key": "r1"}
    )
    assert restored.status_code == 202
    adapter = FakeAdapter()
    adapter.details["123"] = work("123", count=2)
    await run_job(context(settings, adapter, fetched), restored.json()["job"]["id"])
    assert Job.get_by_id(restored.json()["job"]["id"]).state == "succeeded"
    assert len(fetched) == 4 and Artwork.get_by_id("123").archive_status == "complete"
    detail = admin.get("/api/artworks/123").json()
    assert detail["note"] == "남길 메모" and len(detail["pages"]) == 2
    assert admin.get(detail["pages"][0]["url"]).status_code == 200
    # Un-excluding without download makes the metadata visible again and nothing else.
    second = admin.post("/api/artworks/123/delete", headers={"Idempotency-Key": "delete-2"})
    await run_job({"settings": settings}, second.json()["id"])
    only = admin.post(
        "/api/artworks/123/restore", json={"download": False}, headers={"Idempotency-Key": "r2"}
    )
    assert only.status_code == 200 and only.json()["job"] is None
    assert admin.get("/api/artworks/123").json()["archive_status"] == "pending"
    assert admin.get("/api/exclusions").json()["total"] == 0


def test_download_finishing_after_delete_never_resurrects_files(client, settings):
    library.ingest(remote(), "public")

    def deleted_meanwhile():
        Tombstone.create(artwork_id="123", state="deleting")

    with pytest.raises(DomainError, match="excluded"):
        storage.store_image(settings, "123", 0, image_bytes(0), after_move=deleted_meanwhile)
    asset = Asset.get()
    assert asset.state == "deleted"
    assert not storage.safe_path(settings, asset.path).exists()


def test_schedule_runs_once_per_slot_merges_and_reports_connection(client, settings):
    assert schedule.tick() is None
    view = schedule.update({"enabled": True, "interval_hours": 6, "scope": "public"})
    assert view["next_run_at"] >= now() + 6 * 3600 - 5
    slots = iter(range(10, 0, -1))

    def due():
        # Distinct past slots: each due slot has its own idempotency key.
        library.set_setting("schedule_next_at", str(now() - next(slots)))

    due()
    assert schedule.tick() == "credential_missing"
    assert schedule.view()["next_run_at"] >= now() + 6 * 3600 - 5
    assert schedule.tick() is None
    connected(settings)
    due()
    assert schedule.tick() == "queued"
    job = Job.get(Job.origin == "schedule")
    assert (job.scope, job.mode) == ("public", "full")  # no full refresh recorded yet
    due()
    assert schedule.tick() == "merged"
    assert Job.select().where(Job.kind == "sync").count() == 1
    jobs.action(job.id, "cancel")
    schedule.record_full_refresh("public")
    assert schedule.view()["next_mode"] == "normal"
    due()
    assert schedule.tick() == "queued"
    latest = Job.select().where(Job.origin == "schedule").order_by(SQL("rowid DESC")).first()
    assert (latest.state, latest.mode) == ("queued", "normal")
    PixivAccount.update(status="auth_expired").execute()
    due()
    assert schedule.tick() == "auth_expired"
    accounts.disconnect()
    assert schedule.update({"enabled": False})["next_run_at"] is None


def test_backup_restore_into_new_data_dir_and_incremental_mirror(client, settings, tmp_path):
    library.ingest(remote(), "public")
    storage.store_image(settings, "123", 0, image_bytes(0))
    library.edit_tags(["123"], "복원 확인", "add")
    library.edit_note("123", "복원 메모")
    library.ingest(remote(id="124"), "public")
    Tombstone.create(artwork_id="124")
    root = tmp_path / "backups"
    first = backup.create(settings, root, media=True)
    assert first["media"]["copied"] == 1 and first["secrets_included"] is False
    snapshot = Path(first["path"])
    assert backup.verify(snapshot, None, full_hash=True)["missing"] == []
    second = backup.create(settings, root, media=True)
    assert second["media"]["copied"] == 0 and second["media"]["already_present"] == 1
    target = replace(settings, data_dir=tmp_path / "restored")
    summary = backup.restore(target, snapshot, None, full_hash=True)
    assert summary["missing"] == [] and summary["restored_files"] == 1
    initialize(target.data_dir)
    try:
        detail = library.detail("123")
        assert detail["note"] == "복원 메모"
        assert ("복원 확인", "local") in tag_sources("123")
        assert Tombstone.get_by_id("124")
        assert storage.inspect_asset(target, Asset.get()) == "ready"
        with pytest.raises(backup.BackupError, match="비어 있지"):
            backup.restore(target, snapshot, None, full_hash=False)
    finally:
        db.close()
    # A mirror missing a file restores the metadata and marks the file for repair.
    for path in (root / backup.MIRROR / "media").rglob("*.png"):
        path.unlink()
    broken = replace(settings, data_dir=tmp_path / "broken")
    assert len(backup.restore(broken, snapshot, None, full_hash=False)["missing"]) == 1
    initialize(broken.data_dir)
    try:
        assert Asset.get().state == "missing"
        assert Artwork.get_by_id("123").archive_status == "pending"
    finally:
        db.close()


def test_exports_include_sources_and_files_but_never_credentials(admin, settings):
    accounts.store_credential(settings, "42_export_secret_cookie_value")
    library.ingest(remote(), "public")
    storage.store_image(settings, "123", 0, image_bytes(0))
    library.edit_tags(["123"], "내보내기", "add")
    response = admin.get("/api/artworks/123/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "pixchive-123.zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["metadata.json", "original/p0000.png"]
        metadata = json.loads(archive.read("metadata.json"))
        digest = hashlib.sha256(archive.read("original/p0000.png")).hexdigest()
    record = metadata["artwork"]
    assert metadata["schema_version"] == 2 and record["files"][0]["sha256"] == digest
    assert {t["source"] for t in record["tags"]} == {"local", "pixiv_work", "pixiv_bookmark"}
    full = admin.get("/api/export/metadata")
    assert [a["id"] for a in full.json()["artworks"]] == ["123"]
    for text in (response.content, full.content):
        assert b"export_secret" not in text and b"ciphertext" not in text
    admin.delete("/api/session")
    assert admin.get("/api/artworks/123/export").status_code == 401
    assert admin.get("/api/export/metadata").status_code == 401


def test_log_redaction_keeps_record_shape():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/api/x?PHPSESSID=42_abcdefghijklmnopqrstuvwxyz", "1.1", 200),
        None,
    )
    assert RedactingFilter().filter(record)
    message = record.getMessage()
    assert "abcdefghijklmnopqrstuvwxyz" not in message and message.endswith("200")
    assert "secret" not in redact("Cookie: PHPSESSID=secret-value; other=1")
    assert "gAAAAA" not in redact("token gAAAAABlongfernettokenvalue123456789")
