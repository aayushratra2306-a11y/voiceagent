"""Task 7.5 — the website crawler.

Two layers, each tested the way its own risk actually looks:

  - `extract_text_and_links` is a pure function (no network at all), tested
    exhaustively against real, occasionally malformed HTML.
  - `crawl_website` is tested against a REAL local HTTP server serving a
    small, known link graph — proving the depth/scope limits, the
    same-domain restriction, and robots.txt honouring against actual
    request/response behaviour rather than asserting on mocked calls that
    could quietly stop matching what httpx actually does.
"""

import http.server
import threading

import pytest

from app.services.knowledge_sources.website import crawl_website, extract_text_and_links

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# extract_text_and_links — pure, no network
# ---------------------------------------------------------------------------


def test_visible_text_is_extracted():
    html = "<html><body><p>Hello there.</p><p>Second paragraph.</p></body></html>"
    _title, text, _links = extract_text_and_links(html, "https://example.com")
    assert "Hello there." in text
    assert "Second paragraph." in text


def test_script_and_style_content_is_never_extracted_as_text():
    html = (
        "<html><head><style>body{color:red}</style></head>"
        "<body><script>alert('hi')</script><p>Real content.</p></body></html>"
    )
    _title, text, _links = extract_text_and_links(html, "https://example.com")
    assert "color:red" not in text
    assert "alert" not in text
    assert "Real content." in text


def test_the_title_is_extracted_separately_from_body_text():
    html = "<html><head><title>My Page Title</title></head><body><p>Body text.</p></body></html>"
    title, text, _links = extract_text_and_links(html, "https://example.com")
    assert title == "My Page Title"
    assert "My Page Title" not in text


def test_relative_links_are_resolved_against_the_page_url():
    html = '<a href="/about">About</a><a href="contact.html">Contact</a>'
    _title, _text, links = extract_text_and_links(html, "https://example.com/products/")
    assert "https://example.com/about" in links
    assert "https://example.com/products/contact.html" in links


def test_absolute_links_are_kept_as_is():
    html = '<a href="https://other.com/page">Other</a>'
    _title, _text, links = extract_text_and_links(html, "https://example.com")
    assert "https://other.com/page" in links


def test_a_url_fragment_is_stripped_so_it_is_not_treated_as_a_separate_page():
    html = '<a href="/faq#section-2">FAQ section 2</a>'
    _title, _text, links = extract_text_and_links(html, "https://example.com")
    assert links == ["https://example.com/faq"]


def test_self_closing_anchor_tags_are_still_captured():
    """Some templating produces XHTML-style self-closing tags; a link here
    must not be silently dropped just because of that style."""
    html = '<a href="/xhtml-style" />'
    _title, _text, links = extract_text_and_links(html, "https://example.com")
    assert "https://example.com/xhtml-style" in links


def test_malformed_html_degrades_to_empty_rather_than_raising():
    malformed = "<html><body><p>unclosed <div>tags<span>everywhere"
    title, text, links = extract_text_and_links(malformed, "https://example.com")
    # Must not raise; whatever it recovers (if anything) is acceptable.
    assert isinstance(title, str)
    assert isinstance(text, str)
    assert isinstance(links, list)


def test_completely_invalid_input_never_raises():
    for bad in ("", "\x00\x01\x02", "<" * 10000, None):
        extract_text_and_links(bad or "", "https://example.com")  # must not raise


# ---------------------------------------------------------------------------
# crawl_website — against a real local server
# ---------------------------------------------------------------------------

# A small, known link graph: home -> about, home -> contact -> secret
# (2 hops from home), and an off-domain link that must never be followed.
_PAGES = {
    "/": (
        "<html><head><title>Home</title></head><body>"
        "<p>Welcome to the home page.</p>"
        '<a href="/about">About us</a>'
        '<a href="/contact">Contact</a>'
        '<a href="https://off-domain.test/elsewhere">Off-site link</a>'
        "</body></html>"
    ),
    "/about": (
        "<html><head><title>About</title></head><body>"
        "<p>This is the about page, with real content to extract.</p>"
        "</body></html>"
    ),
    "/contact": (
        "<html><head><title>Contact</title></head><body>"
        "<p>Contact us here.</p>"
        '<a href="/secret-depth-2">Deep link</a>'
        "</body></html>"
    ),
    "/secret-depth-2": (
        "<html><head><title>Deep</title></head><body>"
        "<p>Two hops from home.</p>"
        "</body></html>"
    ),
    "/disallowed": (
        "<html><head><title>Disallowed</title></head><body>"
        "<p>robots.txt says stay out.</p></body></html>"
    ),
    "/robots.txt": "User-agent: *\nDisallow: /disallowed\n",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server's own required name
        path = self.path
        body = _PAGES.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        content_type = "text/plain" if path == "/robots.txt" else "text/html"
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):  # silence the default stderr access log
        pass


@pytest.fixture
def local_site(monkeypatch):
    """A real HTTP server on loopback serving _PAGES, plus the
    allow_private_outbound_urls flag turned on — exactly the "local
    development" case app/core/url_safety.py documents that setting for.
    Without it, the crawler would (correctly, in production) refuse to
    fetch a loopback address at all, and this test would learn nothing
    about the crawler's own logic."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "allow_private_outbound_urls", True)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


async def test_the_crawl_finds_every_linked_page_within_depth(local_site):
    items = (await crawl_website(local_site + "/", max_pages=50, max_depth=3)).items
    urls = {item.url for item in items}

    assert local_site + "/" in urls
    assert local_site + "/about" in urls
    assert local_site + "/contact" in urls
    assert local_site + "/secret-depth-2" in urls


async def test_extracted_content_is_the_real_page_text(local_site):
    items = (await crawl_website(local_site + "/", max_pages=50, max_depth=3)).items
    about = next(item for item in items if item.url == local_site + "/about")
    assert "about page, with real content" in about.text
    assert about.title == "About"


async def test_an_off_domain_link_is_never_followed(local_site):
    """The off-domain link on the home page points at off-domain.test,
    which does not exist — if the crawler tried to follow it, this would
    either hang on DNS resolution or raise, not silently succeed. Passing
    at all proves it was correctly skipped."""
    items = (await crawl_website(local_site + "/", max_pages=50, max_depth=3)).items
    assert all("off-domain.test" not in item.url for item in items)


async def test_a_page_disallowed_by_robots_txt_is_not_fetched(local_site):
    # Home page does not link to /disallowed, so fetch it directly to prove
    # the crawler itself would refuse it even if reachable.
    items = (await crawl_website(local_site + "/disallowed", max_pages=10, max_depth=0)).items
    assert items == []


async def test_the_page_count_cap_is_respected(local_site):
    items = (await crawl_website(local_site + "/", max_pages=2, max_depth=3)).items
    assert len(items) <= 2


async def test_the_depth_cap_stops_the_crawl_from_going_further(local_site):
    """max_depth=1: home's direct links (about, contact) are fetched, but
    contact's OWN link (secret-depth-2, two hops away) is not."""
    items = (await crawl_website(local_site + "/", max_pages=50, max_depth=1)).items
    urls = {item.url for item in items}
    assert local_site + "/contact" in urls
    assert local_site + "/secret-depth-2" not in urls


async def test_an_unreachable_start_url_returns_an_empty_list_not_an_exception(monkeypatch):
    """A genuine connection failure (nothing listening on this port) — with
    the dev override on so this is actually exercising the network-failure
    path and not the SSRF refusal tested separately below."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "allow_private_outbound_urls", True)
    items = (await crawl_website("http://127.0.0.1:1/", max_pages=5, max_depth=1)).items
    assert items == []


async def test_a_non_http_url_is_rejected_without_attempting_a_request():
    items = (await crawl_website("ftp://example.com/", max_pages=5, max_depth=1)).items
    assert items == []


async def test_crawling_a_private_address_is_refused_without_the_dev_override():
    """The real production behaviour: no monkeypatched override this time,
    so the same SSRF protection every other outbound feature uses must
    refuse a loopback target outright — never even attempting a request."""
    items = (await crawl_website("http://127.0.0.1:1/", max_pages=5, max_depth=1)).items
    assert items == []


# ---------------------------------------------------------------------------
# Reporting whether the crawl actually saw the whole site
#
# The crawler promises never to raise, which means "the site is down" and
# "the site is empty" arrived at the caller looking identical — and
# knowledge_sync.py deletes what a source no longer returns. See
# FetchResult in app/services/knowledge_sources/__init__.py.
# ---------------------------------------------------------------------------


async def test_a_clean_crawl_reports_itself_complete(local_site):
    result = await crawl_website(local_site + "/", max_pages=50, max_depth=3)
    assert result.complete is True
    assert result.error == ""
    assert result.items


async def test_an_unreachable_site_reports_incomplete_not_merely_empty(monkeypatch):
    """The scenario that destroyed data: nothing came back, and the caller
    had no way to tell that from a site whose pages were all deleted."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "allow_private_outbound_urls", True)
    result = await crawl_website("http://127.0.0.1:1/", max_pages=5, max_depth=1)
    assert result.items == []
    assert result.complete is False
    assert result.error


async def test_a_non_http_start_url_reports_incomplete(monkeypatch):
    result = await crawl_website("ftp://example.com/", max_pages=5, max_depth=1)
    assert result.items == []
    assert result.complete is False


async def test_stopping_at_the_page_cap_reports_incomplete(local_site):
    """Pages beyond the cap were never looked at, so their absence is not
    evidence they were deleted — without this the pages past the cap would
    be deleted and re-crawled on alternating syncs."""
    result = await crawl_website(local_site + "/", max_pages=2, max_depth=3)
    assert len(result.items) == 2
    assert result.complete is False
    assert "limit" in result.error


async def test_a_site_that_fits_well_inside_the_cap_is_complete(local_site):
    result = await crawl_website(local_site + "/", max_pages=50, max_depth=3)
    assert result.complete is True


# ---------------------------------------------------------------------------
# The crawl is bounded on pages FETCHED, not only on pages that had text
# ---------------------------------------------------------------------------

_EMPTY_PAGE_COUNT = 40


class _EmptyPagesHandler(http.server.BaseHTTPRequestHandler):
    """Every page links to the next and none of them contain extractable
    text — image galleries and JavaScript-rendered pages look exactly like
    this. `len(items) < max_pages` counts only pages that PRODUCED text, so
    a site shaped like this was crawled with no effective limit at all."""

    fetches = 0

    def do_GET(self):  # noqa: N802 - http.server's own required name
        type(self).fetches += 1
        try:
            index = int(self.path.strip("/") or 0)
        except ValueError:
            self.send_response(404)
            self.end_headers()
            return
        body = (
            f'<html><head><title>Empty {index}</title></head><body>'
            f'<a href="/{index + 1}"><img src="thumb.png"></a></body></html>'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def empty_page_site(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "allow_private_outbound_urls", True)
    _EmptyPagesHandler.fetches = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EmptyPagesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


async def test_a_site_of_textless_pages_is_still_bounded(empty_page_site):
    """Without a fetch budget this crawl walks the link chain forever —
    the unbounded crawl this module's docstring claims to prevent."""
    result = await crawl_website(empty_page_site + "/0", max_pages=5, max_depth=100)

    assert result.items == []  # nothing had text, correctly
    assert _EmptyPagesHandler.fetches <= 5 * 4 + 2, (
        f"crawled {_EmptyPagesHandler.fetches} pages with a 5-page cap"
    )
    assert result.complete is False


# ---------------------------------------------------------------------------
# An oversized response body is refused rather than read into memory
# ---------------------------------------------------------------------------


class _HugePageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        from app.services.knowledge_sources.website import MAX_PAGE_BYTES

        body = (
            "<html><body><p>"
            + ("padding " * ((MAX_PAGE_BYTES // 8) + 1000))
            + "</p></body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def test_a_page_larger_than_the_limit_is_skipped(monkeypatch):
    """A crawler fetches whatever a customer-configured URL serves, and a
    Content-Type header is not a promise about size."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "allow_private_outbound_urls", True)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HugePageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        result = await crawl_website(base + "/", max_pages=5, max_depth=0)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.items == []
    assert result.complete is False
    assert "size limit" in result.error
