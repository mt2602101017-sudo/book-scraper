"""Goodreads helpers: the editions listing, and paging reviews past the first ~30.

Split out of ``goodreads.py`` only for size; nothing here is used elsewhere. The
leading underscore also keeps it out of adapter auto-discovery.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..extract import deref, iso_date
from ..http import HttpClient
from ..models import Review
from ..parse import absolutise, html_text, review, text


def _published(millis: Any) -> Optional[str]:
    """Review timestamps are epoch-ms in UTC (unlike publication dates)."""
    from ._goodreads_fields import published
    return published(millis, pacific=False)

BASE = "https://www.goodreads.com"
#: 10 edition rows per page; two pages is enough for the six-cover cap.
ROWS_PER_PAGE = 10
MAX_EDITION_PAGES = 2
GRAPHQL_PAGE_SIZE = 100
MAX_REVIEW_PAGES = 5
MIN_REVIEW_CHARS = 20

_PUBLISHED_BY = re.compile(r"^published\s+(.*?)(?:\s+by\s+(.+))?$", re.IGNORECASE | re.DOTALL)
_EDITION_TOTAL = re.compile(r"Showing\s+[\d,]+\s*-\s*[\d,]+\s+of\s+([\d,]+)")
_ISBN13 = re.compile(r"\b(97[89][0-9]{10})\b")
_ISBN10 = re.compile(r"ISBN10:\s*([0-9]{9}[0-9Xx])")

#: Downscale directives (``._SY75_``) and the size class in ``/books/<ts><class>/``
#: both have to be rewritten or every saved cover is a thumbnail.
_SIZE_SUFFIX = re.compile(r"\._S[XY]\d+_(?=\.[A-Za-z0-9]{2,5}$)")
_SIZE_CLASS = re.compile(r"(/books/\d+)[a-z](?=/)")
_IMAGE_KEY = re.compile(r"/books/(.+)$")
_PLACEHOLDERS = ("no-cover", "nophoto", "no_cover", "blank.g")

#: Recovered from Goodreads' own bundle. Schema introspection is blocked, so
#: added fields cannot be validated -- keep this document byte-identical.
_REVIEWS_QUERY = """
query getReviews($filters: BookReviewsFilterInput!, $pagination: PaginationInput) {
  getReviews(filters: $filters, pagination: $pagination) {
    totalCount
    pageInfo { prevPageToken nextPageToken }
    edges { node { id rating createdAt text creator { name webUrl } } }
  }
}
"""
_APP_CHUNK = re.compile(r"/_next/static/chunks/pages/_app-[A-Za-z0-9_.-]+\.js")
_APPSYNC_KEY = re.compile(r"""apiKey["']?\s*:\s*["'](da2-[a-z0-9]+)["']""")
_APPSYNC_ENDPOINT = re.compile(
    r"""endpoint["']?\s*:\s*["'](https://[A-Za-z0-9.\-/]+appsync-api[A-Za-z0-9.\-/]*)["']""")


def clean_image(base: str, candidate: Any) -> Optional[str]:
    """Absolutise a cover URL, reject placeholders, upgrade it to full size."""
    url = absolutise(base, candidate)
    if not url or any(marker in url.lower() for marker in _PLACEHOLDERS):
        return None
    return _SIZE_CLASS.sub(r"\1i", _SIZE_SUFFIX.sub("", url))


def image_key(url: str) -> str:
    """Identity of a cover across the two Goodreads CDN hostnames."""
    match = _IMAGE_KEY.search(url)
    return match.group(1).lower() if match else url.lower()


def rating_of(raw: Any) -> Optional[str]:
    """``4`` -> ``"4/5"``; anything else cleaned, or ``None``."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw:
        return f"{int(raw)}/5"
    return text(raw) or None


def editions(client: HttpClient, work_id: str, wanted_covers: int
             ) -> Tuple[List[str], Dict[str, Any], List[Any]]:
    """Walk ``/work/editions/<id>`` for extra covers and the missing details.

    Returns ``(cover_urls, details, soups)``. ``details`` carries whatever
    ``publisher``/``date``/``language`` the rows reveal -- the book page markup
    carries no publisher at all, so this listing is the only fallback. ``soups``
    goes to the origin probe, so the probe reports a layer it really read.
    """
    covers: List[str] = []
    details: Dict[str, Any] = {}
    soups: List[Any] = []
    pages = min(MAX_EDITION_PAGES, max(1, -(-max(0, wanted_covers) // ROWS_PER_PAGE)))
    for number in range(1, pages + 1):
        url = f"{BASE}/work/editions/{work_id}" + (f"?page={number}" if number > 1 else "")
        soup = client.soup(url, referer=BASE)
        if soup is None:
            break
        soups.append(soup)
        rows = soup.select("div.elementList.clearFix")
        for row in rows:
            if (node := row.select_one("div.leftAlignedImage img")) is not None:
                if found := clean_image(url, node.get("src")):
                    covers.append(found)
            for data_row in row.select("div.dataRow"):
                label_node = data_row.select_one("div.dataTitle")
                value = text(data_row)
                if label_node is None:
                    # An unlabelled row is the "published <date> by <publisher>" one.
                    if (match := _PUBLISHED_BY.match(value)) and value.lower().startswith("published"):
                        details.setdefault("date", iso_date(match.group(1)))
                        if match.group(2):
                            details.setdefault("publisher", text(match.group(2)))
                    continue
                label = text(label_node).rstrip(":").lower()
                value = text(data_row).replace(text(label_node), "", 1).strip()
                if label == "edition language":
                    details.setdefault("language", value)
        total = _EDITION_TOTAL.search(text(soup.get_text(" ")))
        if len(rows) < ROWS_PER_PAGE or (total and number * ROWS_PER_PAGE >= int(
                total.group(1).replace(",", ""))):
            break
    return covers, details, soups


def embedded_reviews(state: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """The ~30 reviews already in ``__NEXT_DATA__``, in Goodreads' own ranking."""
    root = (state.get("ROOT_QUERY") or {}).get("getReviews")
    edges = (root or {}).get("edges") if isinstance(root, dict) else None
    if edges:
        for edge in edges:
            if node := deref(state, edge.get("node") if isinstance(edge, dict) else None):
                yield node
        return
    # Unordered fallback: the nodes are in the cache even when the edge list is not.
    for key, value in state.items():
        if key.startswith("Review:") and isinstance(value, dict):
            yield value


def appsync(client: HttpClient, soup: Any, page_url: str) -> Optional[Tuple[str, str]]:
    """Recover the anonymous AppSync ``(endpoint, api_key)`` from the live bundle.

    Never hardcoded: Goodreads rotates the key, so it is re-derived per book from
    the ``_app-*.js`` chunk the page itself loads.
    """
    srcs = [s for node in soup.find_all("script", src=True)
            if (s := _APP_CHUNK.search(node.get("src") or ""))]
    if not srcs:
        return None
    response = client.get(absolutise(page_url, srcs[0].group(0)), referer=page_url)
    if response is None:
        return None
    body = response.text or ""
    index = -1
    for _ in range(20):
        index = body.find("Production", index + 1)
        if index < 0:
            break
        window = body[index:index + 2000]
        key, endpoint = _APPSYNC_KEY.search(window), _APPSYNC_ENDPOINT.search(window)
        if key and endpoint:
            return endpoint.group(1), key.group(1)
    return None


def paged_reviews(client: HttpClient, soup: Any, page_url: str, resource: Tuple[str, str],
                  token: Optional[str], needed: int, seen: set) -> List[Review]:
    """Page reviews past the embedded ~30 through Goodreads' GraphQL endpoint."""
    found = appsync(client, soup, page_url)
    if found is None:
        return []
    endpoint, api_key = found
    resource_type, resource_id = resource
    out: List[Review] = []
    for _ in range(MAX_REVIEW_PAGES):
        if len(out) >= needed:
            break
        pagination: Dict[str, Any] = {
            "limit": max(1, min(GRAPHQL_PAGE_SIZE, needed - len(out) + 5))}
        if token:
            pagination["after"] = token
        data = client.post_json(endpoint, {
            "query": _REVIEWS_QUERY,
            "variables": {"filters": {"resourceType": resource_type,
                                      "resourceId": resource_id},
                          "pagination": pagination}},
            headers={"X-Api-Key": api_key, "Origin": BASE, "Referer": page_url})
        if not isinstance(data, dict) or data.get("errors"):
            break
        block = (data.get("data") or {}).get("getReviews")
        if not isinstance(block, dict) or not (edges := block.get("edges")):
            break
        for edge in edges:
            node = (edge or {}).get("node") or {}
            body = html_text(node.get("text"))
            keys = {str(node.get("id") or ""), re.sub(r"\W+", " ", body[:200]).strip().lower()}
            if not body or keys & seen:
                continue
            item = review(body, reviewer=(node.get("creator") or {}).get("name"),
                          rating=rating_of(node.get("rating")),
                          date=_published(node.get("createdAt")), min_chars=MIN_REVIEW_CHARS)
            if item is not None:
                seen |= keys
                out.append(item)
        if not (token := (block.get("pageInfo") or {}).get("nextPageToken")):
            break
    return out


def strip_librarian_note(body: str) -> str:
    """Drop the "Librarian's note:" editorial preamble Goodreads blurbs carry."""
    pattern = re.compile(r"Librarian'?s note\s*:", re.IGNORECASE)
    match = pattern.search(body)
    if match is None:
        return body
    if match.start() > 0:
        return body[:match.start()].strip() or body
    rest = body[match.end():]
    # The note opens the blurb: skip past its paragraph, then any later note.
    break_at = re.search(r"\n\s*\n", rest)
    remainder = rest[break_at.end():] if break_at else rest.split("\n", 1)[-1]
    return pattern.split(remainder, maxsplit=1)[0].strip() or body
