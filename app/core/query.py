from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

TagName = Annotated[str, Field(min_length=1, max_length=100)]
ArtworkSort = Literal["collected", "created", "title", "artist"]
DEFAULT_ARTWORK_SORT: ArtworkSort = "created"


class PageQuery(BaseModel):
    page: int = Field(default=1, ge=1, le=1_000_000)
    page_size: int = Field(default=20, ge=1, le=100)


class DirectoryQuery(PageQuery):
    q: str = Field(default="", max_length=100)
    sort: Literal["name", "count"] = "name"


class TagDirectoryQuery(DirectoryQuery):
    source: Literal["pixiv_work", "pixiv_bookmark", "local"] | None = None


class FileQuery(PageQuery):
    role: Literal["original", "ugoira_zip", "ugoira_timing", "thumbnail", "video"] | None = None


class JobQuery(PageQuery):
    kind: (
        Literal[
            "sync", "validate", "convert", "preview", "delete", "restore", "repair", "integrity"
        ]
        | None
    ) = None
    state: (
        Literal[
            "queued",
            "running",
            "pausing",
            "paused",
            "cancelling",
            "cancelled",
            "succeeded",
            "partial",
            "failed",
            "waiting_auth",
            "waiting_storage",
            "waiting_children",
        ]
        | None
    ) = None
    origin: Literal["manual", "schedule"] | None = None


class ArtworkQuery(BaseModel):
    q: str = Field(default="", max_length=300)
    artist: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")
    tag_id: int | None = Field(default=None, ge=1, le=9_223_372_036_854_775_807)
    tag: list[TagName] = Field(default_factory=list, max_length=20)
    exclude_tag: list[TagName] = Field(default_factory=list, max_length=20)
    tag_match: Literal["all", "any"] = "all"
    tag_source: Literal["pixiv_work", "pixiv_bookmark", "local"] | None = None
    kind: Literal["illustration", "manga", "ugoira"] | None = None
    rating: Literal["safe", "r18", "r18g", "unknown"] | None = None
    visibility: Literal["public", "private"] | None = None
    archive_status: Literal["pending", "partial", "complete"] | None = None
    remote_status: Literal["available", "unknown"] | None = None
    bookmark_status: Literal["seen", "not_seen"] | None = None
    created_from: date | None = None
    created_to: date | None = None
    collected_from: date | None = None
    collected_to: date | None = None
    sort: ArtworkSort = DEFAULT_ARTWORK_SORT
    page: int = Field(default=1, ge=1, le=1_000_000)
    page_size: int = Field(default=24, ge=1, le=100)

    @model_validator(mode="after")
    def valid_dates(self):
        for prefix in ("created", "collected"):
            start, end = getattr(self, f"{prefix}_from"), getattr(self, f"{prefix}_to")
            if start and end and start > end:
                raise ValueError("Invalid date range")
        return self
