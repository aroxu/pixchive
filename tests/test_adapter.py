import json
from importlib.metadata import version
from pathlib import Path

import pytest

from app.adapters.pixiv.contracts import AdapterError
from app.adapters.pixiv.gallery import GALLERY_DL_VERSION, normalize_url_event


def test_pinned_gallery_contract_and_missing_fields():
    assert version("gallery-dl") == GALLERY_DL_VERSION
    event = json.loads(Path("tests/fixtures/gallery-url.json").read_text())
    item = normalize_url_event(event)
    assert item.id == "900000000"
    assert item.artist_id == "800000000"
    assert item.bookmarked_at is None
    assert item.work_tags[0].translations == {"und": "landscape"}
    assert "<p>" not in item.caption
    del event[2]["tags"]
    del event[2]["tags_bookmark"]
    item = normalize_url_event(event)
    assert item.work_tags is None
    assert item.bookmark_tags is None


@pytest.mark.parametrize(
    "url",
    [
        "http://i.pximg.net/a",
        "https://i.pximg.net.evil.invalid/a",
        "https://127.0.0.1/a",
        "https://x@i.pximg.net/a",
    ],
)
def test_untrusted_media_urls(url):
    event = json.loads(Path("tests/fixtures/gallery-url.json").read_text())
    event[1] = url
    with pytest.raises(AdapterError):
        normalize_url_event(event)
