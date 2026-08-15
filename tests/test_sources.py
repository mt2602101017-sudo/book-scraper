"""The adapter contract, with no network access.

Every request goes through a fake client, so these assert the one guarantee the
runner is built on: an adapter always returns a Result and never raises, however
badly the fetch went. ``FakeClient`` is shared with ``test_runner.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bookscraper.base import Source, discover  # noqa: E402
from bookscraper.models import Hint, Result  # noqa: E402


class FakeBrowser:
    available = False

    def html(self, *_: Any, **__: Any) -> None:
        return None

    def close(self) -> None:
        pass


class FakeClient:
    """A client that reaches nothing, and records that it tried."""

    mode = "never"

    def __init__(self, blocked: bool = False, contacted: int = 1) -> None:
        self.browser = FakeBrowser()
        self.blocks = {"example.com": "HTTP 403 wall"} if blocked else {}
        self._blocked = blocked
        self._contacted = contacted
        self.calls: List[str] = []

    def get(self, url: str, **_: Any) -> None:
        self.calls.append(url)
        return None

    def soup(self, url: str, **_: Any) -> None:
        self.calls.append(url)
        return None

    def json(self, url: str, **_: Any) -> None:
        self.calls.append(url)
        return None

    def post_json(self, url: str, *_: Any, **__: Any) -> None:
        return None

    def rendered(self, url: str, **_: Any) -> None:
        self.calls.append(url)
        return None

    def download(self, *_: Any, **__: Any) -> None:
        return None

    @staticmethod
    def host_of(url: str) -> str:
        return "example.com"

    def track_hosts(self) -> None:
        pass

    def contacted(self) -> int:
        return self._contacted

    def touched_block(self) -> Optional[str]:
        return "HTTP 403 wall" if self._blocked else None

    def block_reason(self, _: str) -> Optional[str]:
        return "HTTP 403 wall" if self._blocked else None

    def close(self) -> None:
        pass


ADAPTERS = list(discover().items())


def test_all_six_adapters_are_discovered() -> None:
    assert [name for name, _ in ADAPTERS] == [
        "openlibrary", "goodreads", "amazon", "bookbub", "kobo", "audible"]


@pytest.mark.parametrize("name,adapter", ADAPTERS, ids=[n for n, _ in ADAPTERS])
def test_an_adapter_never_raises_even_when_every_fetch_fails(name: str,
                                                             adapter: type) -> None:
    """The guarantee the runner is built on: always a Result, never an exception."""
    result = adapter(FakeClient()).scrape(Hint(isbn13="9780143127550",
                                               isbn10="0143127551"))
    assert isinstance(result, Result)
    assert result.source == name
    assert result.has_payload() is False
    assert result.warnings, "an adapter that found nothing must say why"


@pytest.mark.parametrize("name,adapter", ADAPTERS, ids=[n for n, _ in ADAPTERS])
def test_an_adapter_declares_the_contract_the_runner_reads(name: str,
                                                           adapter: type) -> None:
    assert issubclass(adapter, Source)
    assert adapter.name == name == name.lower()
    assert isinstance(adapter.needs_browser, bool)


def test_the_browser_only_adapters_say_unreachable_not_absent() -> None:
    """A missing driver must never be reported as 'the book is not in the catalogue'."""
    for name in ("kobo", "bookbub"):
        adapter = dict(ADAPTERS)[name]
        result = adapter(FakeClient()).scrape(Hint(isbn13="9780143127550"))
        assert any("unreachable" in w for w in result.warnings), name
