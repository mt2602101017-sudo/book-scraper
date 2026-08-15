"""Collecting cover images without keeping four copies of the same one.

Every store publishes the same artwork under several URLs -- different renditions,
different CDN hosts, different downscale directives -- so de-duplicating by URL
saves the same cover repeatedly. Each store has its own notion of image identity
and its own way of spelling "full size", and both are passed in; the counting,
ordering and capping are the same everywhere and live here.
"""

from __future__ import annotations

import re
from typing import Any, Callable, List, Optional

from .parse import absolutise

#: Names a store uses for "we have no cover for this book".
PLACEHOLDERS = ("no-cover", "nophoto", "no_cover", "blank.g", "placeholder")


class Covers:
    """Ordered, de-duplicated cover URLs for one book from one source.

    :param base: the page URL, for resolving relative ``src`` attributes.
    :param key: maps a URL to the identity of the *artwork*, so two renditions of
        one cover collapse. Defaults to the whole URL.
    :param upgrade: rewrites a URL to its full-size form.
    :param limit: how many to keep.
    """

    def __init__(self, base: str, *, key: Optional[Callable[[str], str]] = None,
                 upgrade: Optional[Callable[[str], str]] = None,
                 limit: int = 6) -> None:
        self.base = base
        self._key = key or (lambda url: url.lower())
        self._upgrade = upgrade or (lambda url: url)
        self.limit = limit
        self._seen: set = set()
        self.urls: List[str] = []

    def add(self, raw: Any) -> bool:
        """Offer one candidate. ``True`` if it was kept."""
        url = absolutise(self.base, raw)
        if not url or len(self.urls) >= self.limit:
            return False
        if any(marker in url.lower() for marker in PLACEHOLDERS):
            return False   # never save a "no cover" image as if it were one
        full = self._upgrade(url)
        identity = self._key(full)
        if not identity or identity in self._seen:
            return False
        self._seen.add(identity)
        self.urls.append(full)
        return True

    def extend(self, candidates: Any) -> None:
        for candidate in candidates or ():
            self.add(candidate)

    @property
    def full(self) -> bool:
        return len(self.urls) >= self.limit

    def __len__(self) -> int:
        return len(self.urls)


def filename_key(url: str) -> str:
    """Identity by last path segment, ignoring its extension and any query."""
    return url.split("?")[0].rstrip("/").rsplit("/", 1)[-1].split(".", 1)[0].lower()


def strip_modifier(pattern: "re.Pattern[str]") -> Callable[[str], str]:
    """An upgrade function that deletes a size directive from the URL."""
    return lambda url: pattern.sub("", url)
