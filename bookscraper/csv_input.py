"""Read the run's ISBN list from a CSV file.

Pure stdlib, one file read, no network. The job is narrow but the input is
untrusted, so this module is deliberately forgiving about *shape* and strict
about *values*:

* the delimiter is sniffed (``,`` ``;`` tab ``|``), so a semicolon-separated
  export from a European spreadsheet works unchanged;
* a UTF-8 BOM, CRLF line endings, quoted cells, comment lines and blank lines
  are all normal;
* the ISBN column is found from the header (``Isbn-13``, ``ISBN``, ``EAN``...),
  and when the header says nothing useful the column that actually contains the
  most valid ISBNs is used -- so a headerless one-column file and a 12-column
  library export both work;
* every value still goes through :func:`bookscraper.isbn.to_isbn13`, so an
  ISBN-10 is converted and a bad checksum is reported *with its row number* and
  skipped rather than scraped.

Nothing here raises for bad data: a row that cannot be used becomes an entry in
:attr:`CsvIsbns.problems` and the rest of the file is still read.
:class:`CsvInputError` is reserved for the genuine usage errors -- the file
cannot be read at all, or ``--isbn-column`` names a column that does not exist.
"""

from __future__ import annotations

import sys
import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import isbn as isbn_utils

__all__ = [
    "IsbnEntry",
    "RowProblem",
    "CsvIsbns",
    "CsvInputError",
    "read_isbns",
    "looks_like_csv_path",
]


#: Suffixes that mean "this argument is a list file, not an ISBN".
CSV_SUFFIXES = frozenset({".csv", ".tsv", ".txt", ".psv"})

#: Delimiters offered to :class:`csv.Sniffer`, and counted in the fallback.
_DELIMITERS = ",;\t|"

#: Header spellings that mean "this column holds the ISBN", most specific first.
#: Compared after :func:`_norm_header` strips punctuation and case, so
#: ``Isbn-13``, ``ISBN_13`` and ``isbn13`` are all the same key.
_ISBN_HEADERS: Tuple[Tuple[str, ...], ...] = (
    ("isbn13", "ean13", "isbnthirteen"),
    ("isbn", "isbns", "isbnnumber", "isbnno"),
    ("isbn10", "isbnten"),
    ("ean", "barcode", "gtin", "gtin13"),
)

#: Header spellings used only to label rows in logs and the batch manifest.
_TITLE_HEADERS = frozenset({"title", "booktitle", "name", "book", "bookname"})

#: A row whose first non-empty cell starts with this is a comment, not data.
_COMMENT_PREFIX = "#"


class CsvInputError(Exception):
    """The CSV could not be used at all (unreadable file, unknown column)."""


@dataclass(frozen=True)
class IsbnEntry:
    """One ISBN the run should scrape.

    :param isbn13: normalised, checksum-valid ISBN-13 -- what the pipeline uses.
    :param raw: the cell exactly as it appeared, so messages can quote the input.
    :param row: 1-based *physical* row number in the file (``0`` for an ISBN
        given on the command line), so a report points at a line the user can open.
    :param label: an adjacent title/name cell when the CSV had one; cosmetic only.
    """

    isbn13: str
    raw: str = ""
    row: int = 0
    label: Optional[str] = None

    def describe(self) -> str:
        """Short form for log lines: the ISBN plus whatever context we have."""
        bits = [self.isbn13]
        if self.label:
            bits.append(repr(self.label))
        if self.raw and isbn_utils.normalize(self.raw) != self.isbn13:
            bits.append(f"(from {self.raw!r})")
        return " ".join(bits)


@dataclass(frozen=True)
class RowProblem:
    """A row that could not be turned into an ISBN, and why."""

    row: int
    raw: str
    reason: str

    def __str__(self) -> str:
        where = f"row {self.row}" if self.row else "input"
        return f"{where}: {self.reason}"


@dataclass
class CsvIsbns:
    """Everything one CSV read produced: the usable ISBNs plus what went wrong."""

    path: Path
    entries: List[IsbnEntry] = field(default_factory=list)
    #: Rows that were skipped, each carrying its physical row number.
    problems: List[RowProblem] = field(default_factory=list)
    #: ISBN-13s that appeared more than once (the first occurrence is kept).
    duplicates: List[str] = field(default_factory=list)
    #: How the ISBN column was chosen, for one explanatory log line.
    column_label: str = "column 1"
    delimiter: str = ","
    #: Data rows considered (excludes the header, blank lines and comments).
    rows_read: int = 0
    #: The header cells, when the file had a header row.
    headers: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def summarise(self) -> str:
        """One line describing the read, suitable for ``log.info``."""
        bits = [
            f"{len(self.entries)} ISBN(s) from {self.path}",
            self.column_label,
            f"delimiter {self.delimiter!r}",
        ]
        if self.duplicates:
            bits.append(f"{len(self.duplicates)} duplicate(s) dropped")
        if self.problems:
            bits.append(f"{len(self.problems)} row(s) skipped")
        return ", ".join(bits)


def looks_like_csv_path(value: str) -> bool:
    """True when ``value`` should be read as a list file rather than as an ISBN.

    Either it names something that exists on disk, or it carries a list-file
    suffix -- in which case a *missing* file is a usage error the caller must
    report, not a string to run through the ISBN parser.
    """
    if not value or not value.strip():
        return False
    try:
        path = Path(value.strip()).expanduser()
    except (RuntimeError, OSError, ValueError):
        return False
    if path.suffix.lower() in CSV_SUFFIXES:
        return True
    try:
        return path.is_file()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _norm_header(text: str) -> str:
    """Reduce a header cell to a comparable key: ``'Isbn-13 '`` -> ``'isbn13'``."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").casefold())


def _decode(path: Path) -> Tuple[str, Optional[str]]:
    """Read ``path`` as text. Returns ``(text, problem_or_None)``.

    UTF-8 (BOM tolerated) first, then cp1252 -- between them they cover every
    spreadsheet export we have met. Anything else is read as ``latin-1``, which
    cannot fail, and the substitution is reported rather than hidden.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CsvInputError(f"cannot read {path}: {exc}") from exc
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding), None
        except UnicodeDecodeError:
            continue
    return (
        raw.decode("latin-1", "replace"),
        f"{path.name} is not valid UTF-8 or cp1252; it was read as latin-1 and some "
        "characters may be wrong (ISBN digits are unaffected)",
    )


def _sniff_delimiter(sample: str) -> str:
    """Best-effort delimiter detection with a deterministic fallback."""
    if not sample.strip():
        return ","
    try:
        return csv.Sniffer().sniff(sample, delimiters=_DELIMITERS).delimiter
    except csv.Error:
        pass
    # Sniffer gives up on single-column files (this project's own CSV is one),
    # so count candidates on the first non-empty line instead.
    first = next((line for line in sample.splitlines() if line.strip()), "")
    counts = {d: first.count(d) for d in _DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def _read_rows(text: str, delimiter: str) -> Tuple[List[Tuple[int, List[str]]], List[RowProblem]]:
    """Parse ``text`` into ``(row_number, cells)`` pairs, keeping physical numbers.

    ``csv.reader`` handles quoted cells containing the delimiter or a newline, so
    row numbers come from its ``line_num`` rather than an ``enumerate`` index.
    """
    problems: List[RowProblem] = []
    rows: List[Tuple[int, List[str]]] = []
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    while True:
        try:
            cells = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            # A NUL byte or a runaway quote kills one row, not the whole file.
            problems.append(
                RowProblem(reader.line_num, "", f"could not be parsed ({exc})")
            )
            continue
        rows.append((reader.line_num, [c.strip() for c in cells]))
    return rows, problems


def _is_data(cells: Sequence[str]) -> bool:
    """True unless the row is entirely blank or a ``#`` comment."""
    first = next((c for c in cells if c), "")
    return bool(first) and not first.startswith(_COMMENT_PREFIX)


def _parses(cell: str) -> bool:
    """True when ``cell`` is a checksum-valid ISBN in any punctuation style."""
    if not cell:
        return False
    try:
        isbn_utils.to_isbn13(cell)
    except ValueError:
        return False
    return True


def _looks_like_header(cells: Sequence[str]) -> bool:
    """True when the first data row is a header rather than a record.

    A header contains no parseable ISBN *and* at least one cell with a letter.
    That keeps ``Isbn-13`` a header and ``0143127551,Eat Pray Love`` data, which
    a bare "does any cell contain a letter" test would misread.
    """
    if any(_parses(c) for c in cells):
        return False
    return any(re.search(r"[A-Za-z]", c) for c in cells if c)


def _column_counts(rows: Sequence[Tuple[int, List[str]]], sample: int = 200) -> Dict[int, int]:
    """How many valid ISBNs each column index holds across the first ``sample`` rows.

    Only a sample is checksummed: column detection is a shape question, and a
    10 000-row file should not pay for a second full validation pass.
    """
    counts: Dict[int, int] = {}
    for _, cells in rows[:sample]:
        for index, cell in enumerate(cells):
            if _parses(cell):
                counts[index] = counts.get(index, 0) + 1
    return counts


def _requested_column(requested: str, headers: Sequence[str], width: int) -> int:
    """Resolve ``--isbn-column`` to an index: a header name or a 1-based number.

    :raises CsvInputError: when it matches neither, listing what *is* available.
    """
    wanted = _norm_header(requested)
    for index, header in enumerate(headers):
        if wanted and _norm_header(header) == wanted:
            return index
    text = str(requested).strip()
    if text.isdigit():
        number = int(text)
        columns = max(width, len(headers))
        if 1 <= number <= columns:
            return number - 1
        raise CsvInputError(
            f"the isbn_column setting {requested!r} is out of range: the file has "
            f"{columns} column(s)"
        )
    available = ", ".join(repr(h) for h in headers if h) or "(no header row)"
    raise CsvInputError(
        f"the isbn_column setting {requested!r} matches no column. Available: "
        f"{available}. You can also give a 1-based column number."
    )


def _header_column(headers: Sequence[str]) -> Optional[int]:
    """Index of the best ISBN-ish header, or ``None`` when none look like one."""
    normalised = [_norm_header(h) for h in headers]
    for group in _ISBN_HEADERS:
        for index, key in enumerate(normalised):
            if key in group:
                return index
    return None


def _label_column(headers: Sequence[str], isbn_index: int) -> Optional[int]:
    """Index of a title-ish column to label rows with, if the header offers one."""
    for index, header in enumerate(headers):
        if index != isbn_index and _norm_header(header) in _TITLE_HEADERS:
            return index
    return None


def _choose_column(
    headers: Sequence[str],
    data: Sequence[Tuple[int, List[str]]],
    requested: Optional[str],
    width: int,
) -> Tuple[int, str, List[str]]:
    """Decide which column holds the ISBN.

    Returns ``(index, label_for_logs, notes)``. Precedence:

    1. an explicit ``--isbn-column`` -- honoured even when it turns out to hold
       no ISBN, because the per-row problems then say exactly what was wrong
       with the user's own choice;
    2. an ISBN-ish header, *provided* that column really does contain ISBNs;
    3. whichever column contains the most valid ISBNs;
    4. the ISBN-ish header, else column 1, so every row can still be reported.
    """
    notes: List[str] = []
    counts = _column_counts(data)
    named = _header_column(headers)

    def label(index: int) -> str:
        header = headers[index] if index < len(headers) and headers[index] else ""
        return f"column {index + 1}" + (f" ({header!r})" if header else "")

    if requested is not None:
        index = _requested_column(requested, headers, width)
        if not counts.get(index) and counts:
            best = max(counts, key=lambda i: (counts[i], -i))
            notes.append(
                f"the isbn_column setting selected {label(index)}, which holds no valid ISBN, "
                f"while {label(best)} does. Every row below is reported as skipped "
                "for that reason."
            )
        return index, label(index), notes

    if named is not None and counts.get(named):
        return named, label(named), notes

    if counts:
        best = max(counts, key=lambda i: (counts[i], -i))
        if named is not None:
            notes.append(
                f"the {headers[named]!r} header suggested {label(named)}, but no valid "
                f"ISBN was found there; using {label(best)} instead"
            )
        elif headers:
            notes.append(
                f"no ISBN-like header found; using {label(best)}, which holds valid ISBNs"
            )
        return best, label(best), notes

    index = named if named is not None else 0
    notes.append(
        "no column in this file contains a checksum-valid ISBN; reading "
        f"{label(index)} so that each row can be reported"
    )
    return index, label(index), notes


def read_isbns(
    path: Path | str,
    column: Optional[str] = None,
) -> CsvIsbns:
    """Read the ISBNs out of the CSV at ``path``, in file order, de-duplicated.

    :param column: optional ``--isbn-column`` override (header name or 1-based
        column number). ``None`` auto-detects.
    :raises CsvInputError: if the file cannot be read, is not a file, or
        ``column`` names something that does not exist.
    """
    try:
        target = Path(path).expanduser()
    except RuntimeError as exc:
        raise CsvInputError(f"cannot resolve {path!r} to a path ({exc})") from exc
    if target.is_dir():
        raise CsvInputError(f"{target} is a directory, not a CSV file")
    if not target.exists():
        raise CsvInputError(f"{target} does not exist")

    text, decode_problem = _decode(target)
    result = CsvIsbns(path=target)
    if decode_problem:
        result.problems.append(RowProblem(0, "", decode_problem))
    if not text.strip():
        result.problems.append(RowProblem(0, "", f"{target.name} is empty"))
        return result

    result.delimiter = _sniff_delimiter(text[:8192])
    rows, parse_problems = _read_rows(text, result.delimiter)
    result.problems.extend(parse_problems)

    usable = [(number, cells) for number, cells in rows if _is_data(cells)]
    if not usable:
        result.problems.append(
            RowProblem(0, "", f"{target.name} contains no data rows")
        )
        return result

    if _looks_like_header(usable[0][1]):
        result.headers = list(usable[0][1])
        usable = usable[1:]
        if not usable:
            result.problems.append(
                RowProblem(0, "", f"{target.name} has a header row but no data rows")
            )
            return result

    width = max(len(cells) for _, cells in usable)
    index, column_label, notes = _choose_column(result.headers, usable, column, width)
    result.column_label = column_label
    for note in notes:
        print('warning: %s: %s' % (target.name, note), file=sys.stderr)

    label_index = _label_column(result.headers, index) if result.headers else None
    seen: Dict[str, int] = {}

    for number, cells in usable:
        result.rows_read += 1
        cell = cells[index] if index < len(cells) else ""
        if not cell:
            result.problems.append(
                RowProblem(number, "", f"{column_label} is empty")
            )
            continue
        try:
            isbn13 = isbn_utils.to_isbn13(cell)
        except ValueError as exc:
            result.problems.append(RowProblem(number, cell, str(exc)))
            continue
        if isbn13 in seen:
            result.duplicates.append(isbn13)
            print('row %d: %s already appeared on row %d; it is scraped once' % (number, isbn13, seen[isbn13]), file=sys.stderr)
            continue
        seen[isbn13] = number
        label = None
        if label_index is not None and label_index < len(cells):
            label = cells[label_index] or None
        result.entries.append(
            IsbnEntry(isbn13=isbn13, raw=cell, row=number, label=label)
        )

    return result
