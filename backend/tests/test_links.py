import email.message
import socket
import urllib.error
import urllib.request

import pytest

PUBLIC_IP = "93.184.215.14"


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self._body = body
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def web(monkeypatch):
    """Fakes DNS (host -> IP via `web.dns`) and the network (url -> FakeResponse
    or exception via `web.pages`); records every URL actually opened."""
    import links

    class Web:
        dns: dict[str, str] = {}
        pages: dict = {}
        opened: list[str] = []

    w = Web()
    w.dns, w.pages, w.opened = {}, {}, []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (w.dns.get(host, PUBLIC_IP), 0))]

    def fake_open(req, timeout=None):
        w.opened.append(req.full_url)
        page = w.pages[req.full_url]
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(links._opener, "open", fake_open)
    return w


def preview(client, auth_headers, url):
    resp = client.get("/links/preview", params={"url": url}, headers=auth_headers)
    assert resp.status_code == 200
    return resp.json()


def test_reads_og_tags(client, auth_headers, web):
    web.pages["https://example.com/post"] = FakeResponse(
        b"""<html><head>
        <title>Fallback title</title>
        <meta property="og:title" content="  Real   Title &amp; More ">
        <meta property="og:description" content="What this page is about.">
        </head><body><svg><title>icon</title></svg></body></html>"""
    )
    assert preview(client, auth_headers, "https://example.com/post") == {
        "title": "Real Title & More",
        "description": "What this page is about.",
    }


def test_falls_back_to_title_tag_and_meta_description(client, auth_headers, web):
    web.pages["https://example.com/"] = FakeResponse(
        b'<title>\n  Plain Page\n</title><meta name="Description" content="Plain description">'
        b"<svg><title>later svg title</title></svg>"
    )
    assert preview(client, auth_headers, "https://example.com/") == {
        "title": "Plain Page",
        "description": "Plain description",
    }


def test_prefers_complete_description_over_truncated_double_escaped_one(client, auth_headers, web):
    # Real shape of GitHub's repo page tags.
    web.pages["https://github.com/DavidHDev/react-bits"] = FakeResponse(
        b'<meta name="description" content="React &amp; Next.js components. - DavidHDev/react-bits">'
        b'<meta name="twitter:description" content="React &amp;amp;amp; Next.js components. - D...">'
        b'<meta property="og:description" content="React &amp;amp; Next.js components. - David...">'
    )
    assert preview(client, auth_headers, "https://github.com/DavidHDev/react-bits")["description"] == (
        "React & Next.js components. - DavidHDev/react-bits"
    )


def test_decodes_declared_charset(client, auth_headers, web):
    web.pages["https://example.com/"] = FakeResponse(
        "<title>Café crème</title>".encode("latin-1"), "text/html; charset=iso-8859-1"
    )
    assert preview(client, auth_headers, "https://example.com/")["title"] == "Café crème"


def test_unknown_or_wrong_charset_does_not_crash(client, auth_headers, web):
    web.pages["https://example.com/a"] = FakeResponse("<title>naïve</title>".encode(), "text/html; charset=x-bogus")
    web.pages["https://example.com/b"] = FakeResponse(b"<title>bad \xff byte</title>", "text/html")
    assert preview(client, auth_headers, "https://example.com/a")["title"] == "naïve"
    assert preview(client, auth_headers, "https://example.com/b")["title"] == "bad � byte"


def test_fetch_failure_returns_empty_preview(client, auth_headers, web):
    web.pages["https://medium.com/x"] = urllib.error.HTTPError("https://medium.com/x", 403, "Forbidden", None, None)
    assert preview(client, auth_headers, "https://medium.com/x") == {"title": None, "description": None}


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "10.0.0.5", "::1", "::ffff:127.0.0.1"])
def test_non_public_host_is_never_fetched(client, auth_headers, web, ip):
    web.dns["internal.example"] = ip
    assert preview(client, auth_headers, "http://internal.example:9001/") == {"title": None, "description": None}
    assert web.opened == []


def test_redirect_to_non_public_host_is_refused(web):
    import links

    web.dns["evil.example"] = "169.254.169.254"
    req = urllib.request.Request("https://example.com/")
    with pytest.raises(ValueError):
        links._CheckedRedirect().redirect_request(req, None, 302, "Found", {}, "http://evil.example/latest")
    with pytest.raises(ValueError):
        links._CheckedRedirect().redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")
    # a public hop is still followed
    assert links._CheckedRedirect().redirect_request(req, None, 302, "Found", {}, "https://example.org/").full_url == (
        "https://example.org/"
    )


def test_youtube_uses_oembed(client, auth_headers, web):
    oembed = "https://www.youtube.com/oembed?format=json&url=https%3A%2F%2Fyoutu.be%2FdQw4w9WgXcQ%3Fsi%3Dabc"
    web.pages[oembed] = FakeResponse(b'{"title": "Never Gonna Give You Up", "author_name": "Rick Astley"}')
    assert preview(client, auth_headers, "https://youtu.be/dQw4w9WgXcQ?si=abc") == {
        "title": "Never Gonna Give You Up",
        "description": "Rick Astley",
    }


def test_rejects_non_http_url(client, auth_headers):
    resp = client.get("/links/preview", params={"url": "ftp://example.com/file"}, headers=auth_headers)
    assert resp.status_code == 422


def test_requires_auth(client):
    assert client.get("/links/preview", params={"url": "https://example.com/"}).status_code in (401, 403)
