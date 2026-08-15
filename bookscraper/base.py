"""The :class:`BaseSource` contract every site adapter implements.

An adapter subclasses :class:`BaseSource`, sets ``name``/``display_name``, and
implements :meth:`BaseSource.find_book_url` and :meth:`BaseSource.scrape`. It is
picked up automatically by :func:`bookscraper.sources.discover_sources` -- there
is no registry to edit.

Three rules adapters must honour:

1. :meth:`scrape` **never raises**. A missing selector appends a string to
   ``result.warnings`` and carries on; a partial :class:`ScrapeResult` is a
   success, not a failure.
2. No scraped value is ever hard-coded. Every field comes from a live parse.
3. A warning is **derived from the page, not asserted about it**. If a warning
   names the layers it searched or counts something the page contains, the code
   must have done that searching and that counting on this run -- see
   :meth:`BaseSource.probe_origin`, whose ``searched`` list is what the
   ``origin`` warnings are built from. A hand-written claim about live page
   content is a hard-coded scraped value wearing prose.

Everything in the "helpers" section below is shared parsing plumbing that
subclasses should reuse rather than re-inventing.
"""

from __future__ import annotations

import sys
from .verbosity import verbose
import html as html_module
import json
import re
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime
from typing import (
    Any, Dict, Iterable, Iterator, List, NamedTuple, Optional, Sequence, Tuple, Union,
)
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from .http_client import HttpClient
from .models import BookHint, BookMetadata, ReviewItem, ScrapeResult

__all__ = [
    "BaseSource",
    "JsonPair",
    "OriginProbe",
    "ORIGIN_KEYS",
    "ORIGIN_KEY_SPELLINGS",
    "normalise_key",
]

_WS_RE = re.compile(r"[^\S\n]+")
_BLANKS_RE = re.compile(r"\n{3,}")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Date layouts we try, most specific first, when normalising to ISO-8601.
_DATE_FORMATS: Sequence[str] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %Y",
    "%b %Y",
    "%Y-%m",
)

#: Splitters for "Fiction, Fantasy; Young Adult / Romance" style lists.
_LIST_SPLIT_RE = re.compile(r"\s*(?:[,;/|]|\band\b|&)\s*", re.IGNORECASE)

#: Ordinal/number tokens that turn one title into its sequel. When two otherwise
#: identical titles disagree only here, they are different books -- see
#: :meth:`BaseSource.is_sequel_pair`. Shared because Amazon and BookBub both need
#: it and had drifted into keeping identical private copies.
ORDINAL_TOKENS: Dict[str, str] = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6", "7": "7",
    "8": "8", "9": "9", "10": "10",
}

# -- shared "place of publication" probe ------------------------------------
#
# ``origin`` is the one field no storefront currently publishes, so the warning
# that explains its ``null`` is the only evidence a reader gets. That warning
# must therefore be *derived*: :meth:`BaseSource.probe_origin` does the looking
# and reports which layers it really searched, so the sentence cannot drift away
# from the code, and so the field self-heals the day a site starts printing it.

_KEY_JUNK_RE = re.compile(r"[^0-9a-z]+")


def normalise_key(raw: Any) -> str:
    """Fold a JSON key or visible label to a comparable form.

    Case, whitespace, punctuation, bidi marks and separators all disappear, so
    ``"countryOfOrigin"``, ``"country_of_origin"`` and ``"Country of Origin ‏ : ‎"``
    become the single token ``"countryoforigin"``.
    """
    return _KEY_JUNK_RE.sub("", str(raw or "").casefold())


#: Every spelling that genuinely means "place of publication" -- JSON keys first,
#: then the visible label forms sites print in a details table. Compared after
#: :func:`normalise_key` folding, so each entry covers its camelCase,
#: snake_case, spaced and colon-suffixed variants at once.
#:
#: Country-*shaped* keys are deliberately absent: ``eligibleRegion``,
#: ``ineligibleRegion``, ``regionsAllowed`` and ``priceCurrency`` are sales
#: territory / distribution licensing and must never be read as an origin.
#: Matching is exact-on-the-folded-token (never substring) so they cannot creep
#: in, and neither can ``datePublished`` or ``publisher``.
ORIGIN_KEY_SPELLINGS: Tuple[str, ...] = (
    "placeOfPublication",
    "publicationPlace",
    "publishPlace",
    "publish_places",
    "placePublished",
    "countryOfOrigin",
    "country_of_origin",
    "publicationCountry",
    "publish_country",
    "Place of publication",
    "Country of origin",
    "Country/Region of Origin",
    "Published in",
)

#: The folded form of :data:`ORIGIN_KEY_SPELLINGS`, which is what the probe
#: compares against.
ORIGIN_KEYS: frozenset = frozenset(normalise_key(s) for s in ORIGIN_KEY_SPELLINGS)

#: Keys used to render a place out of a nested object such as
#: ``{"@type": "Place", "name": "London"}``.
_ORIGIN_VALUE_KEYS: Tuple[str, ...] = (
    "name", "@value", "value", "text", "alternateName",
    "addressCountry", "addressLocality", "addressRegion",
)

#: Never read text out of these; a JSON island or a CSS rule is not a label.
_TEXT_SKIP_TAGS: frozenset = frozenset(
    {"script", "style", "noscript", "template", "code", "svg"}
)

#: Longest plausible place string. Anything longer is a paragraph or a code list.
_ORIGIN_VALUE_MAX = 120
#: Longest text node that can plausibly *be* a label.
_ORIGIN_LABEL_MAX = 200
#: Junk that clings to a value split out of ``"key": "value",``.
_ORIGIN_VALUE_STRIP = " \t\"'`{}[],;:|-–—"


class JsonPair(NamedTuple):
    """One mapping entry found by :meth:`BaseSource.iter_json_pairs`."""

    #: Dotted/indexed path from the walk root, e.g. ``work.potentialAction[0].x``.
    path: str
    #: The key exactly as it appeared.
    key: str
    #: The value at that key (any type).
    value: Any
    #: The mapping that carried the key, so callers can read its siblings.
    parent: Dict[str, Any]


class OriginProbe(NamedTuple):
    """What :meth:`BaseSource.probe_origin_detail` found, and where it looked."""

    #: The place of publication, if one was genuinely on the page.
    value: Optional[str]
    #: Where the hit came from (layer name + key path or DOM label), else ``None``.
    where: Optional[str]
    #: Names of the layers that had content and were really searched, in order.
    searched: List[str]


class BaseSource(ABC):
    """Abstract base class for one book source (one website).

    :cvar name: lowercase slug used **verbatim** in output filenames and in
        ``--sources``. Must be unique across adapters.
    :cvar display_name: human-friendly label for logs and the summary table.
    :cvar prefers_browser: hint that this site needs JavaScript rendering. The
        pipeline still works when no browser is available; the adapter must
        degrade.
    :cvar enabled_by_default: whether a run that names no ``--sources`` includes
        this adapter. ``False`` makes it **opt-in** rather than removed: it is
        still discovered, still named in ``--help``, and still runs when
        asked for explicitly (``--sources <name>``). Use it for an adapter whose
        cost has stopped justifying its yield -- a site that has changed under us
        and now spends most of a run's wall clock returning little -- so the
        working code survives for whoever repairs it, instead of being deleted or
        commented out.
    """

    name: str = ""
    display_name: str = ""
    prefers_browser: bool = False
    enabled_by_default: bool = True

    def __init__(self, client: HttpClient) -> None:
        self.client = client

        # Optional budget hints. The pipeline overwrites these on the instance
        # from the CLI flags *before* calling scrape(); adapters may read them
        # to decide how many review pages to paginate, and may equally ignore
        # them (the pipeline enforces the caps again when persisting).
        self.min_reviews: int = 25
        self.max_reviews: Optional[int] = None
        self.want_covers: bool = True

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance
        return f"<{type(self).__name__} name={self.name!r}>"

    @property
    def label(self) -> str:
        """``display_name`` when set, else ``name``."""
        return self.display_name or self.name

    # -- abstract API --------------------------------------------------------

    @abstractmethod
    def find_book_url(self, hint: BookHint) -> Optional[str]:
        """Resolve ``hint`` to this site's canonical product/book page URL.

        Return ``None`` (after logging why) when the book cannot be located;
        must not raise.
        """

    @abstractmethod
    def _scrape_into(self, hint: BookHint, result: ScrapeResult) -> None:
        """Fill ``result`` with everything this source can offer for ``hint``.

        Called by :meth:`scrape`, which owns the parts every adapter shares: making
        the result, catching anything that escapes, and flushing deferred warnings.
        An implementation therefore only has to do the site-specific work, and *may*
        raise -- :meth:`scrape` is the net.

        Set as many of these as the site supports, leaving the rest at their empty
        defaults: ``book_url``, ``metadata``, ``genres``, ``blurb``, ``cover_urls``,
        ``reviews``, and ``hint_updates`` (only when the page's own identity was
        confirmed -- a wrong title here propagates into every later source's
        title+author search).
        """

    # -- the shared scrape shell ---------------------------------------------

    def scrape(self, hint: BookHint) -> ScrapeResult:
        """Scrape everything this source can offer for ``hint``. Never raises.

        This is the template every adapter shares, and the guarantee the pipeline
        relies on: **always** a :class:`ScrapeResult`, never an exception, never
        ``None``. A crash in one site's parsing becomes a warning on that source's
        result, so the other four still run and the book still gets written.

        Each adapter used to carry its own copy of this shell -- four of the five
        were the same six lines, differing only in the wording of the warning -- so
        the shape is defined once here and the site-specific work happens in
        :meth:`_scrape_into`.
        """
        result = self.new_result(hint)
        try:
            self._scrape_into(hint, result)
        except Exception as exc:  # noqa: BLE001 - scrape() must never raise
            result.warn(
                f"{self.name}: unexpected {type(exc).__name__} while scraping "
                f"({exc}); returning a partial result"
            )
            print(f"warning: {self.display_name or self.name} hit an unexpected "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
        # Adapters that queue warnings before a result exists flush them here.
        for note in self.pending_warnings():
            result.warn(note)
        return result

    def pending_warnings(self) -> List[str]:
        """Warnings queued before the result existed. Overridden where needed.

        Amazon and BookBub both accumulate notes during discovery -- long before
        there is a :class:`ScrapeResult` to attach them to -- and each had its own
        flush step. :meth:`scrape` now does it for them.
        """
        return []

    # -- helpers -------------------------------------------------------------

    def new_result(self, hint: BookHint) -> ScrapeResult:
        """Return an empty :class:`ScrapeResult` tagged with this source."""
        return ScrapeResult(source=self.name, isbn13=hint.isbn13)

    def new_metadata(self, hint: BookHint) -> BookMetadata:
        """Return a :class:`BookMetadata` keyed to the requested ISBN."""
        return BookMetadata(isbn13=hint.isbn13)

    # -- shared ISBN -> title/author seed ------------------------------------

    def seed_from_openlibrary(self, hint: BookHint) -> Tuple[Optional[str], Optional[str], List[str]]:
        """Resolve ``hint``'s ISBN to ``(title, subtitle, authors)`` via Open Library.

        BookBub, Kobo and Audible cannot be looked up by ISBN at all -- they are
        storefronts with their own product IDs -- so each one needs a title+author
        pair before it can search. That pair normally arrives in the shared
        :class:`BookHint`, seeded by whichever ISBN-native source ran first. When
        one of them runs on its own (``--sources kobo``) the hint is empty and this
        fills it from Open Library, which is ISBN-indexed by design.

        This is a **discovery aid only**: the returned strings build a search query
        or a URL slug, and never become a reported metadata value. Each adapter
        still parses every field from its own page.

        It used to be three near-identical private copies, one per adapter, which
        had quietly drifted apart -- only BookBub's tried ``/search.json``, only
        Audible's passed the ISBN-10 as well, and only BookBub's kept the subtitle.
        This is their union, so all three now get the best of what any one had.
        ``subtitle`` comes back separately because the callers disagree about it
        for good reason: BookBub's slugs include it, Kobo's search does better
        without it.
        """
        keys = [k for k in (hint.isbn13, hint.isbn10) if k]
        if not keys:
            return None, None, []

        # /api/books answers for either ISBN in one request.
        payload = self.client.get_json(
            "https://openlibrary.org/api/books",
            params={"bibkeys": ",".join(f"ISBN:{k}" for k in keys),
                    "format": "json", "jscmd": "data"},
        )
        if isinstance(payload, dict):
            for key in keys:
                record = payload.get(f"ISBN:{key}")
                if not isinstance(record, dict):
                    continue
                title = self.clean_text(record.get("title")) or None
                if title:
                    authors = [self.clean_text(a.get("name"))
                               for a in record.get("authors") or []
                               if isinstance(a, dict) and a.get("name")]
                    return (title, self.clean_text(record.get("subtitle")) or None,
                            [a for a in authors if a])

        # Some editions are only in the search index, not the books API.
        docs = self.client.get_json(
            "https://openlibrary.org/search.json",
            params={"isbn": hint.isbn13, "fields": "title,subtitle,author_name", "limit": 1},
        )
        if isinstance(docs, dict):
            for doc in docs.get("docs") or []:
                if not isinstance(doc, dict):
                    continue
                title = self.clean_text(doc.get("title")) or None
                if title:
                    authors = [self.clean_text(a) for a in doc.get("author_name") or []]
                    return (title, self.clean_text(doc.get("subtitle")) or None,
                            [a for a in authors if a])

        if verbose():
            print(f"  Open Library could not resolve {hint.isbn13} to a title/author",
                  file=sys.stderr)
        return None, None, []

    def search_terms(
        self, hint: BookHint, result: ScrapeResult, *, keep_subtitle: bool = False
    ) -> Tuple[Optional[str], List[str]]:
        """The ``(title, authors)`` to search this storefront with.

        Prefers the shared hint -- already resolved, so no extra request -- and
        falls back to :meth:`seed_from_openlibrary` when there is none. Warns
        either way about *where* the query came from, because a title-matched hit
        is a weaker identification than an ISBN lookup and the record should say so.
        """
        title = (hint.title or "").strip() or None
        authors = [a for a in (self.clean_text(a) for a in hint.authors or []) if a]
        if title:
            return title, authors

        seeded, subtitle, seeded_authors = self.seed_from_openlibrary(hint)
        if not seeded:
            result.warn(
                f"{self.name}: no title/author hint was seeded and Open Library could "
                f"not resolve ISBN {hint.isbn13} either, so this store cannot be "
                "searched (it has no ISBN lookup of its own)"
            )
            return None, authors
        if keep_subtitle and subtitle:
            seeded = f"{seeded}: {subtitle}"
        result.warn(
            f"{self.name}: no title/author hint was seeded, so the query was resolved "
            f"from Open Library: {seeded!r} by {', '.join(seeded_authors) or 'unknown'}. "
            "Every field reported below is still parsed from this store's own page."
        )
        print(f"Seeded the {self.display_name} search from openlibrary.org: {seeded!r} "
              f"by {', '.join(seeded_authors) or 'unknown'}", file=sys.stderr)
        return seeded, (authors or seeded_authors)

    # -- shared origin probe -------------------------------------------------

    def iter_json_pairs(
        self,
        node: Any,
        *,
        path: str = "",
        max_depth: int = 8,
        max_nodes: int = 20000,
    ) -> Iterator[JsonPair]:
        """Yield every mapping entry inside ``node``, breadth-first and bounded.

        Written for the blobs adapters already parse (JSON-LD objects, gizmo
        configs, Apollo caches), so it is deliberately unshockable: unexpected
        types are skipped, ``bs4`` nodes and strings are not descended into,
        containers are visited at most once by identity (so a self-referencing
        structure terminates), and the walk stops after ``max_nodes`` steps or
        ``max_depth`` levels. It never raises.

        Breadth-first matters: a shallow key is a better answer than a deep one,
        so the first hit a caller sees is the least-nested.
        """
        if node is None:
            return
        budget = max(1, int(max_nodes))
        depth_cap = max(0, int(max_depth))
        queue: deque = deque([(str(path or ""), node, 0)])
        seen: set = {id(node)}
        try:
            while queue and budget > 0:
                here, current, depth = queue.popleft()
                budget -= 1
                if isinstance(current, (str, bytes, Tag)) or depth > depth_cap:
                    continue
                if isinstance(current, dict):
                    for key, value in list(current.items()):
                        if budget <= 0:
                            return
                        budget -= 1
                        name = str(key)
                        child = f"{here}.{name}" if here else name
                        yield JsonPair(child, name, value, current)
                        if isinstance(value, (dict, list, tuple, set)) and id(value) not in seen:
                            seen.add(id(value))
                            queue.append((child, value, depth + 1))
                elif isinstance(current, (list, tuple, set)):
                    for index, value in enumerate(current):
                        if budget <= 0:
                            return
                        budget -= 1
                        if isinstance(value, (dict, list, tuple, set)) and id(value) not in seen:
                            seen.add(id(value))
                            queue.append((f"{here}[{index}]", value, depth + 1))
        except Exception as exc:  # a malformed blob must not take the adapter down
            if verbose():
                print('  Stopped walking a JSON layer early: %s: %s' % (type(exc).__name__, exc), file=sys.stderr)

    def probe_origin(
        self, layers: Sequence[Tuple[str, Any]]
    ) -> Tuple[Optional[str], List[str]]:
        """Search ``layers`` for a place of publication.

        This is the one implementation of "look for ``origin``" in the project;
        every adapter calls it with the layers it has already parsed, so the
        "layers searched" clause in an ``origin`` warning is *produced by the
        search* rather than asserted alongside it.

        :param layers: ``(name, payload)`` pairs, most authoritative first.
            ``payload`` may be a dict/list from a JSON blob or a ``bs4``
            document/fragment; ``None`` and empty payloads are skipped, because
            a layer that was not on the page was not searched.
        :returns: ``(value, searched)`` -- the place of publication if one was
            genuinely found (else ``None``), plus the names of the layers that
            really were searched, in order. Use
            :meth:`probe_origin_detail` when you also want to report *where* a
            hit came from.
        """
        probe = self.probe_origin_detail(layers)
        return probe.value, probe.searched

    def probe_origin_detail(
        self,
        layers: Sequence[Tuple[str, Any]],
        *,
        max_depth: int = 8,
        max_nodes: int = 20000,
    ) -> OriginProbe:
        """:meth:`probe_origin` plus the provenance of the hit.

        Recognises the key and label spellings in
        :data:`ORIGIN_KEY_SPELLINGS`, case- and separator-insensitively, and
        stops at the first layer that yields a plausible value.
        """
        searched: List[str] = []
        for entry in layers or ():
            try:
                name, payload = entry
            except Exception:
                if verbose():
                    print('  Ignoring malformed origin layer entry: %r' % (entry,), file=sys.stderr)
                continue
            label = str(name or "an unnamed layer")
            found: Optional[Tuple[str, str]] = None
            try:
                if isinstance(payload, Tag):
                    if payload.find(True) is None:
                        continue
                    searched.append(label)
                    found = self._origin_from_dom(payload, max_nodes=max_nodes)
                elif isinstance(payload, (dict, list, tuple, set)):
                    if not payload:
                        continue
                    searched.append(label)
                    found = self._origin_from_json(
                        payload, max_depth=max_depth, max_nodes=max_nodes)
                elif payload is None:
                    continue
                else:
                    if verbose():
                        print('  Origin layer %r has unsearchable type %s' % (label, type(payload).__name__), file=sys.stderr)
                    continue
            except Exception as exc:  # never let a probe break a scrape
                if verbose():
                    print('  Origin probe of layer %r failed: %s: %s' % (label, type(exc).__name__, exc), file=sys.stderr)
                continue
            if found is not None:
                return OriginProbe(found[0], f"{label} -> {found[1]}", searched)
        return OriginProbe(None, None, searched)

    def origin_layers_clause(self, searched: Sequence[str]) -> str:
        """Render a probe's ``searched`` list as an English list.

        Says so plainly when the probe had nothing to search, because "no layer
        was available" and "every layer was read and was empty of the field" are
        different facts about a run.
        """
        names = [str(n).strip() for n in (searched or []) if str(n).strip()]
        if not names:
            return "no parsed layer was available to search on this run"
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + " and " + names[-1]

    def _origin_from_json(self, payload: Any, *, max_depth: int,
                          max_nodes: int) -> Optional[Tuple[str, str]]:
        """First ``(value, key_path)`` in a JSON layer whose key means "origin"."""
        for pair in self.iter_json_pairs(
            payload, max_depth=max_depth, max_nodes=max_nodes
        ):
            if normalise_key(pair.key) not in ORIGIN_KEYS:
                continue
            value = self._origin_value(pair.value)
            if value:
                return value, f"the {pair.path or pair.key} key"
        return None

    def _origin_from_dom(self, node: Tag,
                         *, max_nodes: int) -> Optional[Tuple[str, str]]:
        """First ``(value, where)`` in a DOM layer: meta, itemprop, then labels."""
        limit = max(1, int(max_nodes))

        for tag in node.find_all("meta", limit=limit):
            for attr in ("property", "name", "itemprop"):
                if normalise_key(tag.get(attr)) in ORIGIN_KEYS:
                    value = self._origin_value(tag.get("content"))
                    if value:
                        return value, f'meta[{attr}="{tag.get(attr)}"]'

        for tag in node.find_all(attrs={"itemprop": True}, limit=limit):
            if normalise_key(tag.get("itemprop")) in ORIGIN_KEYS:
                value = self._origin_value(tag.get("content")) or self._origin_value(tag)
                if value:
                    return value, f'[itemprop="{tag.get("itemprop")}"]'

        for text_node in node.find_all(string=True, limit=limit):
            raw = str(text_node)
            if not raw.strip() or len(raw) > _ORIGIN_LABEL_MAX:
                continue
            parent = getattr(text_node, "parent", None)
            if not isinstance(parent, Tag) or parent.name in _TEXT_SKIP_TAGS:
                continue
            label, _, remainder = raw.partition(":")
            if normalise_key(label) not in ORIGIN_KEYS:
                continue
            value = self._origin_dom_value(parent, remainder)
            if value:
                return value, (f"the <{parent.name}> label "
                               f"{self.clean_text(label)!r}")
        return None

    def _origin_dom_value(self, parent: Tag, remainder: str) -> str:
        """Value that belongs to a matched DOM label: inline, sibling, then cell."""
        value = self._origin_value(remainder)
        if not value:
            sibling = parent.find_next_sibling()
            if isinstance(sibling, (Tag, NavigableString)):
                value = self._origin_value(sibling)
        if not value and parent.name in ("th", "dt", "b", "strong", "label"):
            cell = parent.find_next(("td", "dd"))
            if isinstance(cell, Tag):
                value = self._origin_value(cell)
        return value

    def _origin_value(self, raw: Any, depth: int = 0) -> str:
        """Render a probe hit as a place string, or ``''`` if it is not one.

        Numbers, booleans, structures, empty strings, values that are really
        markup and anything longer than a place name could plausibly be are all
        rejected, so a hit is only reported when it looks like an answer.
        """
        if raw is None or isinstance(raw, bool) or depth > 3:
            return ""
        if isinstance(raw, (int, float)):
            return ""
        if isinstance(raw, dict):
            for key in _ORIGIN_VALUE_KEYS:
                if key in raw:
                    value = self._origin_value(raw.get(key), depth + 1)
                    if value:
                        return value
            return ""
        if isinstance(raw, (list, tuple, set)):
            for item in list(raw)[:8]:
                value = self._origin_value(item, depth + 1)
                if value:
                    return value
            return ""
        text = self.clean_text(raw).strip(_ORIGIN_VALUE_STRIP)
        text = " ".join(text.split())
        if len(text) < 2 or len(text) > _ORIGIN_VALUE_MAX:
            return ""
        if text.startswith(("{", "[", "<")) or text.casefold() in ("null", "none", "n/a"):
            return ""
        return text

    def origin_unavailable(self, result: ScrapeResult, probed: str) -> None:
        """Record that this storefront publishes no place of publication.

        ``origin`` is a **storefront-only** field: every value this project
        emits must come from the page it says it came from, so when the site
        publishes no place of publication the field is ``null`` and the reason
        is written into ``_warnings``. No third-party bibliographic service is
        consulted, because a value from a sixth host is not a scraped value.

        Call this only *after* :meth:`probe_origin` has come back empty.
        ``probed`` is a sentence fragment naming **what was parsed** and **why it
        yielded nothing** (no trailing full stop), and every layer it names must
        come from the probe's ``searched`` list -- see
        :meth:`origin_layers_clause` -- so the sentence cannot outlive the code
        that justifies it. This method adds the invariant that applies to every
        source: origin is never inferred.
        """
        result.warn(
            f"origin (country/place of publication) is null: {probed}. It is never "
            "inferred from the publisher's imprint, the storefront locale or the "
            "delivery country -- that would be a guess rather than a scraped value -- "
            "and no non-storefront source is consulted for this field"
        )
        return None

    def clean_text(self, s: Any) -> str:
        """Unescape HTML entities, drop control chars, collapse whitespace, strip.

        Newlines are preserved (runs of 3+ collapse to 2) so paragraph structure
        survives; horizontal whitespace collapses to single spaces. Accepts a
        bs4 ``Tag`` or ``None`` as well as ``str``.
        """
        if s is None:
            return ""
        if isinstance(s, Tag):
            s = s.get_text("\n")
        text = str(s)
        text = html_module.unescape(text)
        text = text.replace(" ", " ").replace("​", "").replace("﻿", "")
        text = _CTRL_RE.sub("", text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = _WS_RE.sub(" ", text)
        text = "\n".join(line.strip() for line in text.split("\n"))
        text = _BLANKS_RE.sub("\n\n", text)
        return text.strip()

    def html_to_text(self, s: Any) -> str:
        """Convert an HTML fragment to readable plain text.

        ``<br>`` becomes a newline, block elements become blank-line-separated
        paragraphs, ``<script>``/``<style>`` are dropped, then
        :meth:`clean_text` finishes the job. Accepts a raw HTML string or a bs4
        node.
        """
        if s is None:
            return ""
        if isinstance(s, Tag):
            fragment = s
        else:
            text = str(s)
            if "<" not in text:
                return self.clean_text(text)
            fragment = self._soup(text)
            if fragment is None:
                return self.clean_text(re.sub(r"<[^>]+>", " ", text))

        try:
            working = BeautifulSoup(str(fragment), "html.parser")
        except Exception as exc:  # bs4 can raise on pathological input
            if verbose():
                print('  html_to_text could not re-parse fragment: %s' % (exc,), file=sys.stderr)
            return self.clean_text(re.sub(r"<[^>]+>", " ", str(fragment)))

        for junk in working.find_all(["script", "style", "noscript", "template"]):
            junk.decompose()
        for br in working.find_all("br"):
            br.replace_with("\n")
        for block in working.find_all(
            ["p", "div", "li", "tr", "section", "article", "h1", "h2", "h3", "h4",
             "h5", "h6", "blockquote"]
        ):
            block.insert_before("\n")
            block.insert_after("\n")
        return self.clean_text(working.get_text(""))

    def _soup(self, markup: str) -> Optional[BeautifulSoup]:
        """Parse a fragment with lxml, falling back to html.parser."""
        for parser in ("lxml", "html.parser"):
            try:
                return BeautifulSoup(markup, parser)
            except Exception as exc:
                if verbose():
                    print('  Fragment parser %s failed: %s' % (parser, exc), file=sys.stderr)
        return None

    def absolutise(self, base: str, url: Any) -> str:
        """Resolve a possibly-relative ``url`` against ``base``.

        Protocol-relative ``//host/path`` URLs are promoted to https. Returns
        ``''`` for empty input and never raises.
        """
        if not url:
            return ""
        text = str(url).strip()
        if not text or text.startswith(("javascript:", "data:", "#", "mailto:")):
            return ""
        if text.startswith("//"):
            return "https:" + text
        try:
            return urljoin(base or "", text)
        except ValueError:
            return text

    def jsonld(self, soup: Optional[BeautifulSoup],
               want_type: Optional[Union[str, Iterable[str]]] = None) -> List[Dict[str, Any]]:
        """Return every JSON-LD object in ``soup``, flattened and filtered.

        Tolerates the three things sites actually do: a bare object, a top-level
        array, and an ``@graph`` wrapper -- plus nested combinations. Blocks
        that fail to parse are logged at debug level and skipped, never raised.

        :param want_type: keep only objects whose ``@type`` matches (case
            insensitive; ``@type`` may itself be a list). ``None`` keeps all.
        """
        if soup is None:
            return []

        wanted: Optional[set] = None
        if want_type is not None:
            if isinstance(want_type, str):
                wanted = {want_type.lower()}
            else:
                wanted = {str(t).lower() for t in want_type}

        collected: List[Dict[str, Any]] = []

        def _absorb(node: Any, depth: int = 0) -> None:
            if depth > 6:
                return
            if isinstance(node, list):
                for item in node:
                    _absorb(item, depth + 1)
                return
            if not isinstance(node, dict):
                return
            collected.append(node)
            for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement"):
                if key in node:
                    _absorb(node[key], depth + 1)

        try:
            blocks = soup.find_all("script", attrs={"type": re.compile(
                r"application/ld\+json", re.IGNORECASE)})
        except Exception as exc:
            if verbose():
                print('  Could not scan for ld+json blocks: %s' % (exc,), file=sys.stderr)
            return []

        for block in blocks:
            raw = block.string
            if raw is None:
                raw = block.get_text()
            if not raw or not raw.strip():
                continue
            payload = self._loads_lenient(raw)
            if payload is None:
                continue
            _absorb(payload)

        if wanted is None:
            return collected

        def _matches(obj: Dict[str, Any]) -> bool:
            declared = obj.get("@type")
            if declared is None:
                return False
            if isinstance(declared, (list, tuple, set)):
                return any(str(d).lower() in wanted for d in declared)
            return str(declared).lower() in wanted

        return [obj for obj in collected if _matches(obj)]

    def _loads_lenient(self, raw: str) -> Optional[Any]:
        """json.loads with two cheap repairs for real-world ld+json sloppiness.

        ``RecursionError`` is caught alongside ``ValueError``: ``json.loads``
        raises it (not a ``ValueError``) on a pathologically nested array, and a
        page carrying such a block must degrade to "unparseable blob" like any
        other, not take the adapter down.
        """
        text = raw.strip()
        try:
            return json.loads(text)
        except (ValueError, RecursionError):
            pass
        repaired = _CTRL_RE.sub(" ", text)
        repaired = re.sub(r",\s*([}\]])", r"\1", repaired)  # trailing commas
        try:
            return json.loads(repaired)
        except (ValueError, RecursionError) as exc:
            if verbose():
                print('  Skipping unparseable ld+json block: %s' % (exc,), file=sys.stderr)
            return None

    def dedupe(self, seq: Optional[Iterable[Any]]) -> List[Any]:
        """Order-preserving de-duplication.

        Strings are compared case-insensitively on their stripped form (so
        ``"Fiction"`` and ``" fiction "`` collapse) but the **first** spelling
        seen is the one kept. Unhashable items are compared by ``repr``.
        """
        out: List[Any] = []
        seen: set = set()
        for item in seq or []:
            if isinstance(item, str):
                key = item.strip().casefold()
                if not key:
                    continue
            else:
                try:
                    hash(item)
                    key = item
                except TypeError:
                    key = repr(item)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    # -- convenience extras (additive; adapters may ignore them) -------------

    def select_text(self, soup: Optional[BeautifulSoup], *selectors: str) -> Optional[str]:
        """Return the cleaned text of the first CSS selector that matches.

        A bad selector is logged and skipped rather than raised, so adapters can
        list several fallbacks cheaply.
        """
        if soup is None:
            return None
        for selector in selectors:
            if not selector:
                continue
            try:
                node = soup.select_one(selector)
            except Exception as exc:
                if verbose():
                    print('  Bad selector %r: %s' % (selector, exc), file=sys.stderr)
                continue
            if node is None:
                continue
            text = self.clean_text(node)
            if text:
                return text
        return None

    def select_texts(self, soup: Optional[BeautifulSoup], *selectors: str) -> List[str]:
        """Cleaned, de-duplicated text of every node matching any selector."""
        if soup is None:
            return []
        found: List[str] = []
        for selector in selectors:
            if not selector:
                continue
            try:
                nodes = soup.select(selector)
            except Exception as exc:
                if verbose():
                    print('  Bad selector %r: %s' % (selector, exc), file=sys.stderr)
                continue
            for node in nodes:
                text = self.clean_text(node)
                if text:
                    found.append(text)
        return self.dedupe(found)

    def meta(self, soup: Optional[BeautifulSoup], *keys: str) -> Optional[str]:
        """Return the first matching ``<meta>`` content for ``property``/``name``/``itemprop``.

        >>> self.meta(soup, "og:title", "twitter:title", "title")
        """
        if soup is None:
            return None
        for key in keys:
            if not key:
                continue
            for attr in ("property", "name", "itemprop"):
                try:
                    node = soup.find("meta", attrs={attr: key})
                except Exception as exc:
                    if verbose():
                        print('  meta lookup %s=%r failed: %s' % (attr, key, exc), file=sys.stderr)
                    continue
                if node is None:
                    continue
                value = self.clean_text(node.get("content") or "")
                if value:
                    return value
        return None

    @staticmethod
    def is_sequel_pair(left: str, right: str) -> bool:
        """True when two normalised titles differ *only* in a sequel ordinal.

        ``"ready player one"`` vs ``"ready player two"``: same word count, one
        differing token, and that token is an ordinal on both sides. That is a
        sequel, not another edition -- and the raw similarity ratio for such a pair
        is ~0.88, over any sane acceptance threshold, so it needs an explicit veto
        or the wrong book gets filed under the requested ISBN.

        Lives here because Amazon and BookBub both need it, and each had grown its
        own copy of the same logic over an identical ordinal table.
        """
        a, b = left.split(), right.split()
        if len(a) != len(b) or not a:
            return False
        differing = [(x, y) for x, y in zip(a, b) if x != y]
        if len(differing) != 1:
            return False
        x, y = differing[0]
        return (
            x in ORDINAL_TOKENS
            and y in ORDINAL_TOKENS
            and ORDINAL_TOKENS[x] != ORDINAL_TOKENS[y]
        )

    def split_list(self, raw: Any) -> List[str]:
        """Split "Fiction, Fantasy & Young Adult" into a clean, deduped list.

        Accepts a string, or an iterable of strings/dicts with a ``name`` key
        (the shape JSON-LD ``genre``/``author`` fields take).
        """
        items: List[str] = []
        if raw is None:
            return []
        if isinstance(raw, str):
            candidates = _LIST_SPLIT_RE.split(raw)
        elif isinstance(raw, dict):
            candidates = [str(raw.get("name") or raw.get("@id") or "")]
        elif isinstance(raw, Iterable):
            candidates = []
            for item in raw:
                if isinstance(item, dict):
                    candidates.append(str(item.get("name") or ""))
                else:
                    candidates.append(str(item))
        else:
            candidates = [str(raw)]
        for candidate in candidates:
            text = self.clean_text(candidate).strip(" .,-|/")
            if text and len(text) <= 120:
                items.append(text)
        return self.dedupe(items)

    def iso_date(self, raw: Any) -> Optional[str]:
        """Normalise a human date to ISO-8601 where derivable, else return the text.

        ``"January 5, 2016"`` -> ``"2016-01-05"``; ``"June 2016"`` ->
        ``"2016-06"``; ``"2016"`` -> ``"2016"``. Unrecognisable but non-empty
        input is returned cleaned, so nothing is silently lost.
        """
        text = self.clean_text(raw)
        if not text:
            return None
        text = re.sub(r"^(?:first\s+)?published[:\s]*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
        candidate = text.strip(" ,.;")

        iso_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", candidate)
        if iso_match:
            return iso_match.group(0)

        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(candidate, fmt)
            except ValueError:
                continue
            if fmt in ("%B %Y", "%b %Y", "%Y-%m"):
                return parsed.strftime("%Y-%m")
            return parsed.strftime("%Y-%m-%d")

        # Try to pull a "Month DD, YYYY" or "DD Month YYYY" out of a longer phrase.
        phrase = re.search(
            r"\b(?:(\d{1,2})\s+)?([A-Za-z]{3,9})\.?\s+(?:(\d{1,2}),?\s+)?(\d{4})\b",
            candidate,
        )
        if phrase:
            day = phrase.group(1) or phrase.group(3)
            month_name, year = phrase.group(2), phrase.group(4)
            for fmt in ("%B", "%b"):
                try:
                    month = datetime.strptime(month_name.title(), fmt).month
                except ValueError:
                    continue
                if day and 1 <= int(day) <= 31:
                    try:
                        return datetime(int(year), month, int(day)).strftime("%Y-%m-%d")
                    except ValueError:
                        pass
                return f"{year}-{month:02d}"

        year_only = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", candidate)
        if year_only:
            return year_only.group(1)
        return candidate

    def make_review(
        self,
        text: Any,
        *,
        reviewer: Any = None,
        rating: Any = None,
        date: Any = None,
        url: Any = None,
        min_chars: int = 1,
    ) -> Optional[ReviewItem]:
        """Build a cleaned :class:`ReviewItem`, or ``None`` if the body is too thin."""
        body = self.html_to_text(text)
        if len(body) < max(1, int(min_chars)):
            return None
        return ReviewItem(
            text=body,
            reviewer=self.clean_text(reviewer) or None,
            rating=self.clean_text(rating) or None,
            date=self.clean_text(date) or None,
            url=self.clean_text(url) or None,
        )
