"""DB-backed sync schedule and refresh policy. The worker is the clock; Redis is not."""

from app.core.db import db
from app.core.errors import DomainError
from app.core.time import now
from app.models.entities import Credential, PixivAccount
from app.services import jobs
from app.services.library import get_setting, set_setting

DAY = 86400
DEFAULTS = {
    "schedule_enabled": "false",
    "schedule_interval_hours": "6",
    "schedule_scope": "all",
    "full_refresh_days": "7",
    "metadata_refresh_days": "30",
    "request_interval_ms": "1000",
}
FIELDS = {
    "enabled": "schedule_enabled",
    "interval_hours": "schedule_interval_hours",
    "scope": "schedule_scope",
    "full_refresh_days": "full_refresh_days",
    "metadata_refresh_days": "metadata_refresh_days",
    "request_interval_ms": "request_interval_ms",
}
FULL_REFRESH_KEYS = {
    "public": "last_public_full_sync_at",
    "private": "last_private_full_sync_at",
}


def number(key: str) -> int:
    try:
        return int(get_setting(key, DEFAULTS[key]))
    except ValueError:
        return int(DEFAULTS[key])


def timestamp(key: str) -> int | None:
    value = get_setting(key, "")
    return int(value) if value.isdigit() else None


def enabled() -> bool:
    return get_setting("schedule_enabled", DEFAULTS["schedule_enabled"]) == "true"


def full_refreshes() -> dict[str, int | None]:
    # A legacy shared timestamp does not prove which visibility was refreshed.
    return {scope: timestamp(key) for scope, key in FULL_REFRESH_KEYS.items()}


def last_full_refresh(scope: str | None = None) -> int | None:
    scope = scope or get_setting("schedule_scope", DEFAULTS["schedule_scope"])
    refreshed = full_refreshes()
    selected = tuple(FULL_REFRESH_KEYS) if scope == "all" else (scope,)
    times = [refreshed.get(item) for item in selected]
    if any(value is None for value in times):
        return None
    return min(times)


def record_full_refresh(scope: str):
    """Called inside the final metadata checkpoint, before originals/children finish."""
    set_setting(FULL_REFRESH_KEYS[scope], str(now()))
    combined = last_full_refresh("all")
    if combined is not None:
        set_setting("last_full_sync_at", str(combined))


def full_due(at: int | None = None, *, scope: str | None = None) -> bool:
    days = number("full_refresh_days")
    if days <= 0:
        return False
    last = last_full_refresh(scope)
    return last is None or (now() if at is None else at) - last >= days * DAY


def refresh_before() -> int:
    """Artwork details fetched before this time are refreshed by a normal sync."""
    return now() - number("metadata_refresh_days") * DAY


def request_interval() -> float:
    return number("request_interval_ms") / 1000


def view() -> dict:
    active = enabled()
    scope = get_setting("schedule_scope", DEFAULTS["schedule_scope"])
    return {
        "enabled": active,
        "interval_hours": number("schedule_interval_hours"),
        "scope": scope,
        "full_refresh_days": number("full_refresh_days"),
        "metadata_refresh_days": number("metadata_refresh_days"),
        "request_interval_ms": number("request_interval_ms"),
        "next_run_at": timestamp("schedule_next_at") if active else None,
        "next_mode": "full" if full_due(timestamp("schedule_next_at"), scope=scope) else "normal",
        "last_run_at": timestamp("schedule_last_at"),
        "last_result": get_setting("schedule_last_result", "") or None,
        "last_job_id": get_setting("schedule_last_job", "") or None,
        "last_full_sync_at": last_full_refresh(scope),
        "full_refreshes": full_refreshes(),
    }


def update(changes: dict) -> dict:
    with db.atomic("IMMEDIATE"):
        was_enabled = enabled()
        for field, value in changes.items():
            if value is None or field not in FIELDS:
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            set_setting(FIELDS[field], str(value))
        if not enabled():
            set_setting("schedule_next_at", "")
        elif not was_enabled or changes.get("interval_hours") is not None:
            interval = number("schedule_interval_hours") * 3600
            base = (timestamp("schedule_last_at") if was_enabled else None) or now()
            set_setting("schedule_next_at", str(max(now(), base + interval)))
    return view()


def tick() -> str | None:
    """Enqueue at most one scheduled sync per due slot; overlapping runs are merged."""
    if not enabled():
        return None
    with db.atomic("IMMEDIATE"):
        if not enabled():
            return None
        current = now()
        interval = number("schedule_interval_hours") * 3600
        due = timestamp("schedule_next_at")
        if due is None:
            set_setting("schedule_next_at", str(current + interval))
            return None
        if current < due:
            return None
        # Skip missed slots after downtime instead of replaying a backlog.
        set_setting("schedule_next_at", str(current + interval))
        set_setting("schedule_last_at", str(current))
        job_id = None
        scope = get_setting("schedule_scope", DEFAULTS["schedule_scope"])
        mode = "full" if full_due(current, scope=scope) else "normal"
        credential = Credential.get_or_none(Credential.account == 1)
        account = PixivAccount.get_or_none(PixivAccount.id == 1)
        if not credential:
            result = "credential_missing"
        elif credential.kind != "pixiv_cookie":
            result = "cookie_required"
        elif not account or account.status != "connected":
            result = account.status if account and account.status else "validation_required"
        elif active := jobs.active_sync(scope, mode):
            result, job_id = "merged", active.id
        else:
            try:
                job = jobs.enqueue_account(
                    "sync",
                    f"schedule:{due}",
                    scope,
                    mode=mode,
                    origin="schedule",
                )
                result, job_id = "queued", job.id
            except DomainError as exc:
                result = exc.code
        set_setting("schedule_last_result", result)
        set_setting("schedule_last_job", job_id or "")
        return result
