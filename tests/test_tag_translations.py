"""Translation regressions against the temporary SQLite DB and authenticated APIs."""

import io
import json
import zipfile

import pytest

from app.adapters.pixiv.contracts import RemoteArtwork, RemoteTag
from app.models.entities import (
    Artwork,
    ArtworkSnapshot,
    ArtworkTag,
    BookmarkTag,
    Tag,
    TagTranslation,
)
from app.services import library

TRANSLATIONS = {"ko": "풍경", "en": "landscape"}
SOURCE_COUNTS = {"local": 2, "pixiv_bookmark": 2, "pixiv_work": 1}


def remote(artwork_id="101", **changes):
    # Neither the title nor artist/caption contains a translation search term.
    return RemoteArtwork(
        **(
            {
                "id": artwork_id,
                "artist_id": "201",
                "artist_name": "Synthetic artist",
                "title": "Synthetic artwork",
                "kind": "illustration",
                "rating": "safe",
                "page_count": 1,
                "work_tags": [RemoteTag(name="風景", translations=TRANSLATIONS.copy())],
                "bookmark_tags": [],
            }
            | changes
        )
    )


def translation_views(names):
    return [
        {"language": language, "name": name, "source": "pixiv"}
        for language, name in sorted(names.items())
    ]


def get_json(admin, path, **params):
    response = admin.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def add_local(admin, artwork_ids, name):
    response = admin.post(
        "/api/artworks/tags/bulk",
        json={"artwork_ids": artwork_ids, "name": name, "action": "add"},
    )
    assert response.status_code == 204, response.text


def artwork_ids(admin, **params):
    result = get_json(admin, "/api/artworks", **params)
    ids = {item["id"] for item in result["items"]}
    assert result["total"] == len(result["items"]) == len(ids)
    return ids


@pytest.fixture
def translated_archive(admin):
    library.ingest(remote(bookmark_tags=["風景"]), "public")
    library.ingest(remote("102", work_tags=[]), "public")
    library.ingest(remote("103", work_tags=[], bookmark_tags=["風景"]), "private")
    library.ingest(remote("104", work_tags=[RemoteTag(name="無関係")]), "public")
    add_local(admin, ["101", "102"], "風景")
    return Tag.get(Tag.name == "風景").id


def test_both_languages_share_original_tag_without_losing_translation_or_relation_sources(admin):
    observation = remote(bookmark_tags=["風景"])
    library.ingest(observation, "public")
    add_local(admin, ["101"], "風景")
    library.ingest(observation, "public")

    tag = Tag.get()
    assert tag.name == "風景"
    assert Tag.select().count() == 1
    rows = list(
        TagTranslation.select(TagTranslation.language, TagTranslation.name, TagTranslation.source)
        .where(TagTranslation.tag == tag)
        .order_by(TagTranslation.language)
        .tuples()
    )
    assert rows == [("en", "landscape", "pixiv"), ("ko", "풍경", "pixiv")]
    assert ArtworkTag.select().count() == 2
    assert BookmarkTag.select().count() == 1
    assert ArtworkSnapshot.select().count() == 1

    detail = get_json(admin, "/api/artworks/101")
    assert detail["tags"] == [
        {
            "id": tag.id,
            "name": "風景",
            "sources": ["local", "pixiv_bookmark", "pixiv_work"],
            "translations": translation_views(TRANSLATIONS),
        }
    ]
    profile = get_json(admin, f"/api/tags/{tag.id}")
    assert profile["translations"] == translation_views(TRANSLATIONS)
    assert profile["artwork_count"] == 1
    assert profile["source_counts"] == [
        {"source": source, "artwork_count": 1} for source in sorted(SOURCE_COUNTS)
    ]


@pytest.mark.parametrize("language,replacement", [("ko", "경치"), ("en", "scenery")])
def test_one_language_refresh_preserves_other_translation_local_tags_and_note(
    admin, language, replacement
):
    library.ingest(remote(bookmark_tags=["風景"]), "public")
    add_local(admin, ["101"], "風景")
    add_local(admin, ["101"], "내 분류")
    note = "수집 후에도 남겨 둘 메모"
    response = admin.patch("/api/artworks/101", json={"note": note})
    assert response.status_code == 200, response.text

    refreshed = remote(
        work_tags=[RemoteTag(name="風景", translations={language: replacement})],
        bookmark_tags=None,
    )
    library.ingest(refreshed, "private")
    library.ingest(refreshed, "private")
    expected = TRANSLATIONS | {language: replacement}
    assert TagTranslation.select().count() == 2
    assert {(row.language, row.name, row.source) for row in TagTranslation.select()} == {
        (key, value, "pixiv") for key, value in expected.items()
    }
    assert Artwork.get_by_id("101").note == note
    assert ArtworkSnapshot.select().count() == 2

    detail = get_json(admin, "/api/artworks/101")
    assert detail["note"] == note
    tags = {tag["name"]: tag for tag in detail["tags"]}
    assert set(tags) == {"風景", "내 분류"}
    assert tags["風景"]["sources"] == ["local", "pixiv_bookmark", "pixiv_work"]
    assert tags["風景"]["translations"] == translation_views(expected)
    assert tags["내 분류"]["sources"] == ["local"]
    assert tags["내 분류"]["translations"] == []
    for name in expected.values():
        assert artwork_ids(admin, q=name) == {"101"}
    assert artwork_ids(admin, q=TRANSLATIONS[language]) == set()
    assert artwork_ids(admin, q="내 분류", tag_source="local") == {"101"}


@pytest.mark.parametrize("work_tags", [None, [RemoteTag(name="風景")]])
def test_missing_translation_information_does_not_clear_saved_languages(admin, work_tags):
    library.ingest(remote(), "public")
    library.ingest(remote(work_tags=work_tags, bookmark_tags=None), "public")
    tag = get_json(admin, "/api/artworks/101")["tags"][0]
    assert tag["translations"] == translation_views(TRANSLATIONS)
    assert TagTranslation.select().count() == 2
    for name in TRANSLATIONS.values():
        assert artwork_ids(admin, q=name, tag_source="pixiv_work") == {"101"}


@pytest.mark.parametrize(
    "q", ["풍경", "풍", "landscape", "LANDSCAPE", "ＬＡＮＤＳＣＡＰＥ", "dsca", "풍경 landscape"]
)
def test_translation_search_follows_relation_source_and_counts_distinct_artworks(
    admin, translated_archive, q
):
    assert artwork_ids(admin, q=q) == {"101", "102", "103"}
    for source, expected in (
        ("pixiv_work", {"101"}),
        ("pixiv_bookmark", {"101", "103"}),
        ("local", {"101", "102"}),
    ):
        assert artwork_ids(admin, q=q, tag_source=source) == expected


@pytest.mark.parametrize("q", ["없는번역", "풍경 missing", "%", "_"])
def test_translation_search_does_not_match_unrelated_terms_or_sql_wildcards(
    admin, translated_archive, q
):
    assert artwork_ids(admin, q=q) == set()
    assert get_json(admin, "/api/tags", q=q) == []
    result = get_json(admin, "/api/tags/catalog/pages", q=q)
    assert result["total"] == 0
    assert result["items"] == []


def test_translations_do_not_replace_original_tag_identity_in_exact_filters(
    admin, translated_archive
):
    assert artwork_ids(admin, q="풍경", tag_id=translated_archive) == {"101", "102", "103"}
    assert artwork_ids(admin, tag="風景") == {"101", "102", "103"}
    for translated_name in TRANSLATIONS.values():
        assert artwork_ids(admin, tag=translated_name) == set()


@pytest.mark.parametrize("q", ["풍경", "풍", "landscape", "ＬＡＮＤＳＣＡＰＥ"])
def test_tag_directory_search_and_pagination_preserve_both_languages_and_source_counts(
    admin, translated_archive, q
):
    matching = get_json(admin, "/api/tags", q=q)
    catalog = get_json(admin, "/api/tags/catalog")
    assert len(matching) == 3
    assert [row for row in catalog if row["id"] == translated_archive] == matching

    pages = [
        get_json(admin, "/api/tags/catalog/pages", q=q, page=page, page_size=1)
        for page in range(1, 4)
    ]
    for page, result in enumerate(pages, start=1):
        assert (result["total"], result["page"], result["page_size"]) == (3, page, 1)
        assert len(result["items"]) == 1
    rows = [page["items"][0] for page in pages]
    assert rows == matching
    assert [row["source"] for row in rows] == sorted(SOURCE_COUNTS)
    for row in rows:
        assert row["id"] == translated_archive
        assert row["name"] == "風景"
        assert row["translations"] == translation_views(TRANSLATIONS)
        assert row["artwork_count"] == SOURCE_COUNTS[row["source"]]
        assert row["editable"] is False
        filtered = get_json(admin, "/api/tags/catalog/pages", q=q, source=row["source"])
        assert filtered["total"] == 1
        assert filtered["items"] == [row]

    profile = get_json(admin, f"/api/tags/{translated_archive}")
    assert profile["artwork_count"] == 3
    assert profile["translations"] == translation_views(TRANSLATIONS)
    assert profile["source_counts"] == [
        {"source": source, "artwork_count": count}
        for source, count in sorted(SOURCE_COUNTS.items())
    ]


def test_translation_only_revisions_preserve_observed_history_and_current_metadata_exports(admin):
    original = remote(bookmark_tags=["風景"])
    library.ingest(original, "public")
    first = get_json(admin, "/api/artworks/101/history/pages", page_size=1)
    assert first["total"] == 1
    original_id = first["items"][0]["id"]
    add_local(admin, ["101"], "風景")
    add_local(admin, ["101"], "개인 분류")
    response = admin.patch("/api/artworks/101", json={"note": "보존할 메모"})
    assert response.status_code == 200, response.text

    refreshed = remote(
        work_tags=[RemoteTag(name="風景", translations={"en": "scenery"})], bookmark_tags=None
    )
    library.ingest(refreshed, "public")
    library.ingest(refreshed, "public")
    latest = get_json(admin, "/api/artworks/101/history/pages", page_size=1)
    older = get_json(admin, "/api/artworks/101/history/pages", page=2, page_size=1)
    assert latest["total"] == older["total"] == 2
    entry = latest["items"][0]
    assert entry["id"] != original_id
    assert entry["state"] == "available"
    assert entry["comparison_available"] is True
    assert entry["changes"] == ["work_tags"]
    assert entry["tag_changes"] == []  # The original tag name did not change.
    assert older["items"][0]["id"] == original_id
    assert older["items"][0]["initial"] is True

    for snapshot_id, observation in ((original_id, original), (entry["id"], refreshed)):
        snapshot = get_json(admin, f"/api/artworks/101/history/{snapshot_id}")
        expected_tags = [tag.model_dump() for tag in observation.work_tags]
        assert snapshot["work_tags"] == expected_tags
        assert snapshot["bookmark_tags"] == observation.bookmark_tags
        stored = json.loads(ArtworkSnapshot.get_by_id(snapshot_id).metadata)
        assert stored["work_tags"] == expected_tags
    # Current dictionary translations accumulate; history records only what was observed then.
    expected_translations = {"ko": "풍경", "en": "scenery"}
    current = get_json(admin, "/api/artworks/101")
    original_tag = next(tag for tag in current["tags"] if tag["name"] == "風景")
    assert original_tag["translations"] == translation_views(expected_translations)
    assert artwork_ids(admin, q="landscape") == set()
    assert artwork_ids(admin, q="풍경 scenery") == {"101"}

    exported = get_json(admin, "/api/export/metadata")
    assert [record["id"] for record in exported["artworks"]] == ["101"]
    record = exported["artworks"][0]
    assert record["note"] == "보존할 메모"
    assert len(record["tags"]) == 4
    assert {(tag["name"], tag["source"]): tag["translations"] for tag in record["tags"]} == {
        ("風景", "pixiv_work"): expected_translations,
        ("風景", "pixiv_bookmark"): expected_translations,
        ("風景", "local"): expected_translations,
        ("개인 분류", "local"): {},
    }
    response = admin.get("/api/artworks/101/export")
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["metadata.json"]
        metadata = json.loads(archive.read("metadata.json"))
    assert metadata["artwork"]["tags"] == record["tags"]
    assert metadata["artwork"]["note"] == record["note"]
