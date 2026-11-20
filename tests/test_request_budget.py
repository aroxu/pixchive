import json

import pytest

from app.models.entities import Job
from app.services import collection, jobs, library, repair, request_budget, schedule
from app.worker import run_job
from tests.test_collection import BookmarkPage, FakeAdapter, connected, context, work
from tests.test_library import remote


class Clock:
    def __init__(self, start=1_000.0):
        self.now = start

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(request_budget, "clock", fake)
    return fake


def advancing(clock, record=None, job_id=None):
    async def sleep(seconds):
        if record is not None:
            job = Job.get_by_id(job_id())
            record.append((job.phase, job.retry_at))
        clock.now += seconds

    return sleep


class Timed(FakeAdapter):
    """Records when the job looked up artworks and listed bookmarks."""

    def __init__(self, clock, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clock = clock
        self.lookups = []
        self.listings = []

    async def listing(self, *args):
        self.listings.append(self.clock.now)
        return await super().listing(*args)

    async def artwork(self, artwork_id):
        self.lookups.append((artwork_id, self.clock.now))
        return await super().artwork(artwork_id)


def test_window_allows_the_limit_then_waits_for_the_oldest_lookup(client, clock):
    assert [request_budget.reserve(2) for _ in range(2)] == [0.0, 0.0]
    clock.now += 10
    assert request_budget.reserve(2) == pytest.approx(50.0)
    clock.now += 50
    assert request_budget.reserve(2) == 0.0
    # Lowering the limit waits until enough recent lookups leave the minute.
    assert request_budget.reserve(1) == pytest.approx(60.0)
    # A timestamp from the future (clock moved back) or a damaged value proves nothing.
    library.set_setting(request_budget.KEY, json.dumps([clock.now + 3600]))
    assert request_budget.reserve(1) == 0.0
    library.set_setting(request_budget.KEY, "not json")
    assert request_budget.reserve(1) == 0.0


def test_schedule_exposes_the_artwork_limit_with_default_five(admin):
    assert admin.get("/api/schedule").json()["artworks_per_minute"] == 5
    changed = admin.patch("/api/schedule", json={"artworks_per_minute": 10})
    assert changed.status_code == 200 and changed.json()["artworks_per_minute"] == 10
    for invalid in (0, 61):
        rejected = admin.patch("/api/schedule", json={"artworks_per_minute": invalid})
        assert rejected.status_code == 422
    assert schedule.artworks_per_minute() == 10
    # A hand-edited value outside the range is clamped instead of stopping collection.
    library.set_setting("pixiv_artworks_per_minute", "0")
    assert schedule.artworks_per_minute() == 1


async def test_only_artwork_lookups_count_toward_the_minute_limit(
    client, settings, clock, monkeypatch
):
    connected(settings)
    schedule.update({"artworks_per_minute": 2})
    job = jobs.enqueue_account("sync", "limited", "public")
    waits = []
    monkeypatch.setattr(collection, "limit_sleep", advancing(clock, waits, lambda: job.id))
    pages = {
        "public": [
            BookmarkPage(items=[work("1"), work("2", count=3)], next_cursor="2"),
            BookmarkPage(items=[work("3")], next_cursor=None, complete=True),
        ]
    }
    adapter = Timed(clock, pages)
    fetched = []
    await run_job(context(settings, adapter, fetched), job.id)
    result = Job.get_by_id(job.id)
    assert result.state == "succeeded"
    # The second list page and five original downloads waited for nothing.
    assert adapter.listings == [1000.0, 1000.0]
    assert len(fetched) == 5
    assert adapter.lookups == [("1", 1000.0), ("2", 1000.0), ("3", 1060.0)]
    # While waiting the job shows the request-limit phase and the next allowed time.
    assert len(waits) == 60 and set(waits) == {("request_limit", 1060)}
    assert result.phase != "request_limit" and result.retry_at is None


async def test_pause_during_the_minute_wait_stops_without_a_lookup(
    client, settings, clock, monkeypatch
):
    connected(settings)
    schedule.update({"artworks_per_minute": 1})
    assert request_budget.reserve(1) == 0.0  # Another job used this minute.
    job = jobs.enqueue_account("sync", "paused-wait", "public")
    sleeps = []

    async def pause_now(seconds):
        sleeps.append(seconds)
        jobs.action(job.id, "pause")

    monkeypatch.setattr(collection, "limit_sleep", pause_now)
    adapter = FakeAdapter(
        {"public": [BookmarkPage(items=[work("1")], next_cursor=None, complete=True)]}
    )
    await run_job(context(settings, adapter), job.id)
    result = Job.get_by_id(job.id)
    assert result.state == "paused" and adapter.detail_calls == []
    assert result.phase == "scanning_public" and result.retry_at is None
    assert sleeps == [1.0]


async def test_artwork_repair_lookup_also_waits_for_the_limit(client, settings, clock, monkeypatch):
    connected(settings)
    library.ingest(remote(), "public")
    schedule.update({"artworks_per_minute": 1})
    assert request_budget.reserve(1) == 0.0
    monkeypatch.setattr(collection, "limit_sleep", advancing(clock))
    job = repair.request("123", "repair-limit")
    adapter = Timed(clock)
    adapter.details["123"] = work("123")
    fetched = []
    await run_job(context(settings, adapter, fetched), job.id)
    assert Job.get_by_id(job.id).state == "succeeded"
    assert adapter.lookups == [("123", 1060.0)] and len(fetched) == 1
