import uuid

from peewee import (
    BooleanField,
    ForeignKeyField,
    IntegerField,
    Model,
    TextField,
)

from app.core.db import db
from app.core.time import now


def uid() -> str:
    return str(uuid.uuid4())


class Base(Model):
    class Meta:
        database = db
        legacy_table_names = False


class AdminUser(Base):
    id = IntegerField(primary_key=True, default=1)
    username = TextField(unique=True)
    password_hash = TextField()


class Session(Base):
    id = TextField(primary_key=True)
    admin = ForeignKeyField(AdminUser)
    csrf = TextField()
    expires_at = IntegerField()


class LoginAttempt(Base):
    id = TextField(primary_key=True)
    count = IntegerField(default=0)
    window_start = IntegerField()


class PixivAccount(Base):
    id = IntegerField(primary_key=True, default=1)
    pixiv_id = TextField(null=True, unique=True)
    status = TextField(default="unverified")
    validated_at = IntegerField(null=True)


class Credential(Base):
    account = ForeignKeyField(PixivAccount, primary_key=True)
    ciphertext = TextField()
    kind = TextField(default="pixiv_cookie")


class Artist(Base):
    id = TextField(primary_key=True)
    name = TextField()
    account = TextField(null=True)
    alias = TextField(default="")
    avatar_url = TextField(null=True)
    avatar_fetched_url = TextField(null=True)
    avatar_path = TextField(null=True)
    avatar_media_type = TextField(null=True)


class ArtistName(Base):
    artist = ForeignKeyField(Artist, backref="names")
    name = TextField()
    first_seen = IntegerField(null=True)
    last_seen = IntegerField(null=True)

    class Meta:
        indexes = ((("artist", "name"), True),)


class Artwork(Base):
    id = TextField(primary_key=True)
    artist = ForeignKeyField(Artist, backref="artworks")
    artist_name_at_collection = TextField()
    title = TextField()
    caption = TextField(default="")
    kind = TextField(default="illustration")
    rating = TextField(default="unknown")
    rating_evidence = TextField(null=True)
    page_count = IntegerField()
    created_at = TextField(null=True)
    collected_at = IntegerField(default=now)
    archive_status = TextField(default="pending")
    remote_status = TextField(default="unknown")
    note = TextField(default="")
    is_fixture = BooleanField(default=False)
    metadata_fetched_at = IntegerField(null=True)
    listing_fingerprint = TextField(null=True)


class ArtworkSnapshot(Base):
    artwork = ForeignKeyField(Artwork, backref="snapshots")
    revision = TextField()
    metadata = TextField()
    fetched_at = IntegerField(default=now)

    class Meta:
        indexes = ((("artwork", "revision"), True),)


class Bookmark(Base):
    account = ForeignKeyField(PixivAccount)
    artwork = ForeignKeyField(Artwork)
    visibility = TextField()
    bookmarked_at = TextField(null=True)
    first_seen = IntegerField(default=now)
    last_seen = IntegerField(default=now)
    generation = TextField(null=True)
    status = TextField(default="seen")

    class Meta:
        indexes = ((("account", "artwork"), True),)


class Tag(Base):
    name = TextField(unique=True)
    search_key = TextField(index=True)
    local_created = BooleanField(default=False)


class TagTranslation(Base):
    tag = ForeignKeyField(Tag, backref="translations")
    language = TextField()
    name = TextField()
    source = TextField(default="pixiv")

    class Meta:
        indexes = ((("tag", "language", "source"), True),)


class ArtworkTag(Base):
    artwork = ForeignKeyField(Artwork, backref="tags")
    tag = ForeignKeyField(Tag)
    source = TextField()

    class Meta:
        indexes = ((("artwork", "tag", "source"), True),)


class BookmarkTag(Base):
    bookmark = ForeignKeyField(Bookmark, backref="tags")
    tag = ForeignKeyField(Tag)

    class Meta:
        indexes = ((("bookmark", "tag"), True),)


class Asset(Base):
    id = TextField(primary_key=True, default=uid)
    artwork = ForeignKeyField(Artwork, backref="assets")
    revision = TextField()
    page = IntegerField(default=0)
    role = TextField(default="original")
    path = TextField(unique=True)
    sha256 = TextField()
    size = IntegerField()
    width = IntegerField()
    height = IntegerField()
    media_type = TextField()
    state = TextField(default="staged")
    provenance = TextField(null=True)

    thumbnail = ForeignKeyField("self", null=True, backref="original_sources", on_delete="SET NULL")

    class Meta:
        indexes = ((("artwork", "revision", "page", "role"), True),)


class SyncRun(Base):
    account = ForeignKeyField(PixivAccount)
    scope = TextField()
    generation = TextField(default=uid)
    cursor = TextField(null=True)
    state = TextField(default="partial")


class Job(Base):
    id = TextField(primary_key=True, default=uid)
    kind = TextField()
    scope = TextField(null=True)
    mode = TextField(null=True)
    origin = TextField(default="manual")
    # Finished sync counts or a versioned checkpoint for a resumable archive verification.
    summary = TextField(null=True)
    storage_budget = TextField(null=True)
    phase = TextField(default="queued")
    retry_at = IntegerField(null=True)
    state = TextField(default="queued", index=True)
    idempotency_key = TextField(unique=True)
    active_key = TextField(null=True, unique=True)
    lease_key = TextField()
    owner = TextField(null=True)
    heartbeat = IntegerField(null=True)
    lease_until = IntegerField(null=True)
    processed = IntegerField(default=0)
    total = IntegerField(null=True)
    failed = IntegerField(default=0)
    error_code = TextField(null=True)
    created_at = IntegerField(default=now)
    updated_at = IntegerField(default=now)


class StorageReservation(Base):
    job = ForeignKeyField(Job, primary_key=True, on_delete="CASCADE")
    owner = TextField()
    bytes = IntegerField()


class JobEvent(Base):
    job = ForeignKeyField(Job, backref="events")
    state = TextField()
    code = TextField(null=True)
    created_at = IntegerField(default=now)


class Outbox(Base):
    job = ForeignKeyField(Job, unique=True)
    delivered_at = IntegerField(null=True)
    attempts = IntegerField(default=0)


class Lease(Base):
    key = TextField(primary_key=True)
    owner = TextField()
    expires_at = IntegerField()


class Tombstone(Base):
    artwork_id = TextField(primary_key=True)
    created_at = IntegerField(default=now)
    state = TextField(default="deleted")
    job_id = TextField(null=True)
    file_count = IntegerField(default=0)
    freed_bytes = IntegerField(default=0)


class Setting(Base):
    key = TextField(primary_key=True)
    value = TextField()


class CollectionRun(Base):
    job = ForeignKeyField(Job)
    scope = TextField()
    generation = TextField(default=uid)
    cursor = TextField(null=True)
    state = TextField(default="scanning")

    class Meta:
        indexes = ((("job", "scope"), True),)


class CollectionItem(Base):
    run = ForeignKeyField(CollectionRun)
    artwork_id = TextField()
    error_code = TextField(null=True)
    outcome = TextField(null=True)
    download_skipped = BooleanField(default=False)

    class Meta:
        indexes = ((("run", "artwork_id"), True),)


class DownloadTask(Base):
    job = ForeignKeyField(Job)
    artwork = ForeignKeyField(Artwork)
    page = IntegerField()
    url = TextField()
    provenance = TextField(null=True)
    frames = TextField(null=True)
    asset = ForeignKeyField(Asset, null=True)
    state = TextField(default="pending")
    error_code = TextField(null=True)

    class Meta:
        indexes = ((("job", "artwork", "page"), True),)


class UgoiraBundle(Base):
    id = TextField(primary_key=True, default=uid)
    artwork = ForeignKeyField(Artwork)
    revision = TextField()
    archive = ForeignKeyField(Asset, backref="ugoira_archives")
    timing = ForeignKeyField(Asset, backref="ugoira_timings")
    thumbnail = ForeignKeyField(Asset, null=True, backref="ugoira_thumbnails")
    video = ForeignKeyField(Asset, null=True, backref="ugoira_videos")
    state = TextField(default="pending")
    error_code = TextField(null=True)

    class Meta:
        indexes = ((("artwork", "revision"), True),)


class JobDependency(Base):
    parent = ForeignKeyField(Job, backref="children")
    child = ForeignKeyField(Job, backref="parents")

    class Meta:
        indexes = ((("parent", "child"), True),)


class IntegrityIssue(Base):
    job = ForeignKeyField(Job)
    asset = ForeignKeyField(Asset)
    code = TextField()

    class Meta:
        indexes = ((("job", "asset"), True),)
