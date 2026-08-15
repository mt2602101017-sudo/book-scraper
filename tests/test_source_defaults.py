"""Offline tests for the default source set and the rendered-wait settle logic.

No network and no browser: source selection is checked through
:meth:`Pipeline.select_sources`, and :meth:`HttpClient._await_selector` is driven
with a fake driver so the timing rules can be asserted without Chrome.

What these pin down:

* a run that names no ``--sources`` scrapes only the adapters whose
  ``enabled_by_default`` is true -- and BookBub, specifically, is not one of them
  (it cost 75 % of a run's wall clock while returning nothing);
* naming a source explicitly still runs it, opt-in or not, so ``--sources
  bookbub`` remains a working escape hatch rather than dead code;
* the wait for a ``wait_css`` selector gives up shortly after the document is
  complete instead of burning the caller's full 10-20 s budget on a selector the
  site has removed -- while a page that is *still loading* keeps its whole budget.

Runnable either way:

    .venv/bin/python -m pytest tests/test_source_defaults.py -q
    .venv/bin/python tests/test_source_defaults.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookscraper import http_client as H  # noqa: E402
from bookscraper.base import BaseSource  # noqa: E402
from bookscraper.pipeline import Pipeline, PipelineConfig  # noqa: E402
from bookscraper.sources import discover_sources  # noqa: E402
from bookscraper.sources.bookbub import BookBubSource  # noqa: E402


_ISBN = "9780143127550"


def _selected(sources: Optional[List[str]]) -> List[str]:
    """Slugs :meth:`Pipeline.select_sources` picks for a given ``--sources``."""
    config = PipelineConfig(isbn=_ISBN, sources=sources, out_dir=Path("/nonexistent"))
    pipeline = Pipeline(config)
    try:
        return [name for name, _cls in pipeline.select_sources()]
    finally:
        pipeline.client.close()


# -- the default set -----------------------------------------------------------


def test_all_five_sources_run_by_default() -> None:
    """A plain run scrapes every mandated source, BookBub included.

    BookBub was briefly opt-in over a misdiagnosed selector failure (the check had
    been run against a 404 page). It is on again, so the assignment's five sources
    all run without a flag.
    """
    chosen = _selected(None)
    for name in ("goodreads", "amazon", "bookbub", "kobo", "audible"):
        assert name in chosen, f"{name} should be on by default"


def test_no_adapter_is_opted_out() -> None:
    """A typo in one class attribute would silently shrink every default run."""
    off = [
        name for name, cls in discover_sources().items()
        if not getattr(cls, "enabled_by_default", True)
    ]
    assert off == [], f"unexpectedly opted-out adapters: {off}"
    assert BookBubSource.enabled_by_default is True


def test_naming_sources_explicitly_still_narrows_the_run() -> None:
    assert _selected(["bookbub"]) == ["bookbub"]
    mixed = _selected(["bookbub", "goodreads"])
    assert set(mixed) == {"goodreads", "bookbub"}


def test_the_seed_source_leads_whenever_it_is_selected() -> None:
    """The ISBN-hostile stores search by title+author, so the seed must run first.

    Asserted against ``pipeline.SEED_SOURCE`` rather than a hard-coded name: which
    source seeds the hint is a design decision that has already changed once
    (Goodreads -> Open Library), and this test is about the *ordering guarantee*,
    which must hold whichever source it is.
    """
    from bookscraper.pipeline import SEED_SOURCE

    for other in ("bookbub", "kobo", "audible"):
        chosen = _selected([other, SEED_SOURCE])
        assert chosen[0] == SEED_SOURCE, (
            f"{SEED_SOURCE} must run before {other}, which cannot be looked up by ISBN"
        )
        assert set(chosen) == {SEED_SOURCE, other}


def test_the_opt_out_mechanism_still_works() -> None:
    """Nothing is opted out today, but the machinery must stay functional.

    Otherwise the next adapter that needs disabling would look disabled while
    quietly still running -- so this exercises the filter with a stub rather than
    relying on a real adapter being off.
    """

    class _OptedOut(BaseSource):
        name = "stub-off"
        enabled_by_default = False

        def _scrape_into(self, hint, result):  # pragma: no cover - never called
            raise NotImplementedError

    class _OptedIn(BaseSource):
        name = "stub-on"

        def _scrape_into(self, hint, result):  # pragma: no cover - never called
            raise NotImplementedError

    available = {"stub-off": _OptedOut, "stub-on": _OptedIn}
    default_on = [
        name for name, cls in available.items()
        if getattr(cls, "enabled_by_default", True)
    ]
    assert default_on == ["stub-on"]


def test_base_class_defaults_to_enabled() -> None:
    """A new adapter is on by default; opting out has to be deliberate."""
    assert BaseSource.enabled_by_default is True

    class _Fresh(BaseSource):
        name = "fresh"

        def _scrape_into(self, hint, result):  # pragma: no cover - never called
            raise NotImplementedError

    assert _Fresh.enabled_by_default is True


# -- the rendered-wait settle logic -------------------------------------------


class _FakeDriver:
    """Minimal stand-in for a Selenium driver.

    :param appears_after: seconds until ``find_elements`` starts returning a hit
        (``None`` = never, i.e. the selector has been removed from the page).
    :param ready_after: seconds until ``document.readyState`` is ``'complete'``.
    """

    def __init__(self, appears_after: Optional[float], ready_after: float = 0.0) -> None:
        self.started = time.monotonic()
        self.appears_after = appears_after
        self.ready_after = ready_after
        self.probes = 0

    def _elapsed(self) -> float:
        return time.monotonic() - self.started

    def find_elements(self, _by, _css):
        self.probes += 1
        if self.appears_after is None:
            return []
        return ["<element>"] if self._elapsed() >= self.appears_after else []

    def execute_script(self, script):
        if "readyState" in script:
            return "complete" if self._elapsed() >= self.ready_after else "loading"
        return None


def _client() -> H.HttpClient:
    return H.HttpClient(min_delay=0, max_delay=0, browser='never')


def _await(driver: _FakeDriver, wait_seconds: float) -> tuple:
    """Run ``_await_selector`` against ``driver``; return ``(found, elapsed)``."""
    client = _client()
    try:
        started = time.monotonic()
        found = client._await_selector(driver, "https://x.example/p", ".target",
                                      wait_seconds)
        return found, time.monotonic() - started
    finally:
        client.close()


def test_a_removed_selector_gives_up_shortly_after_the_page_loads() -> None:
    """The regression this whole change exists for.

    A selector the site has deleted used to cost the caller's full timeout on
    every fetch (BookBub: 3 renders x 20 s = 64 s of a 98 s run). It must now cost
    roughly the settle grace instead.
    """
    driver = _FakeDriver(appears_after=None, ready_after=0.0)
    found, elapsed = _await(driver, wait_seconds=20)
    assert found is False
    # Deliberately an ABSOLUTE ceiling, not RENDER_SETTLE_SECONDS + slack: a bound
    # derived from the constant would rise with it, so raising the constant back to
    # "never settle early" would make this test pass while waiting the full 20 s --
    # which is exactly the bug. 8 s is comfortably above any sane grace period and
    # far below the caller's budget.
    assert elapsed < 8.0, (
        f"a removed selector took {elapsed:.1f}s; it must give up once the document "
        "is complete rather than waiting out the caller's 20s budget"
    )


def test_a_selector_that_does_appear_is_still_found_immediately() -> None:
    driver = _FakeDriver(appears_after=0.0)
    found, elapsed = _await(driver, wait_seconds=20)
    assert found is True
    assert elapsed < 1.0, "an already-present selector must not be waited on"


def test_a_late_selector_is_still_waited_for() -> None:
    """Cutting the wait short must not lose content that arrives a bit later."""
    driver = _FakeDriver(appears_after=1.0, ready_after=0.0)
    found, elapsed = _await(driver, wait_seconds=20)
    assert found is True, "a selector injected after load must still be caught"
    assert 0.7 <= elapsed < H.RENDER_SETTLE_SECONDS + 1.5


def test_a_still_loading_page_keeps_its_full_budget() -> None:
    """The settle clock starts at readyState=complete, not at the first probe."""
    # Document stays 'loading' past the settle grace; the selector shows up after.
    late = H.RENDER_SETTLE_SECONDS + 0.75
    driver = _FakeDriver(appears_after=late, ready_after=late)
    found, elapsed = _await(driver, wait_seconds=20)
    assert found is True, (
        "a page that is genuinely still loading must not be cut off at the "
        "settle grace -- that grace only applies once loading has finished"
    )
    assert elapsed >= late - 0.3


def test_the_callers_wait_seconds_remains_a_hard_ceiling() -> None:
    """Never wait longer than asked, even while the page claims to be loading."""
    driver = _FakeDriver(appears_after=None, ready_after=10**6)  # never ready
    found, elapsed = _await(driver, wait_seconds=1)
    assert found is False
    assert elapsed < 2.5, f"exceeded the 1s ceiling ({elapsed:.1f}s)"


def test_a_broken_driver_does_not_raise() -> None:
    """A dead session or invalid selector degrades to 'not found'."""

    class _Broken(_FakeDriver):
        def find_elements(self, _by, _css):
            from selenium.common.exceptions import WebDriverException

            raise WebDriverException("session deleted")

    found, elapsed = _await(_Broken(appears_after=None), wait_seconds=20)
    assert found is False
    assert elapsed < 1.0, "a broken probe must fail fast, not spin"


def _run_all() -> int:
    failures = 0
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
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


# -- the shared scrape() shell -------------------------------------------------


def test_every_adapter_uses_the_inherited_scrape_shell() -> None:
    """``scrape()`` is defined once, in BaseSource, and no adapter overrides it.

    Four of the five adapters used to carry the same six-line shell -- make a
    result, call an inner method, catch anything that escaped -- differing only in
    the wording of the warning. The shape now lives in one place, so the
    never-raises guarantee cannot be weakened in one adapter and not the others.
    """
    for name, cls in discover_sources().items():
        assert cls.scrape is BaseSource.scrape, (
            f"{name} overrides scrape(); the shared shell is what guarantees the "
            "pipeline always gets a ScrapeResult"
        )
        assert "_scrape_into" in cls.__dict__, (
            f"{name} must implement the _scrape_into hook"
        )


def test_a_crashing_adapter_still_returns_a_result() -> None:
    """The guarantee the whole pipeline leans on, now enforced centrally."""
    from bookscraper.http_client import HttpClient
    from bookscraper.models import BookHint, ScrapeResult

    class _Boom(BaseSource):
        name = "boom"
        display_name = "Boom"

        def find_book_url(self, hint):  # pragma: no cover
            return None

        def _scrape_into(self, hint, result) -> None:
            raise RuntimeError("selector exploded")

    client = HttpClient(browser="never")
    try:
        result = _Boom(client).scrape(BookHint(isbn13=_ISBN))
    finally:
        client.close()

    assert isinstance(result, ScrapeResult), "scrape() must never raise"
    assert result.metadata is None
    assert any("selector exploded" in w for w in result.warnings), (
        "the failure must be recorded on the result, not swallowed"
    )


def test_pending_warnings_are_flushed_onto_the_result() -> None:
    """Adapters that collect notes before a result exists still get them through."""
    from bookscraper.http_client import HttpClient
    from bookscraper.models import BookHint

    class _Noter(BaseSource):
        name = "noter"

        def find_book_url(self, hint):  # pragma: no cover
            return None

        def pending_warnings(self):
            return ["a note from discovery"]

        def _scrape_into(self, hint, result) -> None:
            return None

    client = HttpClient(browser="never")
    try:
        result = _Noter(client).scrape(BookHint(isbn13=_ISBN))
    finally:
        client.close()
    assert "a note from discovery" in result.warnings
