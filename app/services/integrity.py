"""Archive verification: hash every kept file, then fill missing library previews."""

import asyncio
import errno
import json

from peewee import SQL, fn

from app.core.db import db
from app.core.errors import DomainError
from app.core.time import now
from app.models.entities import Artwork, Asset, IntegrityIssue, Job, Tombstone
from app.services import jobs, previews, storage, storage_budget
from app.services.execution import Interrupted, disk_operation


def checkpoint(value: str | None) -> dict | None:
    try:
        item = json.loads(value) if value else None
        if (
            not isinstance(item, dict)
            or type(item.get("verification_checkpoint")) is not int
            or item.get("verification_checkpoint") != 1
            or type(item.get("upper")) is not int
            or type(item.get("last")) is not int
            or not 0 <= item["last"] <= item["upper"]
            or item.get("phase") not in ("verifying", "thumbnails", "finished")
            or not isinstance(item.get("last_artwork"), str)
            or len(item["last_artwork"]) > 20
            or (item["last_artwork"] and not item["last_artwork"].isdigit())
        ):
            return None
        return item
    except (ValueError, TypeError):
        return None


def start(job_id, check) -> dict:
    with db.atomic("IMMEDIATE"):
        check()
        job = jobs.require_job(job_id)
        position = checkpoint(job.summary)
        if position is not None:
            return position
        position = {
            "verification_checkpoint": 1,
            "upper": Asset.select(fn.MAX(SQL("rowid"))).scalar() or 0,
            "last": 0,
            "last_artwork": "",
            "phase": "verifying",
        }
        if job.summary is not None or job.processed:
            jobs.event(job, "running", "verification_restarted")
        Job.update(
            total=None, processed=0, failed=0, phase="verifying", summary=json.dumps(position)
        ).where(Job.id == job_id).execute()
        IntegrityIssue.delete().where(IntegrityIssue.job == job_id).execute()
        return position


def advance_thumbnail(job_id, check, position, artwork_id, source=None, code=None):
    with db.atomic("IMMEDIATE"):
        check()
        if source is not None and code:
            current = Asset.get_or_none(Asset.id == source.id)
            if (
                current is not None
                and current.state != "deleted"
                and not Tombstone.select().where(Tombstone.artwork_id == artwork_id).exists()
                and not IntegrityIssue.select()
                .where((IntegrityIssue.job == job_id) & (IntegrityIssue.asset == source))
                .exists()
            ):
                IntegrityIssue.create(job=job_id, asset=current, code=code)
                Job.update(failed=Job.failed + 1).where(Job.id == job_id).execute()
        position["last_artwork"] = artwork_id
        Job.update(summary=json.dumps(position), updated_at=now()).where(Job.id == job_id).execute()


async def execute(ctx, job_id: str, owner: str):
    settings = ctx["settings"]

    def check():
        job = jobs.owned(job_id, owner)
        if not job or job.state != "running":
            raise Interrupted

    excluded = Tombstone.select(Tombstone.artwork_id)
    # Keep a bounded traversal, even while collection keeps adding assets. Deleted/excluded
    # rows can disappear during verification, so the exact processed total is known at the end.
    try:
        position = start(job_id, check)
        upper, last = position["upper"], position["last"]
        while position["phase"] == "verifying":
            check()
            batch = list(
                Asset.select(Asset.id, SQL("rowid"))
                .where(
                    (SQL("rowid") > last)
                    & (SQL("rowid") <= upper)
                    & (Asset.state != "deleted")
                    & Asset.artwork.not_in(excluded)
                )
                .order_by(SQL("rowid"))
                .limit(200)
                .tuples()
            )
            if not batch:
                break
            for asset_id, row_id in batch:
                check()
                asset = Asset.get_or_none(Asset.id == asset_id)
                state = None
                if asset is not None and asset.state != "deleted":
                    state = await disk_operation(storage.verify_asset, settings, asset, check)
                with db.atomic("IMMEDIATE"):
                    check()
                    deleting = (
                        asset is None
                        or Tombstone.select()
                        .where(Tombstone.artwork_id == asset.artwork_id)
                        .exists()
                    )
                    if state is not None and not deleting:
                        current = Asset.get_or_none(Asset.id == asset.id)
                        deleting = current is None or current.state == "deleted"
                        if not deleting and state != "ready":
                            IntegrityIssue.create(job=job_id, asset=asset, code=f"file_{state}")
                    Job.update(
                        processed=Job.processed + 1,
                        failed=Job.failed + (state not in (None, "ready") and not deleting),
                        updated_at=now(),
                    ).where(Job.id == job_id).execute()
                    position["last"] = row_id
                    Job.update(summary=json.dumps(position)).where(Job.id == job_id).execute()
                last = row_id
                await asyncio.sleep(0)
        # Stay within artworks with originals in the starting asset range.
        originals = Asset.select(Asset.artwork).where(
            (SQL("rowid") <= upper) & (Asset.role == "original") & (Asset.page == 0)
        )
        if position["phase"] == "verifying":
            with db.atomic("IMMEDIATE"):
                check()
                position["phase"] = "thumbnails"
                Job.update(
                    phase="thumbnails", total=Job.processed, summary=json.dumps(position)
                ).where(Job.id == job_id).execute()
        last = position["last_artwork"]
        while position["phase"] == "thumbnails":
            batch = [
                artwork_id
                for (artwork_id,) in Artwork.select(Artwork.id)
                .where(
                    (Artwork.id > last)
                    & (Artwork.kind != "ugoira")
                    & Artwork.id.in_(originals)
                    & Artwork.id.not_in(excluded)
                )
                .order_by(Artwork.id)
                .limit(200)
                .tuples()
            ]
            if not batch:
                break
            for artwork_id in batch:
                check()
                candidate = storage.thumbnail_source(artwork_id, upper=upper)
                source = (
                    storage.needs_thumbnail(artwork_id, source=candidate)
                    if candidate is not None
                    else None
                )
                if source is None:
                    code = (
                        f"file_{candidate.state}"
                        if candidate is not None
                        and candidate.state in ("missing", "corrupt", "unreadable")
                        else None
                    )
                    advance_thumbnail(job_id, check, position, artwork_id, candidate, code)
                    continue
                code = None
                try:
                    with storage_budget.hold(
                        settings, job_id, owner, storage_budget.PREVIEW_BYTES, check
                    ):
                        await disk_operation(previews.build, settings, artwork_id, source, check)
                except (OSError, DomainError) as exc:
                    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
                        raise
                    code = (
                        exc.code
                        if isinstance(exc, DomainError) and exc.code.startswith("file_")
                        else "preview_failed"
                    )
                advance_thumbnail(job_id, check, position, artwork_id, source, code)
            last = batch[-1]
        with db.atomic("IMMEDIATE"):
            check()
            position["phase"] = "finished"
            Job.update(phase="finished", summary=json.dumps(position)).where(
                Job.id == job_id
            ).execute()
    except Interrupted:
        jobs.finish(job_id, owner, "succeeded")  # finish maps the durable pause/cancel request.
        return
    result = jobs.require_job(job_id)
    jobs.finish(job_id, owner, "partial" if result.failed else "succeeded")
