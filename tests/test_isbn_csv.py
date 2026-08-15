"""ISBN normalisation and CSV input. No network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bookscraper import csv_input, isbn  # noqa: E402

#: Reference pairs whose checksums are known good.
PAIRS = (("0143127551", "9780143127550"), ("0062316095", "9780062316097"))


# -- ISBN -------------------------------------------------------------------

@pytest.mark.parametrize("ten,thirteen", PAIRS)
def test_isbn_round_trips_both_ways(ten: str, thirteen: str) -> None:
    assert isbn.to_isbn13(ten) == thirteen
    assert isbn.to_isbn13(thirteen) == thirteen
    assert isbn.to_isbn10(thirteen) == ten


def test_isbn_normalisation_tolerates_labels_and_punctuation() -> None:
    assert isbn.normalize(" isbn-10: 0-306-40615-x ") == "030640615X"
    assert isbn.to_isbn13("978-0-14-312755-0") == "9780143127550"


def test_a_979_isbn_has_no_isbn10() -> None:
    thirteen = "979" + "1234567890"
    thirteen = thirteen[:12] + isbn.check13(thirteen[:12])
    assert isbn.to_isbn13(thirteen) == thirteen
    assert isbn.to_isbn10(thirteen) is None


@pytest.mark.parametrize("bad", ["", "abc", "12345", "9780143127551", "0143127552",
                                 "1230143127550"])
def test_bad_isbns_raise_with_a_useful_message(bad: str) -> None:
    with pytest.raises(ValueError) as caught:
        isbn.to_isbn13(bad)
    assert bad[:6] in str(caught.value) or "no ISBN" in str(caught.value)
    assert isbn.is_valid(bad) is False


# -- CSV --------------------------------------------------------------------

def test_reads_the_projects_own_single_column_file(tmp_path: Path) -> None:
    path = tmp_path / "books.csv"
    path.write_text("Isbn-13\n9780821716595\n9780312954154\n", encoding="utf-8")
    found = csv_input.read_isbns(path)
    assert [e.isbn13 for e in found.entries] == ["9780821716595", "9780312954154"]
    assert not found.problems


def test_bad_rows_are_reported_with_their_line_number_not_dropped(tmp_path: Path) -> None:
    path = tmp_path / "books.csv"
    path.write_text("Isbn-13\n9780821716595\nnonsense\n9780312954154\n", encoding="utf-8")
    found = csv_input.read_isbns(path)
    assert len(found.entries) == 2
    assert found.problems and found.problems[0][0] == 3


def test_isbn10_rows_convert_and_duplicates_are_scraped_once(tmp_path: Path) -> None:
    path = tmp_path / "books.csv"
    # The same book in both spellings, plus a real duplicate.
    path.write_text("0143127551\n9780143127550\n0062316095\n", encoding="utf-8")
    found = csv_input.read_isbns(path)
    assert [e.isbn13 for e in found.entries] == ["9780143127550", "9780062316097"]
    assert found.duplicates == 1


def test_bom_comments_blank_lines_and_semicolons(tmp_path: Path) -> None:
    path = tmp_path / "books.csv"
    path.write_text("﻿Title;Isbn-13\n# a comment\n\nEat Pray Love;9780143127550\n",
                    encoding="utf-8")
    found = csv_input.read_isbns(path)
    assert [e.isbn13 for e in found.entries] == ["9780143127550"]
    assert found.entries[0].label == "Eat Pray Love"


def test_the_column_is_found_from_the_data_when_the_header_lies(tmp_path: Path) -> None:
    path = tmp_path / "books.csv"
    path.write_text("isbn,code\nnot-an-isbn,9780143127550\n", encoding="utf-8")
    found = csv_input.read_isbns(path)
    assert [e.isbn13 for e in found.entries] == ["9780143127550"]


def test_missing_and_directory_inputs_raise(tmp_path: Path) -> None:
    for bad in (tmp_path / "nope.csv", tmp_path):
        with pytest.raises(csv_input.CsvError):
            csv_input.read_isbns(bad)


def test_looks_like_path_distinguishes_files_from_isbns(tmp_path: Path) -> None:
    assert csv_input.looks_like_path("books.csv") is True
    assert csv_input.looks_like_path("9780143127550") is False
    real = tmp_path / "list"
    real.write_text("x", encoding="utf-8")
    assert csv_input.looks_like_path(str(real)) is True




# -- CLI exit codes ---------------------------------------------------------
# 0 = something was scraped, 1 = nothing was, 2 = bad usage. A script has to be
# able to tell "this site had no such book" from "you passed me a typo".

def _run(*args: str) -> int:
    import subprocess
    root = Path(__file__).resolve().parents[1]
    return subprocess.run([sys.executable, "main.py", *args], cwd=root,
                          capture_output=True).returncode


def test_bad_usage_exits_two_not_one(tmp_path: Path) -> None:
    assert _run("notanisbn") == 2                      # failed checksum
    assert _run(str(tmp_path / "missing.csv")) == 2    # unreadable file
    empty = tmp_path / "empty.csv"
    empty.write_text("Title\nno isbns here\n", encoding="utf-8")
    assert _run(str(empty)) == 2                       # no usable ISBN


def test_help_lists_every_source_and_only_three_flags() -> None:
    import subprocess
    root = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "main.py", "--help"], cwd=root,
                         capture_output=True, text=True).stdout
    for source in ("openlibrary", "goodreads", "amazon", "bookbub", "kobo", "audible"):
        assert source in out
    assert out.count("--start") and out.count("--end") and out.count("--sources")
    assert "--workers" not in out and "--verbose" not in out
