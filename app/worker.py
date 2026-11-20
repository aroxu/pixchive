import asyncio
import errno
import logging
import threading

from arq.connections import RedisSettings

from app.core.async_cleanup import settle
from app.core.config import Settings
from app.core.data_state import data_access
from app.core.db import db, initialize
from app.core.redaction import install as install_redaction
from app.core.time import now
from app.migrations.runner import migrate
from app.services import (
    browser_auth,
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
    # arq copies its context per delivery; keep the registry object shared.
    ctx["job_heartbeats"] = set()
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
        if settings.browser_url:
            ctx["browser_auth_task"] = asyncio.create_task(browser_auth.loop(ctx))
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
        tasks = [
            ctx[key]
            for key in ("dispatcher_task", "temp_cleanup_task", "browser_auth_task")
            if key in ctx
        ]
        for task in tasks:
            task.cancel()
        for result in await settle(asyncio.gather(*tasks, return_exceptions=True)):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                logger.warning("worker_maintenance_shutdown_failed")
    finally:
        heartbeats = tuple(ctx.get("job_heartbeats", ()))
        try:
            await stop_heartbeats(heartbeats)
        finally:
            ctx.get("job_heartbeats", set()).difference_update(heartbeats)
            if not db.is_closed():
                db.close()
            if access := ctx.pop("data_access", None):
                access.__exit__(None, None, None)


class JobHeartbeat:
    """Renew a live owner's fences even while the event loop is blocked on disk."""

    def __init__(self, job_id, owner):
        self.job_id, self.owner = job_id, owner
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="pixchive-job-heartbeat")

    def start(self):
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                # Peewee connections are thread-local. Release this thread's
                # connection after each renewal, including failed transactions.
                with db.connection_context():
                    if not jobs.heartbeat(self.job_id, self.owner):
                        return
            except Exception:
                # A busy/transient NAS failure is retried; the ownership check on
                # the next pass still rejects a lease that has since expired.
                logger.warning("worker_job_heartbeat_retry")
            if self.stop.wait(5):
                return

    def join(self):
        # Starting a thread can fail before it has an identity.
        if self.thread.ident is not None:
            self.thread.join()


async def stop_heartbeats(heartbeats):
    for heartbeat in heartbeats:
        heartbeat.stop.set()
    if heartbeats:
        # Joining on a separate thread keeps shutdown responsive, but cancellation
        # must not release the DB/data-access lock before the last renewal exits.
        await settle(asyncio.to_thread(join_heartbeats, heartbeats))


def join_heartbeats(heartbeats):
    for heartbeat in heartbeats:
        heartbeat.join()


async def run_job(ctx, job_id: str):
    owner = jobs.claim(job_id)
    if not owner:
        return
    heartbeat = JobHeartbeat(job_id, owner)
    heartbeats = ctx.setdefault("job_heartbeats", set())
    heartbeats.add(heartbeat)
    try:
        # Start synchronously after claiming, before any service can block the
        # event loop; renewal itself uses the thread's independent DB connection.
        heartbeat.start()
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
        try:
            await stop_heartbeats((heartbeat,))
        finally:
            heartbeats.discard(heartbeat)
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
