"""Per-source ISBN lists: what a site does not have, and what we still owe it.

Both are the same shape -- one ISBN-13 per line, sorted, merged rather than
overwritten -- because both are durable state a later run reads, and a partial run
(``--end 100``) must not truncate entries it never looked at.

:class:`NoData` records **absence**: a metadata record is keyed on the book
*existing*, so no shape of "record present" can mean "this site does not carry it".
Only a *trustworthy* empty is recorded -- one where the site was reached and nothing
was walling us. Skipping that guard once wrote off 629 WAF-challenged Goodreads
books as "not on Goodreads", and every one resolved later.

:class:`Pending` records **incompleteness**: a book whose metadata was written but
whose covers or reviews failed transiently. Without it, the skip decision sees the
metadata record, skips the source, and the missing artefact is lost for good -- which
is exactly what happened to 91 Open Library covers when archive.org stalled.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, Set

from .storage import file_lock, sanitise, write_text
from .transport import warn

if TYPE_CHECKING:  # pragma: no cover
    from .storage import Storage


class IsbnList:
    """One flat ISBN-13 list per source, read lazily and merged on :meth:`flush`.

    Reading a file is ``set(text.split())``: there is no format to get wrong.
    """

    #: Filename tail, set by each subclass.
    SUFFIX = "_isbns.txt"

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._known: Dict[str, Set[str]] = {}
        self._added: Dict[str, Set[str]] = {}
        self._removed: Dict[str, Set[str]] = {}
        #: source -> how many this run wrote, kept separately because
        #: :meth:`flush` clears ``_added`` before the digest renders.
        self.recorded: Dict[str, int] = {}

    def path_for(self, source: str) -> Path:
        return self.directory / f"{sanitise(source, 'source')}{self.SUFFIX}"

    def _load(self, source: str) -> Set[str]:
        """The ISBNs on disk for ``source``, read once. Never raises."""
        if source not in self._known:
            path = self.path_for(source)
            try:
                self._known[source] = (set(path.read_text(encoding="utf-8").split())
                                       if path.is_file() else set())
            except (OSError, ValueError):
                self._known[source] = set()
        return self._known[source]

    def contains(self, source: str, isbn13: str) -> bool:
        source = str(source).strip().lower()
        if isbn13 in self._removed.get(source, set()):
            return False
        return (isbn13 in self._load(source)
                or isbn13 in self._added.get(source, set()))

    def note(self, source: str, isbn13: str) -> None:
        """Add an ISBN. Held in memory until :meth:`flush`."""
        source = str(source).strip().lower()
        if not isbn13 or not source:
            return
        self._removed.get(source, set()).discard(isbn13)
        if isbn13 not in self._load(source):
            self._added.setdefault(source, set()).add(isbn13)

    def discard(self, source: str, isbn13: str) -> None:
        """Remove an ISBN, because whatever it recorded is no longer true."""
        source = str(source).strip().lower()
        if not isbn13 or not source:
            return
        self._added.get(source, set()).discard(isbn13)
        if isbn13 in self._load(source):
            self._removed.setdefault(source, set()).add(isbn13)

    @property
    def sources(self) -> Set[str]:
        return set(self._added) | set(self._removed)

    def flush(self) -> None:
        """Merge this run's changes into the lists on disk. Never raises."""
        for source in sorted(self.sources):
            added = self._added.get(source, set())
            removed = self._removed.get(source, set())
            if not added and not removed:
                continue
            path = self.path_for(source)
            try:
                # Re-read under the lock rather than trusting the cached copy: a
                # second process may have added entries, and a merge must not drop
                # them.
                with file_lock(path):
                    on_disk = (set(path.read_text(encoding="utf-8").split())
                               if path.is_file() else set())
                    merged = (on_disk | added) - removed
                    write_text(path, "\n".join(sorted(merged)) + "\n" if merged else "")
            except (OSError, ValueError) as exc:
                warn(f"warning: could not write {path} ({exc}); {len(added)} "
                     "entry(ies) will be recomputed next run")
                continue
            self._known[source] = merged
            if added:
                self.recorded[source] = self.recorded.get(source, 0) + len(added)
        self._added.clear()
        self._removed.clear()


class NoData(IsbnList):
    """ISBNs a source answered about and genuinely does not carry."""

    SUFFIX = "_no_data.txt"


class Pending(IsbnList):
    """ISBNs whose record was written but whose artefacts are incomplete.

    Consulted by the skip decision, so a book listed here is scraped again even
    though its metadata is already on disk.
    """

    SUFFIX = "_incomplete.txt"


def queue_missing_covers(storage: "Storage", pending: Pending,
                         sources: Iterable[str]) -> Dict[str, int]:
    """Queue every book whose record is on disk but whose cover is not.

    Recovery for output written before incompleteness was tracked, when a failed
    cover download left a metadata record that the skip check then read as
    "finished". Returns ``{source: queued}``.

    A book that genuinely has no cover corrects itself on the retry: the source
    offers no cover URL, so nothing failed, and it drops off the list again.
    """
    queued: Dict[str, int] = {}
    for source in sources:
        have = {p.name.split("_cp_")[0]
                for p in storage.dir_for("covers").glob(f"*_cp_{source}_*")}
        for record in storage.meta.read(source):
            isbn13 = str(record.get("isbn13") or "").strip()
            if isbn13 and isbn13 not in have:
                pending.note(source, isbn13)
                queued[source] = queued.get(source, 0) + 1
    for source, count in sorted(queued.items()):
        warn(f"{source}: queued {count} book(s) whose record is on disk but whose "
             "cover is not, so this run retries them")
    return queued
