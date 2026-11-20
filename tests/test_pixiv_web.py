import html
import json

import httpx
import pytest

from app.adapters.pixiv.contracts import AdapterError
from app.adapters.pixiv.web import WebBridge

COOKIE = "42_synthetic_session_cookie"


def identity(user="42", *, modern=False):
    profile = {"id": user} if user else None
    if modern:
        state = json.dumps({"userData": {"self": profile}, "api": {"token": "not-exported"}})
        payload = {"props": {"pageProps": {"serverSerializedPreloadedState": state}}}
        return (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script>"
        )
    content = html.escape(json.dumps({"userData": profile, "token": "not-exported"}), quote=True)
    return f'<meta name="global-data" content="{content}">'


def ok(body):
    return httpx.Response(200, json={"error": False, "message": "", "body": body})


def summary(artwork_id="123", bookmark_id="456"):
    return {
        "id": artwork_id,
        "title": "합성 작품",
        "userId": "7",
        "userName": "작가",
        "pageCount": 2,
        "tags": ["風景"],
        "updateDate": "2026-09-02T12:00:00+09:00",
        "profileImageUrl": "https://i.pximg.net/user-profile/img/7_50.jpg",
        "bookmarkData": {"id": bookmark_id},
    }


def artwork(kind=0):
    return {
        "id": "123",
        "title": "합성 작품",
        "description": "<p>캡션</p>",
        "userId": "7",
        "userName": "작가",
        "illustType": kind,
        "xRestrict": 0,
        "pageCount": 1 if kind == 2 else 2,
        "createDate": "2026-09-01T12:00:00+09:00",
        "tags": {"tags": [{"tag": "風景", "translation": {"en": "landscape"}}]},
    }


def bridge_with(handler, *, user="42", modern=False):
    def route(request):
        assert request.url.host == "www.pixiv.net" and request.url.scheme == "https"
        assert request.headers["Cookie"] == f"PHPSESSID={COOKIE}"
        assert "Authorization" not in request.headers
        assert request.method == "GET"
        if request.url.path == "/":
            return httpx.Response(200, text=identity(user, modern=modern))
        return handler(request)

    bridge = WebBridge(COOKIE, httpx.Client(transport=httpx.MockTransport(route)))
    bridge.validate()
    return bridge


def router(request, *, kind=0, tags=None):
    path = request.url.path
    if path.endswith("/illusts/bookmarks"):
        return ok({"works": [summary()], "total": 1, "bookmarkTags": {"456": tags or []}})
    if path == "/ajax/illust/123":
        return ok(artwork(kind))
    if path.endswith("/pages"):
        return ok([{"urls": {"original": f"https://i.pximg.net/p{i}.jpg"}} for i in range(2)])
    if path.endswith("/ugoira_meta"):
        return ok(
            {
                "originalSrc": "https://i.pximg.net/server_selected_archive.zip",
                "src": "https://i.pximg.net/preview.zip",
                "frames": [{"file": "000000.jpg", "delay": 123}],
            }
        )
    pytest.fail(f"Unexpected path: {path}")


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("scope,rest", [("public", "show"), ("private", "hide")])
def test_cookie_identity_scopes_original_pages_and_tag_sources(modern, scope, rest):
    def handler(request):
        if request.url.path.endswith("bookmarks"):
            assert request.url.params["rest"] == rest
            assert request.url.params["offset"] == "0"
            assert request.url.params["tag"] == ""
        return router(request, tags=["개인 분류"])

    bridge = bridge_with(handler, modern=modern)
    result = bridge.bookmark_page("42", scope, None)
    assert result["complete"] and result["next_cursor"] is None
    assert result["total"] == 1 and result["first_bookmark_id"] == "456"
    entry = result["entries"][0]
    assert entry["bookmark_tags"] == ["개인 분류"] and entry["work_tags"] == ["風景"]
    assert (entry["artist_id"], entry["artist_name"], entry["page_count"]) == ("7", "작가", 2)
    assert entry["avatar_url"] == "https://i.pximg.net/user-profile/img/7_50.jpg"
    work = bridge.artwork("123").model_dump(mode="json")
    assert len(work["pages"]) == 2 and work["artist_id"] == "7"
    assert work["bookmark_tags"] is None  # Bookmark tags come only from the list.
    assert work["work_tags"] == [{"name": "風景", "translations": {"en": "landscape"}}]
    assert work["bookmarked_at"] is None and "<p>" not in work["caption"]
    for value in (result, work):
        assert COOKIE not in json.dumps(value) and "not-exported" not in json.dumps(value)


def test_ugoira_uses_server_original_zip_and_exact_timing():
    work = bridge_with(lambda request: router(request, kind=2)).artwork("123")
    work = work.model_dump(mode="json")
    assert work["pages"] == []
    assert work["ugoira_zip_url"] == "https://i.pximg.net/server_selected_archive.zip"
    assert work["ugoira_frames"] == [{"file": "000000.jpg", "delay": 123}]


@pytest.mark.parametrize("kind", [0, 2])
def test_collects_english_and_korean_from_pixiv_localized_en_slot(kind):
    languages = []

    def handler(request):
        if request.url.path == "/ajax/illust/123":
            language = request.url.params["lang"]
            languages.append(language)
            data = artwork(kind)
            data["tags"]["tags"] = (
                [
                    {"tag": "風景", "translation": {"en": "landscape"}},
                    {"tag": "Original", "translation": {"en": "original"}},
                ]
                if language == "en"
                else [
                    {"tag": "Original", "translation": {"en": "original"}},
                    {"tag": "風景", "translation": {"en": "풍경"}},
                    {"tag": "別のタグ", "translation": {"en": "다른 태그"}},
                ]
            )
            return ok(data)
        return router(request, kind=kind)

    work = bridge_with(handler).artwork("123")
    assert languages == ["en", "ko"]
    assert [tag.model_dump() for tag in work.work_tags] == [
        {"name": "風景", "translations": {"en": "landscape", "ko": "풍경"}},
        {"name": "Original", "translations": {"en": "original"}},
    ]


def test_keeps_explicit_translation_languages_without_another_request():
    languages = []

    def handler(request):
        if request.url.path == "/ajax/illust/123":
            languages.append(request.url.params["lang"])
            data = artwork()
            data["tags"]["tags"][0]["translation"] = {"en": "landscape", "ko": "풍경"}
            return ok(data)
        return router(request)

    work = bridge_with(handler).artwork("123")
    assert languages == ["en"]
    assert work.work_tags[0].translations == {"en": "landscape", "ko": "풍경"}


@pytest.mark.parametrize("tags", [None, []])
def test_missing_or_empty_work_tags_do_not_request_translations(tags):
    languages = []

    def handler(request):
        if request.url.path == "/ajax/illust/123":
            languages.append(request.url.params["lang"])
            data = artwork()
            data["tags"] = {"tags": tags}
            return ok(data)
        return router(request)

    work = bridge_with(handler).artwork("123")
    assert languages == ["en"]
    assert work.work_tags == tags


def test_missing_korean_tag_fields_preserve_the_english_observation():
    def handler(request):
        if request.url.path == "/ajax/illust/123" and request.url.params["lang"] == "ko":
            data = artwork()
            del data["tags"]
            return ok(data)
        return router(request)

    work = bridge_with(handler).artwork("123")
    assert work.work_tags[0].translations == {"en": "landscape"}


def test_korean_response_keeps_explicit_language_keys_and_ignores_empty_values():
    def handler(request):
        if request.url.path == "/ajax/illust/123":
            data = artwork()
            data["tags"]["tags"][0]["translation"] = (
                {"en": None, "ko": "  "}
                if request.url.params["lang"] == "en"
                else {"en": "landscape", "ko": "풍경", "ja": "風景"}
            )
            return ok(data)
        return router(request)

    work = bridge_with(handler).artwork("123")
    assert work.work_tags[0].translations == {"en": "landscape", "ko": "풍경", "ja": "風景"}


@pytest.mark.parametrize("name", ["landscape", "풍경"])
def test_legacy_translation_without_english_baseline_is_saved_and_searchable_as_und(admin, name):
    from app.models.entities import TagTranslation
    from app.services import library

    languages = []

    def handler(request):
        if request.url.path == "/ajax/illust/123":
            language = request.url.params["lang"]
            languages.append(language)
            data = artwork()
            tag = data["tags"]["tags"][0]
            if language == "en":
                del tag["translation"]
            else:
                tag["translation"] = {"en": name}
            return ok(data)
        return router(request)

    work = bridge_with(handler).artwork("123")
    assert languages == ["en", "ko"]
    assert [tag.model_dump() for tag in work.work_tags] == [
        {"name": "風景", "translations": {"und": name}}
    ]
    library.ingest(work, "private")
    rows = TagTranslation.select(
        TagTranslation.language, TagTranslation.name, TagTranslation.source
    ).tuples()
    assert list(rows) == [("und", name, "pixiv")]

    response = admin.get("/api/artworks/123")
    assert response.status_code == 200, response.text
    tags = response.json()["tags"]
    assert len(tags) == 1
    assert tags[0]["name"] == "風景"
    assert tags[0]["sources"] == ["pixiv_work"]
    expected_translations = [{"language": "und", "name": name, "source": "pixiv"}]
    assert tags[0]["translations"] == expected_translations
    # The fixture title/artist do not contain either query; both searches require the tag.
    for query in ("風景", name):
        response = admin.get("/api/artworks", params={"q": query, "tag_source": "pixiv_work"})
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1
        assert [item["id"] for item in response.json()["items"]] == ["123"]
    response = admin.get("/api/tags/catalog/pages", params={"q": name})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == tags[0]["id"]
    assert response.json()["items"][0]["translations"] == expected_translations


@pytest.mark.parametrize(
    "status,code", [(401, "auth_expired"), (403, "access_denied"), (429, "rate_limited")]
)
def test_korean_translation_request_errors_are_not_silently_ignored(status, code):
    def handler(request):
        if request.url.path == "/ajax/illust/123" and request.url.params["lang"] == "ko":
            return httpx.Response(status, headers={"Retry-After": "90"})
        return router(request)

    with pytest.raises(AdapterError, match=code) as error:
        bridge_with(handler).artwork("123")
    if status == 429:
        assert error.value.retry_after == 90


def test_korean_translation_response_cannot_mix_artwork_identities():
    def handler(request):
        if request.url.path == "/ajax/illust/123" and request.url.params["lang"] == "ko":
            data = artwork()
            data["id"] = "999"
            return ok(data)
        return router(request)

    with pytest.raises(AdapterError, match="contract"):
        bridge_with(handler).artwork("123")


def test_web_localized_translations_are_saved_and_searchable(admin):
    from app.services import library

    def handler(request):
        if request.url.path == "/ajax/illust/123":
            data = artwork()
            name = "landscape" if request.url.params["lang"] == "en" else "풍경"
            data["tags"]["tags"][0]["translation"] = {"en": name}
            return ok(data)
        return router(request)

    library.ingest(bridge_with(handler).artwork("123"), "private")
    for query in ("風景", "풍경", "LANDSCAPE"):
        response = admin.get("/api/artworks", params={"q": query})
        assert response.status_code == 200
        assert response.json()["total"] == 1
        assert response.json()["items"][0]["id"] == "123"


@pytest.mark.parametrize("mapping", [{}, []])
def test_explicit_empty_bookmark_tag_map_is_distinct_from_missing_field(mapping):
    def handler(request):
        if request.url.path.endswith("bookmarks"):
            return ok({"works": [summary()], "total": 1, "bookmarkTags": mapping})
        return router(request)

    result = bridge_with(handler).bookmark_page("42", "public", None)
    assert result["entries"][0]["bookmark_tags"] == []


def test_homepage_locale_redirect_keeps_cookie_on_same_origin():
    paths = []

    def handler(request):
        assert request.url.host == "www.pixiv.net"
        assert request.headers["cookie"] == f"PHPSESSID={COOKIE}"
        paths.append(request.url.path)
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/ko/"})
        return httpx.Response(200, text=identity(modern=True))

    bridge = WebBridge(COOKIE, httpx.Client(transport=httpx.MockTransport(handler)))
    assert bridge.validate() == "42"
    assert paths == ["/", "/ko/"]


def test_missing_tags_and_inaccessible_artwork_never_erase_existing_data():
    def handler(request):
        if request.url.path.endswith("bookmarks"):
            works = [summary(), {**summary("999", "9999"), "isMasked": True}]
            return ok({"works": works[: int(request.url.params["limit"])], "total": 2})
        return router(request)

    result = bridge_with(handler).bookmark_page("42", "private", None)
    first, masked = result["entries"]
    assert first["id"] == "123" and first["available"] and first["bookmark_tags"] is None
    assert masked["id"] == "999" and not masked["available"]


@pytest.mark.parametrize("modern", [False, True])
def test_anonymous_page_is_not_valid_authentication(modern):
    with pytest.raises(AdapterError, match="auth_expired"):
        bridge_with(router, user=None, modern=modern)


@pytest.mark.parametrize(
    "target,error",
    [
        ("https://accounts.pixiv.net/login", "auth_expired"),
        ("https://evil.invalid/", "contract"),
        ("https://i.pximg.net/", "contract"),
    ],
)
def test_cookie_is_never_forwarded_to_redirect(target, error):
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": target})

    bridge = WebBridge(
        COOKIE, httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True)
    )
    with pytest.raises(AdapterError, match=error):
        bridge.validate()
    assert len(requests) == 1


@pytest.mark.parametrize(
    "status,code",
    [(401, "auth_expired"), (403, "access_denied"), (429, "rate_limited"), (503, "network")],
)
def test_status_classification_does_not_expose_remote_error(status, code):
    bridge = WebBridge(
        COOKIE,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, text=COOKIE, headers={"Retry-After": "90"})
            )
        ),
    )
    with pytest.raises(AdapterError, match=code) as error:
        bridge.validate()
    assert COOKIE not in str(error.value)
    if status == 429:
        assert error.value.retry_after == 90


def test_session_expiring_during_ajax_error_enters_auth_wait():
    authenticated = True

    def handler(request):
        nonlocal authenticated
        if request.url.path == "/":
            return httpx.Response(200, text=identity("42" if authenticated else None))
        authenticated = False
        return httpx.Response(200, json={"error": True, "message": COOKIE})

    bridge = WebBridge(COOKIE, httpx.Client(transport=httpx.MockTransport(handler)))
    bridge.validate()
    with pytest.raises(AdapterError, match="auth_expired"):
        bridge.bookmark_page("42", "public", None)


@pytest.mark.parametrize(
    "data", [{"works": []}, {"works": [], "total": 3}, {"works": {}, "total": 0}]
)
def test_missing_or_inconsistent_total_never_completes(data):
    with pytest.raises(AdapterError, match="contract"):
        bridge_with(lambda request: ok(data)).bookmark_page("42", "public", None)


def test_pagination_resumes_with_overlap_and_changed_list_never_completes():
    works = [{**summary(str(i + 1), str(1000 + i)), "isMasked": True} for i in range(50)]
    requested = []

    def handler(request):
        start, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
        requested.append(start)
        return ok({"works": works[start : start + limit], "total": len(works), "bookmarkTags": {}})

    bridge = bridge_with(handler)
    first = bridge.bookmark_page("42", "private", None)
    assert not first["complete"] and len(first["entries"]) == 48
    # A new process can resume entirely from the durable cursor.
    second = bridge_with(handler).bookmark_page("42", "private", first["next_cursor"])
    assert second["complete"] and [e["id"] for e in second["entries"]] == ["49", "50"]
    assert second["first_bookmark_id"] == "1000" and second["total"] == 50
    assert 47 in requested
    works[47], works[48] = works[48], works[47]
    with pytest.raises(AdapterError, match="collection_changed"):
        bridge.bookmark_page("42", "private", first["next_cursor"])


def test_changed_front_at_end_and_legacy_cursor_rejected():
    def handler(request):
        if request.url.path.endswith("bookmarks") and request.url.params["limit"] == "1":
            return ok({"works": [summary("777", "7777")], "total": 1})
        return router(request)

    bridge = bridge_with(handler)
    listing = bridge.bookmark_page("42", "public", None)
    assert listing["complete"]
    with pytest.raises(AdapterError, match="collection_changed"):
        bridge.verify("42", "public", listing["total"], listing["first_bookmark_id"])
    with pytest.raises(AdapterError, match="contract"):
        bridge.bookmark_page("42", "public", "12345")
    with pytest.raises(AdapterError, match="contract"):
        bridge.bookmark_page("43", "private", None)


def test_request_interval_spaces_pixiv_requests(monkeypatch):
    import app.adapters.pixiv.web as web

    moments, sleeps = iter([0.0, 0.1, 0.1, 0.2, 5.0, 5.0]), []
    monkeypatch.setattr(web.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(web.time, "sleep", sleeps.append)
    monkeypatch.setattr(web.random, "uniform", lambda low, high: 0)
    bridge = WebBridge(
        COOKIE,
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=identity()))),
        interval=1.0,
    )
    bridge.validate()
    bridge.validate()
    assert sleeps == [pytest.approx(0.9)]


@pytest.mark.parametrize("interval", [0.5, 1.0, 30.0])
def test_request_jitter_varies_gaps_without_shortening_the_interval(monkeypatch, interval):
    import app.adapters.pixiv.web as web

    current, requests, sleeps = [1000.0], [], []
    samples = iter([0.0, 0.5, 1.0])
    monkeypatch.setattr(web.time, "monotonic", lambda: current[0])
    monkeypatch.setattr(web.random, "uniform", lambda low, high: high * next(samples))

    def sleep(seconds):
        sleeps.append(seconds)
        current[0] += seconds

    def handler(request):
        requests.append(current[0])
        return httpx.Response(200, text=identity())

    monkeypatch.setattr(web.time, "sleep", sleep)
    bridge = WebBridge(COOKIE, httpx.Client(transport=httpx.MockTransport(handler)), interval)
    for _ in range(4):
        bridge.validate()
    bound = min(interval * 0.3, 1.0)
    gaps = [right - left for left, right in zip(requests, requests[1:], strict=False)]
    assert gaps == pytest.approx([interval, interval + bound / 2, interval + bound])
    assert sleeps == pytest.approx(gaps)


def test_jitter_does_not_wait_when_interval_is_disabled_or_already_elapsed(monkeypatch):
    import app.adapters.pixiv.web as web

    current, sleeps, samples = [1000.0], [], []
    monkeypatch.setattr(web.time, "monotonic", lambda: current[0])
    monkeypatch.setattr(web.time, "sleep", sleeps.append)

    def sample(low, high):
        samples.append(high)
        return high

    monkeypatch.setattr(web.random, "uniform", sample)
    for interval in (0.0, 0.5):
        bridge = WebBridge(
            COOKIE,
            httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, text=identity()))
            ),
            interval,
        )
        bridge.validate()
        current[0] += 2
        bridge.validate()
    assert sleeps == [] and samples == [0.15]


def test_identity_redirect_keeps_the_minimum_gap_and_jitter(monkeypatch):
    import app.adapters.pixiv.web as web

    current, requests = [1000.0], []
    monkeypatch.setattr(web.time, "monotonic", lambda: current[0])
    monkeypatch.setattr(web.random, "uniform", lambda low, high: high)

    def sleep(seconds):
        current[0] += seconds

    def handler(request):
        requests.append((request.url.path, current[0]))
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/en/"})
        return httpx.Response(200, text=identity())

    monkeypatch.setattr(web.time, "sleep", sleep)
    bridge = WebBridge(COOKIE, httpx.Client(transport=httpx.MockTransport(handler)), interval=1.0)
    bridge.validate()
    assert requests == [("/", 1000.0), ("/en/", 1001.3)]


def test_invalid_identity_and_oversized_response_fail_closed():
    for response in (
        httpx.Response(200, text="<html>unexpected</html>"),
        httpx.Response(200, headers={"content-length": str(17 * 1024 * 1024)}),
    ):
        bridge = WebBridge(
            COOKIE,
            httpx.Client(
                transport=httpx.MockTransport(lambda request, response=response: response)
            ),
        )
        with pytest.raises(AdapterError):
            bridge.validate()
