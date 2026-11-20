from dataclasses import replace
from pathlib import Path

import pytest

from app.core.db import db, initialize
from app.migrations.runner import migrate
from app.models.entities import Artwork, Asset, SavedCollection, SavedCollectionArtwork, Tombstone
from app.services import backup, library, storage
from app.services import saved_collections as saved
from app.services.fixtures import image_bytes
from tests.test_library import remote


def test_stars_survive_resync_and_apply_to_search_and_neighbors(admin):
    for identity in ("123", "124", "125"):
        library.ingest(remote(id=identity, title="같은 제목"), "public")
    for identity in ("123", "125"):
        for _ in range(2):
            assert (
                admin.put(f"/api/artworks/{identity}/favorite", json={"favorite": True}).json()[
                    "favorite"
                ]
                is True
            )
    library.edit_note("123", "내 메모")
    library.ingest(remote(id="123", title="새 원격 제목"), "private")
    detail = admin.get("/api/artworks/123").json()
    assert detail["favorite"] and detail["note"] == "내 메모"
    listing = admin.get("/api/artworks?favorite=true&sort=title").json()
    assert {item["id"] for item in listing["items"]} == {"123", "125"}
    assert admin.get("/api/artworks?favorite=false").json()["total"] == 1
    assert admin.get("/api/artworks?favorite=true&q=새").json()["total"] == 1
    neighbors = admin.get("/api/artworks/125/neighbors?favorite=true&sort=title").json()
    assert (neighbors["total"], neighbors["next"]) == (2, "123")
    admin.put("/api/artworks/123/favorite", json={"favorite": False})
    assert admin.get("/api/artworks?favorite=true").json()["total"] == 1


def test_multiple_lists_rename_and_delete_keep_artworks_and_files(admin, settings):
    library.ingest(remote(), "public")
    asset = storage.store_image(settings, "123", 0, image_bytes(0))
    first = admin.post("/api/collections", json={"name": "  풍경  ", "artwork_id": "123"}).json()
    second = admin.post("/api/collections", json={"name": "다시 볼 작품"}).json()
    assert first["name"] == "풍경" and first["artwork_count"] == 1
    for _ in range(2):
        response = admin.put(f"/api/collections/{second['id']}/artworks/123")
        assert response.status_code == 200
    assert SavedCollectionArtwork.select().count() == 2
    assert admin.get("/api/artworks/123").json()["collection_count"] == 2
    assert admin.get(f"/api/artworks?collection_id={first['id']}").json()["total"] == 1
    assert (
        admin.patch(f"/api/collections/{first['id']}", json={"name": "나의 풍경"}).json()["id"]
        == first["id"]
    )
    assert admin.get("/api/collections?q=풍경").json()["items"][0]["name"] == "나의 풍경"
    admin.put("/api/artworks/123/favorite", json={"favorite": True})
    assert admin.delete(f"/api/collections/{first['id']}/artworks/123").status_code == 200
    assert admin.delete(f"/api/collections/{first['id']}/artworks/123").status_code == 200
    assert admin.delete(f"/api/collections/{second['id']}").status_code == 204
    assert admin.get("/api/artworks/123").json()["favorite"] is True
    assert admin.get("/api/artworks/123").json()["collection_count"] == 0
    assert (
        Asset.get_by_id(asset.id).state == "ready"
        and storage.safe_path(settings, asset.path).is_file()
    )


def test_names_search_and_paging_are_literal_and_duplicates_do_not_merge(admin):
    first = admin.post("/api/collections", json={"name": "ＰＬＡＹ"}).json()
    assert admin.post("/api/collections", json={"name": "play"}).status_code == 409
    admin.post("/api/collections", json={"name": "100%"})
    admin.post("/api/collections", json={"name": "한국어"})
    assert admin.get("/api/collections?q=%25").json()["total"] == 1
    assert admin.get("/api/collections?q=play").json()["items"][0]["id"] == first["id"]
    one = admin.get("/api/collections?page_size=1").json()
    two = admin.get("/api/collections?page_size=1&page=2").json()
    assert one["total"] == 3 and one["items"][0]["id"] != two["items"][0]["id"]
    assert (
        admin.patch(f"/api/collections/{first['id']}", json={"name": "한국어"}).status_code == 409
    )
    assert SavedCollection.select().count() == 3


@pytest.mark.parametrize("name", ["", "  ", "x" * 101, "bad\x00name", "\u200b"])
def test_invalid_names_are_rejected_without_creating_a_list(admin, name):
    assert admin.post("/api/collections", json={"name": name}).status_code in (400, 422)
    assert SavedCollection.select().count() == 0


def test_hidden_and_deleted_items_do_not_leak_counts_or_allow_edits(admin):
    library.ingest(remote(), "public")
    library.ingest(remote(id="124"), "public")
    item = saved.create("비공개 로컬 정리", artwork_id="123")
    saved.membership(item["id"], "124", True)
    saved.favorite("123", True)
    Artwork.update(rating="r18").where(Artwork.id == "123").execute()
    Tombstone.create(artwork_id="124")
    assert admin.get("/api/collections").json()["items"][0]["artwork_count"] == 0
    assert admin.get(f"/api/collections/{item['id']}").json()["artwork_count"] == 0
    assert admin.get(f"/api/artworks?collection_id={item['id']}&favorite=true").json()["total"] == 0
    for identity in ("123", "124"):
        assert admin.get(f"/api/artworks/{identity}/saved").status_code == 404
        assert (
            admin.put(f"/api/artworks/{identity}/favorite", json={"favorite": False}).status_code
            == 404
        )
        assert admin.delete(f"/api/collections/{item['id']}/artworks/{identity}").status_code == 404
    assert (
        admin.post("/api/collections", json={"name": "허용 안 됨", "artwork_id": "124"}).status_code
        == 404
    )
    assert SavedCollection.select().count() == 1 and SavedCollectionArtwork.select().count() == 2
    admin.patch("/api/settings", json={"show_sensitive": True})
    assert admin.get("/api/collections").json()["items"][0]["artwork_count"] == 1
    assert admin.get("/api/artworks/123").json()["favorite"] is True


def test_api_session_csrf_and_strict_favorite_input(admin):
    library.ingest(remote(), "public")
    item = saved.create("인가 확인")
    assert admin.put("/api/artworks/123/favorite", json={"favorite": "false"}).status_code == 422
    assert (
        admin.put(
            "/api/artworks/123/favorite", json={"favorite": True}, headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    assert (
        admin.put(
            f"/api/collections/{item['id']}/artworks/123", headers={"Origin": "http://other.test"}
        ).status_code
        == 403
    )
    assert not Artwork.get_by_id("123").favorite and SavedCollectionArtwork.select().count() == 0
    admin.delete("/api/session")
    assert admin.get("/api/collections").status_code == 401
    assert admin.get("/api/artworks/123/saved").status_code == 401
    assert admin.delete(f"/api/collections/{item['id']}").status_code == 401


def test_v10_upgrade_preserves_existing_local_metadata(client, settings):
    library.ingest(remote(), "public")
    library.edit_note("123", "업그레이드 전 메모")
    asset = storage.store_image(settings, "123", 0, image_bytes(0))
    db.execute_sql("DROP TABLE saved_collection_artwork")
    db.execute_sql("DROP TABLE saved_collection")
    db.execute_sql("DROP INDEX artwork_favorite")
    db.execute_sql("ALTER TABLE artwork DROP COLUMN favorite")
    db.execute_sql("DELETE FROM schema_version WHERE version=11")
    migrate()
    migrate()
    assert library.detail("123")["note"] == "업그레이드 전 메모"
    assert library.detail("123")["favorite"] is False
    assert library.detail("123")["collection_count"] == 0
    assert storage.inspect_asset(settings, asset) == "ready"
    assert db.execute_sql("SELECT max(version) FROM schema_version").fetchone()[0] == 11


def test_backup_restore_and_export_preserve_stars_memberships_and_empty_lists(
    admin, settings, tmp_path
):
    library.ingest(remote(), "public")
    storage.store_image(settings, "123", 0, image_bytes(0))
    item = saved.create("오래 간직할 목록", artwork_id="123")
    empty = saved.create("비어 있는 목록")
    saved.favorite("123", True)
    metadata = admin.get("/api/export/metadata").json()
    assert {i["id"] for i in metadata["saved_collections"]} == {item["id"], empty["id"]}
    assert metadata["artworks"][0]["favorite"] is True
    assert metadata["artworks"][0]["saved_collections"] == [
        {"id": item["id"], "name": item["name"]}
    ]
    snapshot = Path(backup.create(settings, tmp_path / "backups", media=True)["path"])
    target = replace(settings, data_dir=tmp_path / "restored")
    assert backup.restore(target, snapshot, None, full_hash=True)["missing"] == []
    initialize(target.data_dir)
    try:
        assert saved.save_state("123") == {"favorite": True, "collection_ids": [item["id"]]}
        assert saved.detail(empty["id"])["artwork_count"] == 0
        assert storage.inspect_asset(target, Asset.get()) == "ready"
    finally:
        db.close()
