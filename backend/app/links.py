"""GET /links/preview -- title + description for a shared link.

Reads the page's own link-preview tags (og:title / og:description, the ones
WhatsApp/Slack use) -- no crawler, no LLM, stdlib only. See
docs/LINK_SUMMARIES_PLAN.md.
"""
import html
import ipaddress
import json
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import HttpUrl

from auth import get_current_user
from models import LinkPreview

router = APIRouter(prefix="/links", tags=["links"], dependencies=[Depends(get_current_user)])

FETCH_TIMEOUT_SECONDS = 5
MAX_BYTES = 512_000  # the preview tags live in <head>
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


def _check_public(url: str) -> None:
    """Raises unless `url` is http(s) and every address its host resolves to
    is public. Without this the endpoint could reach the Lambda Runtime API
    on 127.0.0.1:9001, link-local metadata addresses, etc."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"refusing to fetch {url!r}")
    for *_, sockaddr in socket.getaddrinfo(parts.hostname, None):
        if not ipaddress.ip_address(sockaddr[0]).is_global:
            raise ValueError(f"refusing to fetch non-public address for {parts.hostname!r}")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    # _check_public only sees the first URL -- re-run it on every hop so a
    # public page can't redirect us somewhere private.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirect)


def _fetch(url: str) -> str:
    _check_public(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    with _opener.open(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
        body = resp.read(MAX_BYTES)
        charset = resp.headers.get_content_charset() or "utf-8"
    try:
        return body.decode(charset, "replace")
    except LookupError:  # header names a charset Python doesn't know
        return body.decode("utf-8", "replace")


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            a = dict(attrs)
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        # Only the first <title> -- later ones are usually inline SVG titles.
        self._in_title = tag == "title" and not self.title

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _clean(value: Optional[str]) -> str:
    # The parser already unescaped once; some sites (GitHub) double-escape, e.g. "&amp;amp;".
    return " ".join(html.unescape(value).split()) if value else ""


def _first(*values: Optional[str]) -> Optional[str]:
    return next((c for c in map(_clean, values) if c), None)


def _longest(*values: Optional[str]) -> Optional[str]:
    return max(map(_clean, values), key=len) or None


def _html_preview(page: str) -> LinkPreview:
    p = _MetaParser()
    p.feed(page)
    m = p.meta
    return LinkPreview(
        title=_first(m.get("og:title"), m.get("twitter:title"), p.title),
        # Longest, not first: sites truncate some copies (GitHub's og:description
        # ends in "- David..." while its plain description is complete).
        description=_longest(m.get("og:description"), m.get("description"), m.get("twitter:description")),
    )


def _youtube_preview(url: str) -> LinkPreview:
    # youtube.com serves non-browsers an empty shell; oEmbed accepts every URL
    # form as-is (youtu.be, /shorts/, m., music., extra query params).
    oembed = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
    data = json.loads(_fetch(oembed))
    return LinkPreview(title=_first(data.get("title")), description=_first(data.get("author_name")))


@router.get("/preview", response_model=LinkPreview)
def preview(url: HttpUrl) -> LinkPreview:
    url = str(url)
    try:
        if urllib.parse.urlsplit(url).hostname in YOUTUBE_HOSTS:
            return _youtube_preview(url)
        return _html_preview(_fetch(url))
    except Exception as e:
        # Best effort: blocked, unreachable, 403, timeout, junk -- the client
        # just keeps the bare URL, so there's no error case for it to handle.
        print(f"link preview failed for {url}: {e!r}")  # -> CloudWatch
        return LinkPreview()
