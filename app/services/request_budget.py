"""A per-minute cap on Pixiv artwork lookups, shared by every account job.

Only artwork information requests count. Bookmark list pages and image downloads are
paced by the request interval alone. The window lives in the DB so it holds across
jobs and worker restarts.
"""

import json
import time

from app.core.db import db
from app.services.library import get_setting, set_setting

WINDOW = 60.0
KEY = "pixiv_artwork_lookups"
# Replaced in tests.
clock = time.time


def recent(current: float) -> list[float]:
    try:
        values = json.loads(get_setting(KEY, "[]"))
    except ValueError:
        return []
    if not isinstance(values, list):
        return []
    # A lookup "from the future" means the clock moved back; it no longer proves anything.
    return sorted(
        float(value)
        for value in values
        if isinstance(value, int | float)
        and not isinstance(value, bool)
        and current - WINDOW < value <= current
    )


def wait_seconds(lookups: list[float], limit: int, current: float) -> float:
    """Seconds until the last minute holds fewer than limit lookups."""
    if len(lookups) < limit:
        return 0.0
    return max(0.0, lookups[len(lookups) - limit] + WINDOW - current)


def reserve(limit: int) -> float:
    """Record one artwork lookup when the last minute has room, else return the wait.

    A full minute is answered from a plain read, without taking the write lock.
    """
    current = clock()
    wait = wait_seconds(recent(current), limit, current)
    if wait > 0:
        return wait
    with db.atomic("IMMEDIATE"):
        current = clock()
        lookups = recent(current)
        wait = wait_seconds(lookups, limit, current)
        if wait > 0:
            return wait
        lookups.append(round(current, 3))
        set_setting(KEY, json.dumps(lookups))
    return 0.0
