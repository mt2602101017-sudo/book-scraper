"""Per-host rate limits and the failure cooldown. No network."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bookscraper import limits  # noqa: E402
from bookscraper.transport import Transport  # noqa: E402


def test_the_longest_host_match_wins() -> None:
    """covers.openlibrary.org must not inherit openlibrary.org's looser limit."""
    t = Transport(min_delay=1.0, max_delay=2.0, timeout=25)
    assert t.limits_for("covers.openlibrary.org") == (3.5, 5.0, 45)
    assert t.limits_for("openlibrary.org") == (2.5, 4.0, 30)
    # A subdomain inherits its parent's entry: -L covers come off an ia*.archive.org.
    assert t.limits_for("ia601504.us.archive.org") == t.limits_for("archive.org")
    # Anything unlisted keeps the run's own defaults.
    assert t.limits_for("www.goodreads.com") == (1.0, 2.0, 25)
    assert t.limits_for("") == (1.0, 2.0, 25)


def test_a_rate_limited_host_is_actually_waited_on(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list = []
    monkeypatch.setattr(time, "sleep", slept.append)
    t = Transport(min_delay=0.0, max_delay=0.0)
    for _ in range(3):
        t.throttle("https://covers.openlibrary.org/b/id/1-L.jpg")
    # Three requests to a 3.5-5.0 s host cannot cost less than two gaps.
    assert sum(slept) >= 7.0, slept


def test_delays_stay_per_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)
    t = Transport(min_delay=1.0, max_delay=1.0)
    t.throttle("https://a.example/x")
    before = dict(t._next_at)
    t.throttle("https://b.example/y")
    # Hitting b must not move a's clock: waiting on one host should never slow
    # another, which would cost time without being politer to either.
    assert t._next_at["a.example"] == before["a.example"]


def test_repeated_failures_pause_a_host_instead_of_burning_retries(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)
    t = Transport()
    host = "covers.openlibrary.org"
    for _ in range(limits.STRIKES_BEFORE_COOLDOWN - 1):
        t._strike(host)
    assert host not in t._next_at, "a couple of failures is not yet a pattern"
    t._strike(host)
    assert t._next_at[host] >= time.monotonic() + limits.COOLDOWN_SECONDS - 1

    # It lengthens while the failures continue...
    first = t._next_at[host]
    t._strike(host)
    assert t._next_at[host] > first
    # ...and a success clears the record.
    t._clear_strikes(host)
    assert host not in t._strikes


def test_the_cooldown_is_capped_so_one_bad_host_cannot_wedge_a_run(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)
    t = Transport()
    for _ in range(40):
        t._strike("dead.example")
    ceiling = limits.COOLDOWN_SECONDS * limits.MAX_COOLDOWN_STEPS
    assert t._next_at["dead.example"] <= time.monotonic() + ceiling + 1
