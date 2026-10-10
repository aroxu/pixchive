"""Offline contract for gallery-dl JSON URL events; no live authentication is claimed."""

from html.parser import HTMLParser
from urllib.parse import urlparse

from app.adapters.pixiv.contracts import (
    AdapterError,
    RatingEvidence,
    RemoteArtwork,
    RemotePage,
    RemoteTag,
)

GALLERY_DL_VERSION = "1.32.13"


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p"}:
            self.parts.append("\n")


def normalize_url_event(event: list) -> RemoteArtwork:
    """Map one URL event, preserving missing tags and never inventing bookmark time.

    A URL event is not a whole bookmark page. Callers must aggregate events by artwork
    before ingestion and cannot infer a completed full scan from this function.
    """
    try:
        message, url, data = event
        parsed = urlparse(url)
        if (
            message != 3
            or parsed.scheme != "https"
            or not parsed.hostname
            or not (parsed.hostname == "pximg.net" or parsed.hostname.endswith(".pximg.net"))
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("invalid URL event")
        kind = {"illust": "illustration", "manga": "manga", "ugoira": "ugoira"}[data["type"]]
        tags = (
            None
            if "tags" not in data
            else [
                RemoteTag(
                    name=tag["name"],
                    translations={"und": tag["translated_name"]}
                    if tag.get("translated_name")
                    else {},
                )
                for tag in data["tags"]
            ]
        )
        caption = PlainText()
        caption.feed(data.get("caption", ""))
        evidence = (
            RatingEvidence(source="gallery_dl", field="x_restrict", value=data["x_restrict"])
            if "x_restrict" in data
            else None
        )
        return RemoteArtwork(
            id=str(data["id"]),
            artist_id=str(data["user"]["id"]),
            artist_name=data["user"]["name"],
            artist_account=data["user"].get("account"),
            title=data["title"],
            caption="".join(caption.parts),
            kind=kind,
            rating=evidence.normalized if evidence else "unknown",
            rating_evidence=evidence,
            created_at=data.get("create_date"),
            page_count=data["page_count"],
            pages=[RemotePage(index=data["num"], url=url, source="gallery_url")],
            work_tags=tags,
            bookmark_tags=data.get("tags_bookmark"),
            ugoira_frames=data.get("frames"),
        )
    except (ValueError, TypeError, KeyError):
        raise AdapterError("contract") from None
