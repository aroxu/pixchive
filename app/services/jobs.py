import json
import math
import uuid

from peewee import SQL, fn

from app.core.db import db
from app.core.errors import DomainError
from app.core.time import now
from app.models.entities import (
    CollectionItem,
    CollectionRun,
    Credential,
    DownloadTask,
    IntegrityIssue,
    Job,
    JobDependency,
    JobEvent,
    Lease,
    Outbox,
    PixivAccount,
    Setting,
    Tombstone,
    UgoiraBundle,
)
from app.services import preferences, storage_budget

ACTIVE = ("running", "pausing", "cancelling")
LEASE_SECONDS = 30
# Jobs that own child conversion jobs and wait for them before reporting a result.
PARENTS = ("sync", "restore", "repair")
# A shared conversion can still be required by another collection or recovery job.
CHILD_DEMAND = ("queued", "running", "waiting_children")
CHILD_RETAIN = (*CHILD_DEMAND, "pausing", "paused", "waiting_auth", "waiting_storage")
# Jobs that call Pixiv and must respect the shared 429 cooldown.
PIXIV_KINDS = ("sync", "validate", "restore", "repair")
SCAN_OUTCOMES = ("added", "changed", "checked", "unchanged", "excluded", "unavailable", "error")


def scan_counts(job_ids: list[str]) -> dict[str, dict]:
    counts = {
        job_id: dict.fromkeys(("discovered", *SCAN_OUTCOMES, "download_skipped"), 0)
        for job_id in job_ids
    }
    if not job_ids:
        return counts
    live = []
    for job_id, summary in Job.select(Job.id, Job.summary).where(Job.id.in_(job_ids)).tuples():
        try:
            stored = json.loads(summary) if summary else None
        except ValueError:
            stored = None
        if isinstance(stored, dict):
            counts[job_id].update({k: int(stored.get(k, 0)) for k in counts[job_id]})
        else:
            live.append(job_id)
    if not live:
        return counts
    markers = ",".join("?" for _ in live)
    for job_id, outcome, count, skipped in db.execute_sql(
        f"""SELECT cr.job_id, ci.outcome, count(*), sum(ci.download_skipped)
        FROM collection_item ci
        JOIN collection_run cr ON cr.id = ci.run_id WHERE cr.job_id IN ({markers})
        GROUP BY cr.job_id, ci.outcome""",
        live,
    ):
        counts[job_id]["discovered"] += count
        counts[job_id]["download_skipped"] += skipped
        if outcome in SCAN_OUTCOMES:
            counts[job_id][outcome] += count
    return counts


def compact_scan(job_id: str):
    """Keep a finished scan's counts and problem rows, not one row per bookmark forever."""
    with db.atomic("IMMEDIATE"):
        summary = scan_counts([job_id])[job_id]
        Job.update(summary=json.dumps(summary)).where(Job.id == job_id).execute()
        runs = CollectionRun.select(CollectionRun.id).where(CollectionRun.job == job_id)
        CollectionItem.delete().where(
            CollectionItem.run.in_(runs) & CollectionItem.error_code.is_null()
        ).execute()


def resume_waiting_auth():
    """After a successful connection check, account jobs held for auth continue."""
    for job in Job.select().where(Job.kind.in_(PARENTS) & (Job.state == "waiting_auth")):
        try:
            action(job.id, "resume")
        except DomainError:
            pass


def serialize(job: Job, scans: dict | None = None) -> dict:
    result = {
        k: getattr(job, k)
        for k in (
            "id",
            "kind",
            "scope",
            "mode",
            "origin",
            "phase",
            "retry_at",
            "state",
            "processed",
            "total",
            "failed",
            "error_code",
            "created_at",
            "updated_at",
        )
    }
    result["artwork_id"] = None
    result["scan"] = None
    result["storage_budget"] = storage_budget.view(job.storage_budget)
    if job.kind == "sync":
        result["scan"] = (scans or scan_counts([job.id])).get(job.id)
    if job.kind == "convert":
        bundle = UgoiraBundle.get_or_none(UgoiraBundle.id == job.scope)
        result["artwork_id"] = bundle.artwork_id if bundle else None
        result["scope"] = None
    elif job.kind in ("delete", "restore", "repair", "preview"):
        result["artwork_id"], result["scope"] = job.scope, None
    return result


def list_jobs() -> list[dict]:
    items = list(Job.select().order_by(Job.created_at.desc(), SQL("rowid DESC")).limit(100))
    scans = scan_counts([job.id for job in items if job.kind == "sync"])
    return [serialize(job, scans) for job in items]


def page_jobs(query, page: int, page_size: int) -> dict:
    total = query.count()
    page = min(page, max(1, math.ceil(total / page_size)))
    items = list(query.order_by(Job.created_at.desc(), Job.id.desc()).paginate(page, page_size))
    scans = scan_counts([job.id for job in items if job.kind == "sync"])
    return {
        "items": [serialize(job, scans) for job in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def history(page: int, page_size: int, kind=None, state=None, origin=None) -> dict:
    query = Job.select()
    for field, value in ((Job.kind, kind), (Job.state, state), (Job.origin, origin)):
        if value is not None:
            query = query.where(field == value)
    return page_jobs(query, page, page_size)


def detail(job_id: str) -> dict:
    job = require_job(job_id)
    counts = dict.fromkeys(("pending", "ready", "failed", "excluded", "skipped"), 0)
    for state, count in (
        DownloadTask.select(DownloadTask.state, fn.COUNT(DownloadTask.id))
        .where(DownloadTask.job == job_id)
        .group_by(DownloadTask.state)
        .tuples()
    ):
        if state in counts:
            counts[state] = count
    return {
        **serialize(job),
        "downloads": counts if job.kind in PARENTS else None,
        "parent_count": JobDependency.select().where(JobDependency.child == job_id).count(),
        "child_count": JobDependency.select().where(JobDependency.parent == job_id).count(),
    }


def related(job_id: str, direction: str, page: int, page_size: int) -> dict:
    require_job(job_id)
    if direction == "children":
        relation, target = JobDependency.parent, JobDependency.child
    else:
        relation, target = JobDependency.child, JobDependency.parent
    query = Job.select().join(JobDependency, on=(Job.id == target)).where(relation == job_id)
    return page_jobs(query, page, page_size)


def event_history(job_id: str, page: int, page_size: int) -> dict:
    require_job(job_id)
    query = JobEvent.select().where(JobEvent.job == job_id)
    total = query.count()
    page = min(page, max(1, math.ceil(total / page_size)))
    return {
        "items": [
            {"state": item.state, "code": item.code, "created_at": item.created_at}
            for item in query.order_by(JobEvent.id.desc()).paginate(page, page_size)
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def issue_history(job_id: str, page: int, page_size: int) -> dict:
    job = require_job(job_id)
    if job.kind in ("integrity", "preview"):
        sql = """SELECT a.artwork_id, a.page, a.role, i.code, 0 AS category, i.id AS ordinal
        FROM integrity_issue i JOIN asset a ON a.id = i.asset_id WHERE i.job_id = ?"""
        params = [job_id]
    else:
        sql = """SELECT ci.artwork_id, NULL AS page, NULL AS role, ci.error_code AS code,
            0 AS category, ci.id AS ordinal
        FROM collection_item ci JOIN collection_run cr ON cr.id = ci.run_id
        WHERE cr.job_id = ? AND ci.error_code IS NOT NULL
        UNION ALL SELECT d.artwork_id, d.page, NULL, d.error_code, 1, d.id
        FROM download_task d WHERE d.job_id = ? AND d.error_code IS NOT NULL
        UNION ALL SELECT b.artwork_id, NULL, 'video',
            coalesce(j.error_code, 'conversion_pending'), 2, jd.id
        FROM job_dependency jd JOIN job j ON j.id = jd.child_id
        JOIN ugoira_bundle b ON b.id = j.scope
        WHERE jd.parent_id = ? AND j.kind = 'convert' AND j.state != 'succeeded'"""
        params = [job_id, job_id, job_id]
    total = db.execute_sql(f"SELECT count(*) FROM ({sql})", params).fetchone()[0]
    page = min(page, max(1, math.ceil(total / page_size)))
    rows = db.execute_sql(
        f"SELECT artwork_id, page, role, code FROM ({sql}) "
        "ORDER BY category, ordinal DESC LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    )
    return {
        "items": [
            dict(zip(("artwork_id", "page", "role", "code"), row, strict=True)) for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def events(job_id: str) -> list[dict]:
    require_job(job_id)
    return [
        {"state": item.state, "code": item.code, "created_at": item.created_at}
        for item in JobEvent.select()
        .where(JobEvent.job == job_id)
        .order_by(JobEvent.id.desc())
        .limit(100)
    ]


def issues(job_id: str) -> list[dict]:
    require_job(job_id)
    if require_job(job_id).kind in ("integrity", "preview"):
        return [
            {
                "artwork_id": item.asset.artwork_id,
                "page": item.asset.page,
                "role": item.asset.role,
                "code": item.code,
            }
            for item in IntegrityIssue.select()
            .where(IntegrityIssue.job == job_id)
            .order_by(IntegrityIssue.id)
            .limit(100)
        ]
    metadata = (
        CollectionItem.select()
        .join(CollectionRun)
        .where((CollectionRun.job == job_id) & CollectionItem.error_code.is_null(False))
        .order_by(CollectionItem.id.desc())
        .limit(100)
    )
    downloads = (
        DownloadTask.select()
        .where((DownloadTask.job == job_id) & DownloadTask.error_code.is_null(False))
        .order_by(DownloadTask.id.desc())
        .limit(100)
    )
    result = [
        {"artwork_id": item.artwork_id, "page": None, "code": item.error_code} for item in metadata
    ] + [
        {"artwork_id": item.artwork_id, "page": item.page, "code": item.error_code}
        for item in downloads
    ]
    children = (
        Job.select()
        .join(JobDependency, on=(Job.id == JobDependency.child))
        .where((JobDependency.parent == job_id) & (Job.state != "succeeded"))
        .limit(100)
    )
    for child in children:
        bundle = UgoiraBundle.get_or_none(UgoiraBundle.id == child.scope)
        if bundle:
            result.append(
                {
                    "artwork_id": bundle.artwork_id,
                    "page": None,
                    "code": child.error_code or "conversion_pending",
                }
            )
    return result


def require_job(job_id: str) -> Job:
    job = Job.get_or_none(Job.id == job_id)
    if not job:
        raise DomainError("not_found", "작업을 찾을 수 없습니다.", 404)
    return job


def event(job: Job, state: str, code: str | None = None):
    JobEvent.create(job=job, state=state, code=code)


def enqueue_integrity(key: str) -> Job:
    with db.atomic("IMMEDIATE"):
        existing = Job.get_or_none(
            (Job.idempotency_key == f"integrity:{key}") | (Job.active_key == "integrity")
        )
        if existing:
            return existing
        job = Job.create(
            kind="integrity",
            idempotency_key=f"integrity:{key}",
            active_key="integrity",
            lease_key="integrity",
        )
        Outbox.create(job=job)
        event(job, "queued")
        return job


def enqueue_preview(artwork_id: str, key: str) -> Job:
    with db.atomic("IMMEDIATE"):
        active = f"preview:1:{artwork_id}"
        existing = Job.get_or_none(Job.idempotency_key == f"{active}:{key}")
        if existing:
            return existing
        existing = Job.get_or_none(Job.active_key == active)
        if existing:
            if existing.state == "cancelling":
                raise DomainError("job_stopping", "이전 작업의 종료 후 다시 요청해 주세요.", 409)
            return existing
        job = Job.create(
            kind="preview",
            scope=artwork_id,
            idempotency_key=f"{active}:{key}",
            active_key=active,
            # Deletion waits for both image previews and ugoira conversion to stop.
            lease_key=f"convert:{artwork_id}",
        )
        Outbox.create(job=job)
        event(job, "queued")
        return job


def dedupe_key(job: Job) -> str:
    if job.kind == "convert":
        return f"convert:{job.scope}"
    if job.kind == "sync":
        return f"sync:1:{job.scope}:{job.mode or 'normal'}"
    return "integrity" if job.kind == "integrity" else f"{job.kind}:1:{job.scope or ''}"


def enqueue_conversion(bundle_id: str, key: str) -> Job:
    with db.atomic("IMMEDIATE"):
        bundle = UgoiraBundle.get_by_id(bundle_id)
        active = f"convert:{bundle_id}"
        if key == "auto":
            # A rescan must follow a newer manual retry, not an old failed auto job.
            current = Job.get_or_none(Job.active_key == active)
            if current:
                return current
            if bundle.state == "ready":
                completed = (
                    Job.select()
                    .where(
                        (Job.kind == "convert")
                        & (Job.scope == bundle_id)
                        & (Job.state == "succeeded")
                    )
                    .order_by(SQL("rowid DESC"))
                    .first()
                )
                if completed:
                    return completed
        existing = Job.get_or_none(
            (Job.idempotency_key == f"{active}:{key}") | (Job.active_key == active)
        )
        if existing:
            return existing
        job = Job.create(
            kind="convert",
            scope=bundle_id,
            lease_key=f"convert:{bundle.artwork_id}",
            active_key=active,
            idempotency_key=f"{active}:{key}",
        )
        Outbox.create(job=job)
        event(job, "queued")
        return job


def active_sync(scope: str, mode: str, *, exclude_id: str | None = None) -> Job | None:
    """A full request cannot be satisfied by a normal job or another visibility."""
    query = Job.select().where(
        (Job.kind == "sync")
        & (Job.scope == scope)
        & Job.active_key.is_null(False)
        & (Job.state != "cancelling")
    )
    if mode == "full":
        query = query.where(Job.mode == "full")
    else:
        query = query.where(fn.COALESCE(Job.mode, "normal").in_(("normal", "full")))
    if exclude_id is not None:
        query = query.where(Job.id != exclude_id)
    # Prefer a full job when it already covers a request for normal refresh.
    return query.order_by((Job.mode == "full").desc(), Job.created_at, Job.id).first()


def enqueue_account(
    kind: str,
    key: str,
    scope: str | None = None,
    *,
    mode: str | None = None,
    origin: str = "manual",
) -> Job:
    with db.atomic("IMMEDIATE"):
        credential = Credential.get_or_none(Credential.account == 1)
        if not credential:
            raise DomainError("credential_missing", "먼저 Pixiv 쿠키를 등록해 주세요.", 409)
        if credential.kind != "pixiv_cookie":
            raise DomainError("cookie_required", "PHPSESSID 쿠키를 새로 등록해 주세요.", 409)
        account = PixivAccount.get_by_id(1)
        if kind in PARENTS and account.status != "connected":
            raise DomainError("validation_required", "먼저 Pixiv 연결을 검증해 주세요.", 409)
        base = f"{kind}:1:{scope or ''}"
        idempotency = f"{base}:{key}"  # Preserve replay of existing request IDs.
        existing = Job.get_or_none(Job.idempotency_key == idempotency)
        if existing:
            return existing
        active_key = f"{base}:{mode or 'normal'}" if kind == "sync" else base
        existing = (
            active_sync(scope, mode or "normal")
            if kind == "sync"
            else Job.get_or_none(Job.active_key == active_key)
        )
        if existing:
            return existing
        if Job.select().where(Job.active_key == active_key).exists():
            raise DomainError("job_stopping", "이전 작업의 종료 후 다시 요청해 주세요.", 409)
        job = Job.create(
            kind=kind,
            scope=scope,
            mode=(mode or "normal") if kind == "sync" else None,
            origin=origin,
            idempotency_key=idempotency,
            active_key=active_key,
            lease_key="account:1",
        )
        Outbox.create(job=job)
        event(job, "queued")
        return job


def shared_children(job_id: str, states: tuple[str, ...]):
    children = JobDependency.select(JobDependency.child).where(JobDependency.parent == job_id)
    return (
        JobDependency.select(JobDependency.child)
        .join(Job, on=(JobDependency.parent == Job.id))
        .where(
            JobDependency.child.in_(children)
            & (JobDependency.parent != job_id)
            & Job.active_key.is_null(False)
            & Job.state.in_(states)
        )
        .distinct()
    )


def stopping_children(job_id: str) -> bool:
    shared = shared_children(job_id, CHILD_DEMAND)
    return (
        Job.select()
        .join(JobDependency, on=(Job.id == JobDependency.child))
        .where((JobDependency.parent == job_id) & Job.state.in_(ACTIVE) & Job.id.not_in(shared))
        .exists()
    )


def stop_children(job: Job, command: str):
    needed = {row[0] for row in shared_children(job.id, CHILD_DEMAND).tuples()}
    retained = (
        {row[0] for row in shared_children(job.id, CHILD_RETAIN).tuples()}
        if command == "cancel"
        else set()
    )
    for dependency in JobDependency.select().where(JobDependency.parent == job):
        child = dependency.child
        if child.id in needed:
            continue
        child_command = "pause" if child.id in retained else command
        if child_command == "pause" and child.state in ("queued", "running"):
            action(child.id, "pause")
        elif child_command == "cancel" and child.state in (
            "queued",
            "running",
            "pausing",
            "paused",
            "waiting_auth",
            "waiting_storage",
        ):
            action(child.id, "cancel")


def reactivate_children(job: Job, command: str):
    # Resolve a replacement conversion before changing the parent's dependency.
    for dependency in list(JobDependency.select().where(JobDependency.parent == job)):
        child = dependency.child
        if child.state in ("pausing", "cancelling"):
            raise DomainError("job_stopping", "이전 변환의 종료 후 다시 요청해 주세요.", 409)
        if child.state == "paused":
            target = action(child.id, "resume")
        elif command == "retry" and child.state in (
            "partial",
            "failed",
            "waiting_storage",
            "waiting_auth",
        ):
            target = action(child.id, "retry")
        elif command == "retry" and child.kind == "convert" and child.state == "cancelled":
            bundle = UgoiraBundle.get_or_none(UgoiraBundle.id == child.scope)
            if not bundle or Tombstone.get_or_none(Tombstone.artwork_id == bundle.artwork_id):
                continue
            target = enqueue_conversion(bundle.id, f"parent-retry:{job.id}:{uuid.uuid4()}")
        else:
            continue
        if target.id != child.id:
            if target.state == "paused":
                target = action(target.id, "resume")
            elif command == "retry" and target.state in ("waiting_auth", "waiting_storage"):
                target = action(target.id, "retry")
            if target.state == "cancelling":
                raise DomainError("job_stopping", "이전 변환의 종료 후 다시 요청해 주세요.", 409)
            JobDependency.get_or_create(parent=job, child=target)
            dependency.delete_instance()


def action(job_id: str, command: str) -> Job:
    with db.atomic("IMMEDIATE"):
        job = require_job(job_id)
        if job.kind == "delete" and command in ("pause", "cancel"):
            # A started deletion is completed or retried, never left half-reverted.
            raise DomainError("invalid_transition", "삭제 작업은 멈추거나 취소할 수 없습니다.", 409)
        if job.state == "waiting_children" and command in ("pause", "cancel"):
            state = "pausing" if command == "pause" else "cancelling"
        elif command == "pause" and job.state in ("queued", "running"):
            state = "pausing" if job.state == "running" else "paused"
        elif command == "cancel" and job.state in (
            "queued",
            "running",
            "pausing",
            "paused",
            "waiting_auth",
            "waiting_storage",
        ):
            state = "cancelling" if job.state in ACTIVE else "cancelled"
        elif command in ("resume", "retry") and job.state in (
            "paused",
            "failed",
            "partial",
            "waiting_storage",
            "waiting_auth",
        ):
            other = (
                active_sync(job.scope, job.mode or "normal", exclude_id=job.id)
                if job.kind == "sync"
                else Job.get_or_none(
                    (Job.active_key == dedupe_key(job))
                    & (Job.id != job.id)
                    & (Job.state != "cancelling")
                )
            )
            if other:
                return other
            if (
                Job.select()
                .where((Job.active_key == dedupe_key(job)) & (Job.id != job.id))
                .exists()
            ):
                raise DomainError("job_stopping", "이전 작업의 종료 후 다시 요청해 주세요.", 409)
            state = "queued"
            job.active_key = dedupe_key(job)
            job.error_code = None
            if command == "retry":
                if job.kind != "integrity" or job.state not in ("paused", "waiting_storage"):
                    job.processed, job.failed, job.total = 0, 0, None
                if job.kind == "sync" and job.state in ("failed", "partial"):
                    job.summary = None
                    runs = CollectionRun.select().where(CollectionRun.job == job)
                    CollectionItem.delete().where(CollectionItem.run.in_(runs)).execute()
                    for run in runs:
                        run.cursor, run.state, run.generation = None, "scanning", str(uuid.uuid4())
                        run.save()
                if job.kind in PARENTS:
                    DownloadTask.update(state="pending", error_code=None).where(
                        (DownloadTask.job == job) & (DownloadTask.state == "failed")
                    ).execute()
            if job.kind == "integrity" and job.state in ("failed", "partial"):
                job.summary = None
                job.processed, job.failed, job.total = 0, 0, None
                job.phase = "verifying"
            Outbox.update(delivered_at=None).where(Outbox.job == job).execute()
        else:
            raise DomainError("invalid_transition", "현재 상태에서는 실행할 수 없습니다.", 409)
        if job.kind in PARENTS:
            if command in ("pause", "cancel"):
                stop_children(job, command)
                if state in ("paused", "cancelled") and stopping_children(job.id):
                    state = "pausing" if command == "pause" else "cancelling"
            else:
                reactivate_children(job, command)
        job.state, job.updated_at = state, now()
        if state == "cancelled":
            job.active_key = None
        job.save()
        event(job, state)
        if job.kind in PARENTS and state in ("pausing", "cancelling") and job.owner is None:
            resolve_children()
        return require_job(job_id)


def claim(job_id: str) -> str | None:
    with db.atomic("IMMEDIATE"):
        job = require_job(job_id)
        if job.state != "queued":
            return None
        if job.kind == "convert":
            # Serialized with every claim across workers; pausing conversions still occupy a slot.
            running = (
                Job.select()
                .where(
                    (Job.kind == "convert")
                    & Job.state.in_(ACTIVE)
                    & Job.owner.is_null(False)
                    & (Job.lease_until > now())
                )
                .count()
            )
            if running >= preferences.conversion_concurrency():
                return None
        if job.kind in PIXIV_KINDS:
            cooldown = Setting.get_or_none(Setting.key == "pixiv_retry_at")
            if cooldown and int(cooldown.value) > now():
                Job.update(retry_at=int(cooldown.value)).where(Job.id == job_id).execute()
                return None
        lease = Lease.get_or_none(Lease.key == job.lease_key)
        if lease and lease.expires_at > now():
            return None
        owner = str(uuid.uuid4())
        Lease.insert(key=job.lease_key, owner=owner, expires_at=now() + LEASE_SECONDS).on_conflict(
            conflict_target=[Lease.key],
            update={
                Lease.owner: owner,
                Lease.expires_at: now() + LEASE_SECONDS,
            },
        ).execute()
        job.state, job.owner, job.heartbeat = "running", owner, now()
        job.retry_at = None
        job.lease_until, job.updated_at = now() + LEASE_SECONDS, now()
        job.save()
        event(job, "running")
        return owner


def backoff(job_id, owner, error, delay):
    if error.code != "rate_limited":
        return
    with db.atomic("IMMEDIATE"):
        if not owned(job_id, owner):
            return
        existing = Setting.get_or_none(Setting.key == "pixiv_retry_at")
        until = max(now() + math.ceil(delay), int(existing.value) if existing else 0)
        Setting.insert(key="pixiv_retry_at", value=str(until)).on_conflict(
            conflict_target=[Setting.key], update={Setting.value: str(until)}
        ).execute()
        Job.update(retry_at=until).where(Job.id == job_id).execute()


def owned(job_id: str, owner: str) -> Job | None:
    return Job.get_or_none(
        (Job.id == job_id)
        & (Job.owner == owner)
        & (Job.lease_until > now())
        & Job.state.in_(ACTIVE)
    )


def heartbeat(job_id: str, owner: str) -> bool:
    with db.atomic("IMMEDIATE"):
        job = owned(job_id, owner)
        if not job:
            return False
        Job.update(heartbeat=now(), lease_until=now() + LEASE_SECONDS).where(
            Job.id == job_id
        ).execute()
        Lease.update(expires_at=now() + LEASE_SECONDS).where(
            (Lease.key == job.lease_key) & (Lease.owner == owner)
        ).execute()
        return True


def finish(job_id: str, owner: str, state: str, code: str | None = None):
    with db.atomic("IMMEDIATE"):
        job = owned(job_id, owner)
        if not job:
            return
        if job.state == "pausing":
            state = "paused"
        elif job.state == "cancelling":
            state = "cancelled"
        if job.kind in PARENTS and state in ("paused", "cancelled") and stopping_children(job_id):
            state = "pausing" if state == "paused" else "cancelling"
        job.state, job.error_code, job.updated_at = state, code, now()
        job.owner, job.lease_until = None, None
        if state not in (
            "paused",
            "pausing",
            "cancelling",
            "waiting_auth",
            "waiting_storage",
            "waiting_children",
        ):
            job.active_key = None
        job.save()
        storage_budget.release(job_id, owner)
        Lease.delete().where((Lease.key == job.lease_key) & (Lease.owner == owner)).execute()
        event(job, state, code)


def continue_interrupted(job_id: str, owner: str):
    """The caller has joined its disk work and stopped its subprocesses before releasing."""
    with db.atomic("IMMEDIATE"):
        job = owned(job_id, owner)
        if job is None:
            return
        if job.state in ("pausing", "cancelling"):
            finish(job_id, owner, "succeeded")
            return
        job.state, job.owner, job.lease_until, job.updated_at = "queued", None, None, now()
        job.save()
        storage_budget.release(job_id, owner)
        Lease.delete().where((Lease.key == job.lease_key) & (Lease.owner == owner)).execute()
        Outbox.update(delivered_at=None).where(Outbox.job == job).execute()
        event(job, "queued", "execution_interrupted")


def has_children(job_id):
    return JobDependency.select().where(JobDependency.parent == job_id).exists()


def resolve_children():
    with db.atomic("IMMEDIATE"):
        for parent in Job.select().where(
            Job.kind.in_(PARENTS)
            & Job.owner.is_null()
            & Job.state.in_(("waiting_children", "pausing", "cancelling"))
        ):
            if parent.state in ("pausing", "cancelling"):
                if stopping_children(parent.id):
                    continue
                parent.state = "paused" if parent.state == "pausing" else "cancelled"
                if parent.state == "cancelled":
                    parent.active_key = None
                parent.updated_at = now()
                parent.save()
                event(parent, parent.state)
                continue
            children = list(
                Job.select()
                .join(JobDependency, on=(Job.id == JobDependency.child))
                .where(JobDependency.parent == parent)
            )
            if not children or any(
                child.state not in ("succeeded", "partial", "failed", "cancelled")
                for child in children
            ):
                continue
            parent.failed += sum(child.state != "succeeded" for child in children)
            parent.state = "partial" if parent.failed else "succeeded"
            parent.phase, parent.active_key, parent.updated_at = "finished", None, now()
            parent.save()
            event(parent, parent.state)


def recover_expired():
    with db.atomic("IMMEDIATE"):
        for job in Job.select().where(Job.state.in_(ACTIVE) & (Job.lease_until <= now())):
            state = {"pausing": "paused", "cancelling": "cancelled"}.get(job.state, "queued")
            if job.kind in PARENTS and job.state in ("pausing", "cancelling"):
                state = job.state  # Wait for children stopped on this parent's behalf.
            Lease.delete().where(
                (Lease.key == job.lease_key) & (Lease.owner == job.owner)
            ).execute()
            job.state, job.owner, job.lease_until, job.updated_at = state, None, None, now()
            if state == "cancelled":
                job.active_key = None
            job.save()
            Outbox.update(delivered_at=None).where(Outbox.job == job).execute()
            event(job, state, "lease_expired")
        storage_budget.cleanup()


async def dispatch(redis):
    recover_expired()
    resolve_children()
    # Periodically re-deliver queued DB jobs, including previously delivered messages.
    # A Redis flush or message expiry must not strand a persistent job.
    pending = list(
        Outbox.select()
        .join(Job)
        .where(
            (Job.state == "queued")
            & ((Outbox.delivered_at.is_null()) | (Outbox.delivered_at <= now() - 10))
        )
        .order_by(fn.COALESCE(Outbox.delivered_at, 0), Outbox.id)
        .limit(100)
    )
    for item in pending:
        # An orphaned Redis execution key must not strand a recovered DB job. Each
        # delivery is expendable; claim/lease and checkpoints guard the stable DB job ID.
        await redis.enqueue_job(
            "run_job", item.job_id, _job_id=f"pixchive:{item.job_id}:{uuid.uuid4()}"
        )
        Outbox.update(delivered_at=now(), attempts=Outbox.attempts + 1).where(
            Outbox.id == item.id
        ).execute()
