import asyncio
import errno

import pytest
from pydantic import BaseModel, Field

from app.adapters.pixiv.contracts import (
    AdapterError,
    BookmarkEntry,
    BookmarkListing,
    RemoteArtwork,
    RemotePage,
)
from app.core.db import db
from app.core.errors import DomainError
from app.models.entities import (
    Artwork,
    ArtworkTag,
    Asset,
    Bookmark,
    CollectionRun,
    DownloadTask,
    Job,
    Lease,
    PixivAccount,
    Tombstone,
)
from app.services import accounts, jobs, library, storage
from app.services.fixtures import image_bytes
from app.worker import run_job
from tests.test_library import remote


class BookmarkPage(BaseModel):
    """Test fixture: one list page plus the details the fake adapter will return."""

    items: list[RemoteArtwork]
    next_cursor: str | None
    complete: bool = False
    seen_ids: list[str] = Field(default_factory=list)
    issues: dict[str, str] = Field(default_factory=dict)


def entry_for(work: RemoteArtwork, issue: str | None) -> BookmarkEntry:
    tags = work.bookmark_tags if work.bookmark_tags is not None else []
    return BookmarkEntry(
        id=work.id,
        title=work.title,
        artist_id=work.artist_id,
        artist_name=work.artist_name,
        page_count=work.page_count,
        work_tags=[t.name for t in work.work_tags] if work.work_tags is not None else None,
        bookmark_tags=None if issue == "bookmark_tags_unavailable" else tags,
    )


def work(artwork_id="123", count=1, revision="one", **kwargs):
    return remote(
        id=artwork_id,
        page_count=count,
        pages=[
            RemotePage(index=i, url=f"https://i.pximg.net/{artwork_id}/{revision}_p{i}.png")
            for i in range(count)
        ],
        **kwargs,
    )


class FakeAdapter:
    def __init__(self, pages=None, error=None, account_id="42"):
        self.pages = pages or {}
        self.error = error
        self.account_id = account_id
        self.cursors = []
        self.details = {}
        self.failures = {}
        self.detail_calls = []
        self.position = {}
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def validate(self):
        if self.error:
            raise self.error
        return self.account_id

    async def listing(self, account_id, scope, cursor=None):
        self.cursors.append((scope, cursor))
        pages = self.pages.get(scope, [BookmarkPage(items=[], next_cursor=None, complete=True)])
        # A scan without a cursor starts at the front again, like the real list.
        index = 0 if cursor is None else self.position.get(scope, 0)
        if index >= len(pages):
            raise AdapterError("contract")
        self.position[scope] = index + 1
        page = pages[index]
        if isinstance(page, Exception):
            raise page
        entries = {}
        for item in page.items:
            entries[item.id] = entry_for(item, page.issues.get(item.id))
            self.details[item.id] = item
        for artwork_id in page.seen_ids:
            if artwork_id in entries:
                continue
            code = page.issues.get(artwork_id, "unavailable")
            if code == "unavailable":
                entries[artwork_id] = BookmarkEntry(id=artwork_id, available=False)
            else:
                entries[artwork_id] = BookmarkEntry(id=artwork_id)
                self.failures[artwork_id] = code
        return BookmarkListing(
            entries=list(entries.values()), next_cursor=page.next_cursor, complete=page.complete
        )

    async def artwork(self, artwork_id):
        self.detail_calls.append(artwork_id)
        if artwork_id in self.failures:
            raise AdapterError(self.failures[artwork_id])
        item = self.details.get(artwork_id)
        if item is None:
            raise AdapterError("unavailable")
        return item.model_copy(update={"bookmark_tags": None}, deep=True)

    async def verify(self, account_id, scope, total, first_bookmark_id):
        return None


def connected(settings):
    accounts.store_credential(settings, "synthetic-cookie-no-network")
    _, credential = accounts.load_credential(settings)
    accounts.validated(credential, "42")
    return credential


def context(settings, adapter, fetched=None):
    async def fetch(url):
        if fetched is not None:
            fetched.append(url)
        return image_bytes(0)

    return {
        "settings": settings,
        "adapter_factory": lambda token, **options: adapter,
        "download": fetch,
    }


async def test_connection_validation_is_queued_and_secret_free(admin, settings):
    admin.put("/api/accounts/1/credential", json={"phpsessid": "synthetic-cookie-no-network"})
    response = admin.post("/api/accounts/1/validate", headers={"Idempotency-Key": "one"})
    assert response.status_code == 202
    assert "synthetic-cookie" not in response.text
    adapter = FakeAdapter()
    await run_job(context(settings, adapter), response.json()["id"])
    assert adapter.closed
    account = admin.get("/api/accounts").json()[0]
    assert account["status"] == "connected" and account["pixiv_id"] == "42"
    assert account["validated_at"] is not None


async def test_complete_scopes_all_pages_and_no_duplicate_downloads(admin, settings):
    connected(settings)
    a, b = work(count=2), work("124")
    pages = {
        "public": [
            BookmarkPage(items=[a], next_cursor="8"),
            BookmarkPage(items=[b], next_cursor=None, complete=True),
        ],
        "private": [BookmarkPage(items=[a], next_cursor=None, complete=True)],
    }
    fetched = []
    job = jobs.enqueue_account("sync", "one", "all")
    await run_job(context(settings, FakeAdapter(pages), fetched), job.id)
    result = Job.get_by_id(job.id)
    assert (result.state, result.processed, result.total, result.failed) == ("succeeded", 3, 3, 0)
    assert Asset.select().where(Asset.role == "original").count() == 3
    assert Artwork.select().where(Artwork.archive_status == "complete").count() == 2
    library.edit_tags(["123"], "수동 보존", "add")
    second = jobs.enqueue_account("sync", "two", "all")
    await run_job(context(settings, FakeAdapter(pages), fetched), second.id)
    assert len(fetched) == 3
    assert Asset.select().where(Asset.role == "original").count() == 3
    assert ArtworkTag.select().where(ArtworkTag.source == "local").count() == 1
    assert Job.get_by_id(second.id).state == "succeeded"


async def test_partial_scan_never_marks_absent_and_resume_uses_committed_cursor(client, settings):
    connected(settings)
    library.ingest(work("999"), "public")
    job = jobs.enqueue_account("sync", "one", "public")
    adapter = FakeAdapter(
        {
            "public": [
                BookmarkPage(items=[work()], next_cursor="8"),
                AdapterError("auth_expired"),
            ]
        }
    )
    fetched = []
    await run_job(context(settings, adapter, fetched), job.id)
    assert Job.get_by_id(job.id).state == "waiting_auth"
    assert CollectionRun.get().cursor == "8"
    assert Bookmark.get(Bookmark.artwork == "999").status == "seen"
    connected(settings)
    jobs.action(job.id, "retry")
    adapter = FakeAdapter(
        {"public": [BookmarkPage(items=[work("124")], next_cursor=None, complete=True)]}
    )
    await run_job(context(settings, adapter, fetched), job.id)
    assert adapter.cursors == [("public", "8")]
    assert len(fetched) == 2
    assert Bookmark.get(Bookmark.artwork == "999").status == "not_seen"
    assert Artwork.get_by_id("999")


async def test_scope_absence_and_unavailable_do_not_delete_originals(client, settings):
    connected(settings)
    library.ingest(work(), "public")
    library.ingest(work("124"), "private")
    asset = storage.store_image(settings, "123", 0, image_bytes(0))
    job = jobs.enqueue_account("sync", "one", "public")
    page = BookmarkPage(
        items=[], seen_ids=["123"], issues={"123": "unavailable"}, next_cursor=None, complete=True
    )
    await run_job(context(settings, FakeAdapter({"public": [page]})), job.id)
    # An inaccessible remote work is reported, not counted as a local failure.
    assert Job.get_by_id(job.id).state == "succeeded"
    assert jobs.serialize(Job.get_by_id(job.id))["scan"]["unavailable"] == 1
    assert jobs.issues(job.id) == [{"artwork_id": "123", "page": None, "code": "unavailable"}]
    assert Bookmark.get(Bookmark.artwork == "123").status == "seen"
    assert Bookmark.get(Bookmark.artwork == "124").status == "seen"
    assert storage.safe_path(settings, asset.path).is_file()
    assert Artwork.get_by_id("123").remote_status == "unknown"


async def test_invalid_artwork_is_isolated_from_the_rest_of_the_page(client, settings):
    connected(settings)
    job = jobs.enqueue_account("sync", "one", "public")
    invalid = work(count=2)
    invalid.pages.pop()
    page = BookmarkPage(items=[work("124"), invalid], next_cursor=None, complete=True)
    await run_job(context(settings, FakeAdapter({"public": [page]})), job.id)
    assert Job.get_by_id(job.id).state == "partial"
    assert jobs.issues(job.id) == [{"artwork_id": "123", "page": None, "code": "contract"}]
    assert [a.id for a in Artwork.select()] == ["124"]
    assert DownloadTask.select().count() == 1


async def test_bad_page_rolls_back_metadata_and_cursor(client, settings):
    connected(settings)
    job = jobs.enqueue_account("sync", "one", "public")
    # complete=False without a next cursor is a list contract violation.
    page = BookmarkPage(items=[work("124")], next_cursor=None, complete=False)
    await run_job(context(settings, FakeAdapter({"public": [page]})), job.id)
    assert Job.get_by_id(job.id).error_code == "contract"
    assert CollectionRun.get().cursor is None
    assert Artwork.select().count() == 0
    assert DownloadTask.select().count() == 0


async def test_tombstone_is_never_downloaded(admin, settings):
    connected(settings)
    Tombstone.create(artwork_id="123")
    page = BookmarkPage(items=[work()], next_cursor=None, complete=True)
    fetched = []
    job = jobs.enqueue_account("sync", "one", "public")
    await run_job(context(settings, FakeAdapter({"public": [page]}), fetched), job.id)
    assert not fetched
    assert Artwork.select().count() == 0
    assert Job.get_by_id(job.id).state == "succeeded"


async def test_one_file_failure_does_not_prevent_other_files(client, settings):
    connected(settings)
    page = BookmarkPage(items=[work(count=2)], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))

    async def fetch(url):
        if "p0" in url:
            raise AdapterError("unavailable")
        return image_bytes(0)

    ctx["download"] = fetch
    job = jobs.enqueue_account("sync", "one", "public")
    await run_job(ctx, job.id)
    assert (Job.get_by_id(job.id).state, Job.get_by_id(job.id).failed) == ("partial", 1)
    assert Asset.get(Asset.role == "original").page == 1
    assert Artwork.get().archive_status == "partial"
    jobs.action(job.id, "retry")
    fetched = []
    await run_job(context(settings, FakeAdapter({"public": [page]}), fetched), job.id)
    assert len(fetched) == 1 and "p0" in fetched[0]
    assert Job.get_by_id(job.id).state == "succeeded"


async def test_pause_terminates_active_fetch_before_ack_and_resume_skips_saved(client, settings):
    connected(settings)
    page = BookmarkPage(items=[work(count=2)], next_cursor=None, complete=True)
    adapter = FakeAdapter({"public": [page]})
    ctx = context(settings, adapter)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def fetch(url):
        if "p0" in url:
            return image_bytes(0)
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    ctx["download"] = fetch
    job = jobs.enqueue_account("sync", "one", "public")
    task = asyncio.create_task(run_job(ctx, job.id))
    await asyncio.wait_for(started.wait(), 5)
    assert jobs.action(job.id, "pause").state == "pausing"
    await asyncio.wait_for(task, 5)
    assert stopped.is_set() and adapter.closed
    assert Job.get_by_id(job.id).state == "paused"
    jobs.action(job.id, "resume")
    fetched = []
    await run_job(context(settings, FakeAdapter(), fetched), job.id)
    assert len(fetched) == 1 and "p1" in fetched[0]
    assert Job.get_by_id(job.id).state == "succeeded"


async def test_disk_full_waits_and_preserves_pending_task(client, settings, monkeypatch):
    connected(settings)
    page = BookmarkPage(items=[work()], next_cursor=None, complete=True)
    ctx = context(settings, FakeAdapter({"public": [page]}))

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "synthetic disk full")

    monkeypatch.setattr(storage, "store_image", full)
    job = jobs.enqueue_account("sync", "one", "public")
    await run_job(ctx, job.id)
    assert Job.get_by_id(job.id).state == "waiting_storage"
    assert DownloadTask.get().state == "pending"


async def test_move_before_commit_recovery_reuses_file_without_network(
    client, settings, monkeypatch
):
    connected(settings)
    page = BookmarkPage(items=[work()], next_cursor=None, complete=True)
    job = jobs.enqueue_account("sync", "one", "public")
    original = storage.store_image

    def crash(*args, **kwargs):
        def after_move():
            raise RuntimeError("synthetic crash")

        return original(*args, **kwargs, after_move=after_move)

    monkeypatch.setattr(storage, "store_image", crash)
    with pytest.raises(RuntimeError, match="archive_job_failed"):
        await run_job(context(settings, FakeAdapter({"public": [page]})), job.id)
    assert DownloadTask.get().asset_id
    assert Asset.get(Asset.role == "original").state == "staged"
    monkeypatch.setattr(storage, "store_image", original)
    jobs.action(job.id, "retry")
    fetched = []
    await run_job(context(settings, FakeAdapter({"public": [page]}), fetched), job.id)
    assert not fetched
    assert Asset.get(Asset.role == "original").state == "ready"
    assert Job.get_by_id(job.id).state == "succeeded"


async def test_changed_original_url_creates_new_revision(client, settings):
    connected(settings)
    for revision in ("one", "two"):
        page = BookmarkPage(items=[work(revision=revision)], next_cursor=None, complete=True)
        ctx = context(settings, FakeAdapter({"public": [page]}))

        async def fetch(url):
            return image_bytes(0 if "one" in url else 1)

        ctx["download"] = fetch
        # A list row cannot reveal a replaced original URL; a full refresh reads details.
        job = jobs.enqueue_account("sync", revision, "public", mode="full")
        await run_job(ctx, job.id)
    assert Asset.select().where(Asset.role == "original").count() == 2


def test_account_jobs_share_lease_and_same_request_is_idempotent(client, settings):
    connected(settings)
    first = jobs.enqueue_account("sync", "one", "public")
    assert jobs.enqueue_account("sync", "two", "public").id == first.id
    second = jobs.enqueue_account("sync", "one", "private")
    owner = jobs.claim(first.id)
    assert jobs.claim(second.id) is None
    jobs.finish(first.id, owner, "succeeded")
    assert jobs.claim(second.id)
    assert Lease.select().count() == 1


async def test_token_replacement_cannot_be_overwritten_by_old_validation(client, settings):
    old = connected(settings)
    accounts.store_credential(settings, "a-new-synthetic-cookie")
    with pytest.raises(DomainError, match="credential_changed"):
        accounts.validated(old, "42")
    accounts.authentication_failed(old)
    assert PixivAccount.get().status == "unverified"
    _, new = accounts.load_credential(settings)
    with pytest.raises(DomainError, match="account_mismatch"):
        accounts.validated(new, "43")
    assert PixivAccount.get().status == "account_mismatch"
    assert PixivAccount.get().pixiv_id == "42"


def test_existing_schema_upgrade_preserves_data(settings):
    from pathlib import Path

    from app.core.db import initialize
    from app.migrations.runner import migrate

    if not db.is_closed():
        db.close()
    initialize(settings.data_dir)
    for statement in Path("app/migrations/001_initial.sql").read_text().split(";"):
        if statement.strip():
            db.execute_sql(statement)
    db.execute_sql("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
    db.execute_sql("INSERT INTO schema_version VALUES(1)")
    db.execute_sql("INSERT INTO setting VALUES('preserved', 'yes')")
    migrate()
    assert library.get_setting("preserved") == "yes"
    assert Job.select().count() == 0
    db.close()


def test_rate_limit_cooldown_survives_retry_and_blocks_other_account_jobs(client, settings):
    from app.core.time import now

    connected(settings)
    job = jobs.enqueue_account("sync", "one", "all")
    owner = jobs.claim(job.id)
    jobs.backoff(job.id, owner, AdapterError("rate_limited", 600), 600)
    jobs.finish(job.id, owner, "failed", "rate_limited")
    jobs.action(job.id, "retry")
    assert jobs.claim(job.id) is None
    validation = jobs.enqueue_account("validate", "one")
    assert jobs.claim(validation.id) is None
    assert Job.get_by_id(validation.id).retry_at >= now() + 599
    assert jobs.claim(jobs.enqueue_integrity("one").id)
