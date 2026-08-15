"""Read the run's ISBN list from a CSV file. Pure stdlib, one file read.

Forgiving about shape, strict about values: the delimiter is sniffed, a BOM and
CRLF are normal, the ISBN column is found from the header or from whichever
column actually holds the most valid ISBNs, and every cell still goes through
:func:`bookscraper.isbn.to_isbn13`. A bad row is reported with its line number
and skipped; only an unreadable file raises.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

from . import isbn as isbn_utils

#: Suffixes that mean "this argument is a list file, not an ISBN".
SUFFIXES = frozenset({".csv", ".tsv", ".txt", ".psv"})
_DELIMS = ",;\t|"
#: Header spellings meaning "ISBN column", most specific group first.
_ISBN_HEADERS = (("isbn13", "ean13"), ("isbn", "isbns"), ("isbn10",),
                 ("ean", "barcode", "gtin", "gtin13"))
_TITLE_HEADERS = frozenset({"title", "booktitle", "name", "book", "bookname"})


class CsvError(Exception):
    """The CSV could not be used at all (missing, unreadable, a directory)."""


@dataclass(frozen=True)
class Entry:
    """One ISBN to scrape, with the physical row it came from."""

    isbn13: str
    row: int = 0
    label: Optional[str] = None


@dataclass
class Isbns:
    """What one CSV read produced: usable ISBNs plus what went wrong."""

    path: Path
    entries: List[Entry] = field(default_factory=list)
    #: ``(row, reason)`` for every row that yielded no ISBN.
    problems: List[Tuple[int, str]] = field(default_factory=list)
    duplicates: int = 0
    column: str = "column 1"

    def summarise(self) -> str:
        bits = [f"{len(self.entries)} ISBN(s) from {self.path}", self.column]
        if self.duplicates:
            bits.append(f"{self.duplicates} duplicate(s) dropped")
        if self.problems:
            bits.append(f"{len(self.problems)} row(s) skipped")
        return ", ".join(bits)


def looks_like_path(value: str) -> bool:
    """True when ``value`` should be read as a list file rather than an ISBN.

    A *missing* file with a list-file suffix still returns True, so the caller
    reports it as a usage error instead of parsing it as an ISBN.
    """
    text = (value or "").strip()
    if not text:
        return False
    try:
        path = Path(text).expanduser()
        return path.suffix.lower() in SUFFIXES or path.is_file()
    except (RuntimeError, OSError, ValueError):
        return False


def _key(text: str) -> str:
    """``'Isbn-13 '`` -> ``'isbn13'``, so header spellings compare equal."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").casefold())


def _decode(path: Path) -> str:
    """UTF-8 (BOM tolerated), then cp1252, then latin-1 which cannot fail."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CsvError(f"cannot read {path}: {exc}") from exc
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def _delimiter(sample: str) -> str:
    """Sniff the delimiter, counting candidates when the sniffer gives up.

    ``csv.Sniffer`` fails on single-column files -- which this project's own CSV
    is -- so the fallback counts candidates on the first non-empty line.
    """
    try:
        return csv.Sniffer().sniff(sample, delimiters=_DELIMS).delimiter
    except csv.Error:
        first = next((ln for ln in sample.splitlines() if ln.strip()), "")
        best = max(_DELIMS, key=first.count)
        return best if first.count(best) else ","


def _is_header(cells: Sequence[str]) -> bool:
    """True when the first row is a header: no ISBN in it, but some letters."""
    return (not any(isbn_utils.is_valid(c) for c in cells)
            and any(re.search(r"[A-Za-z]", c) for c in cells if c))


def _pick_column(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> Tuple[int, str]:
    """Choose the ISBN column: an ISBN-ish header that holds ISBNs, else the
    column holding the most valid ISBNs across a 200-row sample."""
    counts: dict = {}
    for cells in rows[:200]:
        for index, cell in enumerate(cells):
            if isbn_utils.is_valid(cell):
                counts[index] = counts.get(index, 0) + 1

    named = next((i for group in _ISBN_HEADERS for i, h in enumerate(headers)
                  if _key(h) in group), None)
    chosen = named if named is not None and counts.get(named) else (
        max(counts, key=lambda i: (counts[i], -i)) if counts
        else (named if named is not None else 0))
    header = headers[chosen] if chosen < len(headers) and headers[chosen] else ""
    return chosen, f"column {chosen + 1}" + (f" ({header!r})" if header else "")


def read_isbns(path: Union[Path, str]) -> Isbns:
    """Read the ISBNs out of the CSV at ``path``, in file order, de-duplicated."""
    target = Path(str(path).strip()).expanduser()
    if target.is_dir():
        raise CsvError(f"{target} is a directory, not a CSV file")
    if not target.exists():
        raise CsvError(f"{target} does not exist")

    text = _decode(target)
    found = Isbns(path=target)
    if not text.strip():
        found.problems.append((0, f"{target.name} is empty"))
        return found

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(text[:8192]))
    rows: List[Tuple[int, List[str]]] = []
    while True:  # a runaway quote or NUL kills one row, not the file
        try:
            cells = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            found.problems.append((reader.line_num, f"unparseable ({exc})"))
            continue
        stripped = [c.strip() for c in cells]
        if any(stripped) and not next(c for c in stripped if c).startswith("#"):
            rows.append((reader.line_num, stripped))

    headers: List[str] = []
    if rows and _is_header(rows[0][1]):
        headers = rows.pop(0)[1]
    if not rows:
        found.problems.append((0, f"{target.name} has no data rows"))
        return found

    index, found.column = _pick_column(headers, [cells for _, cells in rows])
    label_at = next((i for i, h in enumerate(headers)
                     if i != index and _key(h) in _TITLE_HEADERS), None)
    seen: set = set()

    for number, cells in rows:
        cell = cells[index] if index < len(cells) else ""
        if not cell:
            found.problems.append((number, f"{found.column} is empty"))
            continue
        try:
            isbn13 = isbn_utils.to_isbn13(cell)
        except ValueError as exc:
            found.problems.append((number, str(exc)))
            continue
        if isbn13 in seen:
            found.duplicates += 1
            continue
        seen.add(isbn13)
        label = cells[label_at] if label_at is not None and label_at < len(cells) else None
        found.entries.append(Entry(isbn13=isbn13, row=number, label=label or None))

    return found
