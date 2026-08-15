"""Resolving a Goodreads book page, and confirming it is the right book.

``/book/isbn/<isbn>`` 301s to the book page and is the one route robots.txt allows;
``/search`` is disallowed for generic crawlers, which is why it is the last resort.
ISBN-10 is a genuine second rung -- many older editions resolve only under it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..extract import deref, jsonld, script_json
from ..http import soup_of
from ..match import best
from ..models import Hint, Result
from ..parse import absolutise, sel, sels, text

BASE = "https://www.goodreads.com"
_LEGACY_ID = re.compile(r"/book/show/(\d+)")
_WORK_ID = re.compile(r"/work/(?:editions|quotes|shelves|)/?(\d+)")
#: Trim a series suffix and an em-dash tagline before searching or seeding.
_SERIES_SUFFIX = re.compile(r"\s*\([^()]*#\s*\d+[^()]*\)\s*$")


@dataclass
class Page:
    """A resolved book page and the Apollo cache it carries."""

    url: str
    soup: Any
    state: Dict[str, Any] = field(default_factory=dict)
    book: Dict[str, Any] = field(default_factory=dict)
    work: Dict[str, Any] = field(default_factory=dict)
    #: True when the page was matched by title rather than by ISBN.
    fuzzy: bool = False

    @property
    def details(self) -> Dict[str, Any]:
        found = self.book.get("details")
        return found if isinstance(found, dict) else {}


def query(title: str) -> str:
    """Normalise a title for searching: drop a tagline and a series number."""
    trimmed = re.split(r"\s+[–—]\s+", title)[0].strip()
    return (_SERIES_SUFFIX.sub("", trimmed).strip() or title)[:180]


def _legacy_id(soup: Any, url: str) -> str:
    if match := _LEGACY_ID.search(url):
        return match.group(1)
    link = soup.find("link", attrs={"rel": "canonical"})
    match = _LEGACY_ID.search((link.get("href") or "") if link else "")
    return match.group(1) if match else ""


def work_id(page: Page) -> str:
    """The work id the editions listing is addressed by."""
    if legacy := text(page.work.get("legacyId")):
        return legacy
    candidates = [(page.work.get("editions") or {}).get("webUrl"),
                  (page.work.get("details") or {}).get("webUrl")]
    candidates += [a.get("href") for a in page.soup.select('a[href*="/work/"]')]
    for candidate in candidates:
        if match := _WORK_ID.search(str(candidate or "")):
            return match.group(1)
    return ""


def _book_node(state: Dict[str, Any], legacy_id: str) -> Dict[str, Any]:
    """The real Book node, not one of the many title-less stubs in the cache.

    Most ``Book:`` entries in ``apolloState`` are stubs, so resolution must go
    through ``ROOT_QUERY``. The legacyId argument is a JSON *string* inside the
    serialised field key.
    """
    root = state.get("ROOT_QUERY") or {}
    ref = root.get('getBookByLegacyId({"legacyId":"%s"})' % legacy_id) or next(
        (v for k, v in root.items() if k.startswith("getBookByLegacyId(")), None)
    return deref(state, ref)


def _verify(soup: Any, book: Dict[str, Any], hint: Hint, fuzzy: bool) -> bool:
    """Does this page's own ISBN agree with the one we asked for?"""
    details = book.get("details") or {}
    found13 = text(details.get("isbn13")).replace("-", "")
    if not found13:
        for blob in jsonld(soup, "Book"):
            candidate = text(blob.get("isbn")).replace("-", "")
            if len(candidate) == 13:
                found13 = candidate
                break
    if found13 and hint.isbn13 and found13 != hint.isbn13:
        return fuzzy      # a fuzzy match may legitimately be another edition
    return True


def _by_title(client: Any, hint: Hint, result: Result) -> List[str]:
    """Title-field search URLs, best match first. Needs authors to verify with.

    Deliberately a title-*field* search plus explicit author matching: putting
    title and author in one ``q`` ranks study guides and "15-minute summary"
    cash-ins above the real book.
    """
    if not (hint.title and hint.authors):
        return []
    soup = client.soup(f"{BASE}/search", referer=BASE, params={
        "q": query(hint.title), "search[field]": "title"})
    rows = []
    for row in (soup.select("tr[itemtype], table.tableList tr") if soup else []):
        link = row.select_one('a.bookTitle[itemprop="url"], a.bookTitle')
        if link is None:
            continue
        rows.append((text(link.select_one('span[itemprop="name"]') or link),
                     sels(row, 'a.authorName > span[itemprop="name"]', "a.authorName"),
                     absolutise(BASE, (link.get("href") or "").split("?")[0])))
    ranked = best(hint.title, hint.authors, rows, floor=0.80)
    if ranked:
        result.warn("goodreads: the ISBN did not resolve, so this book was matched by "
                    "title and author -- a weaker identification than an ISBN")
    return [url for _, url in ranked[:3]]


def load(client: Any, hint: Hint, result: Result) -> Optional[Page]:
    """Resolve and parse the book page, or ``None`` if no candidate holds up."""
    candidates: List[Tuple[str, bool]] = [(f"{BASE}/book/isbn/{hint.isbn13}", False)]
    if hint.isbn10 and hint.isbn10 != hint.isbn13:
        candidates.append((f"{BASE}/book/isbn/{hint.isbn10}", False))
    candidates += [(url, True) for url in _by_title(client, hint, result)]

    for url, fuzzy in candidates:
        response = client.get(url, referer=BASE)
        soup = None
        if response is not None:
            soup = soup_of(response.text, str(response.url))
            url = str(response.url or url)
        elif client.block_reason(url) and client.browser.available:
            soup = client.rendered(url, wait_css="h1")   # WAF recovery only
        if soup is None:
            continue

        payload = script_json(soup, "__NEXT_DATA__") or {}
        state = (((payload.get("props") or {}).get("pageProps") or {})
                 .get("apolloState") or {})
        # There must never be a bare h1 fallback here: the anti-bot interstitial's
        # <noscript> says "<h1>JavaScript is disabled</h1>", so a loose selector
        # "succeeds" with garbage and files an all-null record.
        if not state and not sel(soup, 'h1[data-testid="bookTitle"]'):
            continue
        book = _book_node(state, _legacy_id(soup, url))
        if not _verify(soup, book, hint, fuzzy):
            continue
        if "/book/show/" in (web := text(book.get("webUrl")) or ""):
            url = web    # canonical; never og:url, which names another edition
        return Page(url=url, soup=soup, state=state, book=book,
                    work=deref(state, book.get("work")), fuzzy=fuzzy)
    return None
