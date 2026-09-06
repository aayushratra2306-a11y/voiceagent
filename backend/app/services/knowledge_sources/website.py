"""Task 7.5 — crawl a customer's own website and pull out readable text.

Deliberately built on the standard library's `html.parser` rather than a
third-party HTML library: this project already avoids adding a dependency
where the stdlib does the job (see tool_registry.py's own hand-rolled URL
handling), and extracting visible text plus links is a small, well-bounded
problem that does not need a full DOM.

Two safety properties matter more than crawl sophistication:

  - **Sensible depth and scope limits**, the manual's own phrasing. Same
    domain only, a page-count cap, and a depth cap — an unbounded crawl
    of an unbounded site is not a knowledge base feature, it is a
    denial-of-service against whichever website a customer points this at
    (and against this server's own outbound bandwidth).
  - **The exact same SSRF check every other outbound request in this
    project already goes through** (`app/core/url_safety.py`). A website
    URL is customer-configured input this server fetches, which is
    identical in shape to a bot tool's URL (task 3.1) or a webhook
    subscription's URL (task 3.8) — the metadata-service threat on this
    GCP VM does not care which feature made the request. Checked on
    every single page in the crawl, not only the starting URL: a page on
    an otherwise legitimate site could contain a link into
    `169.254.169.254`, whether by accident or by a compromised page.

robots.txt is honoured on a best-effort basis: fetched once per crawl (also
through the same SSRF check), and a fetch or parse failure is treated as
"no restrictions stated" rather than refusing to crawl at all — a broken or
absent robots.txt is common and is not itself a signal to stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger

from app.core.url_safety import rejection_reason
from app.services.knowledge_sources import FetchedItem, FetchResult

DEFAULT_MAX_PAGES = 50
DEFAULT_MAX_DEPTH = 3
PAGE_TIMEOUT_SECONDS = 8.0

# A hard ceiling on how many pages are FETCHED, as opposed to how many
# yielded usable text. The page cap alone was not actually a bound on the
# crawl: `len(items) < max_pages` only counts pages that produced text, so
# a site of image galleries, redirects, or JavaScript-rendered pages (all
# of which extract to nothing here) was crawled without any effective
# limit at all — precisely the unbounded crawl this module's own docstring
# claims to prevent. Set well above max_pages so an ordinary site with some
# empty pages still reaches its real page cap.
_FETCH_BUDGET_MULTIPLIER = 4

# Refuse to read an unbounded response body into memory. A crawler fetches
# whatever a customer-configured URL serves, and "it said Content-Type:
# text/html" is not a promise about size — one 2 GB response would take
# this process down and every live call with it.
MAX_PAGE_BYTES = 5 * 1024 * 1024

# Identifies this server honestly to site owners reading their own access
# logs — a customer who connected their own site to this feature should be
# able to recognise the traffic, and a site owner who did NOT expect this
# traffic has something to search for.
USER_AGENT = "VoiceAgentKnowledgeSync/1.0 (+https://github.com/aayushratra2306-a11y/voiceagent)"

_SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template"})


class _TextAndLinkExtractor(HTMLParser):
    """A single pass: visible text, the <title>, and every <a href> link —
    everything website.py needs from one parse, without building a full DOM
    the way a general-purpose HTML library would."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._in_title = False
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self.raw_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.raw_links.append(href)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # A self-closing <a href="..." /> never reaches handle_starttag in
        # some parser code paths — handled explicitly so a link inside
        # XHTML-style markup is not silently dropped.
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.raw_links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        stripped = data.strip()
        if stripped:
            self.text_parts.append(stripped)


def extract_text_and_links(html: str, base_url: str) -> tuple[str, str, list[str]]:
    """(title, visible text, absolute+de-fragmented links). A pure function
    deliberately — the actual HTML parsing is what a test can drive
    directly and exhaustively, with no network involved at all."""
    parser = _TextAndLinkExtractor()
    try:
        parser.feed(html)
    except Exception as e:
        # Malformed markup must degrade to "nothing extracted", not crash a
        # crawl over one bad page on an otherwise fine site.
        logger.warning(f"[CRAWL] could not parse HTML from {base_url}: {e}")
        return "", "", []

    title = " ".join(parser.title_parts).strip()
    text = "\n".join(parser.text_parts)

    links: list[str] = []
    for href in parser.raw_links:
        try:
            absolute = urljoin(base_url, href)
        except ValueError:
            continue
        links.append(absolute.split("#", 1)[0])  # fragments are not separate pages
    return title, text, links


async def _load_robots(scheme: str, netloc: str) -> robotparser.RobotFileParser | None:
    robots_url = f"{scheme}://{netloc}/robots.txt"
    if rejection_reason(robots_url):
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(robots_url)
        if resp.status_code >= 400:
            return None
        parser = robotparser.RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser
    except Exception as e:
        logger.info(f"[CRAWL] could not load robots.txt for {netloc}: {e}")
        return None


@dataclass
class _CrawlState:
    visited: set[str]
    items: list[FetchedItem]


async def crawl_website(
    start_url: str,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> FetchResult:
    """Breadth-first, same-domain, bounded on both pages and depth.

    Never raises: one unreachable page, one malformed one, or the whole
    site being briefly down should mean fewer results, not a failed sync
    that leaves every OTHER source untouched.

    What it DOES report is whether the crawl actually saw the whole site
    (`FetchResult.complete`). A network error, a 5xx, a rate-limit, or
    simply running into the page cap all mean pages were missed — and the
    caller must not read a missed page as a deleted one. See FetchResult's
    own comment for what happened when it could not tell the difference.

    A 4xx other than 429 deliberately does NOT mark the crawl incomplete:
    a linked page returning 404 is the ordinary way a website says that
    page is gone, which is exactly the signal a sync SHOULD act on.
    """
    parsed_start = urlparse(start_url)
    if parsed_start.scheme not in ("http", "https") or not parsed_start.netloc:
        logger.warning(f"[CRAWL] not a fetchable URL: {start_url!r}")
        return FetchResult(items=[], complete=False, error=f"not a fetchable URL: {start_url!r}")

    allowed_netloc = parsed_start.netloc
    robots = await _load_robots(parsed_start.scheme, allowed_netloc)

    state = _CrawlState(visited=set(), items=[])
    queue: list[tuple[str, int]] = [(start_url.split("#", 1)[0], 0)]
    fetch_budget = max(max_pages * _FETCH_BUDGET_MULTIPLIER, max_pages)
    pages_fetched = 0
    problems: list[str] = []

    async with httpx.AsyncClient(
        timeout=PAGE_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT},
        follow_redirects=False,  # each hop re-checked below, same reasoning as tool_registry.py
    ) as client:
        while queue and len(state.items) < max_pages and pages_fetched < fetch_budget:
            url, depth = queue.pop(0)
            if url in state.visited:
                continue
            state.visited.add(url)

            if urlparse(url).netloc != allowed_netloc:
                continue  # a link led off-site; scope is same-domain only

            if robots is not None and not robots.can_fetch(USER_AGENT, url):
                logger.info(f"[CRAWL] robots.txt disallows {url}")
                continue

            unsafe = rejection_reason(url)
            if unsafe:
                logger.warning(f"[CRAWL] refused {url} — {unsafe}")
                continue

            pages_fetched += 1
            try:
                response = await client.get(url)
            except Exception as e:
                logger.info(f"[CRAWL] could not fetch {url}: {type(e).__name__}: {e}")
                problems.append(f"{url} unreachable ({type(e).__name__})")
                continue

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if location and depth < max_depth:
                    target = urljoin(url, location).split("#", 1)[0]
                    queue.append((target, depth))  # re-checked from the top on its own turn
                continue
            if response.status_code >= 400:
                # 5xx and 429 are the site failing to serve a page it may
                # well still have; any other 4xx is the site saying the page
                # is genuinely not there.
                if response.status_code >= 500 or response.status_code == 429:
                    problems.append(f"{url} returned {response.status_code}")
                continue
            if "text/html" not in response.headers.get("content-type", ""):
                continue
            if len(response.content) > MAX_PAGE_BYTES:
                logger.warning(
                    f"[CRAWL] skipping {url} — {len(response.content)} bytes exceeds "
                    f"the {MAX_PAGE_BYTES}-byte page limit"
                )
                problems.append(f"{url} exceeded the page size limit")
                continue

            title, text, links = extract_text_and_links(response.text, url)
            if text.strip():
                state.items.append(FetchedItem(
                    external_id=url, url=url, title=title or url, text=text,
                ))

            if depth < max_depth:
                for link in links:
                    if link not in state.visited and urlparse(link).netloc == allowed_netloc:
                        queue.append((link, depth + 1))

    # Stopping early because a cap was reached means there are pages on this
    # site this crawl never looked at — so anything already stored that did
    # not come back this time might simply be one of them, not a page the
    # customer deleted.
    hit_a_cap = bool(queue) and (len(state.items) >= max_pages or pages_fetched >= fetch_budget)
    if hit_a_cap:
        problems.append(
            f"stopped at the crawl limit ({len(state.items)} pages kept, "
            f"{pages_fetched} fetched) with more pages still queued"
        )

    if problems:
        return FetchResult(
            items=state.items, complete=False, error="; ".join(problems[:5]),
        )
    return FetchResult(items=state.items, complete=True)
