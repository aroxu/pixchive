import asyncio
import contextlib
import errno
import logging

from arq.connections import RedisSettings

from app.core.async_cleanup import settle
from app.core.config import Settings
from app.core.data_state import data_access
from app.core.db import db, initialize
from app.core.redaction import install as install_redaction
from app.core.time import now
from app.migrations.runner import migrate
from app.services import (
    collection,
    integrity,
    jobs,
    library,
    previews,
    retention,
    schedule,
    storage,
    tempfiles,
    ugoira,
)
from app.services.execution import disk_operation

logger = logging.getLogger(__name__)


async def dispatch_jobs(ctx):
    library.set_setting("worker_heartbeat", str(now()))
    schedule.tick()
    await jobs.dispatch(ctx["redis"])


async def dispatcher_loop(ctx):
    # Scheduling, delivery and liveness cannot wait for a free collection/conversion slot.
    while True:
        await asyncio.sleep(5)
        try:
            async with asyncio.timeout(15):
                await dispatch_jobs(ctx)
        except Exception:
            logger.warning("worker_dispatch_retry")


async def cleanup_loop(ctx):
    while True:
        try:
            result = await disk_operation(tempfiles.reap, ctx["settings"])
            if result["failed"]:
                logger.warning("temporary_workspace_cleanup_incomplete count=%s", result["failed"])
            if result["removed"] or result["unknown"]:
                logger.info(
                    "temporary_workspace_cleanup removed=%s active=%s unknown=%s",
                    result["removed"],
                    result["active"],
                    result["unknown"],
                )
        except Exception:
            logger.warning("temporary_workspace_cleanup_retry")
        await asyncio.sleep(60)


async def startup(ctx):
    install_redaction()
    settings = Settings.from_env()
    ctx["settings"] = settings
    access = data_access(settings.data_dir)
    access.__enter__()
    ctx["data_access"] = access
    try:
        initialize(settings.data_dir)
        migrate()
        # Reconciliation hashes files outside the event loop and SQLite write transactions.
        await disk_operation(reconcile_files, settings)
        await dispatch_jobs(ctx)
        ctx["dispatcher_task"] = asyncio.create_task(dispatcher_loop(ctx))
        ctx["temp_cleanup_task"] = asyncio.create_task(cleanup_loop(ctx))
    except BaseException:
        if not db.is_closed():
            db.close()
        ctx.pop("data_access").__exit__(None, None, None)
        raise


def reconcile_files(settings):
    with db.connection_context():
        storage.reconcile(settings)


async def shutdown(ctx):
    try:
        tasks = [ctx[key] for key in ("dispatcher_task", "temp_cleanup_task") if key in ctx]
        for task in tasks:
            task.cancel()
        for result in await settle(asyncio.gather(*tasks, return_exceptions=True)):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                logger.warning("worker_maintenance_shutdown_failed")
    finally:
        if not db.is_closed():
            db.close()
        if access := ctx.pop("data_access", None):
            access.__exit__(None, None, None)


async def pulse(job_id, owner):
    while True:
        await asyncio.sleep(5)
        if not jobs.heartbeat(job_id, owner):
            return


async def run_job(ctx, job_id: str):
    owner = jobs.claim(job_id)
    if not owner:
        return
    pulse_task = asyncio.create_task(pulse(job_id, owner))
    try:
        job = jobs.require_job(job_id)
        if job.kind == "convert":
            await ugoira.execute(ctx, job_id, owner)
            return
        if job.kind == "preview":
            await previews.execute(ctx, job_id, owner)
            return
        if job.kind in ("validate", "sync", "restore", "repair"):
            await collection.execute(ctx, job_id, owner)
            return
        if job.kind == "delete":
            await retention.execute_delete(ctx, job_id, owner)
            return
        if job.kind == "integrity":
            await integrity.execute(ctx, job_id, owner)
            return
        jobs.finish(job_id, owner, "failed", "unsupported_job")
    except asyncio.CancelledError:
        # Services have stopped subprocesses and joined disk I/O before unwinding.
        # A timeout/shutdown is another delivery attempt, not the archive job's end.
        jobs.continue_interrupted(job_id, owner)
        raise
    except OSError as exc:
        full = exc.errno in (errno.ENOSPC, errno.EDQUOT)
        jobs.finish(
            job_id,
            owner,
            "waiting_storage" if full else "failed",
            "storage_full" if full else "storage_error",
        )
    except Exception:
        # Never place exception strings (possibly containing credentials/URLs) in job output.
        jobs.finish(job_id, owner, "failed", "internal_error")
        raise RuntimeError("archive_job_failed") from None
    finally:
        pulse_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pulse_task
        jobs.resolve_children()


class WorkerSettings:
    # Accept old queued dispatcher calls during upgrade; new dispatch uses its own task.
    functions = [run_job, dispatch_jobs]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(Settings.from_env().redis_url)
    # One account job + integrity + up to two conversions. DB claims enforce resource limits.
    max_jobs = 4
    # Limit one delivery, not the persistent DB job: cleanup joins before re-queueing.
    job_timeout = 3600
    keep_result = 0
    log_results = False
