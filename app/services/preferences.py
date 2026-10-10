"""Separate archive download choices from library visibility."""

import json

from app.core.db import db
from app.models.entities import Setting

RATINGS = ("safe", "r18", "r18g", "unknown")


def download_ratings() -> list[str]:
    stored = Setting.get_or_none(Setting.key == "download_ratings")
    if stored is None:
        # Preserve existing installations' behavior; visibility still defaults to safe.
        return list(RATINGS)
    try:
        values = json.loads(stored.value)
    except ValueError:
        return []
    if not isinstance(values, list):
        return []
    return [rating for rating in RATINGS if rating in values]


def download_allowed(rating: str) -> bool:
    return rating in download_ratings()


def require_download(rating: str):
    from app.core.errors import DomainError

    if not download_allowed(rating):
        raise DomainError(
            "download_rating_excluded", "설정에서 이 작품의 등급 다운로드를 허용해 주세요.", 409
        )


def conversion_concurrency() -> int:
    stored = Setting.get_or_none(Setting.key == "conversion_concurrency")
    try:
        value = int(stored.value) if stored else 1
    except ValueError:
        return 1
    return value if 1 <= value <= 2 else 1


def view() -> dict:
    visible = Setting.get_or_none(Setting.key == "show_sensitive")
    return {
        "show_sensitive": visible is not None and visible.value == "true",
        "download_ratings": download_ratings(),
        "conversion_concurrency": conversion_concurrency(),
        "live_sync_available": True,
    }


def update(values: dict) -> dict:
    with db.atomic("IMMEDIATE"):
        for key, value in values.items():
            if value is None:
                continue
            if key == "download_ratings":
                encoded = json.dumps([rating for rating in RATINGS if rating in value])
            elif key == "conversion_concurrency":
                encoded = str(value)
            elif key == "show_sensitive":
                encoded = "true" if value else "false"
            else:
                continue
            Setting.insert(key=key, value=encoded).on_conflict(
                conflict_target=[Setting.key], update={Setting.value: encoded}
            ).execute()
    return view()
