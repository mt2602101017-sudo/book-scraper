"""Amazon helpers: the product-detail map, the image island, and review parsing.

Split out of ``amazon.py`` for size. The underscore keeps it out of discovery.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from ..extract import iso_date, loads
from ..models import Review
from ..parse import dedupe, review, text

BASE = "https://www.amazon.com"

#: **The single most load-bearing expression in this adapter.** Amazon renders
#: every detail label as ``'Publisher ‏ : ‎'`` -- LRM, RLM, ZWSP, the
#: U+202A..U+202E range and a BOM. Without stripping them, every label match fails
#: and publisher, date, language, ISBN and the rank genres all come back null.
_BIDI = re.compile("[‎‏​‪-‮﻿]")

#: Amazon inlines the paragraph break inside the *following* span, so ``<br>`` has
#: to become a newline before extraction; and the separator must be empty, or
#: ``21<sup>st</sup>`` extracts as "21 st".
_READ_MORE = re.compile(r"\s*Read (?:more|less)\s*$")
#: Boilerplate prepended to every ``reviewText`` body.
_A11Y_NOISE = re.compile(
    r"(?:Brief|Full) content visible, double tap to read (?:full|brief) content\.\s*",
    re.IGNORECASE)
_REVIEW_DATE = re.compile(r"Reviewed in (?P<country>.+?) on (?P<date>.+?)\s*$",
                          re.IGNORECASE)

#: The inline image island. A non-greedy regex parses the single-image case and
#: silently truncates multi-image products, so this only finds the opening brace
#: and a balanced-bracket walk does the rest.
_COLOR_IMAGES = re.compile(r"['\"]colorImages['\"]\s*:\s*\{\s*['\"]initial['\"]\s*:\s*")
#: ``._SL1500_`` and friends are downscale directives; stripping them yields the
#: ~1.55x master image.
_IMAGE_MODIFIER = re.compile(r"\._[A-Za-z0-9_,%+-]+_\.(jpe?g|png)$", re.IGNORECASE)

_BULLET_SELECTORS = ("#detailBullets_feature_div li span.a-list-item",
                     "#detailBulletsWrapper_feature_div li span.a-list-item")
_TABLE_SELECTORS = ("table.prodDetTable tr", "#productDetails_detailBullets_sections1 tr",
                    "#productDetails_techSpec_section_1 tr", "table.a-keyvalue tr")


def label_of(raw: Any) -> str:
    """Clean a detail label: strip bidi marks, NBSP, a trailing colon."""
    cleaned = _BIDI.sub("", str(raw or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", cleaned).strip().rstrip(":").strip()


def value_of(node: Any) -> str:
    """Clean a detail value. Amazon inlines ``<script>`` inside detail bullets."""
    if node is None:
        return ""
    clone = BeautifulSoup(str(node), "html.parser")
    for junk in clone.find_all(["script", "style", "noscript"]):
        junk.decompose()
    return label_of(clone.get_text(" ")).rstrip(":").strip()


def prose(node: Any) -> str:
    """Extract blurb/review prose: ``<br>`` first, then an *empty* separator.

    Amazon welds paragraphs across spans (``...this yet.</span><span><br/><br/>So
    begins...``), so br-first fixes "this yet.So begins"; and a space separator
    would break intra-word inline markup.
    """
    if node is None:
        return ""
    clone = BeautifulSoup(str(node), "html.parser")
    for junk in clone.find_all(["script", "style", "noscript"]):
        junk.decompose()
    for br in clone.find_all("br"):
        br.replace_with("\n")
    return _READ_MORE.sub("", text(clone.get_text(""))).strip()


def details(soup: Any) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Both product-detail layouts, **merged** -- not tried as fallbacks.

    Amazon sometimes renders the bullets *and* the table, printing a given label
    in only one of them ("Country of Origin" is the case that matters). Bullets
    win on conflict. The container node is kept per label too, because the
    Best-Sellers-Rank genre extraction needs the node, not its text.
    """
    values: Dict[str, str] = {}
    nodes: Dict[str, Any] = {}
    for selector in _TABLE_SELECTORS:
        for row in soup.select(selector):
            head, cell = row.find("th"), row.find("td")
            if head is not None and cell is not None and (key := label_of(head.get_text(" "))):
                values.setdefault(key, value_of(cell))
                nodes.setdefault(key, row)
    for selector in _BULLET_SELECTORS:
        for item in soup.select(selector):
            bold = item.select_one("span.a-text-bold")
            if bold is None or not (key := label_of(bold.get_text(" "))):
                continue
            values[key] = value_of(bold.find_next_sibling("span"))
            nodes[key] = item
    return values, nodes


def bullet(values: Dict[str, str], *labels: str) -> Optional[str]:
    """The first detail value whose label matches, compared after cleaning."""
    folded = {k.casefold(): v for k, v in values.items()}
    return next((v for label in labels
                 if (v := folded.get(label_of(label).casefold()))), None)


def image_island(html: str) -> List[Dict[str, Any]]:
    """Decode the ``colorImages.initial`` array with a balanced-bracket walk."""
    match = _COLOR_IMAGES.search(html)
    if match is None:
        return []
    start = html.find("[", match.end() - 1)
    if start < 0:
        return []
    depth, in_string, escaped, quote = 0, False, False, ""
    for index in range(start, min(len(html), start + 500_000)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in "\"'":
            in_string, quote = True, char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                found = loads(html[start:index + 1])
                return [e for e in found or [] if isinstance(e, dict)]
    return []


def physical_id(url: str) -> str:
    """``.../I/81MDdbYh-8L._SL1500_.jpg`` -> ``81MDdbYh-8L``.

    Required: Amazon publishes the same art under different physical ids per
    rendition, so de-duplicating by URL keeps four copies of one cover.
    """
    return url.rsplit("/", 1)[-1].split(".", 1)[0]


def master(url: str) -> str:
    """Strip the downscale directive to get the master image."""
    return _IMAGE_MODIFIER.sub(lambda m: "." + m.group(1), url)


def parse_reviews(soup: Any, url: str, seen: set) -> List[Review]:
    """Every review on this page. Listing pages use ``li``, detail pages ``div``."""
    out: List[Review] = []
    for node in soup.select('div[data-hook="review"], li[data-hook="review"]'):
        body_node = (node.select_one('div[data-hook="reviewRichContentContainer"]')
                     or node.select_one('span[data-hook="review-body"]')
                     or node.select_one('div[data-hook="reviewText"]'))
        body = _A11Y_NOISE.sub("", prose(body_node)).strip()
        if not body:
            continue
        heading_node = (node.select_one('h5[data-hook="reviewTitle"]')
                        or node.select_one('[data-hook="review-title"]'))
        heading = text(heading_node)
        stem = heading.rstrip(". ")
        # The rich-content container often already repeats the title.
        if heading and not body.casefold().startswith(stem[:40].casefold()):
            body = f"{heading}\n\n{body}"

        node_id = (node.get("id") or "").strip()
        fingerprint = node_id or f"body:{body[:160].casefold()}"
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        raw_date = text(node.select_one('span[data-hook="review-date"]'))
        # "Reviewed in the United States on July 5, 2022" -> the date part, in ISO.
        match = _REVIEW_DATE.search(raw_date)
        item = review(
            body,
            reviewer=text(node.select_one("span.a-profile-name")),
            # ``cmps-`` is the cross-marketplace variant of the rating widget.
            rating=text(node.select_one('i[data-hook="review-star-rating"] span.a-icon-alt')
                        or node.select_one('i[data-hook="cmps-review-star-rating"] span.a-icon-alt')),
            date=iso_date(match.group("date") if match else raw_date),
            url=f"{BASE}/gp/customer-reviews/{node_id}" if node_id else None,
            min_chars=2)
        if item is not None:
            out.append(item)
    return dedupe(out)
