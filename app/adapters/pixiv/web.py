"""Read-only Pixiv web adapter. Cookie headers never leave www.pixiv.net."""

import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from app.adapters.pixiv.contracts import (
    AdapterError,
    BookmarkEntry,
    BookmarkListing,
    RatingEvidence,
    RemoteArtwork,
    RemotePage,
    RemoteTag,
    UgoiraFrame,
)
from app.adapters.pixiv.gallery import PlainText
from app.adapters.pixiv.transport import check_status, media_url

COOKIE_PATTERN = r"[A-Za-z0-9._~-]{16,4096}"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
PAGE_SIZE = 48
MAX_INTERVAL = 30.0


def identifier(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise AdapterError("contract")
    result = str(value)
    if not re.fullmatch(r"[0-9]{1,20}", result) or int(result) == 0:
        raise AdapterError("contract")
    return result


def optional_text(value):
    return value if isinstance(value, str) else None


def string_list(value):
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return None


class IdentityPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.global_data = None
        self.next_data = []
        self.in_next = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and (
            attrs.get("id") == "meta-global-data" or attrs.get("name") == "global-data"
        ):
            self.global_data = attrs.get("content")
        if tag == "script" and attrs.get("id") == "__NEXT_DATA__":
            self.in_next = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_next = False

    def handle_data(self, data):
        if self.in_next:
            self.next_data.append(data)

    def account_id(self):
        try:
            if self.next_data:
                payload = json.loads("".join(self.next_data))
                state = json.loads(payload["props"]["pageProps"]["serverSerializedPreloadedState"])
                user = state["userData"]["self"]
            elif self.global_data is not None:
                user = json.loads(self.global_data)["userData"]
            else:
                raise AdapterError("contract")
            if user is None or user is False:
                raise AdapterError("auth_expired")
            return identifier(user["id"])
        except (ValueError, TypeError, KeyError):
            raise AdapterError("contract") from None


class WebBridge:
    def __init__(self, cookie: str, client=None, interval: float = 0.0):
        if not re.fullmatch(COOKIE_PATTERN, cookie):
            raise AdapterError("auth_expired")
        self.cookie = cookie
        self.client = client or httpx.Client(timeout=20, trust_env=False, follow_redirects=False)
        self.account_id = None
        # A minimum gap between Pixiv page/API requests. Not a guaranteed safe rate.
        try:
            self.interval = min(max(float(interval), 0.0), MAX_INTERVAL)
        except (TypeError, ValueError):
            raise AdapterError("contract") from None
        self.last_request = None

    def pace(self):
        if self.interval and self.last_request is not None:
            wait = self.last_request + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)

    def request(self, path, params=None, *, redirects=0):
        # Paths are constructed locally from validated IDs, never from remote URLs.
        if not re.fullmatch(r"/(?:ajax/[A-Za-z0-9/_]+|[a-z]{2}(?:-[a-z]{2})?/)?", path):
            raise AdapterError("contract")
        self.pace()
        try:
            with self.client.stream(
                "GET",
                "https://www.pixiv.net" + path,
                params=params,
                headers={
                    "Cookie": "PHPSESSID=" + self.cookie,
                    "Referer": "https://www.pixiv.net/",
                    "Origin": "https://www.pixiv.net",
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "ko,en;q=0.8,ja;q=0.6",
                    "Cache-Control": "no-cache",
                },
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    destination = urlparse(response.headers.get("location", ""))
                    if destination.hostname == "accounts.pixiv.net" or (
                        destination.hostname in (None, "www.pixiv.net")
                        and destination.path in ("/login.php", "/login")
                    ):
                        raise AdapterError("auth_expired")
                    if (
                        redirects < 2
                        and not path.startswith("/ajax/")
                        and destination.scheme in ("", "https")
                        and destination.netloc in ("", "www.pixiv.net")
                        and not destination.query
                        and not destination.fragment
                        and re.fullmatch(r"/[a-z]{2}(?:-[a-z]{2})?/", destination.path)
                    ):
                        return self.request(destination.path, redirects=redirects + 1)
                    raise AdapterError("contract")
                if response.status_code == 403:
                    raise AdapterError("access_denied")
                check_status(response.status_code, response.headers)
                length = response.headers.get("content-length")
                if length and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
                    raise AdapterError("unsupported")
                content = bytearray()
                for chunk in response.iter_bytes(64 * 1024):
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise AdapterError("unsupported")
                    content.extend(chunk)
                return bytes(content)
        except httpx.HTTPError:
            raise AdapterError("network") from None
        finally:
            self.last_request = time.monotonic()

    def validate(self):
        identity = IdentityPage()
        identity.feed(self.request("/").decode("utf-8"))
        account_id = identity.account_id()
        if self.account_id and self.account_id != account_id:
            raise AdapterError("auth_expired")
        self.account_id = account_id
        return account_id

    def get(self, path, params=None):
        try:
            envelope = json.loads(self.request(path, params))
            if envelope["error"] is True:
                # An expired session may produce HTTP 200 error JSON. Verify identity
                # without relying on localized error text or confusing it with deletion.
                self.validate()
                raise AdapterError("unavailable")
            if envelope["error"] is not False:
                raise AdapterError("contract")
            return envelope["body"]
        except (ValueError, KeyError, TypeError):
            raise AdapterError("contract") from None

    def fetch_listing(self, account_id, scope, offset, limit=PAGE_SIZE):
        data = self.get(
            f"/ajax/user/{account_id}/illusts/bookmarks",
            {
                "tag": "",
                "offset": offset,
                "limit": limit,
                "rest": "show" if scope == "public" else "hide",
            },
        )
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("works"), list)
            or type(data.get("total")) is not int
            or data["total"] < 0
            or len(data["works"]) > limit
            or (not data["works"] and offset < data["total"])
            or offset + len(data["works"]) > data["total"]
        ):
            raise AdapterError("contract")
        return data

    @staticmethod
    def bookmark_id(work):
        try:
            return identifier(work["bookmarkData"]["id"])
        except (TypeError, KeyError):
            raise AdapterError("contract") from None

    def entry(self, raw, mapping) -> BookmarkEntry:
        artwork_id = identifier(raw["id"])
        if raw.get("isMasked") or raw.get("title") == "-----":
            return BookmarkEntry(id=artwork_id, available=False)
        # Web API supplies a sparse bookmark-ID map (or [] when empty).
        # A missing whole field is unavailable; an explicit empty map is not.
        tags = (
            mapping.get(self.bookmark_id(raw), [])
            if isinstance(mapping, dict)
            else []
            if mapping == []
            else None
        )
        try:
            artist_id = identifier(raw.get("userId"))
        except AdapterError:
            artist_id = None
        page_count = raw.get("pageCount")
        avatar = None
        if isinstance(raw.get("profileImageUrl"), str):
            try:
                avatar = media_url(raw["profileImageUrl"])
            except AdapterError:
                avatar = None
        return BookmarkEntry(
            id=artwork_id,
            title=optional_text(raw.get("title")),
            artist_id=artist_id,
            artist_name=optional_text(raw.get("userName")),
            page_count=page_count if type(page_count) is int and page_count >= 1 else None,
            updated_at=optional_text(raw.get("updateDate")),
            work_tags=string_list(raw.get("tags")),
            bookmark_tags=string_list(tags),
            avatar_url=avatar,
        )

    def bookmark_page(self, account_id, scope, cursor):
        """One list page without artwork details; the worker decides what to refresh."""
        if account_id != self.account_id or scope not in ("public", "private"):
            raise AdapterError("contract")
        # Public results must not silently become anonymous halfway through a scan.
        self.validate()
        offset, expected_total, boundary, first = 0, None, None, None
        if cursor is not None:
            try:
                version, offset, expected_total, boundary, first = json.loads(cursor)
                if (
                    version != "web-v1"
                    or type(offset) is not int
                    or type(expected_total) is not int
                    or not 0 < offset < expected_total
                ):
                    raise ValueError
                identifier(boundary)
                identifier(first)
            except (ValueError, TypeError):
                raise AdapterError("contract") from None
        # Overlap one bookmark to detect offset drift during pause/resume or edits.
        data = self.fetch_listing(account_id, scope, max(0, offset - 1))
        works, total = data["works"], data["total"]
        if expected_total is not None:
            if total != expected_total or not works or self.bookmark_id(works[0]) != boundary:
                raise AdapterError("collection_changed")
            works = works[1:]
        if not works and offset < total:
            raise AdapterError("contract")
        if first is None and works:
            first = self.bookmark_id(works[0])
        next_offset = offset + len(works)
        complete = next_offset == total
        next_cursor = (
            None
            if complete
            else json.dumps(["web-v1", next_offset, total, self.bookmark_id(works[-1]), first])
        )
        mapping = data.get("bookmarkTags")
        entries = {}
        for raw in works:
            try:
                item = self.entry(raw, mapping)
            except (TypeError, KeyError, AttributeError):
                raise AdapterError("contract") from None
            entries.setdefault(item.id, item)
        return BookmarkListing(
            entries=list(entries.values()),
            next_cursor=next_cursor,
            complete=complete,
            total=total,
            first_bookmark_id=first,
        ).model_dump(mode="json")

    def verify(self, account_id, scope, total, first):
        """A changed front or total invalidates the scan before not_seen is committed."""
        if (
            account_id != self.account_id
            or scope not in ("public", "private")
            or type(total) is not int
            or total < 0
        ):
            raise AdapterError("contract")
        if first is not None:
            identifier(first)
        self.validate()
        head = self.fetch_listing(account_id, scope, 0, 1)
        head_id = self.bookmark_id(head["works"][0]) if head["works"] else None
        if head["total"] != total or head_id != first:
            raise AdapterError("collection_changed")
        return True

    def artwork(self, artwork_id):
        raw = self.get(f"/ajax/illust/{artwork_id}")
        if identifier(raw["id"]) != artwork_id:
            raise AdapterError("contract")
        kind = {0: "illustration", 1: "manga", 2: "ugoira"}[raw["illustType"]]
        caption = PlainText()
        caption.feed(raw.get("description", ""))
        tag_block = raw.get("tags")
        tags = tag_block.get("tags") if isinstance(tag_block, dict) else None
        evidence = (
            RatingEvidence(source="pixiv_web", field="xRestrict", value=raw["xRestrict"])
            if "xRestrict" in raw
            else None
        )
        work = RemoteArtwork(
            id=artwork_id,
            artist_id=identifier(raw["userId"]),
            artist_name=raw["userName"],
            artist_account=raw.get("userAccount"),
            title=raw["title"],
            caption="".join(caption.parts),
            kind=kind,
            rating=evidence.normalized if evidence else "unknown",
            rating_evidence=evidence,
            created_at=raw.get("createDate"),
            page_count=raw["pageCount"],
            work_tags=None
            if tags is None
            else [
                RemoteTag(name=tag["tag"], translations=tag.get("translation") or {})
                for tag in tags
            ],
        )
        if kind == "ugoira":
            metadata = self.get(f"/ajax/illust/{artwork_id}/ugoira_meta")
            work.ugoira_zip_url = media_url(metadata["originalSrc"])
            work.ugoira_source = "pixiv_ugoira_original_src"
            work.ugoira_frames = [UgoiraFrame.model_validate(frame) for frame in metadata["frames"]]
            if not work.ugoira_frames or len(work.ugoira_frames) > 5000 or work.page_count != 1:
                raise AdapterError("contract")
        else:
            pages = self.get(f"/ajax/illust/{artwork_id}/pages")
            if not isinstance(pages, list) or len(pages) != work.page_count:
                raise AdapterError("contract")
            work.pages = [
                RemotePage(
                    index=index,
                    url=media_url(page["urls"]["original"]),
                    source="pixiv_pages_original",
                )
                for index, page in enumerate(pages)
            ]
        return work
