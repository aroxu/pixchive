import pytest

from app.adapters.pixiv.contracts import RemoteArtwork, RemoteTag
from app.core.db import db
from app.core.errors import DomainError
from app.models.entities import Artist, ArtistName, Artwork, ArtworkTag, Bookmark, Tombstone
from app.services import library
from app.services.fixtures import seed


def remote(**changes):
    return RemoteArtwork(
        **(
            {
                "id": "123",
                "artist_id": "456",
                "artist_name": "以前の名前",
                "title": "夜の海 한국어",
                "kind": "illustration",
                "rating": "safe",
                "page_count": 1,
                "work_tags": [RemoteTag(name="風景", translations={"en": "landscape"})],
                "bookmark_tags": ["북마크 태그"],
            }
            | changes
        )
    )


def test_resync_preserves_local_and_missing_remote_fields(client):
    library.ingest(remote(), "public")
    library.edit_tags(["123"], "風景", "add")
    library.edit_tags(["123"], "개인 메모", "add")
    library.ingest(remote(artist_name="現在の名前", work_tags=None, bookmark_tags=None), "private")
    artwork = library.detail("123")
    assert next(t for t in artwork["tags"] if t["name"] == "風景")["sources"] == [
        "local",
        "pixiv_work",
    ]
    assert artwork["artist_name_at_collection"] == "以前の名前"
    assert ArtistName.select().count() == 2
    assert Bookmark.select().count() == 1
    assert Bookmark.get().visibility == "private"
    assert Bookmark.get().bookmarked_at is None
    library.edit_tags(["123"], "風景", "remove")
    assert ArtworkTag.select().where(ArtworkTag.source == "pixiv_work").count() == 1
    library.ingest(remote(work_tags=[]), "private")
    assert [t["name"] for t in library.detail("123")["tags"]] == ["개인 메모", "북마크 태그"]


@pytest.mark.parametrize(
    "q",
    [
        "海",
        "夜の",
        "한국",
        "landscape",
        "북마크",
        '"夜の海" 한국',
        "以前",
        "123",
        "456",
        "ＬＡＮＤＳＣＡＰＥ",
    ],
)
def test_cjk_search_and_translations(client, q):
    library.ingest(remote(), "public")
    assert library.list_artworks(q=q)["total"] == 1


def test_search_is_literal_and_terms_are_and(client):
    library.ingest(remote(), "public")
    assert library.list_artworks(q="海 없는말")["total"] == 0
    assert library.list_artworks(q="%")["total"] == 0
    with pytest.raises(DomainError):
        library.list_artworks(q='"unclosed')


def test_default_rating_filters_artworks_counts_and_media(admin, settings):
    seed(settings)
    item = Artwork.get_by_id("900000000")
    url = library.detail(item.id)["pages"][0]["url"]
    item.rating = "unknown"
    item.save()
    assert admin.get("/api/artworks").json()["total"] == 5
    assert admin.get(url).status_code == 404
    assert sum(a["artwork_count"] for a in admin.get("/api/artists").json()) == 5
    assert admin.patch("/api/settings", json={"show_sensitive": True}).status_code == 200
    assert admin.get(url).status_code == 200
    assert admin.get("/api/artworks").json()["total"] == 6


def test_tombstone_and_identity(client):
    library.ingest(remote(), "public")
    library.ingest(remote(id="124", artist_id="457"), "public")
    assert Artist.select().count() == 2
    Tombstone.create(artwork_id="123")
    assert library.ingest(remote(title="Should not overwrite"), "public") is None
    assert library.list_artworks()["total"] == 1
    assert Artwork.get_by_id("123").title == "夜の海 한국어"


@pytest.mark.parametrize("name", ["", "  ", "x" * 101, "test\x00", "\u200b"])
def test_invalid_local_tags(client, name):
    library.ingest(remote(), "public")
    with pytest.raises(DomainError):
        library.edit_tags(["123"], name, "add")


def test_bulk_edit_atomic_and_idempotent(client):
    library.ingest(remote(), "public")
    with pytest.raises(DomainError):
        library.edit_tags(["123", "999"], "test", "add")
    assert ArtworkTag.select().where(ArtworkTag.source == "local").count() == 0
    library.edit_tags(["123", "123"], "  한글  ", "add")
    library.edit_tags(["123"], "한글", "add")
    assert ArtworkTag.select().where(ArtworkTag.source == "local").count() == 1


def test_migration_and_wal(client):
    from app.migrations.runner import migrate

    migrate()
    assert db.execute_sql("PRAGMA journal_mode").fetchone() == ("wal",)
    assert db.execute_sql("PRAGMA foreign_keys").fetchone() == (1,)
    assert db.execute_sql("SELECT version FROM schema_version ORDER BY version").fetchall() == [
        (1,),
        (2,),
        (3,),
        (4,),
        (5,),
        (6,),
        (7,),
        (8,),
        (9,),
        (10,),
    ]
