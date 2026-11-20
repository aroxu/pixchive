"""Durable bookmark scans, metadata refresh, and original collection, independent of HTTP."""

import asyncio
import errno
import hashlib
import json
import math
import shutil
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass

from peewee import Case, fn

from app.adapters.pixiv.contracts import (
    AdapterError,
    BookmarkEntry,
    BookmarkListing,
    RemoteArtwork,
    RemotePage,
)
from app.adapters.pixiv.live import CookiePixivAdapter
from app.adapters.pixiv.transport import MAX_IMAGE_BYTES, MediaDownloadSession, download
from app.core.async_cleanup import settle
from app.core.db import db
from app.core.errors import DomainError
from app.core.time import now
from app.models.entities import (
    Artist,
    Artwork,
    Asset,
    Bookmark,
    CollectionItem,
    CollectionRun,
    DownloadTask,
    Job,
    Tombstone,
)
from app.services import (
    accounts,
    jobs,
    library,
    preferences,
    previews,
    repair,
    request_budget,
    schedule,
    storage,
    storage_budget,
    tempfiles,
    ugoira,
)
from app.services.execution import Interrupted, disk_operation, watch

# Errors that stop the account job. Other per-artwork errors are recorded and skipped.
FATAL = ("auth_expired", "rate_limited", "network", "access_denied", "collection_changed")
AUTH_CODES = (
    "credential_missing",
    "cookie_required",
    "key_unavailable",
    "credential_changed",
    "account_mismatch",
)
MAX_AVATAR_FAILURES = 200
PARALLEL_PAGES = 3
# Replaced in tests. The per-minute wait re-checks pause/cancel every second.
limit_sleep = asyncio.sleep


@dataclass
class _CredentialState:
    ciphertext: str


def guard(job_id, owner, credential):
    job = jobs.owned(job_id, owner)
    if not job or job.state != "running":
        raise Interrupted
    expected = credential.ciphertext if isinstance(credential, _CredentialState) else credential
    if not accounts.credential_current(expected):
        raise DomainError("credential_changed", "쿠키가 변경되어 작업을 중단했습니다.", 409)


async def interruptible(operation, job_id, owner, credential):
    """Cancel network/subprocess activity and await cleanup before acknowledging pause."""
    return await watch(operation, lambda: guard(job_id, owner, credential))


async def artwork_slot(job_id, owner, credential):
    """Wait for room under the per-minute artwork lookup limit.

    Only artwork information requests count; bookmark lists and image downloads do not.
    While waiting, the job shows the request-limit phase and the next allowed time.
    """
    previous = None
    try:
        while True:
            guard(job_id, owner, credential)
            wait = request_budget.reserve(schedule.artworks_per_minute())
            if wait <= 0:
                return
            if previous is None:
                previous = Job.get_by_id(job_id).phase
                Job.update(
                    phase="request_limit",
                    retry_at=math.ceil(request_budget.clock() + wait),
                ).where((Job.id == job_id) & (Job.owner == owner)).execute()
            await limit_sleep(min(wait, 1.0))
    finally:
        if previous is not None:
            Job.update(phase=previous, retry_at=None).where(
                (Job.id == job_id) & (Job.owner == owner) & (Job.phase == "request_limit")
            ).execute()


def ensure_space(settings):
    """Stop before a new download when free space falls under the configured reserve."""
    if shutil.disk_usage(settings.data_dir).free < settings.min_free_bytes:
        raise OSError(errno.ENOSPC, "storage_low")


def fingerprint(entry: BookmarkEntry) -> str:
    payload = [
        entry.title,
        entry.page_count,
        entry.artist_id,
        entry.artist_name,
        entry.updated_at,
        sorted(entry.work_tags) if entry.work_tags is not None else None,
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def needs_details(entry: BookmarkEntry, artwork: Artwork | None, mode, refresh_before) -> bool:
    """A normal sync reads every list page but fetches details only when useful."""
    if artwork is None or mode != "normal":
        return True
    if artwork.archive_status != "complete" and preferences.download_allowed(artwork.rating):
        return True
    if artwork.remote_status != "available":
        return True
    if artwork.metadata_fetched_at is None or artwork.metadata_fetched_at < refresh_before:
        return True
    if artwork.listing_fingerprint is not None:
        return artwork.listing_fingerprint != fingerprint(entry)
    # Collected before list fingerprints existed: compare the cheap fields once.
    return any(
        observed is not None and observed != stored
        for observed, stored in (
            (entry.title, artwork.title),
            (entry.page_count, artwork.page_count),
            (entry.artist_id, artwork.artist_id),
            (entry.artist_name, artwork.artist.name),
        )
    )


def download_plan(remote: RemoteArtwork) -> tuple[list[RemotePage], str | None]:
    if remote.kind == "ugoira":
        if not remote.ugoira_zip_url or not remote.ugoira_frames or remote.page_count != 1:
            raise AdapterError("contract")
        frames = json.dumps([frame.model_dump() for frame in remote.ugoira_frames])
        return [RemotePage(index=0, url=remote.ugoira_zip_url, source=remote.ugoira_source)], frames
    if len(remote.pages) != remote.page_count or {p.index for p in remote.pages} != set(
        range(remote.page_count)
    ):
        raise AdapterError("contract")
    return remote.pages, None


def plan_downloads(job_id, artwork_id, remote: RemoteArtwork) -> int:
    """Queue only pages whose observed source differs from a ready local copy."""
    if not preferences.download_allowed(remote.rating):
        return 0
    pages, frames = download_plan(remote)
    planned = 0
    for page in pages:
        kept = (
            DownloadTask.select()
            .join(Asset, on=(DownloadTask.asset == Asset.id))
            .where(
                (DownloadTask.artwork == artwork_id)
                & (DownloadTask.page == page.index)
                & (DownloadTask.url == page.url)
                & (DownloadTask.frames == frames)
                & (DownloadTask.state == "ready")
                & (DownloadTask.job != job_id)
                & (Asset.state == "ready")
            )
            .exists()
        )
        if kept and frames:
            # A ready ZIP alone cannot prove timing/video health after an integrity check.
            bundle = ugoira.latest_bundle(artwork_id)
            kept = bool(
                bundle
                and bundle.archive.state == "ready"
                and bundle.timing.state == "ready"
                and bundle.state == "ready"
                and bundle.video_id
                and bundle.video.state == "ready"
            )
        if kept:
            continue
        task, created = DownloadTask.get_or_create(
            job=job_id,
            artwork=artwork_id,
            page=page.index,
            defaults={"url": page.url, "frames": frames, "provenance": page.source},
        )
        if not created and (task.url != page.url or task.frames != frames):
            task.url, task.asset, task.state = page.url, None, "pending"
            task.frames, task.error_code = frames, None
            task.provenance = page.source
            task.save()
        elif not created and task.provenance is None and page.source is not None:
            task.provenance = page.source
            task.save()
        planned += 1
    return planned


async def observe(adapter, listing: BookmarkListing, mode, refresh_before, job_id, owner, cred):
    """Fetch details outside any DB transaction; persistence happens in checkpoint()."""
    observed = {}
    for entry in listing.entries:
        guard(job_id, owner, cred)
        if Tombstone.get_or_none(Tombstone.artwork_id == entry.id):
            observed[entry.id] = {"outcome": "excluded"}
            continue
        if not entry.available:
            observed[entry.id] = {"outcome": "unavailable", "issue": "unavailable"}
            continue
        artwork = Artwork.get_or_none(Artwork.id == entry.id)
        if not needs_details(entry, artwork, mode, refresh_before):
            observed[entry.id] = {"outcome": "unchanged"}
            continue
        await artwork_slot(job_id, owner, cred)
        try:
            remote = await interruptible(adapter.artwork(entry.id), job_id, owner, cred)
            if remote.id != entry.id:
                raise AdapterError("contract")
            if preferences.download_allowed(remote.rating):
                download_plan(remote)
        except AdapterError as exc:
            if exc.code in FATAL:
                raise
            outcome = "unavailable" if exc.code == "unavailable" else "error"
            observed[entry.id] = {"outcome": outcome, "issue": exc.code}
            continue
        # The bookmark list is the source of bookmark tags; details never carry them.
        remote.bookmark_tags = entry.bookmark_tags
        observed[entry.id] = {"remote": remote}
    return observed


def checkpoint(job_id, owner, credential, run_id, listing: BookmarkListing, observed):
    with db.atomic("IMMEDIATE"):
        guard(job_id, owner, credential)
        run = CollectionRun.get_by_id(run_id)
        if not listing.complete and listing.next_cursor == run.cursor:
            raise AdapterError("contract")
        for entry in listing.entries:
            result = observed[entry.id]
            outcome, issue = result.get("outcome"), result.get("issue")
            Bookmark.update(
                generation=run.generation, status="seen", visibility=run.scope, last_seen=now()
            ).where((Bookmark.account == 1) & (Bookmark.artwork == entry.id)).execute()
            if outcome == "unavailable":
                Artwork.update(remote_status="unknown").where(Artwork.id == entry.id).execute()
            remote = result.get("remote")
            if remote is not None:
                existed = Artwork.select().where(Artwork.id == entry.id).exists()
                artwork, changed = library.observe_remote(
                    remote, run.scope, fingerprint=fingerprint(entry)
                )
                if artwork is None:
                    outcome = "excluded"
                else:
                    outcome = "added" if not existed else "changed" if changed else "checked"
                    Bookmark.update(generation=run.generation).where(
                        (Bookmark.account == 1) & (Bookmark.artwork == artwork)
                    ).execute()
                    plan_downloads(job_id, artwork.id, remote)
                    if entry.bookmark_tags is None:
                        issue = "bookmark_tags_unavailable"
            elif outcome == "unchanged":
                library.touch_bookmark(
                    entry.id, run.scope, run.generation, entry.bookmark_tags, fingerprint(entry)
                )
                if entry.bookmark_tags is None:
                    issue = "bookmark_tags_unavailable"
            if outcome not in ("excluded", "unavailable", "error"):
                library.observe_avatar(entry.artist_id, entry.avatar_url)
            item, _ = CollectionItem.get_or_create(run=run, artwork_id=entry.id)
            item.error_code, item.outcome = issue, outcome
            saved = Artwork.get_or_none(Artwork.id == entry.id)
            item.download_skipped = bool(
                saved
                and outcome not in ("excluded", "unavailable", "error")
                and not preferences.download_allowed(saved.rating)
            )
            item.save()
        run.cursor = listing.next_cursor
        if listing.complete:
            run.state = "complete"
            # Scope-specific, successful full enumeration only. Files are untouched.
            Bookmark.update(status="not_seen").where(
                (Bookmark.account == 1)
                & (Bookmark.visibility == run.scope)
                & ((Bookmark.generation.is_null()) | (Bookmark.generation != run.generation))
            ).execute()
        run.save()
        if listing.complete and jobs.require_job(job_id).mode == "full":
            incomplete = (
                CollectionItem.select()
                .where(
                    (CollectionItem.run == run)
                    & CollectionItem.error_code.is_null(False)
                    & (CollectionItem.error_code != "unavailable")
                )
                .exists()
            )
            if not incomplete:
                schedule.record_full_refresh(run.scope)


def save_original(settings, item, content, job_id, owner, credential):
    with db.connection_context():

        def check():
            guard(job_id, owner, credential)

        def staged(asset):
            DownloadTask.update(asset=asset).where(DownloadTask.id == item.id).execute()

        if item.frames:
            return ugoira.store_original(
                settings,
                item.artwork_id,
                content,
                json.loads(item.frames),
                on_staged=staged,
                guard=check,
                parent_job=job_id,
                provenance=item.provenance,
            )
        return storage.store_image(
            settings,
            item.artwork_id,
            item.page,
            content,
            on_staged=staged,
            guard=check,
            provenance=item.provenance,
        )


def save_thumbnail(settings, artwork_id, source, check):
    with db.connection_context():
        return storage.make_thumbnail(settings, artwork_id, source, check)


async def make_preview(settings, artwork_id, job_id, owner, check):
    """Best-effort library preview. A failed preview never fails the original."""
    source = storage.needs_thumbnail(artwork_id)
    if source is None:
        return
    try:
        with storage_budget.hold(settings, job_id, owner, storage_budget.PREVIEW_BYTES, check):
            await disk_operation(save_thumbnail, settings, artwork_id, source, check)
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise
    except DomainError as exc:
        if exc.code == "credential_changed":
            raise


def progress(job_id):
    processed, file_failures = (
        DownloadTask.select(
            fn.COUNT(
                Case(
                    None, ((DownloadTask.state.in_(("ready", "failed", "excluded", "skipped")), 1),)
                )
            ),
            fn.COUNT(Case(None, ((DownloadTask.state == "failed", 1),))),
        )
        .where(DownloadTask.job == job_id)
        .tuples()
        .get()
    )
    # An inaccessible remote work is a reported state, not a failed local operation.
    metadata_failures = (
        CollectionItem.select()
        .join(CollectionRun)
        .where(
            (CollectionRun.job == job_id)
            & CollectionItem.error_code.is_null(False)
            & (CollectionItem.error_code != "unavailable")
        )
        .count()
    )
    Job.update(
        processed=processed, failed=file_failures + metadata_failures, updated_at=now()
    ).where(Job.id == job_id).execute()


def media_fetcher(ctx, job_id, owner):
    async def fetch_original(url):
        return await download(
            url, on_retry=lambda error, delay: jobs.backoff(job_id, owner, error, delay)
        )

    return ctx.get("download", fetch_original)


async def download_pending(ctx, job_id, owner, credential):
    # A page/batch shares one credential-free HTTP connection pool. Synthetic
    # adapters keep their injected fetch function and never open a real client.
    if "download" not in ctx:
        async with MediaDownloadSession(
            on_retry=lambda error, delay: jobs.backoff(job_id, owner, error, delay)
        ) as fetch:
            return await download_pending({**ctx, "download": fetch}, job_id, owner, credential)
    Job.update(phase="downloading").where(Job.id == job_id).execute()
    if (
        not DownloadTask.select()
        .where((DownloadTask.job == job_id) & (DownloadTask.state == "pending"))
        .exists()
    ):
        return

    def check():
        guard(job_id, owner, credential)

    with tempfiles.reuse_blobs(ctx["settings"], guard=check):
        await _download_pending(ctx, job_id, owner, credential)


class _AheadDownload:
    """One next file in memory while the current file is verified and committed.

    Network requests remain serial. No prefetched file is published or marked
    ready until its own normal guard, policy and space checks have passed.
    """

    def __init__(self, fetch, job_id, owner, credential):
        self.fetch = fetch
        self.job_id, self.owner, self.credential = job_id, owner, credential
        self.pending = None

    async def take(self, item):
        await self.align(item)
        pending, self.pending = self.pending, None
        if pending is not None:
            identity, url, task = pending
            if (identity, url) == (item.id, item.url):
                return await task
            # A policy/deletion change can skip the next item. Drain its request
            # before issuing another: preserve any in-flight Retry-After and do
            # not lose a terminal account/rate failure from an unused prefetch.
            await self.drain(task)
        return await interruptible(self.fetch(item.url), self.job_id, self.owner, self.credential)

    async def align(self, item):
        if self.pending is not None and self.pending[:2] != (item.id, item.url):
            pending, self.pending = self.pending, None
            await self.drain(pending[2])
        if self.pending is not None and self.pending[2].done():
            try:
                self.pending[2].result()
            except AdapterError as exc:
                if exc.code in ("auth_expired", "rate_limited"):
                    raise
            except DomainError as exc:
                if exc.code == "credential_changed":
                    raise

    def start(self, current):
        if self.pending is not None:
            return
        guard(self.job_id, self.owner, self.credential)
        item = (
            DownloadTask.select()
            .where(
                (DownloadTask.job == self.job_id)
                & (DownloadTask.state == "pending")
                & (DownloadTask.id > current.id)
            )
            .order_by(DownloadTask.id)
            .first()
        )
        if (
            item is None
            or item.asset_id is not None
            or Tombstone.get_or_none(Tombstone.artwork_id == item.artwork_id)
            or not preferences.download_allowed(item.artwork.rating)
        ):
            return
        # A previous run may have completed this source since the page was
        # planned. Let the regular path inspect it instead of downloading again.
        if (
            DownloadTask.select()
            .where(
                (DownloadTask.artwork == item.artwork_id)
                & (DownloadTask.page == item.page)
                & (DownloadTask.url == item.url)
                & DownloadTask.asset.is_null(False)
                & (DownloadTask.state == "ready")
            )
            .exists()
        ):
            return
        task = asyncio.create_task(
            interruptible(self.fetch(item.url), self.job_id, self.owner, self.credential)
        )
        self.pending = item.id, item.url, task

    @staticmethod
    async def stop(task):
        if not task.done():
            task.cancel()
        await settle(asyncio.gather(task, return_exceptions=True))

    @staticmethod
    async def drain(task):
        try:
            await task
        except AdapterError as exc:
            if exc.code in ("auth_expired", "rate_limited"):
                raise
        except DomainError as exc:
            if exc.code == "credential_changed":
                raise

    async def close(self, *, drain=False):
        pending, self.pending = self.pending, None
        if pending is not None:
            if drain:
                await self.drain(pending[2])
            else:
                await self.stop(pending[2])


async def _download_pending(ctx, job_id, owner, credential):
    fetch = media_fetcher(ctx, job_id, owner)

    def check():
        guard(job_id, owner, credential)

    ahead = _AheadDownload(fetch, job_id, owner, credential)
    completed = False
    try:
        await _download_batch(ctx, job_id, owner, credential, check, ahead)
        completed = True
    finally:
        await ahead.close(drain=completed)


async def _download_batch(ctx, job_id, owner, credential, check, ahead):
    settings = ctx["settings"]
    # No list() over the archive: bounded memory even for long collections.
    after = None
    while True:
        pending = DownloadTask.select().where(
            (DownloadTask.job == job_id) & (DownloadTask.state == "pending")
        )
        if after is not None:
            pending = pending.where(DownloadTask.id > after)
        item = pending.order_by(DownloadTask.id).first()
        if item is None:
            if after is None:
                return
            # A concurrent recovery can make an older row pending again. The
            # cursor is invocation-local; always check the full queue before exit.
            after = None
            continue
        guard(job_id, owner, credential)
        items = parallel_pages(item, job_id)
        with ExitStack() as reservation:
            while len(items) > 1:
                try:
                    reservation.enter_context(
                        storage_budget.hold(
                            settings,
                            job_id,
                            owner,
                            len(items) * (MAX_IMAGE_BYTES + storage_budget.PREVIEW_BYTES),
                            check,
                        )
                    )
                    break
                except OSError as exc:
                    if exc.errno not in (errno.ENOSPC, errno.EDQUOT):
                        raise
                    # A smaller window may fit without waiting for storage.
                    items.pop()
            if len(items) > 1:
                # Reserve the bounded batch before any request; publish serially.
                prepared = None
                try:
                    ensure_space(settings)
                    await ahead.align(items[0])
                    prepared = _PreparedDownloads(
                        await fetch_pages(items, ahead, job_id, owner, credential)
                    )
                    for page in items:
                        await _download_item(
                            ctx,
                            page,
                            job_id,
                            owner,
                            credential,
                            check,
                            prepared,
                            budgeted=True,
                            report_progress=False,
                        )
                finally:
                    if prepared is not None:
                        prepared.results.clear()
                    # States remain durable per file; publish their aggregate even
                    # when a later write, pause, or cancellation stops this chunk.
                    progress(job_id)
            else:
                try:
                    await _download_item(
                        ctx, item, job_id, owner, credential, check, ahead, report_progress=False
                    )
                finally:
                    progress(job_id)
        # Every normal item return retired this head. Other artwork rows can
        # precede later pages in a chunk, so advance only past the original head.
        after = item.id


def parallel_pages(first, job_id):
    """A bounded prefix from one work; existing/staged files use normal recovery."""
    if first.frames or first.asset_id is not None:
        return []
    if Tombstone.get_or_none(Tombstone.artwork_id == first.artwork_id):
        return []
    if not preferences.download_allowed(first.artwork.rating):
        return []
    items = []
    for item in (
        DownloadTask.select()
        .where(
            (DownloadTask.job == job_id)
            & (DownloadTask.artwork == first.artwork_id)
            & (DownloadTask.state == "pending")
            & (DownloadTask.id >= first.id)
        )
        .order_by(DownloadTask.id)
        .limit(PARALLEL_PAGES)
    ):
        if item.frames or item.asset_id is not None:
            break
        if (
            DownloadTask.select()
            .where(
                (DownloadTask.artwork == item.artwork_id)
                & (DownloadTask.page == item.page)
                & (DownloadTask.url == item.url)
                & DownloadTask.asset.is_null(False)
                & (DownloadTask.state == "ready")
            )
            .exists()
        ):
            break
        items.append(item)
    return items


async def fetch_pages(items, ahead, job_id, owner, credential):
    """Fail fast on account/stop errors; retain ordinary per-file failures."""

    async def fetch(item):
        try:
            if item.id == items[0].id:
                return await ahead.take(item)
            return await interruptible(ahead.fetch(item.url), job_id, owner, credential)
        except AdapterError as exc:
            if exc.code in ("auth_expired", "rate_limited"):
                raise
            return exc

    tasks = [asyncio.create_task(fetch(item)) for item in items]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in tasks:
            if task in done and not task.cancelled() and task.exception() is not None:
                raise task.exception()
        return {(item.id, item.url): task.result() for item, task in zip(items, tasks, strict=True)}
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await settle(asyncio.gather(*tasks, return_exceptions=True))


class _PreparedDownloads:
    def __init__(self, results):
        self.results = results

    async def take(self, item):
        result = self.results.pop((item.id, item.url))
        if isinstance(result, AdapterError):
            raise result
        return result

    def start(self, current):
        pass


async def _download_item(
    ctx, item, job_id, owner, credential, check, ahead, *, budgeted=False, report_progress=True
):
    settings = ctx["settings"]
    guard(job_id, owner, credential)
    if Tombstone.get_or_none(Tombstone.artwork_id == item.artwork_id):
        DownloadTask.update(state="excluded").where(DownloadTask.id == item.id).execute()
        if report_progress:
            progress(job_id)
        return
    if not preferences.download_allowed(item.artwork.rating):
        skip_download(item, job_id, report_progress=report_progress)
        return
    try:
        stored = False
        # Reuse only the same observed original URL. A changed original URL creates
        # a new immutable revision and keeps the old one.
        asset = Asset.get_or_none(Asset.id == item.asset_id) if item.asset_id else None
        if asset:
            # Staged after a crash: verify the moved bytes before trusting them.
            state = await disk_operation(storage.verify_asset, settings, asset, check)
            guard(job_id, owner, credential)
            if state != "ready":
                asset = None
        else:
            previous = (
                DownloadTask.select()
                .where(
                    (DownloadTask.artwork == item.artwork_id)
                    & (DownloadTask.page == item.page)
                    & (DownloadTask.url == item.url)
                    & DownloadTask.asset.is_null(False)
                    & (DownloadTask.state == "ready")
                )
                .order_by(DownloadTask.id.desc())
                .first()
            )
            asset = previous.asset if previous else None
            if asset and asset.state == "deleted":
                asset = None
            if asset:
                state = await disk_operation(
                    storage.verify_asset, settings, asset, check, full_hash=False
                )
                guard(job_id, owner, credential)
                if state != "ready":
                    asset = None
        if not asset:
            budget = (
                nullcontext()
                if budgeted
                else storage_budget.hold(
                    settings,
                    job_id,
                    owner,
                    MAX_IMAGE_BYTES + storage_budget.PREVIEW_BYTES,
                    check,
                )
            )
            with budget:
                ensure_space(settings)
                content = await ahead.take(item)
                guard(job_id, owner, credential)
                if not preferences.download_allowed(item.artwork.rating):
                    skip_download(item, job_id, report_progress=report_progress)
                    return
                ahead.start(item)
                asset = await disk_operation(
                    save_original,
                    settings,
                    item,
                    content,
                    job_id,
                    owner,
                    credential,
                )
                stored = True
        elif item.frames:
            # Repair a missing timing file/bundle after a ZIP move interrupted by a crash.
            with storage_budget.hold(
                settings,
                job_id,
                owner,
                MAX_IMAGE_BYTES + storage_budget.PREVIEW_BYTES,
                check,
            ):
                content = await disk_operation(storage.safe_path(settings, asset.path).read_bytes)
                asset = await disk_operation(
                    save_original,
                    settings,
                    item,
                    content,
                    job_id,
                    owner,
                    credential,
                )
                stored = True
        with db.atomic("IMMEDIATE"):
            guard(job_id, owner, credential)
            if Tombstone.get_or_none(Tombstone.artwork_id == item.artwork_id):
                raise storage.excluded_error()
            DownloadTask.update(state="ready", asset=asset, error_code=None).where(
                DownloadTask.id == item.id
            ).execute()
            if not stored:
                # An unchanged verified Asset can be reused after page_count
                # changed. New stores already refresh their authoritative state.
                storage.refresh_archive(item.artwork_id)
        if not item.frames and item.page == 0:
            await make_preview(settings, item.artwork_id, job_id, owner, check)
    except AdapterError as exc:
        if exc.code in ("auth_expired", "rate_limited"):
            raise
        DownloadTask.update(state="failed", error_code=exc.code).where(
            DownloadTask.id == item.id
        ).execute()
    except DomainError as exc:
        if exc.code == "credential_changed":
            raise
        DownloadTask.update(
            state="excluded" if exc.code == "excluded" else "failed", error_code=exc.code
        ).where(DownloadTask.id == item.id).execute()
    if report_progress:
        progress(job_id)


def skip_download(item, job_id, *, report_progress=True):
    DownloadTask.update(state="skipped", error_code=None).where(
        DownloadTask.id == item.id
    ).execute()
    runs = CollectionRun.select(CollectionRun.id).where(CollectionRun.job == job_id)
    CollectionItem.update(download_skipped=True).where(
        CollectionItem.run.in_(runs) & (CollectionItem.artwork_id == item.artwork_id)
    ).execute()
    if report_progress:
        progress(job_id)


async def fetch_avatars(ctx, job_id, owner, credential, failed: set):
    """Best-effort local profile images. Failures keep any previous avatar."""
    if "download" not in ctx:
        async with MediaDownloadSession(
            on_retry=lambda error, delay: jobs.backoff(job_id, owner, error, delay)
        ) as fetch:
            return await fetch_avatars(
                {**ctx, "download": fetch}, job_id, owner, credential, failed
            )
    settings = ctx["settings"]
    fetch = media_fetcher(ctx, job_id, owner)

    def check():
        guard(job_id, owner, credential)

    with ExitStack() as workspaces:
        reused = None
        while len(failed) < MAX_AVATAR_FAILURES:
            query = Artist.select().where(
                Artist.avatar_url.is_null(False)
                & (
                    Artist.avatar_fetched_url.is_null()
                    | (Artist.avatar_fetched_url != Artist.avatar_url)
                )
            )
            if failed:
                query = query.where(Artist.id.not_in(list(failed)))
            artist = query.order_by(Artist.id).first()
            if artist is None:
                return
            check()
            url = artist.avatar_url
            try:
                with storage_budget.hold(settings, job_id, owner, storage.MAX_AVATAR_BYTES, check):
                    content = await interruptible(fetch(url), job_id, owner, credential)
                    if settings.cache_dir is not None and reused is None:
                        # Only the NAS publication workspace is borrowed by avatar
                        # stores; each avatar keeps its own local scratch lifecycle.
                        reused = workspaces.enter_context(
                            tempfiles.reuse_blobs(settings, guard=check)
                        )
                    relative, media_type = await disk_operation(
                        storage.store_avatar, settings, artist.id, content
                    )
            except AdapterError as exc:
                if exc.code in ("auth_expired", "rate_limited"):
                    raise
                failed.add(artist.id)
                continue
            except DomainError as exc:
                if exc.code == "credential_changed":
                    raise
                failed.add(artist.id)
                continue
            with db.atomic("IMMEDIATE"):
                check()
                previous = Artist.get_by_id(artist.id).avatar_path
                Artist.update(
                    avatar_path=relative, avatar_media_type=media_type, avatar_fetched_url=url
                ).where((Artist.id == artist.id) & (Artist.avatar_url == url)).execute()
            if previous and previous != relative:
                try:
                    await disk_operation(storage.avatar_file(settings, previous).unlink, True)
                except (OSError, DomainError):
                    pass


def finish_collection(job_id, owner, credential):
    with db.atomic("IMMEDIATE"):
        guard(job_id, owner, credential)
        previews.enqueue_missing(job_id, limit=100)
    progress(job_id)
    total = DownloadTask.select().where(DownloadTask.job == job_id).count()
    Job.update(total=total, phase="finished").where(Job.id == job_id).execute()
    if jobs.require_job(job_id).kind == "sync":
        jobs.compact_scan(job_id)
    result = jobs.require_job(job_id)
    if jobs.has_children(job_id):
        Job.update(phase="waiting_conversion").where(Job.id == job_id).execute()
        jobs.finish(job_id, owner, "waiting_children")
        jobs.resolve_children()
    else:
        jobs.finish(job_id, owner, "partial" if result.failed else "succeeded")


async def scan(ctx, adapter, job, pixiv_id, owner, credential):
    job_id = job.id
    refresh_before = schedule.refresh_before()
    scopes = ("public", "private") if job.scope == "all" else (job.scope,)
    avatar_failures = set()
    with db.atomic("IMMEDIATE"):
        guard(job_id, owner, credential)
        previews.enqueue_missing(job_id, limit=100)
    # Recover persisted files first, before advancing a cursor or making another page call.
    await download_pending(ctx, job_id, owner, credential)
    for scope in scopes:
        run, _ = CollectionRun.get_or_create(job=job_id, scope=scope)
        visited = set()
        while run.state != "complete":
            if run.cursor in visited:
                raise AdapterError("contract")
            visited.add(run.cursor)
            Job.update(phase=f"scanning_{scope}").where(Job.id == job_id).execute()
            listing = await interruptible(
                adapter.listing(pixiv_id, scope, run.cursor), job_id, owner, credential
            )
            if listing.complete != (listing.next_cursor is None):
                raise AdapterError("contract")
            observed = await observe(
                adapter, listing, job.mode, refresh_before, job_id, owner, credential
            )
            if listing.complete:
                await interruptible(
                    adapter.verify(pixiv_id, scope, listing.total, listing.first_bookmark_id),
                    job_id,
                    owner,
                    credential,
                )
            checkpoint(job_id, owner, credential, run.id, listing, observed)
            progress(job_id)
            await download_pending(ctx, job_id, owner, credential)
            await fetch_avatars(ctx, job_id, owner, credential, avatar_failures)
            run = CollectionRun.get_by_id(run.id)
    with db.atomic("IMMEDIATE"):
        guard(job_id, owner, credential)
        finish_collection(job_id, owner, credential)


async def restore(ctx, adapter, job, owner, credential):
    """Explicit un-exclusion: refetch one artwork and download its current originals."""
    job_id, artwork_id = job.id, job.scope
    if job.kind == "repair":
        await repair.inspect(ctx["settings"], artwork_id, lambda: guard(job_id, owner, credential))
    await download_pending(ctx, job_id, owner, credential)
    if not DownloadTask.select().where(DownloadTask.job == job_id).exists():
        Job.update(phase="fetching").where(Job.id == job_id).execute()
        await artwork_slot(job_id, owner, credential)
        remote = await interruptible(adapter.artwork(artwork_id), job_id, owner, credential)
        if remote.id != artwork_id:
            raise AdapterError("contract")
        preferences.require_download(remote.rating)
        download_plan(remote)
        with db.atomic("IMMEDIATE"):
            guard(job_id, owner, credential)
            artwork, _ = library.observe_remote(remote, None)
            if artwork is None:
                raise DomainError("excluded", "수집에서 제외한 작품입니다.", 409)
            plan_downloads(job_id, artwork.id, remote)
        await download_pending(ctx, job_id, owner, credential)
    await make_preview(
        ctx["settings"], artwork_id, job_id, owner, lambda: guard(job_id, owner, credential)
    )
    with db.atomic("IMMEDIATE"):
        guard(job_id, owner, credential)
        finish_collection(job_id, owner, credential)


async def execute(ctx, job_id, owner):
    credential = None
    try:
        cookie, ciphertext = accounts.load_credential(ctx["settings"])
        credential = _CredentialState(ciphertext)
        job = jobs.require_job(job_id)
        Job.update(phase="validating").where(Job.id == job_id).execute()
        factory = ctx.get("adapter_factory", CookiePixivAdapter)
        async with factory(cookie, interval=schedule.request_interval()) as adapter:
            adapter.on_retry = lambda error, delay: jobs.backoff(job_id, owner, error, delay)
            validated_id = None

            async def rotated(candidate: str, pixiv_id: str | None):
                guard(job_id, owner, credential)
                if pixiv_id is None or (validated_id is not None and pixiv_id != validated_id):
                    raise DomainError(
                        "account_mismatch", "기존과 같은 Pixiv 계정의 쿠키를 등록해 주세요.", 409
                    )
                # No await between CAS and updating the shared guard state: all
                # outstanding watch/disk checks advance only for our own rotation.
                credential.ciphertext = accounts.adopt_verified_cookie(
                    ctx["settings"], candidate, pixiv_id, credential.ciphertext
                )

            adapter.on_cookie = rotated
            cookie = None
            pixiv_id = await interruptible(adapter.validate(), job_id, owner, credential)
            accounts.validated(credential.ciphertext, pixiv_id)
            validated_id = pixiv_id
            if job.kind == "validate":
                with db.atomic("IMMEDIATE"):
                    guard(job_id, owner, credential)
                    Job.update(processed=1, total=1, phase="finished").where(
                        Job.id == job_id
                    ).execute()
                    jobs.finish(job_id, owner, "succeeded")
                jobs.resume_waiting_auth()
            elif job.kind in ("restore", "repair"):
                await restore(ctx, adapter, job, owner, credential)
            else:
                await scan(ctx, adapter, job, pixiv_id, owner, credential)
    except Interrupted:
        jobs.finish(job_id, owner, "succeeded")  # finish maps the durable pause/cancel request.
    except AdapterError as exc:
        if exc.code == "auth_expired":
            if credential:
                accounts.authentication_failed(credential.ciphertext)
            jobs.finish(job_id, owner, "waiting_auth", exc.code)
        else:
            jobs.finish(job_id, owner, "failed", exc.code)
    except DomainError as exc:
        state = "waiting_auth" if exc.code in AUTH_CODES else "failed"
        jobs.finish(job_id, owner, state, exc.code)
