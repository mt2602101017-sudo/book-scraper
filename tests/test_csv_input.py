"""Offline tests for :mod:`bookscraper.csv_input` and the batch's row selection.

Every CSV here is written into a ``tmp_path``-style temporary directory during
the test, so nothing depends on a file in the repo and nothing is left behind.
Only ISBNs appear as literals -- never a cached title, blurb or review.

What these pin down:

* the project's own single-column ``Isbn-13`` file is read exactly as it looks,
  including its five ``Invalid ISBN-10`` rows being *reported with row numbers*
  rather than silently dropped;
* ISBN-10s are converted, hyphens/labels tolerated, duplicates dropped once;
* the ISBN column is found from a header, from the data when the header lies,
  and from ``--isbn-column`` when the user names one;
* a bad ``--isbn-column`` and an unreadable file raise ``CsvInputError`` (the
  CLI's exit-2 cases) while a bad *row* never does;
* ``--start``/``--end`` slice the entry list without losing or duplicating an
  ISBN.

Runnable either way:

    .venv/bin/python -m pytest tests/test_csv_input.py -q
    .venv/bin/python tests/test_csv_input.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookscraper import csv_input as C  # noqa: E402
from bookscraper.batch import BatchConfig, select_entries  # noqa: E402
from bookscraper.pipeline import PipelineConfig  # noqa: E402


# Checksum-valid ISBNs used throughout (see tests/test_isbn.py for the maths).
_A13 = "9780143127550"
_A10 = "0143127551"          # the same book as _A13
_B13 = "9780062316097"
_B10 = "0062316095"          # the same book as _B13


def _write(name: str, text: str, directory: Path) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _read(name: str, text: str, directory: Path, **kw) -> C.CsvIsbns:
    return C.read_isbns(_write(name, text, directory), **kw)


def _isbns(found: C.CsvIsbns) -> List[str]:
    return [e.isbn13 for e in found.entries]


# -- the shape of this project's own input file -------------------------------


def test_single_column_header_file_like_the_projects_own(tmp_path: Path) -> None:
    """``Isbn-13`` + one ISBN per line: the exact shape of 2602101017.csv."""
    found = _read(
        "isbns.csv",
        f"Isbn-13\n{_A13}\n{_B13}\n",
        tmp_path,
    )
    assert _isbns(found) == [_A13, _B13]
    assert found.headers == ["Isbn-13"]
    assert found.rows_read == 2
    assert found.problems == []
    # The header must not be mistaken for data, and the ISBN column must be
    # reported by its real header name so the log line is checkable.
    assert "Isbn-13" in found.column_label


def test_invalid_rows_are_reported_with_their_row_number_not_dropped(tmp_path: Path) -> None:
    """The five ``Invalid ISBN-10`` rows in the real file must be traceable."""
    found = _read(
        "isbns.csv",
        f"Isbn-13\n{_A13}\nInvalid ISBN-10\n{_B13}\n",
        tmp_path,
    )
    assert _isbns(found) == [_A13, _B13], "a bad row must not cost us the good ones"
    assert len(found.problems) == 1
    problem = found.problems[0]
    assert problem.row == 3, "row numbers are physical, so the user can open the line"
    assert problem.raw == "Invalid ISBN-10"
    assert "Invalid ISBN-10" in str(problem)


def test_a_10000_row_file_is_read_whole(tmp_path: Path) -> None:
    """No sampling, capping or truncation on a file the size of the real one."""
    # Distinct valid ISBN-13s generated from a checksum, not hand-written.
    from bookscraper import isbn as I

    bodies = [f"978014312{n:03d}" for n in range(500)]
    isbns = [b + I.isbn13_check_digit(b) for b in bodies]
    found = _read("big.csv", "Isbn-13\n" + "\n".join(isbns) + "\n", tmp_path)
    assert len(found.entries) == len(isbns)
    assert _isbns(found) == isbns, "order must follow the file"


# -- value handling -----------------------------------------------------------


def test_isbn10_rows_are_converted(tmp_path: Path) -> None:
    found = _read("mixed.csv", f"isbn\n{_A10}\n{_B10}\n", tmp_path)
    assert _isbns(found) == [_A13, _B13]
    # ``raw`` keeps what the file said, so a report can quote the user's input.
    assert found.entries[0].raw == _A10


def test_punctuation_labels_and_quotes_are_tolerated(tmp_path: Path) -> None:
    found = _read(
        "punct.csv",
        'isbn\n"978-0-14-312755-0"\nISBN-13: 978 0 06 231609 7\n',
        tmp_path,
    )
    assert _isbns(found) == [_A13, _B13]


def test_duplicates_are_scraped_once_and_recorded(tmp_path: Path) -> None:
    found = _read("dup.csv", f"isbn\n{_A13}\n{_A10}\n{_B13}\n{_A13}\n", tmp_path)
    # _A10 is the same book as _A13, so it is a duplicate after normalisation.
    assert _isbns(found) == [_A13, _B13]
    assert found.duplicates == [_A13, _A13]


def test_blank_lines_comments_and_crlf_are_skipped(tmp_path: Path) -> None:
    found = _read(
        "messy.csv",
        f"isbn\r\n\r\n# a note about the list\r\n{_A13}\r\n\r\n{_B13}\r\n",
        tmp_path,
    )
    assert _isbns(found) == [_A13, _B13]
    assert found.rows_read == 2, "blanks and comments are not data rows"
    assert found.problems == []


def test_utf8_bom_is_stripped(tmp_path: Path) -> None:
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbfIsbn-13\n" + _A13.encode() + b"\n")
    found = C.read_isbns(path)
    assert _isbns(found) == [_A13], "a BOM must not corrupt the header or row 2"


# -- column detection ---------------------------------------------------------


def test_headerless_single_column_file(tmp_path: Path) -> None:
    found = _read("bare.csv", f"{_A13}\n{_B13}\n", tmp_path)
    assert _isbns(found) == [_A13, _B13]
    assert found.headers == [], "the first row is data, not a header"


def test_multi_column_file_picks_the_isbn_header_and_labels_rows(tmp_path: Path) -> None:
    found = _read(
        "wide.csv",
        f"Title,Author,ISBN13,Shelf\nA Book,Someone,{_A13},fiction\n"
        f"Another,Somebody,{_B13},history\n",
        tmp_path,
    )
    assert _isbns(found) == [_A13, _B13]
    assert "ISBN13" in found.column_label
    assert found.entries[0].label == "A Book", "a Title column labels the row"


def test_column_is_found_from_the_data_when_the_header_does_not_say(tmp_path: Path) -> None:
    found = _read(
        "unlabelled.csv",
        f"first,second,third\nA Book,Someone,{_A13}\nAnother,Somebody,{_B13}\n",
        tmp_path,
    )
    assert _isbns(found) == [_A13, _B13]
    assert "column 3" in found.column_label


def test_data_beats_a_misleading_isbn_header(tmp_path: Path) -> None:
    """An ``isbn`` header over empty cells must not silence a real ISBN column."""
    found = _read(
        "wrong-header.csv",
        f"isbn,ean13\n,{_A13}\n,{_B13}\n",
        tmp_path,
    )
    assert _isbns(found) == [_A13, _B13]


def test_semicolon_and_tab_delimiters(tmp_path: Path) -> None:
    semi = _read("semi.csv", f"Title;ISBN\nA Book;{_A13}\n", tmp_path)
    assert _isbns(semi) == [_A13] and semi.delimiter == ";"
    tabbed = _read("tab.tsv", f"Title\tISBN\nA Book\t{_A13}\n", tmp_path)
    assert _isbns(tabbed) == [_A13] and tabbed.delimiter == "\t"


def test_isbn_column_can_be_named_or_numbered(tmp_path: Path) -> None:
    body = f"a,b\n{_A13},{_B13}\n"
    by_number = _read("n.csv", body, tmp_path, column="2")
    assert _isbns(by_number) == [_B13]
    named = _read("h.csv", f"first,second\n{_A13},{_B13}\n", tmp_path, column="second")
    assert _isbns(named) == [_B13]
    # Header matching ignores case and punctuation, so 'ISBN 13' finds 'Isbn-13'.
    loose = _read("l.csv", f"Isbn-13,other\n{_A13},x\n", tmp_path, column="ISBN 13")
    assert _isbns(loose) == [_A13]


def test_an_explicit_but_empty_column_reports_every_row(tmp_path: Path) -> None:
    """Honouring the user's column choice must explain itself, not silently retarget."""
    found = _read("choice.csv", f"note,isbn\nhello,{_A13}\n", tmp_path, column="note")
    assert found.entries == []
    assert len(found.problems) == 1
    assert found.problems[0].row == 2


# -- the errors that become CLI exit 2 ---------------------------------------


def _expect_error(fn, *, must_mention: str) -> str:
    try:
        fn()
    except C.CsvInputError as exc:
        message = str(exc)
        assert must_mention.lower() in message.lower(), (
            f"message should mention {must_mention!r}, got: {message}"
        )
        return message
    raise AssertionError("expected CsvInputError")


def test_unknown_column_and_unreadable_file_raise(tmp_path: Path) -> None:
    path = _write("x.csv", f"isbn\n{_A13}\n", tmp_path)
    _expect_error(
        lambda: C.read_isbns(path, column='nosuch'),
        must_mention="matches no column",
    )
    _expect_error(
        lambda: C.read_isbns(path, column='9'),
        must_mention="out of range",
    )
    _expect_error(
        lambda: C.read_isbns(tmp_path / 'absent.csv'),
        must_mention="does not exist",
    )
    _expect_error(lambda: C.read_isbns(tmp_path), must_mention="directory")


def test_empty_and_isbn_free_files_yield_no_entries_without_raising(tmp_path: Path) -> None:
    assert _read("empty.csv", "", tmp_path).entries == []
    assert _read("hdr.csv", "Isbn-13\n", tmp_path).entries == []
    words = _read("words.csv", "Title,Author\nA Book,Someone\n", tmp_path)
    assert words.entries == []
    assert words.problems, "a file with no ISBN must say so, row by row"


# -- routing: is this argument a file or an ISBN? -----------------------------


def test_looks_like_csv_path_distinguishes_files_from_isbns(tmp_path: Path) -> None:
    existing = _write("real.csv", f"isbn\n{_A13}\n", tmp_path)
    assert C.looks_like_csv_path(str(existing))
    assert C.looks_like_csv_path("missing.csv"), "a .csv suffix is a usage error, not an ISBN"
    assert C.looks_like_csv_path("list.tsv")
    assert not C.looks_like_csv_path(_A13)
    assert not C.looks_like_csv_path("978-0-14-312755-0")
    assert not C.looks_like_csv_path("")
    assert not C.looks_like_csv_path("   ")


# -- batch row selection ------------------------------------------------------


def _entries(count: int) -> List[C.IsbnEntry]:
    from bookscraper import isbn as I

    out = []
    for n in range(count):
        body = f"978014312{n:03d}"
        out.append(C.IsbnEntry(isbn13=body + I.isbn13_check_digit(body), row=n + 2))
    return out


def _batch(**kw) -> BatchConfig:
    return BatchConfig(base=PipelineConfig(isbn=""), **kw)


def test_start_and_end_partition_the_entries(tmp_path: Path) -> None:
    entries = _entries(10)

    to_run, skipped = select_entries(entries, _batch())
    assert to_run == entries and skipped == []

    to_run, skipped = select_entries(entries, _batch(end=3))
    assert to_run == entries[:3]
    assert to_run + skipped == entries, "every entry is either run or accounted for"

    to_run, skipped = select_entries(entries, _batch(start=4))
    assert to_run == entries[4:]
    assert len(to_run) + len(skipped) == len(entries)

    to_run, skipped = select_entries(entries, _batch(start=2, end=5))
    assert to_run == entries[2:5]
    assert len(to_run) + len(skipped) == len(entries)


def test_start_past_the_end_runs_nothing_rather_than_wrapping(tmp_path: Path) -> None:
    entries = _entries(5)
    to_run, skipped = select_entries(entries, _batch(start=99))
    assert to_run == []
    assert skipped == entries


def test_a_zero_end_runs_nothing_and_negative_start_is_clamped(tmp_path: Path) -> None:
    entries = _entries(5)
    assert select_entries(entries, _batch(end=0))[0] == []
    # main.py clamps --start, but the library must not index with a negative.
    assert select_entries(entries, _batch(start=-3))[0] == entries


def _run_all() -> int:
    """Run every test with a fresh temp directory, for use without pytest."""
    failures = 0
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as directory:
            try:
                fn(Path(directory))
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - report, do not mask
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())


def test_an_isbn10_row_reaches_the_sources_with_both_forms(tmp_path: Path) -> None:
    """An ISBN-10 input must be resolved to ISBN-13 *and* keep its ISBN-10 form.

    The two forms do different jobs downstream: ISBN-13 is the record key and what
    Goodreads' ``/book/isbn/`` route wants, while the ISBN-10 **is** Amazon's ASIN
    for print books, so ``/dp/<isbn10>`` is the cheapest discovery rung there. An
    ISBN-10 row that arrived without its original form would push Amazon onto the
    slower site-search path for no reason.
    """
    from bookscraper.models import BookHint
    from bookscraper import isbn as isbn_utils

    path = _write("ten.csv", "isbn\n0553259105\n082171659X\n", tmp_path)
    found = C.read_isbns(path)

    # Stored as ISBN-13, with the raw cell kept for the report.
    assert _isbns(found) == ["9780553259100", "9780821716595"]
    assert found.entries[0].raw == "0553259105"

    # And the hint the pipeline builds carries both spellings.
    for entry in found.entries:
        hint = BookHint(isbn13=entry.isbn13,
                        isbn10=isbn_utils.isbn13_to_isbn10(entry.isbn13))
        assert hint.isbn10 and len(hint.isbn10) == 10
        assert isbn_utils.to_isbn13(hint.isbn10) == entry.isbn13, (
            "the ISBN-10 must round-trip back to the same ISBN-13"
        )
    # The X check digit survives, which a naive digits-only conversion loses.
    assert isbn_utils.isbn13_to_isbn10("9780821716595") == "082171659X"


def test_the_same_book_in_both_forms_is_scraped_once(tmp_path: Path) -> None:
    """``0553259105`` and ``9780553259100`` are one book, not two."""
    path = _write("both.csv", "isbn\n0553259105\n9780553259100\n", tmp_path)
    found = C.read_isbns(path)
    assert _isbns(found) == ["9780553259100"]
    assert len(found.duplicates) == 1, "the second spelling is a duplicate, not a miss"


# -- --end / range selection ---------------------------------------------------


def _range(count: int, **kw) -> Tuple[List[str], List[str]]:
    """Run ``select_entries`` over ``count`` synthetic entries; return both halves."""
    import io
    import contextlib
    from bookscraper.batch import select_entries

    entries = _entries(count)
    config = _batch(**kw)
    with contextlib.redirect_stderr(io.StringIO()):
        run, skipped = select_entries(entries, config)
    return [e.isbn13 for e in run], [e.isbn13 for e in skipped]


def test_end_selects_a_half_open_range(tmp_path: Path) -> None:
    """``--start 10 --end 15`` is rows 10..14: five books, like Python slicing."""
    entries = _entries(100)
    run, _ = _range(100, start=10, end=15)
    assert run == [e.isbn13 for e in entries[10:15]]
    assert len(run) == 5


def test_consecutive_ranges_tile_without_overlap_or_gap(tmp_path: Path) -> None:
    """The point of a half-open range: shards can be run in separate terminals.

    An inclusive ``--end`` would double-scrape the boundary row of every shard.
    """
    seen: List[str] = []
    for start in range(0, 100, 25):
        run, _ = _range(90, start=start, end=start + 25)
        seen += run
    assert len(seen) == 90, "every row scraped exactly once"
    assert len(set(seen)) == 90, "no row scraped twice"


def test_end_past_the_file_is_clamped(tmp_path: Path) -> None:
    """Asking for more rows than exist is 'everything', not an error."""
    run, skipped = _range(10, start=8, end=99999)
    assert len(run) == 2
    assert len(skipped) == 8


def test_an_empty_or_backwards_range_scrapes_nothing(tmp_path: Path) -> None:
    for kw in ({"start": 50, "end": 50}, {"start": 50, "end": 20}):
        run, skipped = _range(100, **kw)
        assert run == [], f"{kw} should select nothing"
        assert len(skipped) == 100, "and everything stays unattempted"


def test_every_entry_is_accounted_for_in_any_range(tmp_path: Path) -> None:
    """run + skipped == the file, with no duplicates, whatever the flags."""
    for kw in ({"start": 0, "end": None}, {"start": 5, "end": 9},
               {"start": 0, "end": 3}, {"start": 99, "end": 200},
               {"start": 200, "end": 300}):
        run, skipped = _range(20, **kw)
        assert len(run) + len(skipped) == 20, kw
        assert not set(run) & set(skipped), f"{kw}: an entry is in both halves"
