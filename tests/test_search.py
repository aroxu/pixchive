from datetime import UTC, datetime

import pytest

from app.adapters.pixiv.contracts import RemoteTag
from app.models.entities import Artwork, Bookmark, Tombstone
from app.services import library
from tests.test_library import remote


def fixtures():
    for index in range(4):
        library.ingest(
            remote(
                id=str(101 + index),
                artist_id=str(201 + index % 2),
                title="同じ 제목",
                work_tags=[RemoteTag(name="海", translations={"en": "ocean"})] if index < 2 else [],
                bookmark_tags=["보관"] if index % 2 else [],
                rating="r18" if index == 3 else "safe",
                kind="manga" if index == 1 else "illustration",
                created_at=["2026-09-02T00:30:00+09:00", "2026-09-01T23:30:00Z", None, None][index],
            ),
            "private" if index % 2 else "public",
        )
    library.edit_tags(["101", "103"], "海", "add")
    library.edit_tags(["101", "102"], "추천", "add")
    Artwork.update(collected_at=int(datetime(2026, 9, 2, tzinfo=UTC).timestamp())).execute()
    Artwork.update(archive_status="partial", remote_status="unknown").where(
        Artwork.id == "102"
    ).execute()
    Bookmark.update(status="not_seen").where(Bookmark.artwork == "102").execute()


def ids(admin, **params):
    result = admin.get("/api/artworks", params=params)
    assert result.status_code == 200, result.text
    return {item["id"] for item in result.json()["items"]}


def test_tag_and_or_exclusion_source_and_counts(admin):
    fixtures()
    assert ids(admin, tag=["海", "추천"]) == {"101", "102"}
    assert ids(admin, tag=["海", "추천"], tag_source="local") == {"101"}
    assert ids(admin, tag=["추천", "보관"], tag_match="any") == {"101", "102"}
    assert ids(admin, tag="海", exclude_tag="추천") == {"103"}
    assert ids(admin, exclude_tag="海", tag_source="pixiv_work") == {"103"}
    assert ids(admin, tag="보관", tag_source="pixiv_bookmark") == {"102"}
    assert ids(admin, q="ocean", tag_source="pixiv_work") == {"101", "102"}
    assert ids(admin, q="ocean", tag_source="pixiv_bookmark") == set()
    assert admin.get("/api/artworks", params={"tag": "海"}).json()["total"] == 3
    # The same artwork/tag at two sources is still one work in counts.
    assert ids(admin, tag=["海", "海"], tag_match="all") == {"101", "102", "103"}


def test_combined_filters_and_rating_boundary_apply_to_directories(admin):
    fixtures()
    filters = {
        "kind": "manga",
        "visibility": "private",
        "archive_status": "partial",
        "remote_status": "unknown",
        "bookmark_status": "not_seen",
    }
    assert ids(admin, **filters) == {"102"}
    assert sum(x["artwork_count"] for x in admin.get("/api/artists", params=filters).json()) == 1
    assert all(x["artwork_count"] == 1 for x in admin.get("/api/tags", params=filters).json())
    assert ids(admin, rating="r18") == set()
    assert admin.get("/api/artists", params={"rating": "r18"}).json() == []
    assert admin.get("/api/tags", params={"rating": "r18"}).json() == []
    admin.patch("/api/settings", json={"show_sensitive": True})
    assert ids(admin, rating="r18") == {"104"}
    Tombstone.create(artwork_id="104")
    assert ids(admin, rating="r18") == set()


def test_dates_use_utc_inclusive_calendar_boundaries_and_nulls(admin):
    fixtures()
    assert ids(admin, created_from="2026-09-01", created_to="2026-09-01") == {"101", "102"}
    assert ids(admin, created_from="2026-09-02") == set()
    assert ids(admin, collected_to="2026-09-01") == set()
    assert ids(admin, collected_from="2026-09-02", collected_to="2026-09-02") == {
        "101",
        "102",
        "103",
    }
    items = admin.get("/api/artworks", params={"sort": "created"}).json()["items"]
    assert [x["id"] for x in items] == ["102", "101", "103"]
    # The default follows remote publication time, including UTC offsets and
    # unknown dates, rather than the archive's ingestion order.
    assert admin.get("/api/artworks").json()["items"] == items
    assert [x["id"] for x in library.list_artworks()["items"]] == ["102", "101", "103"]
    assert admin.get("/api/artworks/101/neighbors").json() == {
        "previous": "102",
        "next": "103",
        "position": 2,
        "total": 3,
    }
    collected = admin.get("/api/artworks", params={"sort": "collected"}).json()["items"]
    assert [x["id"] for x in collected] == ["103", "102", "101"]


@pytest.mark.parametrize(
    "params",
    [
        {"kind": "invalid"},
        {"tag_match": "xor"},
        {"tag_source": "remote"},
        {"created_from": "bad"},
        {"collected_from": "2026-09-02", "collected_to": "2026-09-01"},
        {"tag": ["a"] * 21},
        {"tag": "x" * 101},
        {"page": 0},
        {"page_size": 101},
    ],
)
def test_invalid_filters_are_rejected(admin, params):
    assert admin.get("/api/artworks", params=params).status_code == 422


def test_filtered_neighbors_across_pages_use_same_stable_order_and_authorization(admin):
    fixtures()
    params = {"sort": "title", "page_size": 1, "tag": "海"}
    pages = [
        admin.get("/api/artworks", params={**params, "page": n}).json()["items"][0]["id"]
        for n in range(1, 4)
    ]
    assert pages == ["103", "102", "101"]
    result = admin.get("/api/artworks/102/neighbors", params=params).json()
    assert result == {"previous": "103", "next": "101", "position": 2, "total": 3}
    assert admin.get("/api/artworks/104/neighbors", params=params).status_code == 404
    result = admin.get("/api/artworks/102/neighbors", params={"kind": "illustration"}).json()
    assert result == {"previous": None, "next": None, "position": None, "total": 2}


def test_local_notes_aliases_survive_resync_and_are_authorized(admin):
    library.ingest(remote(), "public")
    note = "내 메모\n<script>literal text</script>"
    assert admin.patch("/api/artworks/123", json={"note": note}).json()["note"] == note
    assert (
        admin.patch("/api/artists/456", json={"alias": "  나의 작가  "}).json()["alias"]
        == "나의 작가"
    )
    library.ingest(remote(artist_name="new name", work_tags=[]), "private")
    detail = admin.get("/api/artworks/123").json()
    assert detail["note"] == note and detail["artist_alias"] == "나의 작가"
    assert ids(admin, q="나의") == {"123"}
    assert admin.patch("/api/artworks/123", json={"note": ""}).json()["note"] == ""
    assert admin.patch("/api/artists/456", json={"alias": ""}).json()["alias"] == ""
    assert admin.patch("/api/artists/456", json={"alias": "bad\x00alias"}).status_code == 400
    Artwork.update(rating="unknown").execute()
    assert admin.patch("/api/artworks/123", json={"note": "hidden"}).status_code == 404
    assert admin.patch("/api/artists/456", json={"alias": "hidden"}).status_code == 404
    assert admin.patch("/api/artworks/123", json={"note": "x" * 10001}).status_code == 422
