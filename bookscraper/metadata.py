"""The metadata files, and the record of what has already been asked.

``book_metadata/<source>_metadata.json`` is **one JSON array with a record per
book**, appended to as the run goes rather than overwritten -- the mandated
filename carries no ISBN, so overwriting would leave five files describing
whichever book finished last. The append seeks past the trailing ``]`` and writes
one record: re-dumping the whole array per book costs ~60 GiB across 10 000
books, this is ~12 MiB and flat. The file is a valid array after every append.

Between two things, "have we already asked this site about this book?" has an
answer for every pair:

* a record in the metadata file -- the site had the book, and we stored it;
* an entry in ``metrics/<source>_no_data.txt`` -- the site answered and
  genuinely does not carry it.

The second exists because no shape of "record present" can mean "absent". On the
shipped 10 000-ISBN CSV that is 20 007 of 49 975 pairs, and re-crawling them
costs ~29 h per run. Only a **trustworthy** empty is recorded: an empty seen
while a host was walling us is not a statement about the catalogue. Skipping that
guard once wrote off 629 WAF-challenged Goodreads books as "not on Goodreads",
and every one of them resolved on a later attempt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .storage import file_lock, sanitise, write_text

#: Per-source caches, keyed by file path. Read once per source per run: asking
#: the file for each of 10 000 x 6 pairs would re-parse a growing multi-megabyte
#: array 60 000 times (measured at ~70 s of pure JSON parsing).
_ISBNS: Dict[str, Set[str]] = {}
_RECORDS: Dict[str, Dict[str, Dict[str, Any]]] = {}


def release_caches() -> None:
    """Drop the caches; they serve one crawl. Also needed by the tests."""
    _ISBNS.clear()
    _RECORDS.clear()


class Metadata:
    """The accumulating ``<source>_metadata.json`` files in one directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def path_for(self, source: str) -> Path:
        return self.directory / f"{sanitise(source, 'source')}_metadata.json"

    @staticmethod
    def _dump(payload: Dict[str, Any]) -> str:
        """One record, indented two spaces to sit inside the array.

        Dumped verbatim -- key order preserved, UTF-8 kept as real characters --
        so :meth:`bookscraper.models.Book.to_json` owns the document shape.
        """
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        return "\n".join("  " + line for line in text.splitlines())

    def append(self, source: str, payload: Dict[str, Any]) -> Path:
        """Append -- or replace -- one book's record. Serialised on a file lock.

        Re-scraping a book replaces its record instead of adding a second one: a
        resumed run deliberately re-does a few finished books, and duplicates
        would corrupt every count taken from the file.
        """
        path = self.path_for(source)
        isbn13 = str(payload.get("isbn13") or "").strip()
        # Only update indexes that already exist. Creating one here would leave a
        # set holding this single ISBN that then looks authoritative, and every
        # other book would be reported as unscraped.
        if isbn13:
            if (known := _ISBNS.get(str(path))) is not None:
                known.add(isbn13)
            if (records := _RECORDS.get(str(path))) is not None:
                records[isbn13] = payload

        with file_lock(path):
            if isbn13 and self._replace(source, path, isbn13, payload):
                return path
            record = self._dump(payload)
            if not path.exists() or path.stat().st_size == 0:
                return write_text(path, f"[\n{record}\n]\n")
            tail = self._tail_offset(path)
            if tail is None:  # truncated by kill -9, or hand-edited: keep it
                os.replace(path, path.with_suffix(".corrupt.json"))
                return write_text(path, f"[\n{record}\n]\n")
            with open(path, "r+", encoding="utf-8", newline="") as handle:
                handle.seek(tail)
                handle.truncate()
                # tail is 1 only for a bare "[", the one case needing no comma.
                handle.write(f"{'' if tail <= 1 else ','}\n{record}\n]\n")
        return path

    def _replace(self, source: str, path: Path, isbn13: str,
                 payload: Dict[str, Any]) -> bool:
        """Rewrite an existing record for this ISBN; ``True`` if there was one.

        Only rewrites when the book is genuinely already there, so a book seen
        for the first time keeps the cheap append path. Caller holds the lock.
        """
        records = self.read(source)
        for index, existing in enumerate(records):
            if str(existing.get("isbn13") or "").strip() == isbn13:
                records[index] = payload
                body = ",\n".join(self._dump(item) for item in records)
                write_text(path, f"[\n{body}\n]\n")
                return True
        return False

    @staticmethod
    def _tail_offset(path: Path) -> Optional[int]:
        """Byte offset just past the final record, or ``None`` if unusable.

        Reads only the last 4 KiB, so this stays flat as the array grows.
        """
        size = path.stat().st_size
        with open(path, "rb") as handle:
            handle.seek(max(0, size - 4096))
            window = handle.read()
        stripped = window.rstrip()
        if not stripped.endswith(b"]"):
            return None
        before = window[:stripped.rindex(b"]")].rstrip()
        return size - len(window) + len(before)

    def read(self, source: str) -> List[Dict[str, Any]]:
        """Every record for ``source`` (``[]`` when absent or unreadable)."""
        path = self.path_for(source)
        if not path.is_file():
            return []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        return [data] if isinstance(data, dict) else []  # an older single-object file

    def scraped(self, source: str) -> Set[str]:
        """ISBN-13s this source's file already holds -- the skip decision."""
        key = str(self.path_for(source))
        if key not in _ISBNS:
            _ISBNS[key] = {i for r in self.read(source)
                           if (i := str(r.get("isbn13") or "").strip())}
        return _ISBNS[key]

    def record(self, source: str, isbn13: str) -> Optional[Dict[str, Any]]:
        """One stored record, to recover a hint when its source was skipped."""
        key = str(self.path_for(source))
        if key not in _RECORDS:
            _RECORDS[key] = {i: r for r in self.read(source)
                             if (i := str(r.get("isbn13") or "").strip())}
        return _RECORDS[key].get(str(isbn13).strip())
