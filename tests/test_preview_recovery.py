"""Durable, bounded preview recovery after original downloads already completed."""

import io
from dataclasses import replace

import pytest
from PIL import Image

from app.core.db import db
from app.core.errors import DomainError
from app.models.entities import Asset, Credential, DownloadTask, Job, Outbox, Tombstone
from app.services import jobs, library, preferences, previews, storage
from app.services.execution import Interrupted
from app.worker import run_job
from tests.test_library import remote


def png(color="teal"):
    output = io.BytesIO()
    Image.new("RGB", (80, 120), color).save(output, "PNG")
    return output.getvalue()


@pytest.fixture
def collection_job(client):
    return Job.create(
        kind="sync", scope="public", idempotency_key="synthetic-parent", lease_key="account:1"
    )


def completed(settings, parent, artwork_id="123", **changes):
    library.ingest(remote(id=artwork_id, **changes), "public")
    source = storage.store_image(settings, artwork_id, 0, png())
    task = DownloadTask.create(
        job=parent,
        artwork=artwork_id,
        page=0,
        url="https://example.invalid/no-network",
        asset=source,
        state="ready",
    )
    return source, task


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("reconciled", [False, True])
async def test_interrupted_thumbnail_is_recovered_as_same_asset_after_original_completed(
    collection_job, settings, monkeypatch, published, reconciled
):
    settings = replace(settings, cache_dir=settings.data_dir.parent / "cache")
    source, task = completed(settings, collection_job, page_count=2)
    second = storage.store_image(settings, source.artwork_id, 1, png("red"))
    originals = {
        asset.id: (
            asset.path,
            asset.size,
            asset.sha256,
            storage.hash_file(storage.safe_path(settings, asset.path)),
        )
        for asset in (source, second)
    }
    store_image = storage.store_image
    staged = []

    def interrupted():
        raise Interrupted()

    def check():
        if staged:
            interrupted()

    def interrupt_thumbnail(*args, **kwargs):
        assert kwargs["role"] == "thumbnail"
        kwargs["on_staged"] = staged.append
        if published:
            kwargs["after_move"] = interrupted
        else:
            kwargs["guard"] = check
        return store_image(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "store_image", interrupt_thumbnail)
        with pytest.raises(Interrupted):
            storage.make_thumbnail(settings, source.artwork_id, source)
    failed_thumbnail = Asset.get_by_id(staged[0].id)
    assert failed_thumbnail.state == "staged"
    assert storage.inspect_asset(settings, failed_thumbnail) == (
        "ready" if published else "missing"
    )
    assert DownloadTask.get_by_id(task.id).state == "ready"
    assert Asset.get_by_id(source.id).thumbnail_id is None
    if reconciled:
        storage.reconcile(settings)
        assert Asset.get_by_id(failed_thumbnail.id).state == ("ready" if published else "missing")

    recovered = previews.enqueue_missing(collection_job.id)
    assert len(recovered) == 1
    preview = recovered[0]
    assert preview.kind == "preview" and preview.scope == source.artwork_id
    assert preview.idempotency_key.endswith(f":recover-source:{source.id}")
    assert preview.lease_key != collection_job.lease_key
    assert Outbox.get(Outbox.job == preview).delivered_at is None
    assert previews.enqueue_missing(collection_job.id) == []
    await run_job({"settings": settings}, preview.id)

    result = Job.get_by_id(preview.id)
    assert (result.state, result.processed, result.failed) == ("succeeded", 1, 0)
    thumbnail = Asset.get_by_id(source.id).thumbnail
    assert thumbnail.id == failed_thumbnail.id
    assert thumbnail.state == "ready"
    assert (thumbnail.size, thumbnail.sha256) == (failed_thumbnail.size, failed_thumbnail.sha256)
    assert storage.inspect_asset(settings, thumbnail) == "ready"
    assert Asset.select().where(Asset.role == "thumbnail").count() == 1
    for identifier, baseline in originals.items():
        current = Asset.get_by_id(identifier)
        assert (current.path, current.size, current.sha256) == baseline[:3]
        assert storage.hash_file(storage.safe_path(settings, current.path)) == baseline[3]
        assert current.state == "ready"
    assert DownloadTask.get_by_id(task.id).state == "ready"
    assert Job.get_by_id(collection_job.id).state == collection_job.state
    assert Credential.select().count() == 0
    assert previews.enqueue_missing(collection_job.id) == []


def test_ready_thumbnail_skipped_and_dedupe_survives_parent_changes(collection_job, settings):
    source, task = completed(settings, collection_job)
    another = Job.create(
        kind="repair",
        scope=source.artwork_id,
        idempotency_key="other-parent",
        lease_key="account:1",
    )
    DownloadTask.create(
        job=another, artwork=source.artwork_id, page=0, asset=source, state="ready", url=task.url
    )
    queued = previews.enqueue_missing(collection_job.id)
    assert len(queued) == 1
    assert previews.enqueue_missing(another.id) == []
    assert Job.select().where(Job.kind == "preview").count() == 1
    storage.make_thumbnail(settings, source.artwork_id, source)
    assert previews.enqueue_missing(collection_job.id) == []
    assert previews.enqueue_missing(another.id) == []


async def test_hidden_kept_original_recovers_without_changing_ui_or_download_policy(
    collection_job, settings
):
    source, _ = completed(settings, collection_job, rating="r18")
    preferences.update({"show_sensitive": False, "download_ratings": []})
    policy = preferences.view()
    with pytest.raises(DomainError, match="hidden_rating"):
        previews.request(source.artwork_id, "ui-request")
    queued = previews.enqueue_missing(collection_job.id)
    assert len(queued) == 1
    await run_job({"settings": settings}, queued[0].id)
    assert Job.get_by_id(queued[0].id).state == "succeeded"
    assert Asset.get_by_id(source.id).thumbnail.state == "ready"
    assert preferences.view() == policy
    with pytest.raises(DomainError, match="hidden_rating"):
        library.require_artwork(source.artwork_id)
    assert Credential.select().count() == 0


def test_tombstoned_artwork_is_never_auto_queued(collection_job, settings):
    source, _ = completed(settings, collection_job)
    Tombstone.create(artwork_id=source.artwork_id)
    assert previews.enqueue_missing(collection_job.id) == []
    assert not Job.select().where(Job.kind == "preview").exists()
    assert storage.safe_path(settings, source.path).is_file()


async def test_deletion_between_enqueue_and_execute_still_fences_recovery(collection_job, settings):
    source, _ = completed(settings, collection_job)
    preview = previews.enqueue_missing(collection_job.id)[0]
    Tombstone.create(artwork_id=source.artwork_id)
    await run_job({"settings": settings}, preview.id)
    result = Job.get_by_id(preview.id)
    assert (result.state, result.error_code) == ("failed", "excluded")
    assert Asset.get_by_id(source.id).thumbnail_id is None
    assert not Asset.select().where(Asset.role == "thumbnail").exists()
    assert previews.enqueue_missing(collection_job.id) == []


async def test_failed_attempt_does_not_loop_and_explicit_retry_still_works(
    collection_job, settings, monkeypatch
):
    source, _ = completed(settings, collection_job)
    preview = previews.enqueue_missing(collection_job.id)[0]

    def failed(*args):
        raise DomainError("invalid_media", "synthetic preview failure")

    with monkeypatch.context() as patch:
        patch.setattr(previews, "build", failed)
        await run_job({"settings": settings}, preview.id)
    assert Job.get_by_id(preview.id).state == "failed"
    assert Asset.get_by_id(source.id).state == "ready"
    for _ in range(5):
        assert previews.enqueue_missing(collection_job.id) == []
    assert Job.select().where(Job.kind == "preview").count() == 1
    jobs.action(preview.id, "retry")
    await run_job({"settings": settings}, preview.id)
    assert Job.get_by_id(preview.id).state == "succeeded"


def test_cancelled_attempt_is_preserved_but_new_source_has_its_own_attempt(
    collection_job, settings
):
    source, task = completed(settings, collection_job)
    preview = previews.enqueue_missing(collection_job.id)[0]
    assert jobs.action(preview.id, "cancel").state == "cancelled"
    assert previews.enqueue_missing(collection_job.id) == []
    newer = storage.store_image(settings, source.artwork_id, 0, png("red"))
    DownloadTask.update(asset=newer).where(DownloadTask.id == task.id).execute()
    queued = previews.enqueue_missing(collection_job.id)
    assert len(queued) == 1 and queued[0].id != preview.id
    assert queued[0].idempotency_key.endswith(f":recover-source:{newer.id}")
    assert Job.get_by_id(preview.id).state == "cancelled"


def test_older_source_cannot_override_current_source_or_use_corrupt_latest(
    collection_job, settings
):
    source, task = completed(settings, collection_job)
    newer = storage.store_image(settings, source.artwork_id, 0, png("red"))
    assert previews.enqueue_missing(collection_job.id) == []
    DownloadTask.update(asset=newer).where(DownloadTask.id == task.id).execute()
    Asset.update(state="corrupt").where(Asset.id == newer.id).execute()
    assert previews.enqueue_missing(collection_job.id) == []
    assert Asset.get_by_id(source.id).state == "ready"


def test_limit_applies_after_ready_active_deleted_and_previous_attempts_are_filtered(
    collection_job, settings, monkeypatch
):
    sources = [completed(settings, collection_job, str(123 + index))[0] for index in range(6)]
    storage.make_thumbnail(settings, sources[0].artwork_id, sources[0])
    cancelled = jobs.enqueue_preview(sources[1].artwork_id, f"recover-source:{sources[1].id}")
    jobs.action(cancelled.id, "cancel")
    active = previews.request(sources[2].artwork_id, "manual-active")
    Tombstone.create(artwork_id=sources[3].artwork_id)
    queries = []
    execute_sql = db.execute_sql

    def tracked(sql, params=None, *args, **kwargs):
        if sql.startswith("SELECT DISTINCT a.rowid"):
            queries.append((sql, params))
        return execute_sql(sql, params, *args, **kwargs)

    monkeypatch.setattr(db, "execute_sql", tracked)
    first = previews.enqueue_missing(collection_job.id, limit=1)
    second = previews.enqueue_missing(collection_job.id, limit=1)
    assert [job.scope for job in first + second] == [source.artwork_id for source in sources[4:]]
    assert previews.enqueue_missing(collection_job.id, limit=1) == []
    assert all("LIMIT ?" in sql and params[-1] == 1 for sql, params in queries)
    assert len(queries) == 3
    assert Job.get_by_id(active.id).state == "queued"
    assert Job.get_by_id(cancelled.id).state == "cancelled"


@pytest.mark.parametrize("task_state", ["pending", "failed", "excluded", "skipped"])
def test_only_completed_original_tasks_are_eligible(collection_job, settings, task_state):
    _, task = completed(settings, collection_job)
    DownloadTask.update(state=task_state).where(DownloadTask.id == task.id).execute()
    assert previews.enqueue_missing(collection_job.id) == []


def test_second_pages_derived_assets_and_ugoira_are_not_preview_recovery_sources(
    collection_job, settings
):
    source, task = completed(settings, collection_job, page_count=2)
    second = storage.store_image(settings, source.artwork_id, 1, png("red"))
    DownloadTask.update(page=1, asset=second).where(DownloadTask.id == task.id).execute()
    assert previews.enqueue_missing(collection_job.id) == []
    thumbnail = storage.make_thumbnail(settings, source.artwork_id, source)
    DownloadTask.update(page=0, asset=thumbnail).where(DownloadTask.id == task.id).execute()
    assert previews.enqueue_missing(collection_job.id) == []
    animation_source, _ = completed(settings, collection_job, "124", kind="ugoira")
    assert animation_source.state == "ready"
    assert previews.enqueue_missing(collection_job.id) == []


def test_enqueue_and_outbox_are_atomic_if_a_later_enqueue_fails(
    collection_job, settings, monkeypatch
):
    completed(settings, collection_job)
    completed(settings, collection_job, "124")
    enqueue = jobs.enqueue_preview
    calls = []

    def fail_second(artwork_id, key):
        calls.append(artwork_id)
        if len(calls) == 2:
            raise RuntimeError("synthetic queue failure")
        return enqueue(artwork_id, key)

    monkeypatch.setattr(jobs, "enqueue_preview", fail_second)
    with pytest.raises(RuntimeError, match="synthetic queue failure"):
        previews.enqueue_missing(collection_job.id)
    assert len(calls) == 2
    assert not Job.select().where(Job.kind == "preview").exists()
    assert Outbox.select().count() == 0


@pytest.mark.parametrize("limit", [-1, 101, True, 1.5, "1", None])
def test_invalid_recovery_limits_fail_before_querying(monkeypatch, limit):
    def no_query(*args, **kwargs):
        pytest.fail("Invalid recovery limits must not query the database")

    monkeypatch.setattr(db, "execute_sql", no_query)
    with pytest.raises(ValueError, match="limit"):
        previews.enqueue_missing("not-a-job", limit=limit)


def test_zero_limit_does_no_work(monkeypatch):
    def no_query(*args, **kwargs):
        pytest.fail("Zero recovery limit must not query the database")

    monkeypatch.setattr(db, "execute_sql", no_query)
    assert previews.enqueue_missing("not-a-job", limit=0) == []
