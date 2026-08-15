"""The polite request engine: courtesy delays, retries and bot-wall recording.

* A randomised delay is slept **before every outbound request, per host**, so
  interleaved adapters cannot hammer one site and a run cannot open with a burst.
* Nothing propagates to the caller. Every failure is a warning and ``None``, so
  adapters never need a try/except around a fetch.
* Transient failures (429, 5xx, resets, timeouts) are retried with exponential
  backoff, honouring ``Retry-After``.
* Bot walls are detected by :mod:`bookscraper.blocks` and *recorded*, never
  fought.

One instance serves a whole run, which is what makes the delay clock and the
block registry properties of the run rather than of one book. Requests go out one
at a time, in the order adapters ask for them, so none of this state needs a lock.
"""

from __future__ import annotations

import random
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Mapping, Optional, Set, Tuple
from urllib.parse import urlsplit

import requests

from . import blocks, limits

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})
#: A ``Retry-After`` past this means the host wants us gone for the day, so
#: burning ``retries x 60s`` on one URL helps nobody.
_MAX_RETRY_AFTER = 120.0



def warn(message: str) -> None:
    """Progress and warnings go to stderr, so ``2>/dev/null`` leaves the report."""
    print(message, file=sys.stderr)


class Transport:
    """Throttled, retrying HTTP with block detection. See :class:`HttpClient`."""

    def __init__(self, min_delay: float = 1.0, max_delay: float = 2.0,
                 timeout: int = 25, retries: int = 3) -> None:
        self.min_delay = max(0.0, min(60.0, float(min_delay)))
        self.max_delay = max(self.min_delay, min(60.0, float(max_delay)))
        self.timeout = int(timeout)
        self.retries = max(0, int(retries))

        #: host -> earliest monotonic time that host may be hit again.
        self._next_at: Dict[str, float] = {}
        #: host -> why we believe we are being blocked.
        self.blocks: Dict[str, str] = {}
        #: Hosts contacted since :meth:`track_hosts`.
        self._hosts: Set[str] = set()
        #: host -> consecutive transport failures, for the cooldown.
        self._strikes: Dict[str, int] = {}

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8"),
            "Accept-Language": "en-US,en;q=0.9",
            # Only codings requests can always decode. Advertising ``br`` with no
            # brotli decoder installed makes every CDN-fronted site hand back
            # bytes it cannot inflate, so ``response.text`` is binary noise and
            # every selector silently misses.
            "Accept-Encoding": "gzip, deflate",
            "Upgrade-Insecure-Requests": "1", "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1", "DNT": "1",
        })

    @staticmethod
    def host_of(url: str) -> str:
        """The lower-cased hostname, or ``''`` when unparseable."""
        try:
            return (urlsplit(url).hostname or "").lower()
        except ValueError:  # an unbalanced bracket in a scraped netloc
            return ""

    def track_hosts(self) -> None:
        """Start a fresh record of which hosts get contacted."""
        self._hosts = set()

    def contacted(self) -> int:
        """How many hosts were reached since :meth:`track_hosts`.

        Zero alongside an empty result means the site was never actually asked,
        so "no such book" cannot be a finding about it.
        """
        return len({h for h in self._hosts if h})

    def touched_block(self) -> Optional[str]:
        """A block reason for any host contacted since :meth:`track_hosts`.

        The question has to be "was a host *I* touched blocked?", not "did a block
        start during my turn?": one client serves the whole batch, so a wall is
        recorded the first time any book meets it, and every later victim of the
        same wall would otherwise be filed as having simply found nothing.

        Hosts this source never spoke to are ignored, so a Kobo wall cannot make
        an Audible miss look transient.
        """
        return next((r for h in sorted(self._hosts) if (r := self.blocks.get(h))), None)

    def limits_for(self, host: str) -> Tuple[float, float, int]:
        """This host's ``(min_delay, max_delay, timeout)``.

        See :mod:`bookscraper.limits` for the policy and how matching works.
        """
        return limits.for_host(host, (self.min_delay, self.max_delay, self.timeout))

    def throttle(self, url: str) -> None:
        """Sleep out this host's courtesy delay. Even first contact waits.

        Measured request-to-request and tracked per host, so interleaving sources
        cannot bypass it and waiting on Goodreads does not also delay Kobo --
        which would slow the run without being politer to either.
        """
        host = self.host_of(url)
        self._hosts.add(host)
        low, high, _ = self.limits_for(host)
        delay = random.uniform(low, high)
        now = time.monotonic()
        due = max(now, self._next_at[host]) if host in self._next_at else now + delay
        self._next_at[host] = due + delay
        if (remaining := due - time.monotonic()) > 0:
            time.sleep(remaining)

    def _strike(self, host: str) -> None:
        """Count a transport failure, and pause the host once they pile up."""
        self._strikes[host] = self._strikes.get(host, 0) + 1
        strikes = self._strikes[host]
        cooldown = limits.cooldown_for(strikes)
        if not cooldown:
            return
        self._next_at[host] = max(self._next_at.get(host, 0.0),
                                  time.monotonic() + cooldown)
        if strikes == limits.STRIKES_BEFORE_COOLDOWN:
            warn(f"warning: {host} has refused {strikes} requests in a row; pausing "
                 f"it for {cooldown:.0f}s rather than burning retries on every book")

    def _clear_strikes(self, host: str) -> None:
        self._strikes.pop(host, None)

    def record_block(self, url: str, reason: str) -> None:
        """Note that a host is walling us, warning only the first time."""
        host = self.host_of(url) or url
        if host not in self.blocks:  # one wall met by 500 books is one warning
            warn(f"warning: {host} appears to be blocking automated access: {reason}")
        self.blocks[host] = reason

    def block_reason(self, url: str) -> Optional[str]:
        """Why we think ``url``'s host blocked us, so an adapter can say so
        honestly ("blocked by CAPTCHA") instead of "selector missing"."""
        return self.blocks.get(self.host_of(url) or (url or "").lower())

    def _backoff(self, attempt: int, response: Optional[requests.Response]) -> Optional[float]:
        """Seconds to wait before the next attempt, or ``None`` to give up."""
        raw = ((response.headers.get("Retry-After") if response is not None else "") or "").strip()
        hinted: Optional[float] = None
        if raw.isdigit():
            hinted = float(raw)
        elif raw:
            try:
                when = parsedate_to_datetime(raw)
                when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
                hinted = max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, IndexError):
                hinted = None
        if hinted is not None:
            return None if hinted > _MAX_RETRY_AFTER else hinted
        return min(max(self.min_delay, 1.0) * 2 ** (attempt - 1) + random.uniform(0, 0.75), 30.0)

    def request(self, method: str, url: str, *, params: Optional[Mapping] = None,
                headers: Optional[Mapping] = None, referer: Optional[str] = None,
                json_body: Any = None, stream: bool = False,
                allow_redirects: bool = True) -> Optional[requests.Response]:
        """One logical request with throttling, retries and block checks.

        Returns the successful response, or ``None`` for any failure whatsoever.
        Never raises.
        """
        if not isinstance(url, str) or not url.startswith("http"):
            warn(f"warning: refusing to fetch {url!r}")
            return None
        sent = dict(headers or {})
        if referer:
            sent.setdefault("Referer", referer)
            sent.setdefault("Sec-Fetch-Site", "same-origin")

        host = self.host_of(url)
        timeout = self.limits_for(host)[2]
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            self.throttle(url)
            try:
                response = self.session.request(
                    method, url, params=params, headers=sent or None,
                    timeout=timeout, json=json_body, stream=stream,
                    allow_redirects=allow_redirects)
            except (requests.Timeout, requests.ConnectionError) as exc:
                self._strike(host)
                wait = self._backoff(attempt, None) if attempt < attempts else None
                if wait is None:
                    warn(f"warning: {method} {url} gave up: {type(exc).__name__}")
                    return None
                warn(f"warning: {method} {url} {type(exc).__name__}; retrying in {wait:.1f}s")
                time.sleep(wait)
                continue
            except (requests.RequestException, ValueError, OSError) as exc:
                # Malformed URL, too many redirects, bad SSL: not retryable.
                warn(f"warning: {method} {url} failed unrecoverably: {exc}")
                return None

            status = response.status_code
            if status in _RETRY_STATUS:
                self._strike(host)
            if status in _RETRY_STATUS and attempt < attempts:
                wait = self._backoff(attempt, response)
                if wait is None:
                    self.record_block(url, f"HTTP {status} with a Retry-After "
                                           "beyond our wait budget")
                    return None
                warn(f"warning: {method} {url} -> HTTP {status}; retrying in {wait:.1f}s")
                time.sleep(wait)
                continue
            if reason := blocks.classify(response):
                self.record_block(url, reason)
                warn(f"warning: {method} {url} -> {reason}; not parsing this body")
                return None
            if status >= 400:
                warn(f"warning: {method} {url} -> HTTP {status}")
                return None
            self._clear_strikes(host)
            return response
        return None
