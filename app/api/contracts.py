from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.adapters.pixiv.contracts import PixivId, RatingEvidence
from app.adapters.pixiv.web import COOKIE_PATTERN


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=12, max_length=256)


class Setup(Login):
    setup_token: str = Field(min_length=16, max_length=256)


class SessionStatus(BaseModel):
    setup_required: bool
    authenticated: bool
    csrf_token: str | None
    username: str | None


class CredentialInput(Input):
    phpsessid: str = Field(min_length=16, max_length=4096, pattern=f"^{COOKIE_PATTERN}$")


class Account(BaseModel):
    id: int
    pixiv_id: str | None
    status: str
    validated_at: int | None
    credential_present: bool
    credential_kind: str | None


class ArtworkSummary(BaseModel):
    id: str
    title: str
    artist_id: str
    artist_name: str
    kind: str
    rating: str
    page_count: int
    archive_status: str
    collected_at: int
    created_at: str | None
    is_fixture: bool
    thumbnail_url: str | None
    width: int | None
    height: int | None


class ArtworkList(BaseModel):
    items: list[ArtworkSummary]
    total: int
    page: int
    page_size: int


class TagTranslationView(BaseModel):
    language: str
    name: str
    source: str


class ArtworkTagView(BaseModel):
    id: int
    name: str
    sources: list[str]
    translations: list[TagTranslationView]


class ArtworkPage(BaseModel):
    index: int
    url: str
    width: int
    height: int


class FileView(BaseModel):
    id: str
    revision: str
    page: int
    role: str
    state: str
    sha256: str
    size: int
    width: int
    height: int
    media_type: str
    provenance: str | None
    url: str | None
    download_name: str


class FileList(BaseModel):
    items: list[FileView]
    total: int
    page: int
    page_size: int


class BookmarkView(BaseModel):
    visibility: str
    status: str
    first_seen: int
    last_seen: int
    bookmarked_at: str | None


class ArtworkDetail(ArtworkSummary):
    download_allowed: bool
    rating_evidence: RatingEvidence | None
    caption: str
    tags: list[ArtworkTagView]
    pages: list[ArtworkPage]
    note: str
    artist_name_at_collection: str
    artist_account: str | None
    artist_alias: str
    artist_previous_names: list[str]
    artist_avatar_url: str | None
    remote_status: str
    metadata_fetched_at: int | None
    bookmark: BookmarkView | None
    playback_url: str | None
    conversion_state: str | None
    conversion_error: str | None
    archive_url: str | None
    timing_url: str | None


class HistoryTagChange(BaseModel):
    source: Literal["pixiv_work", "pixiv_bookmark"]
    added: list[str]
    removed: list[str]


class HistoryEntry(BaseModel):
    id: int
    revision: str
    fetched_at: int
    initial: bool
    state: Literal["available", "hidden", "unreadable"]
    comparison_available: bool
    title: str
    artist_name: str
    changes: list[str]
    added_tags: list[str]
    removed_tags: list[str]
    tag_changes: list[HistoryTagChange]


class HistoryList(BaseModel):
    items: list[HistoryEntry]
    total: int
    page: int
    page_size: int


class HistoryTag(BaseModel):
    name: str
    translations: dict[str, str]


class HistorySnapshot(BaseModel):
    id: int
    revision: str
    fetched_at: int
    title: str
    artist_id: str
    artist_name: str
    artist_account: str | None
    caption: str
    kind: str
    rating: str
    rating_evidence: RatingEvidence | None
    page_count: int
    created_at: str | None
    bookmarked_at: str | None
    work_tags: list[HistoryTag] | None
    bookmark_tags: list[str] | None


class ArtworkNeighbors(BaseModel):
    previous: str | None
    next: str | None
    position: int | None
    total: int


class NoteEdit(Input):
    note: str = Field(max_length=10000)


class AliasEdit(Input):
    alias: str = Field(max_length=100)


class ArtistView(BaseModel):
    id: str
    name: str
    account: str | None
    alias: str
    artwork_count: int
    avatar_url: str | None
    previous_names: list[str]


class ArtistNameView(BaseModel):
    name: str
    first_seen: int | None
    last_seen: int | None


class ArtistList(BaseModel):
    items: list[ArtistView]
    total: int
    page: int
    page_size: int


class ArtistDetail(ArtistView):
    profile_url: str
    name_history: list[ArtistNameView]


class TagView(BaseModel):
    id: int
    name: str
    source: str
    artwork_count: int
    editable: bool
    translations: list[TagTranslationView]


class TagSourceCount(BaseModel):
    source: str
    artwork_count: int


class TagList(BaseModel):
    items: list[TagView]
    total: int
    page: int
    page_size: int


class TagRenamePreview(BaseModel):
    name: str
    merge: bool


class TagDetail(BaseModel):
    id: int
    name: str
    artwork_count: int
    source_counts: list[TagSourceCount]
    translations: list[TagTranslationView]
    editable: bool


class TagNameInput(Input):
    name: str = Field(min_length=1, max_length=100)


class TagRenameResult(BaseModel):
    tag: TagView
    previous_name: str
    merged: bool


class TagEdit(Input):
    artwork_ids: list[PixivId] = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    action: Literal["add", "remove"]


class ScanCounts(BaseModel):
    discovered: int
    added: int
    changed: int
    checked: int
    unchanged: int
    excluded: int
    unavailable: int
    error: int
    download_skipped: int


class StorageBudget(BaseModel):
    free_bytes: int
    additional_bytes: int
    reserved_bytes: int
    reserve_bytes: int
    checked_at: int


class JobView(BaseModel):
    id: str
    storage_budget: StorageBudget | None
    kind: str
    scope: str | None
    mode: str | None
    origin: str
    phase: str
    retry_at: int | None
    artwork_id: str | None
    state: str
    processed: int
    total: int | None
    failed: int
    error_code: str | None
    created_at: int
    updated_at: int
    scan: ScanCounts | None


class JobEventView(BaseModel):
    state: str
    code: str | None
    created_at: int


class JobList(BaseModel):
    items: list[JobView]
    total: int
    page: int
    page_size: int


class JobEventList(BaseModel):
    items: list[JobEventView]
    total: int
    page: int
    page_size: int


class DownloadCounts(BaseModel):
    pending: int
    ready: int
    failed: int
    excluded: int
    skipped: int


class JobDetail(JobView):
    downloads: DownloadCounts | None
    parent_count: int
    child_count: int


class JobAction(Input):
    action: Literal["pause", "resume", "cancel", "retry"]


class SyncInput(Input):
    scope: Literal["public", "private", "all"] = "all"
    # normal: list every bookmark, fetch details only when new/changed/stale; full: all details.
    mode: Literal["normal", "full"] = "normal"


class JobIssue(BaseModel):
    artwork_id: str
    page: int | None
    code: str
    role: str | None = None


class JobIssueList(BaseModel):
    items: list[JobIssue]
    total: int
    page: int
    page_size: int


class SettingsView(BaseModel):
    show_sensitive: bool
    download_ratings: list[Literal["safe", "r18", "r18g", "unknown"]]
    conversion_concurrency: int
    live_sync_available: bool


class SettingsInput(Input):
    show_sensitive: bool | None = None
    download_ratings: list[Literal["safe", "r18", "r18g", "unknown"]] | None = Field(
        default=None, max_length=4
    )
    conversion_concurrency: int | None = Field(default=None, ge=1, le=2, strict=True)

    @model_validator(mode="after")
    def nonempty(self):
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("Specify a setting to update")
        return self


class FullRefreshTimes(BaseModel):
    public: int | None
    private: int | None


class ScheduleView(BaseModel):
    enabled: bool
    interval_hours: int
    scope: Literal["public", "private", "all"]
    full_refresh_days: int
    metadata_refresh_days: int
    request_interval_ms: int
    next_run_at: int | None
    next_mode: Literal["normal", "full"]
    last_run_at: int | None
    last_result: str | None
    last_job_id: str | None
    last_full_sync_at: int | None
    full_refreshes: FullRefreshTimes


class ScheduleInput(Input):
    enabled: bool | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=168)
    scope: Literal["public", "private", "all"] | None = None
    full_refresh_days: int | None = Field(default=None, ge=0, le=90)
    metadata_refresh_days: int | None = Field(default=None, ge=1, le=365)
    request_interval_ms: int | None = Field(default=None, ge=250, le=10000)


class FileUsage(BaseModel):
    role: str
    files: int
    bytes: int


class DeletionPreview(BaseModel):
    artwork_id: str
    title: str
    file_count: int
    bytes: int
    roles: list[FileUsage]


class Exclusion(BaseModel):
    artwork_id: str
    created_at: int
    state: Literal["deleting", "deleted"]
    file_count: int
    freed_bytes: int
    title: str | None
    artist_name: str | None
    hidden: bool


class ExclusionList(BaseModel):
    items: list[Exclusion]
    total: int
    page: int
    page_size: int


class RestoreInput(Input):
    # False only removes the exclusion; the next sync downloads it if still bookmarked.
    download: bool = True


class RestoreResult(BaseModel):
    artwork_id: str
    job: JobView | None


class QueueCounts(BaseModel):
    queued: int
    running: int
    waiting: int


class SystemStatus(BaseModel):
    storage_free: int
    storage_total: int
    storage_reserve: int
    storage_reserved: int
    storage_low: bool
    database_bytes: int
    archive: list[FileUsage]
    artwork_count: int
    artist_count: int
    excluded_count: int
    queue: QueueCounts
    worker_status: Literal["online", "offline", "unknown"]
    worker_last_seen: int | None
    last_backup_at: int | None
    version: str
