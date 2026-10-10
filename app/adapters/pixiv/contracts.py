from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

PixivId = Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]
RatingValue = (
    Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]
    | Annotated[str, Field(strict=True, max_length=64)]
    | Annotated[bool, Field(strict=True)]
    | Annotated[float, Field(strict=True, allow_inf_nan=False)]
    | None
)


class RatingEvidence(BaseModel):
    """One provided rating field, including explicit null; None evidence means absent."""

    model_config = ConfigDict(extra="forbid")
    source: Literal["pixiv_web", "gallery_dl"]
    field: Literal["xRestrict", "x_restrict"]
    value: RatingValue

    @property
    def normalized(self) -> Literal["safe", "r18", "r18g", "unknown"]:
        # bool and float compare equal to integers in Python; neither proves a safe rating.
        if type(self.value) is not int:
            return "unknown"
        return {0: "safe", 1: "r18", 2: "r18g"}.get(self.value, "unknown")


class RemoteTag(BaseModel):
    name: str
    translations: dict[str, str] = Field(default_factory=dict)


class RemotePage(BaseModel):
    index: int = Field(ge=0)
    url: str
    source: Literal["pixiv_pages_original", "pixiv_ugoira_original_src", "gallery_url"] | None = (
        None
    )


class UgoiraFrame(BaseModel):
    file: str = Field(min_length=1, max_length=100)
    delay: int = Field(ge=1, le=60000)


class RemoteArtwork(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: PixivId
    artist_id: PixivId
    artist_name: str
    artist_account: str | None = Field(default=None, strict=True, max_length=128)
    title: str
    caption: str = ""
    kind: Literal["illustration", "manga", "ugoira"]
    rating: Literal["safe", "r18", "r18g", "unknown"] = "unknown"
    rating_evidence: RatingEvidence | None = None
    created_at: str | None = None
    bookmarked_at: str | None = None
    page_count: int = Field(ge=1)
    pages: list[RemotePage] = Field(default_factory=list)
    # None means missing/unavailable, [] means a successfully fetched empty list.
    work_tags: list[RemoteTag] | None = None
    bookmark_tags: list[str] | None = None
    ugoira_frames: list[UgoiraFrame] | None = Field(default=None, max_length=5000)
    ugoira_zip_url: str | None = None
    ugoira_source: Literal["pixiv_ugoira_original_src"] | None = None

    @model_validator(mode="after")
    def apply_rating_evidence(self):
        if self.rating_evidence is not None:
            self.rating = self.rating_evidence.normalized
        return self


class BookmarkEntry(BaseModel):
    """One row of the bookmark list. Optional fields are only cheap change hints."""

    model_config = ConfigDict(extra="forbid")
    id: PixivId
    # False for masked rows: deleted, private, or otherwise inaccessible works.
    available: bool = True
    title: str | None = None
    artist_id: PixivId | None = None
    artist_name: str | None = None
    page_count: int | None = Field(default=None, ge=1)
    updated_at: str | None = None
    work_tags: list[str] | None = None
    # None means the bookmark tag field was unavailable, [] means no bookmark tags.
    bookmark_tags: list[str] | None = None
    avatar_url: str | None = None


class BookmarkListing(BaseModel):
    entries: list[BookmarkEntry]
    next_cursor: str | None
    complete: bool = False
    # Front of the list and total observed at scan start, re-checked before not_seen.
    total: int = Field(default=0, ge=0)
    first_bookmark_id: PixivId | None = None


class AdapterError(Exception):
    def __init__(
        self,
        code: Literal[
            "auth_expired",
            "rate_limited",
            "unavailable",
            "network",
            "unsupported",
            "contract",
            "access_denied",
            "collection_changed",
        ],
        retry_after: float | None = None,
    ):
        self.code, self.retry_after = code, retry_after
        super().__init__(code)


class PixivAdapter(Protocol):
    version: str

    async def validate(self) -> str: ...

    async def listing(
        self, account_id: str, scope: Literal["public", "private"], cursor: str | None = None
    ) -> BookmarkListing: ...

    async def artwork(self, artwork_id: str) -> RemoteArtwork: ...

    async def verify(
        self,
        account_id: str,
        scope: Literal["public", "private"],
        total: int,
        first_bookmark_id: str | None,
    ) -> None: ...
