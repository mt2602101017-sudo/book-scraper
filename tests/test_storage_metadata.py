"""Offline tests for the accumulating metadata file and the flat layout.

No network. What these pin down about
:meth:`bookscraper.storage.Storage.append_metadata`:

* the file is a **single valid JSON array** after every append -- ``json.load()``
  must work mid-run, not only once the batch finishes;
* records accumulate rather than overwrite, which is the whole point: the mandated
  filename ``<source>_metadata.json`` carries no ISBN, so the old behaviour left
  five files describing whichever book finished last;
* re-scraping a book **replaces** its record instead of adding a second one --
  ``--start`` resume deliberately re-runs a few already-finished books, and silent
  duplicates would corrupt every count taken from the file;
* appends from two processes lose nothing (covered in full by test_multiprocess;
  them writing the same handful of files);
* non-ASCII survives the seek-based append -- the offsets are *bytes*, the content
  is UTF-8, and confusing the two would truncate mid-character;
* a corrupt or truncated file is set aside, never silently discarded.

Runnable either way:

    .venv/bin/python -m pytest tests/test_storage_metadata.py -q
    .venv/bin/python tests/test_storage_metadata.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookscraper.storage import Storage  # noqa: E402


_ISBN = "9780143127550"


def _storage(root: Path, **kw) -> Storage:
    storage = Storage(root, **kw)
    storage.ensure_dirs()
    return storage


def _record(isbn: str, **extra) -> Dict[str, Any]:
    payload = {"isbn13": isbn, "title": f"Book {isbn[-3:]}", "authors": ["Someone"]}
    payload.update(extra)
    return payload


def _load(path: Path) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# -- accumulation --------------------------------------------------------------


def test_records_accumulate_instead_of_overwriting(tmp_path: Path) -> None:
    """The behaviour this change exists for."""
    storage = _storage(tmp_path)
    for n in range(1, 6):
        storage.append_metadata("goodreads", _record(f"978000000000{n}"))

    records = _load(storage.metadata_path("goodreads"))
    assert len(records) == 5, "every book must survive in the shared file"
    assert [r["isbn13"][-1] for r in records] == list("12345"), "file order = run order"


def test_the_file_is_valid_json_after_every_single_append(tmp_path: Path) -> None:
    """A consumer must be able to read it mid-run, not just at the end."""
    storage = _storage(tmp_path)
    path = storage.metadata_path("amazon")
    for n in range(1, 8):
        storage.append_metadata("amazon", _record(f"978000000000{n}"))
        records = _load(path)          # raises if the array is ever malformed
        assert len(records) == n
        assert isinstance(records, list)


def test_each_source_gets_its_own_file(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    storage.append_metadata("goodreads", _record(_ISBN))
    storage.append_metadata("amazon", _record(_ISBN))
    storage.append_metadata("goodreads", _record("9780062316097"))

    assert len(_load(storage.metadata_path("goodreads"))) == 2
    assert len(_load(storage.metadata_path("amazon"))) == 1
    names = sorted(p.name for p in storage.dir_for("metadata").glob("*.json"))
    assert names == ["amazon_metadata.json", "goodreads_metadata.json"]


def test_the_record_holds_exactly_the_contract_keys(tmp_path: Path) -> None:
    """The on-disk key set is a contract; drift in either direction is a bug.

    The diagnostics this file used to carry (``genres`` as a list, ``_source``,
    ``_source_url``, ``_scraped_at``, ``_edition_*``, ``_warnings``) are gone, and so
    are the model fields that fed them -- nothing read them once they stopped being
    serialised. Warnings reach the logs and the summary table instead; on a
    10 000-book file the ``_warnings`` arrays dominated the payload.
    """
    from bookscraper.models import BookMetadata

    payload = BookMetadata(
        isbn13=_ISBN,
        title="A Book",
        authors=["Someone"],
        genres=["Fiction", "Mystery"],
    ).to_json_dict()

    assert list(payload) == [
        "isbn13", "title", "authors", "publisher", "origin",
        "date_of_publication", "language", "genre",
    ], "key set or order drifted from the contract"

    for gone in ("genres", "_source", "_source_url", "_scraped_at",
                 "_edition_isbn13", "_edition_matches_requested", "_warnings"):
        assert gone not in payload, f"{gone} should no longer be written"

    # The genre list survives as the comma-separated string the assignment asks
    # for, so no information is actually lost.
    assert payload["genre"] == "Fiction, Mystery"

    # And the same holds once it is on disk.
    storage = _storage(tmp_path)
    storage.append_metadata("goodreads", payload)
    assert list(storage.read_metadata("goodreads")[0]) == list(payload)


def test_read_metadata_round_trips(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    assert storage.read_metadata("kobo") == [], "absent file reads as empty"
    storage.append_metadata("kobo", _record(_ISBN))
    storage.append_metadata("kobo", _record("9780062316097"))
    got = storage.read_metadata("kobo")
    assert [r["isbn13"] for r in got] == [_ISBN, "9780062316097"]


# -- re-scraping ---------------------------------------------------------------


def test_rescraping_a_book_replaces_its_record(tmp_path: Path) -> None:
    """``--start`` resume re-runs already-finished books; duplicates would corrupt counts."""
    storage = _storage(tmp_path)
    storage.append_metadata("goodreads", _record(_ISBN, title="first pass"))
    storage.append_metadata("goodreads", _record("9780062316097"))
    storage.append_metadata("goodreads", _record(_ISBN, title="second pass"))

    records = storage.read_metadata("goodreads")
    assert len(records) == 2, "the re-scraped book must not appear twice"
    assert len({r["isbn13"] for r in records}) == 2
    updated = [r for r in records if r["isbn13"] == _ISBN][0]
    assert updated["title"] == "second pass", "the newer scrape wins"
    # Position is kept, so the file order still mirrors the CSV order.
    assert records[0]["isbn13"] == _ISBN


def test_a_whole_rerun_does_not_double_the_file(tmp_path: Path) -> None:
    isbns = [f"978000000000{n}" for n in range(1, 5)]
    for _pass in range(3):
        storage = _storage(tmp_path)      # a fresh Storage each run, as the CLI does
        for isbn in isbns:
            storage.append_metadata("goodreads", _record(isbn))
    records = _storage(tmp_path).read_metadata("goodreads")
    assert len(records) == len(isbns), (
        f"three identical runs produced {len(records)} records for {len(isbns)} books"
    )


def test_a_record_without_an_isbn_is_still_appended(tmp_path: Path) -> None:
    """No ISBN means no identity to match on, so it cannot replace anything."""
    storage = _storage(tmp_path)
    storage.append_metadata("bookbub", {"title": "No ISBN here"})
    storage.append_metadata("bookbub", {"title": "Nor here"})
    assert len(storage.read_metadata("bookbub")) == 2



# -- encoding ------------------------------------------------------------------


def test_non_ascii_survives_the_seek_based_append(tmp_path: Path) -> None:
    """Offsets are bytes; content is UTF-8. Confusing them truncates mid-character."""
    storage = _storage(tmp_path)
    titles = ["Café Ω 日本語 — em-dash", "Ünïcödé ✓", "Ñoño “quoted” ¡hola!"]
    for index, title in enumerate(titles):
        storage.append_metadata("kobo", _record(f"97800000000{index:02d}", title=title))

    records = _load(storage.metadata_path("kobo"))
    assert [r["title"] for r in records] == titles
    # And the characters are real, not \uXXXX escapes.
    raw = storage.metadata_path("kobo").read_text(encoding="utf-8")
    assert "日本語" in raw and "\\u" not in raw


def test_a_record_with_a_lone_surrogate_does_not_break_the_file(tmp_path: Path) -> None:
    """Scraped text really does carry truncated emoji; the file must stay readable."""
    storage = _storage(tmp_path)
    storage.append_metadata("amazon", _record(_ISBN, title="broken \ud83d emoji"))
    storage.append_metadata("amazon", _record("9780062316097"))
    records = storage.read_metadata("amazon")
    assert len(records) == 2, "one bad string must not cost the file"


# -- damaged files -------------------------------------------------------------


def test_a_truncated_file_is_set_aside_not_destroyed(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    path = storage.metadata_path("amazon")
    path.parent.mkdir(parents=True, exist_ok=True)
    # What a kill -9 mid-write leaves behind.
    path.write_text('[\n  {"isbn13": "OLDBOOK", "title": "half-writ',
                    encoding="utf-8")

    storage.append_metadata("amazon", _record(_ISBN))

    assert [r["isbn13"] for r in _load(path)] == [_ISBN], "a fresh array is started"
    salvaged = list(path.parent.glob("amazon_metadata.corrupt-*.json"))
    assert len(salvaged) == 1, "the damaged file must be kept, not deleted"
    assert "OLDBOOK" in salvaged[0].read_text(encoding="utf-8")


def test_a_json_object_from_an_older_run_is_not_appended_into(tmp_path: Path) -> None:
    """An old single-object file is valid JSON but the wrong shape."""
    storage = _storage(tmp_path)
    path = storage.metadata_path("goodreads")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"isbn13": "OLDBOOK"}\n', encoding="utf-8")

    # read_metadata tolerates it for reading...
    assert storage.read_metadata("goodreads") == [{"isbn13": "OLDBOOK"}]
    # ...and appending sets it aside rather than producing a mangled file.
    storage.append_metadata("goodreads", _record(_ISBN))
    records = _load(path)
    assert isinstance(records, list) and [r["isbn13"] for r in records] == [_ISBN]
    assert list(path.parent.glob("goodreads_metadata.corrupt-*.json"))


def test_an_empty_file_is_treated_as_a_fresh_start(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    path = storage.metadata_path("kobo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    storage.append_metadata("kobo", _record(_ISBN))
    assert [r["isbn13"] for r in _load(path)] == [_ISBN]


def test_an_empty_array_gets_its_first_record_without_a_stray_comma(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    path = storage.metadata_path("kobo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[\n]\n", encoding="utf-8")
    storage.append_metadata("kobo", _record(_ISBN))
    assert [r["isbn13"] for r in _load(path)] == [_ISBN]


# -- the flat layout -----------------------------------------------------------


def test_the_four_named_directories_plus_genres(tmp_path: Path) -> None:
    """The assignment's four, plus genres/ for Task 5."""
    _storage(tmp_path)          # constructing it is what creates the directories
    names = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    assert names == [
        "book_blurb", "book_coverpage", "book_metadata", "book_reviews", "genres",
    ]


def test_filenames_match_the_specified_shapes(tmp_path: Path) -> None:
    """Asserted against the real writers, not a parallel copy of the naming rules.

    ``Storage.expected_names()`` used to build these strings a second time purely so
    this test could compare against them, which meant the test could pass while the
    real writers drifted.
    """
    storage = _storage(tmp_path)
    expected = {kind: path.name for kind, path in {
        "metadata": storage.append_metadata("goodreads", _record(_ISBN)),
        "covers": storage.write_cover(_ISBN, "goodreads", 1, b"\xff\xd8\xffdata"),
        "blurb": storage.write_blurb(_ISBN, "goodreads", "a blurb"),
        "reviews": storage.write_review(_ISBN, "goodreads", 1, "a review"),
        "genres": storage.write_genres(_ISBN, "goodreads", ["Fiction"]),
    }.items()}
    assert expected["metadata"] == "goodreads_metadata.json"
    assert expected["covers"] == f"{_ISBN}_cp_goodreads_1.jpg"
    assert expected["blurb"] == f"{_ISBN}_b_goodreads_1.txt"
    assert expected["reviews"] == f"{_ISBN}_r_goodreads_1.txt"
    assert expected["genres"] == f"{_ISBN}_g_goodreads_1.txt"


def test_two_books_share_the_directories_without_colliding(tmp_path: Path) -> None:
    """Every non-metadata filename carries the ISBN, which is what makes flat safe."""
    storage = _storage(tmp_path)
    for isbn in (_ISBN, "9780062316097"):
        storage.append_metadata("goodreads", _record(isbn))
        storage.write_blurb(isbn, "goodreads", "a blurb")
        storage.write_review(isbn, "goodreads", 1, "a review")
        storage.write_genres(isbn, "goodreads", ["Fiction"])
        storage.write_cover(isbn, "goodreads", 1, b"\xff\xd8\xffdata")

    assert len(storage.read_metadata("goodreads")) == 2
    for kind, count in (("blurb", 2), ("reviews", 2), ("genres", 2), ("covers", 2)):
        files = list(storage.dir_for(kind).iterdir())
        assert len(files) == count, f"{kind}: {[f.name for f in files]}"


def _run_all() -> int:
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
