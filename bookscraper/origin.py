"""``origin`` -- the place of publication, and the one field no store publishes.

Kept as a real probe rather than a hard-coded ``null`` for two reasons: the
warning explaining the null is then *derived from the search* instead of asserted
beside it, and the field self-heals the day a site starts printing it.

It has never yet fired. ``origin`` is null in all 25 571 records scraped so far,
which is a finding about the storefronts, not a defect here -- and it is never
inferred from the publisher's imprint, the store locale or the delivery country,
because that would be a guess wearing a scraped value's clothes.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Iterator, List, Optional, Sequence, Tuple

from bs4 import Tag

from .parse import text

_KEY_JUNK = re.compile(r"[^0-9a-z]+")

#: Every spelling that genuinely means "place of publication", folded by
#: :func:`fold`. Country-*shaped* keys are deliberately absent: ``eligibleRegion``,
#: ``regionsAllowed``, ``priceCurrency`` and a storefront's ``#reviewsCountry``
#: are sales territory and distribution licensing, never an origin. Matching is
#: exact on the folded token -- never a substring -- so they cannot creep in, and
#: neither can ``datePublished`` or ``publisher``.
KEYS = frozenset({"placeofpublication", "publicationplace", "publishplace",
                  "publishplaces", "placepublished", "countryoforigin",
                  "publicationcountry", "publishcountry", "countryregionoforigin",
                  "publishedin"})
#: Keys that render a place out of ``{"@type": "Place", "name": "London"}``.
_PLACE_KEYS = ("name", "@value", "value", "text", "addressCountry", "addressLocality")


def fold(raw: Any) -> str:
    """``"Country of Origin ‏ : ‎"`` -> ``"countryoforigin"``.

    Case, whitespace, punctuation, bidi marks and separators all disappear, so
    one entry covers its camelCase, snake_case and colon-suffixed variants.
    """
    return _KEY_JUNK.sub("", str(raw or "").casefold())


def walk(node: Any, max_depth: int = 8, max_nodes: int = 20000
         ) -> Iterator[Tuple[str, str, Any]]:
    """Yield ``(path, key, value)`` for every mapping entry inside ``node``.

    Breadth-first and bounded, so a shallow key beats a deep one, containers are
    visited once by identity (a self-referencing blob still terminates), and the
    walk stops after ``max_nodes``. Deliberately unshockable: never raises.
    """
    budget = max_nodes
    queue: deque = deque([("", node, 0)])
    seen = {id(node)}
    while queue and budget > 0:
        here, current, depth = queue.popleft()
        budget -= 1
        if isinstance(current, (str, bytes, Tag)) or depth > max_depth:
            continue
        if isinstance(current, dict):
            for key, value in list(current.items()):
                child = f"{here}.{key}" if here else str(key)
                yield child, str(key), value
                if isinstance(value, (dict, list, tuple)) and id(value) not in seen:
                    seen.add(id(value))
                    queue.append((child, value, depth + 1))
        elif isinstance(current, (list, tuple)):
            for index, value in enumerate(current):
                if isinstance(value, (dict, list, tuple)) and id(value) not in seen:
                    seen.add(id(value))
                    queue.append((f"{here}[{index}]", value, depth + 1))


def place(value: Any, depth: int = 0) -> str:
    """Render a probe hit as a place string, or ``''`` when it is not one.

    Numbers, booleans, markup, and anything longer than a place name could
    plausibly be are all rejected, so a hit is only reported when it reads as
    an answer.
    """
    if value is None or isinstance(value, (bool, int, float)) or depth > 3:
        return ""
    if isinstance(value, dict):
        return next((p for k in _PLACE_KEYS if k in value
                     and (p := place(value[k], depth + 1))), "")
    if isinstance(value, (list, tuple, set)):
        return next((p for item in list(value)[:8] if (p := place(item, depth + 1))), "")
    found = " ".join(text(value).strip(" \t\"'`{}[],;:|-–—").split())
    if not 2 <= len(found) <= 120 or found.startswith(("{", "[", "<")):
        return ""
    return "" if found.casefold() in ("null", "none", "n/a") else found


def _from_dom(node: Tag) -> Optional[str]:
    """A place from ``itemprop``/``meta`` attributes, then a details-table label."""
    for tag in node.find_all(["meta"], limit=2000):
        for attr in ("property", "name", "itemprop"):
            if fold(tag.get(attr)) in KEYS and (found := place(tag.get("content"))):
                return found
    for tag in node.find_all(attrs={"itemprop": True}, limit=2000):
        if fold(tag.get("itemprop")) in KEYS:
            if found := (place(tag.get("content")) or place(tag)):
                return found
    for label in node.find_all(string=True, limit=4000):
        raw = str(label)
        head, _, rest = raw.partition(":")
        if len(raw) > 200 or fold(head) not in KEYS:
            continue
        parent = label.parent
        if not isinstance(parent, Tag) or parent.name in ("script", "style", "noscript"):
            continue
        # The value sits inline after the colon, in the next sibling, or -- for a
        # details table -- in the cell the label heads.
        after = parent.find_next_sibling() or parent.find_next(("td", "dd"))
        if found := (place(rest) or place(after)):
            return found
    return None


def probe(layers: Sequence[Tuple[str, Any]]) -> Tuple[Optional[str], List[str]]:
    """Search ``layers`` for a place of publication, most authoritative first.

    :param layers: ``(name, payload)`` pairs. ``payload`` may be a JSON blob or a
        ``bs4`` node; empty and absent payloads are skipped, because a layer that
        was not on the page was not searched.
    :returns: ``(value, searched)`` -- the place if one was genuinely found, plus
        the names of the layers really searched, in order. That list is what an
        ``origin`` warning is built from, so the sentence cannot outlive the code.
    """
    searched: List[str] = []
    for name, payload in layers or ():
        try:
            if isinstance(payload, Tag):
                if payload.find(True) is None:
                    continue
                searched.append(name)
                if found := _from_dom(payload):
                    return found, searched
            elif isinstance(payload, (dict, list, tuple)) and payload:
                searched.append(name)
                for _, key, value in walk(payload):
                    if fold(key) in KEYS and (found := place(value)):
                        return found, searched
        except Exception:  # noqa: BLE001 - a probe must never break a scrape
            continue
    return None, searched


def clause(searched: Sequence[str]) -> str:
    """Render a probe's searched list as an English list, honestly when empty.

    "No layer was available" and "every layer was read and had no such field" are
    different facts about a run, so they get different sentences.
    """
    names = [n for n in (str(s).strip() for s in searched or []) if n]
    if not names:
        return "no parsed layer was available to search on this run"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
