"""The runner: skipping what is answered, and never recording a wall as absence."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bookscraper.base import Source  # noqa: E402
from bookscraper.csv_input import Entry  # noqa: E402
from bookscraper.metadata import release_caches  # noqa: E402
from bookscraper.models import Book, Hint, Result, Review  # noqa: E402
from bookscraper.runner import Config, Runner  # noqa: E402
from test_sources import FakeClient  # noqa: E402


# -- the runner --------------------------------------------------------------

class Stub(Source):
    """An adapter whose output the test decides."""

    name = "stub"
    payload: Optional[Result] = None

    def _scrape(self, hint: Hint, result: Result) -> None:
        if self.payload is None:
            return
        for field in ("book", "blurb", "reviews", "genres", "cover_urls", "book_url"):
            setattr(result, field, getattr(self.payload, field))


def make_runner(tmp_path: Path, adapter: type, **kw: Any) -> Runner:
    release_caches()
    runner = Runner(Config(out_dir=tmp_path / "data",
                           metrics_dir=tmp_path / "metrics", **kw))
    runner.client = kw.pop("client", None) or FakeClient()
    runner.adapters = [adapter]
    runner.storage.ensure_dirs()
    return runner


def full_result() -> Result:
    found = Result(source="stub", isbn13="9780143127550",
                   book_url="https://example.com/b")
    found.book = Book(isbn13="9780143127550", title="T", authors=["A"],
                      publisher="P", date_of_publication="2016", language="English")
    found.blurb = "a blurb"
    found.reviews = [Review(text="a review"), Review(text="another")]
    found.genres = ["Fiction"]
    return found


def test_a_successful_source_writes_every_artefact_kind(tmp_path: Path) -> None:
    Stub.payload = full_result()
    runner = make_runner(tmp_path, Stub)
    outcomes = runner.book(Entry(isbn13="9780143127550"))
    assert outcomes[0].has_metadata is True
    assert outcomes[0].reviews == 2
    assert outcomes[0].genres == 1
    assert outcomes[0].blurb == len("a blurb")
    records = json.load(open(runner.storage.meta.path_for("stub")))
    assert records[0]["title"] == "T"
    assert (runner.storage.dir_for("blurb") / "9780143127550_b_stub_1.txt").exists()
    assert (runner.storage.dir_for("reviews") / "9780143127550_r_stub_2.txt").exists()


def test_a_source_with_a_stored_record_is_not_asked_again(tmp_path: Path) -> None:
    Stub.payload = full_result()
    runner = make_runner(tmp_path, Stub)
    runner.book(Entry(isbn13="9780143127550"))
    release_caches()

    again = make_runner(tmp_path, Stub)
    outcomes = again.book(Entry(isbn13="9780143127550"))
    assert outcomes[0].status == "skipped"
    assert again.report.skipped == {"stub": 1}
    # A different book is still scraped.
    assert again.book(Entry(isbn13="9780062316097"))[0].status != "skipped"


def test_a_recorded_absence_also_stops_a_refetch(tmp_path: Path) -> None:
    Stub.payload = None
    runner = make_runner(tmp_path, Stub)
    outcomes = runner.book(Entry(isbn13="9780143127550"))
    assert outcomes[0].status == "empty"
    assert outcomes[0].trustworthy is True
    runner.nodata.flush()
    assert "9780143127550" in (tmp_path / "metrics" / "stub_no_data.txt").read_text()

    again = make_runner(tmp_path, Stub)
    assert again.book(Entry(isbn13="9780143127550"))[0].status == "skipped"


def test_a_walled_source_is_blocked_not_empty_and_is_never_recorded_absent(
        tmp_path: Path) -> None:
    """Merging these once wrote off 629 WAF-challenged books as 'not on Goodreads'."""
    Stub.payload = None
    runner = make_runner(tmp_path, Stub)
    runner.client = FakeClient(blocked=True)
    outcomes = runner.book(Entry(isbn13="9780143127550"))
    assert outcomes[0].status == "blocked"
    assert outcomes[0].trustworthy is False
    runner.nodata.flush()
    assert not (tmp_path / "metrics" / "stub_no_data.txt").exists()


def test_an_empty_from_a_site_never_reached_is_not_trustworthy(tmp_path: Path) -> None:
    Stub.payload = None
    runner = make_runner(tmp_path, Stub)
    runner.client = FakeClient(contacted=0)
    outcomes = runner.book(Entry(isbn13="9780143127550"))
    assert outcomes[0].status == "empty"
    assert outcomes[0].trustworthy is False
    runner.nodata.flush()
    assert not (tmp_path / "metrics" / "stub_no_data.txt").exists()


def test_a_crashing_adapter_does_not_stop_the_run(tmp_path: Path) -> None:
    class Exploding(Source):
        name = "boom"

        def _scrape(self, hint: Hint, result: Result) -> None:
            raise RuntimeError("selector rotted")

    runner = make_runner(tmp_path, Exploding)
    outcomes = runner.book(Entry(isbn13="9780143127550"))
    assert outcomes[0].status in ("empty", "blocked")
    assert any("RuntimeError" in w for w in outcomes[0].warnings)


def test_the_seed_source_runs_first_and_seeds_the_shared_hint(tmp_path: Path) -> None:
    seen: List[Optional[str]] = []

    class Seeder(Source):
        name = "openlibrary"

        def _scrape(self, hint: Hint, result: Result) -> None:
            result.book = Book(isbn13=hint.isbn13, title="Seeded")
            result.hint = Hint(isbn13=hint.isbn13, title="Seeded", authors=["A"])

    class Follower(Source):
        name = "audible"

        def _scrape(self, hint: Hint, result: Result) -> None:
            seen.append(hint.title)

    runner = make_runner(tmp_path, Seeder)
    runner.adapters = [Follower, Seeder]          # deliberately the wrong order
    runner.adapters = runner._select() or runner.adapters
    runner.adapters = [Seeder, Follower]
    runner.book(Entry(isbn13="9780143127550"))
    assert seen == ["Seeded"], "the seed source's title must reach later sources"


def test_a_skipped_seed_source_still_seeds_from_disk(tmp_path: Path) -> None:
    """Otherwise a resumed run silently turns the storefronts' hits into misses."""
    release_caches()
    runner = make_runner(tmp_path, Stub)
    runner.storage.meta.append("openlibrary", {"isbn13": "9780143127550",
                                               "title": "Stored", "authors": ["A"]})
    release_caches()
    seen: List[Optional[str]] = []

    class Follower(Source):
        name = "audible"

        def _scrape(self, hint: Hint, result: Result) -> None:
            seen.append(hint.title)

    class Seed(Source):
        name = "openlibrary"

        def _scrape(self, hint: Hint, result: Result) -> None:
            raise AssertionError("must not be scraped again")

    runner.adapters = [Seed, Follower]
    runner.book(Entry(isbn13="9780143127550"))
    assert seen == ["Stored"]


def test_the_exit_code_and_digest_reflect_the_run(tmp_path: Path) -> None:
    Stub.payload = full_result()
    runner = make_runner(tmp_path, Stub)
    assert runner.run([Entry(isbn13="9780143127550")]) == 0
    report = (tmp_path / "metrics" / "stub_isbns.txt").read_text()
    assert "1 of 1 succeeded" in report
    assert "9780143127550" in report

    Stub.payload = None
    empty = make_runner(tmp_path / "b", Stub)
    assert empty.run([Entry(isbn13="9780062316097")]) == 1


def test_a_fully_skipped_run_is_a_success_not_a_failure(tmp_path: Path) -> None:
    Stub.payload = full_result()
    make_runner(tmp_path, Stub).run([Entry(isbn13="9780143127550")])
    release_caches()
    again = make_runner(tmp_path, Stub)
    # The data is on disk from the previous run; reporting 1 would make a re-run of
    # a complete batch look like a total failure.
    assert again.run([Entry(isbn13="9780143127550")]) == 0


def test_max_reviews_caps_what_is_written(tmp_path: Path) -> None:
    Stub.payload = full_result()
    runner = make_runner(tmp_path, Stub, max_reviews=1)
    assert runner.book(Entry(isbn13="9780143127550"))[0].reviews == 1
