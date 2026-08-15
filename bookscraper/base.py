"""The :class:`Source` contract every site adapter implements, and discovery.

An adapter subclasses :class:`Source`, sets ``name``, and implements
:meth:`Source._scrape`. It is then picked up automatically -- there is no registry
to edit.

Three rules adapters must honour:

1. **Scraping never raises.** :meth:`Source.scrape` is the net; a missing selector
   appends a warning and carries on, because a partial result is a success.
2. **No scraped value is ever hard-coded.** Every field comes from a live parse.
3. **A warning is derived from the page, not asserted about it.** If a warning
   names the layers it searched, the code must have searched them on this run --
   see :func:`bookscraper.origin.probe`. A hand-written claim about live page
   content is a hard-coded value wearing prose.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple, Type

from . import origin as origin_probe
from .http import HttpClient
from .models import Book, Hint, Result
from .parse import text
from .transport import warn

#: Presentation order, not a registry: an adapter not listed here is still
#: discovered and simply sorts after these. Open Library leads because the runner
#: uses it to seed title/author hints for the stores that index no ISBN.
ORDER = ("openlibrary", "goodreads", "amazon", "bookbub", "kobo", "audible")


class Source(ABC):
    """One book source (one website).

    :cvar name: lowercase slug used **verbatim** in output filenames and in
        ``--sources``; must be unique across adapters.
    :cvar needs_browser: this site serves nothing useful without JavaScript, so
        go straight to the browser rather than spending a guaranteed 403 first.
    """

    name: str = ""
    display_name: str = ""
    needs_browser: bool = False

    def __init__(self, client: HttpClient) -> None:
        self.client = client
        #: Budget hints the runner sets before :meth:`scrape`; adapters may read
        #: them to decide how many review pages to page through, or ignore them
        #: (the runner enforces the cap again when persisting).
        self.min_reviews: int = 25
        self.max_reviews: Optional[int] = None

    @property
    def label(self) -> str:
        return self.display_name or self.name

    @abstractmethod
    def _scrape(self, hint: Hint, result: Result) -> None:
        """Fill ``result`` with everything this source offers for ``hint``.

        May raise -- :meth:`scrape` is the net. Set as many of ``book_url``,
        ``book``, ``genres``, ``blurb``, ``cover_urls``, ``reviews`` and ``hint``
        as the site supports, leaving the rest at their empty defaults. Only set
        ``result.hint`` once the page's identity is confirmed: a wrong title there
        propagates into every later source's title+author search.
        """

    def scrape(self, hint: Hint) -> Result:
        """Scrape everything this source offers. **Never raises, never returns None.**

        This is the guarantee the runner relies on: a crash in one site's parsing
        becomes a warning on that source's result, so the other five still run and
        the book is still written.
        """
        result = Result(source=self.name, isbn13=hint.isbn13)
        try:
            self._scrape(hint, result)
        except Exception as exc:  # noqa: BLE001 - scrape() must never raise
            result.warn(f"{self.name}: unexpected {type(exc).__name__} while "
                        f"scraping ({exc}); returning a partial result")
            warn(f"warning: {self.label} hit an unexpected {type(exc).__name__}: {exc}")
        return result

    def new_book(self, hint: Hint) -> Book:
        """A :class:`Book` keyed to the requested ISBN."""
        return Book(isbn13=hint.isbn13)

    # -- shared ISBN -> title/author seed ------------------------------------

    def seed(self, hint: Hint) -> Tuple[Optional[str], Optional[str], List[str]]:
        """Resolve the ISBN to ``(title, subtitle, authors)`` via Open Library.

        BookBub, Kobo and Audible cannot be looked up by ISBN at all -- they are
        storefronts with their own product ids -- so each needs a title+author
        pair before it can search. That normally arrives in the shared
        :class:`Hint`; when one runs alone (``--sources kobo``) this fills it in.

        A **discovery aid only**: the strings build a query or a URL slug and
        never become a reported metadata value. ``subtitle`` comes back separately
        because callers disagree about it for good reason -- BookBub's slugs
        include it, Kobo's search does better without it.
        """
        keys = [k for k in (hint.isbn13, hint.isbn10) if k]
        if not keys:
            return None, None, []
        payload = self.client.json("https://openlibrary.org/api/books", params={
            "bibkeys": ",".join(f"ISBN:{k}" for k in keys),
            "format": "json", "jscmd": "data"})
        for key in keys if isinstance(payload, dict) else ():
            record = payload.get(f"ISBN:{key}")
            if isinstance(record, dict) and (title := text(record.get("title"))):
                authors = [t for a in record.get("authors") or []
                           if isinstance(a, dict) and (t := text(a.get("name")))]
                return title, text(record.get("subtitle")) or None, authors
        # Some editions are only in the search index, not the books API.
        docs = self.client.json("https://openlibrary.org/search.json", params={
            "isbn": hint.isbn13, "fields": "title,subtitle,author_name", "limit": 1})
        for doc in (docs or {}).get("docs") or () if isinstance(docs, dict) else ():
            if isinstance(doc, dict) and (title := text(doc.get("title"))):
                return (title, text(doc.get("subtitle")) or None,
                        [t for a in doc.get("author_name") or [] if (t := text(a))])
        return None, None, []

    def terms(self, hint: Hint, result: Result, *, subtitle: bool = False
              ) -> Tuple[Optional[str], List[str]]:
        """The ``(title, authors)`` to search this storefront with.

        Prefers the shared hint -- already resolved, so no extra request -- and
        falls back to :meth:`seed`. Warns about *where* the query came from,
        because a title-matched hit is a weaker identification than an ISBN lookup
        and the record should say so.
        """
        title = (hint.title or "").strip() or None
        authors = [t for a in hint.authors or [] if (t := text(a))]
        if title:
            return title, authors
        seeded, sub, seeded_authors = self.seed(hint)
        if not seeded:
            result.warn(f"{self.name}: no title/author hint was seeded and Open "
                        f"Library could not resolve ISBN {hint.isbn13} either, so "
                        "this store cannot be searched (it has no ISBN lookup)")
            return None, authors
        if subtitle and sub:
            seeded = f"{seeded}: {sub}"
        result.warn(f"{self.name}: no hint was seeded, so the query came from Open "
                    f"Library: {seeded!r} by {', '.join(seeded_authors) or 'unknown'}. "
                    "Every field below is still parsed from this store's own page.")
        return seeded, (authors or seeded_authors)

    # -- origin --------------------------------------------------------------

    def origin(self, result: Result, layers: List[Tuple[str, object]]) -> Optional[str]:
        """Probe ``layers`` for a place of publication, warning when there is none.

        The warning names the layers the probe actually searched, so it cannot
        drift from the code -- and the field self-heals if a site starts
        publishing it. See :mod:`bookscraper.origin`.
        """
        found, searched = origin_probe.probe(layers)
        if found:
            return found
        result.warn(
            f"origin (country/place of publication) is null: {self.label} publishes "
            f"no place of publication in {origin_probe.clause(searched)}. It is never "
            "inferred from the publisher's imprint, the storefront locale or the "
            "delivery country -- that would be a guess rather than a scraped value")
        return None


def discover() -> Dict[str, Type[Source]]:
    """``{name: adapter}`` for every importable adapter, in :data:`ORDER`.

    A module that fails to import (syntax error, missing optional dependency)
    warns and is skipped, so one broken adapter cannot take the program down.
    """
    from . import sources

    found: Dict[str, Type[Source]] = {}
    for module_info in pkgutil.iter_modules(sources.__path__):
        if module_info.name.startswith("_"):
            continue
        qualified = f"{sources.__name__}.{module_info.name}"
        try:
            module = importlib.import_module(qualified)
        except Exception as exc:  # noqa: BLE001 - a broken adapter is skipped
            warn(f"warning: skipping {qualified}: it failed to import "
                 f"({type(exc).__name__}: {exc})")
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (issubclass(obj, Source) and obj is not Source
                    and not inspect.isabstract(obj)
                    and obj.__module__ == module.__name__
                    and (key := str(getattr(obj, "name", "")).strip().lower())):
                found.setdefault(key, obj)

    return dict(sorted(found.items(),
                       key=lambda kv: (ORDER.index(kv[0]) if kv[0] in ORDER else len(ORDER),
                                       kv[0])))
