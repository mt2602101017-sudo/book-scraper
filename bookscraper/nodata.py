"""Absence -- the one answer a metadata file cannot hold.

A metadata record is keyed on the book *existing*, so no shape of "record
present" can mean "this site does not carry it". A source that answered honestly
and had no such book leaves nothing behind, and is indistinguishable from one
that was never asked. On the shipped 10 000-ISBN CSV that is 20 007 of 49 975
pairs, and re-crawling them costs roughly 29 h per full run.

So absence gets its own list: ``metrics/<source>_no_data.txt``, one ISBN-13 per
line. Between that and the metadata file, "have we already asked this site about
this book?" has an answer for every pair.

Only a **trustworthy** empty is recorded -- one where the site was actually
reached and no host the source contacted was walling us. Skipping that guard once
wrote off 629 WAF-challenged Goodreads books as "not on Goodreads", and every one
of them resolved on a later attempt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Set

from .storage import file_lock, sanitise, write_text


class NoData:
    """The ISBNs each source answered about and genuinely does not carry.

    One plain list per source: ISBN-13s, one per line, sorted. Reading it is
    ``set(text.split())`` -- there is no format to get wrong. Written once at the
    end of a run and **merged** with what is on disk, because this is durable
    state and a ``--end 100`` slice must not truncate what it never looked at.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._known: Dict[str, Set[str]] = {}
        self._added: Dict[str, Set[str]] = {}
        #: source -> how many this run wrote, kept separately because
        #: :meth:`flush` clears ``_added`` before the digest renders.
        self.recorded: Dict[str, int] = {}

    def path_for(self, source: str) -> Path:
        return self.directory / f"{sanitise(source, 'source')}_no_data.txt"

    def _load(self, source: str) -> Set[str]:
        """The ISBNs on disk for ``source``, read once. Never raises."""
        if source not in self._known:
            path = self.path_for(source)
            try:
                self._known[source] = set(path.read_text(encoding="utf-8").split()) \
                    if path.is_file() else set()
            except (OSError, ValueError):
                self._known[source] = set()
        return self._known[source]

    def contains(self, source: str, isbn13: str) -> bool:
        """True when ``source`` already answered that it has no such book."""
        source = str(source).strip().lower()
        return (isbn13 in self._load(source)
                or isbn13 in self._added.get(source, set()))

    def note(self, source: str, isbn13: str) -> None:
        """Remember a **trustworthy** absence. Held until :meth:`flush`."""
        source = str(source).strip().lower()
        if isbn13 and source and isbn13 not in self._load(source):
            self._added.setdefault(source, set()).add(isbn13)

    def flush(self) -> None:
        """Merge this run's findings into the lists on disk. Never raises."""
        for source, isbns in sorted(self._added.items()):
            path = self.path_for(source)
            try:
                # Re-read under the lock rather than trusting the cached copy: a
                # second process may have added entries, and a merge must not
                # drop them.
                with file_lock(path):
                    on_disk = set(path.read_text(encoding="utf-8").split()) \
                        if path.is_file() else set()
                    merged = on_disk | isbns
                    write_text(path, "\n".join(sorted(merged)) + "\n")
            except (OSError, ValueError) as exc:
                print(f"warning: could not write {path} ({exc}); {len(isbns)} "
                      "miss(es) will be re-checked next run")
                continue
            self._known[source] = merged
            self.recorded[source] = self.recorded.get(source, 0) + len(isbns)
        self._added.clear()
