from app.core.db import db
from app.models.entities import Artwork, Asset, Tombstone
from app.services import library, storage
from app.services.fixtures import image_bytes
from tests.test_collection import work


def test_ready_file_reuse_has_no_database_writes_and_still_observes_deletion(client, settings):
    library.ingest(work(), "public")
    asset = storage.store_image(settings, "123", 0, image_bytes(0))
    changes = db.connection().total_changes
    checks = []
    assert storage.verify_asset(settings, asset, lambda: checks.append(True)) == "ready"
    assert storage.verify_asset(settings, asset, full_hash=False) == "ready"
    assert checks and db.connection().total_changes == changes

    def deleted_during_inspection():
        Tombstone.get_or_create(artwork_id="123")

    assert storage.verify_asset(settings, asset, deleted_during_inspection) is None


def test_status_update_preserves_concurrent_local_edits(client, settings, monkeypatch):
    library.ingest(work(), "public")
    original_get = Artwork.get_by_id
    entered = False

    def read_then_edit(identity):
        nonlocal entered
        artwork = original_get(identity)
        if not entered:
            entered = True
            Artwork.update(note="keep this local note", title="newer remote title").where(
                Artwork.id == identity
            ).execute()
        return artwork

    Asset.create(
        artwork="123",
        revision="a" * 64,
        page=0,
        role="original",
        path="media/123/original",
        sha256="a" * 64,
        size=1,
        width=1,
        height=1,
        media_type="image/png",
        state="ready",
    )
    monkeypatch.setattr(Artwork, "get_by_id", read_then_edit)
    storage.refresh_archive("123")
    artwork = original_get("123")
    assert (artwork.archive_status, artwork.note, artwork.title) == (
        "complete",
        "keep this local note",
        "newer remote title",
    )
    changes = db.connection().total_changes
    storage.refresh_archive("123")
    assert db.connection().total_changes == changes
