"""Running jobs remain visible ahead of newer history, before pagination."""

from app.models.entities import Job


def job(identity, *, state="succeeded", created_at=100, kind="integrity", origin="manual"):
    return Job.create(
        id=identity,
        kind=kind,
        state=state,
        created_at=created_at,
        origin=origin,
        lease_key=f"test:{identity}",
        idempotency_key=f"test:{identity}",
    )


def test_running_jobs_precede_history_before_page_and_legacy_limits(admin):
    for number in range(105):
        job(f"finished-{number:03}", created_at=100 + number)
    job("old-running", state="running", created_at=1)
    job("new-running-a", state="running", created_at=2)
    job("new-running-b", state="running", created_at=2)

    first = admin.get("/api/jobs/history?page_size=2").json()
    second = admin.get("/api/jobs/history?page=2&page_size=2").json()
    assert first["total"] == second["total"] == 108
    assert [item["id"] for item in first["items"]] == ["new-running-b", "new-running-a"]
    assert [item["id"] for item in second["items"]] == ["old-running", "finished-104"]
    legacy = admin.get("/api/jobs").json()
    assert len(legacy) == 100
    assert [item["id"] for item in legacy[:3]] == [
        "new-running-b",
        "new-running-a",
        "old-running",
    ]
    Job.update(state="succeeded").where(Job.id == "old-running").execute()
    last = admin.get("/api/jobs/history?page=999&page_size=2").json()
    assert last["page"] == 54
    assert [item["id"] for item in last["items"]] == ["finished-000", "old-running"]


def test_running_priority_preserves_filters_and_other_state_chronology(admin):
    job("running-other-kind", state="running", kind="sync", created_at=200)
    job("running-scheduled", state="running", origin="schedule", created_at=200)
    job("running", state="running", created_at=1)
    for number, state in enumerate(("queued", "paused", "waiting_auth", "failed", "succeeded")):
        job(state, state=state, created_at=100 + number)

    filtered = admin.get("/api/jobs/history?kind=integrity&origin=manual").json()
    assert filtered["total"] == 6
    assert [item["id"] for item in filtered["items"]] == [
        "running",
        "succeeded",
        "failed",
        "waiting_auth",
        "paused",
        "queued",
    ]
    waiting = admin.get("/api/jobs/history?state=queued").json()
    assert waiting["total"] == 1 and waiting["items"][0]["id"] == "queued"
    empty = admin.get("/api/jobs/history?state=waiting_storage&page=9").json()
    assert empty["items"] == [] and empty["total"] == 0 and empty["page"] == 1
