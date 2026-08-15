"""Covers that fail to download, and the to-do list that stops them being lost.

The bug these guard: a failed cover download used to leave a metadata record
behind, the skip check read that as "finished", and the cover was gone for good.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bookscraper.base import Source  # noqa: E402
from bookscraper.csv_input import Entry  # noqa: E402
from bookscraper.metadata import release_caches  # noqa: E402
from bookscraper.models import Book, Hint, Result  # noqa: E402
from test_runner import make_runner  # noqa: E402
from test_sources import FakeClient  # noqa: E402


# -- covers that fail, and the to-do list that keeps them -------------------
# The bug this guards: a failed cover download used to leave a metadata record
# behind, the skip check read that as "finished", and the cover was lost for good.
# 91 Open Library covers went that way in one run.

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 4000
#: What Open Library returns for a missing cover: HTTP 200 and a 1x1 GIF.
PLACEHOLDER = b"GIF89a\x01\x00\x01\x00\xf0\x00\x00" + b"\x00" * 30


class CoverClient(FakeClient):
    """A client whose cover downloads succeed only for chosen URLs."""

    def __init__(self, ok: tuple = (), placeholder: tuple = ()) -> None:
        super().__init__()
        self.ok, self.placeholder = ok, placeholder
        self.tried: List[str] = []

    def download(self, url: str, **_: Any) -> Any:
        self.tried.append(url)
        if any(m in url for m in self.placeholder):
            return PLACEHOLDER, "image/gif"
        return (JPEG, "image/jpeg") if any(m in url for m in self.ok) else None


class Covered(Source):
    name = "stub"
    urls: Any = []

    def _scrape(self, hint: Hint, result: Result) -> None:
        result.book = Book(isbn13=hint.isbn13, title="T")
        result.cover_urls = list(self.urls)


def test_a_failed_preferred_size_falls_back_instead_of_losing_the_cover(
        tmp_path: Path) -> None:
    """-L comes off archive.org and stalls; -M is served directly. Try both."""
    Covered.urls = [["https://c/x-L.jpg", "https://c/x-M.jpg", "https://c/x-S.jpg"]]
    runner = make_runner(tmp_path, Covered)
    runner.client = CoverClient(ok=("-M.jpg",))
    outcome = runner.book(Entry(isbn13="9780143127550"))[0]
    assert outcome.covers == 1
    assert outcome.incomplete is False
    # It tried the preferred size first, then stopped at the one that worked.
    assert runner.client.tried == ["https://c/x-L.jpg", "https://c/x-M.jpg"]


def test_a_placeholder_pixel_is_not_filed_as_artwork(tmp_path: Path) -> None:
    Covered.urls = [["https://c/x-L.jpg", "https://c/x-M.jpg"]]
    runner = make_runner(tmp_path, Covered)
    runner.client = CoverClient(ok=("-M.jpg",), placeholder=("-L.jpg",))
    outcome = runner.book(Entry(isbn13="9780143127550"))[0]
    assert outcome.covers == 1
    saved = list(runner.storage.dir_for("covers").iterdir())
    assert len(saved) == 1 and saved[0].stat().st_size > 500
    assert any("placeholder" in w for w in outcome.warnings)


def test_a_lost_cover_keeps_the_book_on_the_to_do_list(tmp_path: Path) -> None:
    Covered.urls = [["https://c/x-L.jpg", "https://c/x-M.jpg"]]
    runner = make_runner(tmp_path, Covered)
    runner.client = CoverClient(ok=())          # every size fails
    outcome = runner.book(Entry(isbn13="9780143127550"))[0]
    assert outcome.has_metadata is True         # the record was still written
    assert outcome.covers == 0
    assert outcome.incomplete is True
    runner.pending.flush()
    assert "9780143127550" in (tmp_path / "metrics" / "stub_incomplete.txt").read_text()

    # ...so the next run does NOT skip it, even though the record is on disk.
    release_caches()
    again = make_runner(tmp_path, Covered)
    assert again._answered("stub", "9780143127550") is None
    again.client = CoverClient(ok=("-M.jpg",))  # the CDN has recovered
    retried = again.book(Entry(isbn13="9780143127550"))[0]
    assert retried.covers == 1
    assert retried.incomplete is False
    # And once it is complete it comes off the list.
    again.pending.flush()
    assert not (tmp_path / "metrics" / "stub_incomplete.txt").read_text().strip()


def test_queue_incomplete_finds_records_whose_cover_never_landed(tmp_path: Path) -> None:
    """Recovery for runs made before incompleteness was tracked."""
    Covered.urls = [["https://c/x-L.jpg"]]
    runner = make_runner(tmp_path, Covered)
    runner.client = CoverClient(ok=())
    runner.book(Entry(isbn13="9780143127550"))
    runner.book(Entry(isbn13="9780062316097"))
    # Simulate the old behaviour: records on disk, no covers, nothing queued.
    (tmp_path / "metrics" / "stub_incomplete.txt").unlink(missing_ok=True)
    release_caches()

    fresh = make_runner(tmp_path, Covered)
    assert fresh._answered("stub", "9780143127550") is not None   # would be skipped
    from bookscraper.ledger import queue_missing_covers
    assert queue_missing_covers(fresh.storage, fresh.pending, ["stub"]) == {"stub": 2}
    assert fresh._answered("stub", "9780143127550") is None       # now retried


def test_a_book_that_genuinely_has_no_cover_drops_off_the_list(tmp_path: Path) -> None:
    """Otherwise --retry-incomplete would re-scrape it on every future run."""
    Covered.urls = []                    # the source offers no cover at all
    listed = tmp_path / "metrics" / "stub_incomplete.txt"

    seed = make_runner(tmp_path, Covered)
    seed.pending.note("stub", "9780143127550")
    seed.pending.note("stub", "9780062316097")
    seed.pending.flush()
    assert sorted(listed.read_text().split()) == ["9780062316097", "9780143127550"]

    release_caches()
    runner = make_runner(tmp_path, Covered)
    outcome = runner.book(Entry(isbn13="9780143127550"))[0]
    assert outcome.has_metadata is True
    assert outcome.incomplete is False
    runner.pending.flush()
    # Only the book that was actually finished is removed; the other still waits.
    assert listed.read_text().split() == ["9780062316097"]
