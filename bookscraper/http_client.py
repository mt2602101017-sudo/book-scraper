"""Polite, retrying HTTP layer with an optional Selenium fallback.

Design rules this module exists to enforce:

* A randomised courtesy delay of ``min_delay``..``max_delay`` seconds is slept
  **before every outbound request**, tracked **per host**, so hammering one
  site is impossible even when adapters interleave requests to several sites.
* Nothing ever propagates out to the caller. Failures are logged and reported
  as ``None``. Adapters therefore never need a try/except around a fetch.
* Transient failures (429, 500, 502, 503, 504, connection resets, timeouts)
  are retried with exponential backoff plus jitter, honouring ``Retry-After``.
* Hard bot-blocks (CAPTCHA interstitials, 403 walls, Cloudflare managed
  challenges and AWS WAF's HTTP-202 JavaScript challenge) are *detected and
  recorded*, never fought. Callers ask :meth:`HttpClient.block_reason` and emit
  a warning instead of parsing an error page as if it were a book. No CAPTCHA is
  ever solved, forged or routed around.
* Selenium is a soft dependency, imported lazily on the first
  :meth:`HttpClient.get_rendered_soup` call. Without it the program still runs
  end to end on ``requests`` + ``beautifulsoup4`` alone.

One client per run
------------------
A batch builds **one** client and lends it to every book, which is what makes the
courtesy delay, the block registry and the browser properties of the *run* rather
than of one ISBN. A process per book would reset the delay clock on every book,
re-discover each site's block from scratch, and start a browser per book.

Requests are issued one at a time, in the order the adapters ask for them, so
none of this state needs locking. The one lock left in the project is the
``fcntl`` file lock in :mod:`bookscraper.storage`, which guards against a second
``main.py`` **process** sharing the same output tree -- a different problem.
"""

from __future__ import annotations

import sys
from .verbosity import verbose
import math
import random
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


__all__ = ["HttpClient", "BROWSER_MODES", "RENDER_SETTLE_SECONDS"]

#: Legal values for the ``browser`` constructor argument / ``--browser`` flag.
BROWSER_MODES: Tuple[str, ...] = ("auto", "never", "always")

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: Status codes worth trying again.
_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})

#: Grace period, in seconds, that a ``wait_css`` selector gets *after* the page
#: has finished loading before we conclude it is not on the page at all. A caller
#: asking for ``wait_seconds=20`` still gets 20 s while the document is loading;
#: this only bounds the hopeless case, where a selector the site has removed used
#: to cost the full timeout on every single fetch. See
#: :meth:`HttpClient._await_selector`.
RENDER_SETTLE_SECONDS = 3.0

#: Substrings that can only be an anti-bot interstitial: vendor script names,
#: challenge cookie names and CAPTCHA widget markup. None of these can plausibly
#: occur in a book title, blurb or review, so a bare substring hit is conclusive.
_STRONG_BLOCK_MARKERS: Tuple[str, ...] = (
    "enter the characters you see below",
    "type the characters you see in this image",
    "/errors/validatecaptcha",
    "captcha-delivery.com",
    "g-recaptcha",
    "h-captcha",
    "hcaptcha.com",
    "cf-browser-verification",
    "checking if the site connection is secure",
    "attention required! | cloudflare",
    "request unsuccessful. incapsula",
    "px-captcha",
    "perimeterx",
    # AWS WAF's JavaScript "challenge" interstitial. It arrives as HTTP **202**
    # with a ~2.4 KB body, so neither the retry-status list nor the 401/403/429
    # checks below would ever see it: without these markers a challenge page is
    # handed to adapters as a success and parsed as if it were content.
    "awswafcookiedomainlist",
    "awswafintegration",
    "token.awswaf.com",
    "we need to verify that you're not a robot",
)

#: Substrings that are *ordinary English* and therefore appear in real content:
#: a book can be called "Access Denied", and an ebook-store review can say "got
#: access denied on download". Matching these as bare substrings threw away 30
#: legitimate Kobo reviews and then poisoned the whole host for the rest of the
#: run, so they now require corroboration -- see :meth:`HttpClient._looks_blocked`.
_WEAK_BLOCK_MARKERS: Tuple[str, ...] = (
    "robot check",
    "are you a robot",
    "just a moment...",
    "pardon our interruption",
    "unusual traffic from your computer",
    "access denied",
    "you have been blocked",
)

#: Every marker, for the rendered-DOM scan and for tests.
_BLOCK_MARKERS: Tuple[str, ...] = _STRONG_BLOCK_MARKERS + _WEAK_BLOCK_MARKERS

#: A body this small cannot be a real product page, so a weak marker in it is
#: almost certainly an interstitial rather than content.
_INTERSTITIAL_MAX_CHARS = 20480

#: Upper bound on any courtesy delay, so a fat-fingered ``--min-delay`` cannot
#: wedge the run for hours (and ``inf`` cannot poison ``time.sleep``).
_MAX_DELAY_SECONDS = 60.0

#: A ``Retry-After`` above this means the host wants us gone for the day. Waiting
#: is pointless, so the retry loop aborts rather than sleeping a capped 60s once
#: per attempt (which is what the old code did, contradicting its own comment).
_MAX_RETRY_AFTER_SECONDS = 120.0

#: Hard ceiling on a single binary (cover-image) download. A book cover is tens
#: of KB; anything vastly larger is a redirect to the wrong asset, and buffering
#: it whole would be a memory-exhaustion footgun.
MAX_COVER_BYTES = 12 * 1024 * 1024


#: Response header AWS WAF sets when it issues a challenge or CAPTCHA. Present
#: on 202/405/403 responses alike, so it is checked independently of the status.
_WAF_ACTION_HEADER = "x-amzn-waf-action"

#: How much of the body to sniff for block markers.
_SNIFF_BYTES = 8192


def _supported_accept_encoding() -> str:
    """Advertise only the content codings this install can actually decode.

    Sending ``Accept-Encoding: br`` without a brotli decoder installed makes
    every CDN-fronted site hand back bytes that ``requests`` cannot inflate, so
    ``response.text`` is binary noise and every selector silently misses. We ask
    urllib3 what it supports (it accounts for optional ``brotli``/``zstandard``)
    and fall back to the two codings that are always available.
    """
    try:
        from urllib3.util.request import ACCEPT_ENCODING  # type: ignore

        codings = [c.strip() for c in str(ACCEPT_ENCODING).split(",") if c.strip()]
    except (ImportError, AttributeError):  # pragma: no cover - very old urllib3
        codings = []
    if not codings:
        codings = ["gzip", "deflate"]
    return ", ".join(codings)


class HttpClient:
    """A single shared HTTP client. One instance per pipeline run.

    :param min_delay: lower bound of the per-host courtesy sleep, seconds.
    :param max_delay: upper bound of the per-host courtesy sleep, seconds.
    :param timeout: per-request socket/read timeout, seconds.
    :param max_retries: extra attempts after the first one fails retryably.
    :param browser: ``'auto'`` (lazy Selenium only when a caller asks for a
        rendered page), ``'never'`` (rendering disabled outright), or
        ``'always'`` (route HTML page fetches through the browser).
    :param user_agent: override the default desktop-Chrome UA string.
    :param respect_robots: when True, consult each host's ``robots.txt`` and
        refuse disallowed paths.
    """

    def __init__(
        self,
        min_delay: float = 1.0,
        max_delay: float = 2.0,
        timeout: int = 25,
        max_retries: int = 3,
        browser: str = "auto",
        user_agent: Optional[str] = None,
        respect_robots: bool = False,
    ) -> None:

        if browser not in BROWSER_MODES:
            print("warning: Unknown browser mode %r; falling back to 'auto' (valid: %s)" % (browser, ', '.join(BROWSER_MODES)), file=sys.stderr)
            browser = "auto"

        self.min_delay = self._sane_delay(min_delay, 1.0, "min_delay")
        self.max_delay = max(self.min_delay, self._sane_delay(max_delay, 2.0, "max_delay"))
        self.timeout = int(timeout)
        self.max_retries = max(0, int(max_retries))
        self.browser = browser
        self.user_agent = user_agent or _DEFAULT_USER_AGENT
        self.respect_robots = bool(respect_robots)

        #: host -> monotonic timestamp of the earliest moment that host may be
        #: hit again. Written when a request is about to go out, so the delay is
        #: measured from request to request rather than from response to request.
        self._next_allowed_at: Dict[str, float] = {}
        #: host -> human-readable reason we believe we are being blocked.
        self._blocks: Dict[str, str] = {}
        #: host -> RobotFileParser (or None when robots.txt was unreachable).
        self._robots: Dict[str, Optional[RobotFileParser]] = {}

        self._driver: Any = None
        self._browser_unavailable = False
        self._browser_reason: Optional[str] = None
        #: label -> True once a browser sign-in for that site has succeeded, so a
        #: batch signs in once rather than per book.
        self._logged_in: Dict[str, bool] = {}
        #: Lazily-imported Selenium names, shared by the render helpers.
        self._selenium_cache: Optional[Dict[str, Any]] = None

        #: Hosts contacted since :meth:`begin_host_tracking`, or ``None`` when
        #: nobody is tracking. See :meth:`begin_host_tracking` for why.
        self._hosts: Optional[Set[str]] = None
        #: Retries spent on the request currently in flight, for diagnostics.
        self._last_retries = 0

        self.session = requests.Session()
        self.session.headers.update(self._default_headers())

        if verbose():
            print('  HttpClient ready (delay %.2f-%.2fs/host, timeout %ss, retries %s, browser=%s)' % (self.min_delay, self.max_delay, self.timeout, self.max_retries, self.browser), file=sys.stderr)

    # -- construction helpers ------------------------------------------------

    def _sane_delay(self, value: Any, fallback: float, label: str) -> float:
        """Coerce a courtesy-delay argument to a finite, non-negative, capped float.

        ``float('inf')`` (which ``argparse`` happily produces from ``inf`` or from
        any literal >= 1e309) would make ``random.uniform`` return NaN and
        ``time.sleep(NaN)`` raise, so non-finite input is rejected outright.
        """
        try:
            number = float(value)
        except (TypeError, ValueError):
            print('warning: %s=%r is not a number; using %.2fs' % (label, value, fallback), file=sys.stderr)
            return fallback
        if not math.isfinite(number):
            print('warning: %s=%r is not a finite number; using %.2fs' % (label, value, fallback), file=sys.stderr)
            return fallback
        if number < 0:
            print('warning: %s=%.2f is negative; using 0' % (label, number), file=sys.stderr)
            return 0.0
        if number > _MAX_DELAY_SECONDS:
            print('warning: %s=%.2f exceeds the %.0fs ceiling; clamping' % (label, number, _MAX_DELAY_SECONDS), file=sys.stderr)
            return _MAX_DELAY_SECONDS
        return number

    def _default_headers(self) -> Dict[str, str]:
        """Headers a real desktop browser would send on a top-level navigation."""
        return {
            "User-Agent": self.user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            # Only codings we can decode -- see _supported_accept_encoding().
            "Accept-Encoding": _supported_accept_encoding(),
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
            "DNT": "1",
        }

    # -- politeness ----------------------------------------------------------

    @staticmethod
    def host_of(url: str) -> str:
        """Return the lower-cased hostname of ``url`` (``''`` when unparseable)."""
        try:
            return (urlsplit(url).hostname or "").lower()
        except ValueError:
            return ""

    # -- which hosts a source touched ----------------------------------------

    def begin_host_tracking(self) -> None:
        """Start recording which hosts are contacted from now on.

        Lets a caller ask "was any host I actually touched blocked?" instead of
        "did a host start blocking during my turn?". The distinction matters
        because one client serves the whole batch: a host is recorded as blocked
        the *first* time any book meets its wall, so every later victim of the same
        wall would otherwise look like it simply found nothing. See
        :meth:`Pipeline._status_for`.
        """
        self._hosts = set()

    def hosts_contacted(self) -> Set[str]:
        """Hosts contacted since :meth:`begin_host_tracking`."""
        return set(self._hosts or set())

    def _note_host(self, host: str) -> None:
        """Record one contacted host, if anyone is tracking them."""
        if self._hosts is not None and host:
            self._hosts.add(host)



    def _throttle(self, url: str) -> None:
        """Wait out the courtesy delay for ``url``'s host before hitting it again.

        The delay is a fresh ``random.uniform(min_delay, max_delay)`` measured from
        the previous request to the *same* host, so interleaving sources cannot
        bypass it -- and even the first contact with a host waits, so a run cannot
        open with a burst.

        Tracked per host rather than globally: waiting out Goodreads' delay must
        not also delay the next request to Kobo, because that would slow the run
        down without making it any politer to either site.
        """
        host = self.host_of(url)
        # Every outbound request -- static or rendered -- passes through here, so
        # this is the one place that sees the full set of hosts a source touched.
        self._note_host(host)
        delay = random.uniform(self.min_delay, self.max_delay)

        now = time.monotonic()
        earliest = self._next_allowed_at.get(host)
        if earliest is None:
            # First contact with this host: still be polite, just briefly.
            due = now + (delay if self.min_delay > 0 else 0.0)
        else:
            due = max(now, earliest)
        self._next_allowed_at[host] = due + delay

        remaining = due - time.monotonic()
        if remaining > 0:
            if verbose():
                print('  Throttling %s for %.2fs' % (host or url, remaining), file=sys.stderr)
            time.sleep(remaining)

    # -- robots.txt ----------------------------------------------------------

    def _robots_for(self, url: str) -> Optional[RobotFileParser]:
        """Fetch and cache ``robots.txt`` for ``url``'s origin, once per host."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host in self._robots:
            return self._robots[host]
        parser: Optional[RobotFileParser] = None
        robots_url = f"{parts.scheme or 'https'}://{parts.netloc}/robots.txt"
        try:
            # Deliberately a bare session call: going through _request() would
            # recurse back into the robots check.
            self._throttle(robots_url)
            resp = self.session.get(robots_url, timeout=self.timeout)
            if resp.status_code == 200 and resp.text:
                parser = RobotFileParser()
                parser.parse(resp.text.splitlines())
        except requests.RequestException as exc:
            if verbose():
                print('  Could not read %s: %s' % (robots_url, exc), file=sys.stderr)
        self._robots[host] = parser
        return parser

    def _robots_allows(self, url: str) -> bool:
        """True unless ``respect_robots`` is on and robots.txt forbids ``url``."""
        if not self.respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True  # No readable robots.txt -> nothing to disobey.
        try:
            return bool(parser.can_fetch(self.user_agent, url))
        except (ValueError, AttributeError):
            return True

    # -- block detection -----------------------------------------------------

    def _record_block(self, url: str, reason: str) -> None:
        host = self.host_of(url) or url
        # Warn the first time only: one wall met by 500 books is one warning.
        first_time = host not in self._blocks
        self._blocks[host] = reason
        if first_time:
            print('warning: %s appears to be blocking automated access: %s' % (host, reason), file=sys.stderr)

    def block_reason(self, url_or_host: str) -> Optional[str]:
        """Return why we think ``url_or_host`` blocked us, or ``None``.

        Adapters call this after a fetch returns ``None`` so they can emit an
        honest warning ("blocked by CAPTCHA") rather than "selector missing".
        """
        host = self.host_of(url_or_host) or (url_or_host or "").lower()
        return self._blocks.get(host)

    @property
    def blocks(self) -> Dict[str, str]:
        """Read-only snapshot of ``host -> block reason`` seen this run."""
        return dict(self._blocks)

    def _looks_blocked(self, response: requests.Response) -> Optional[str]:
        """Return a block reason if ``response`` is an anti-bot interstitial."""
        # AWS WAF announces a challenge in a header, on any status code, so this
        # is checked before (and independently of) the body sniff.
        waf_action = ""
        try:
            for key, value in (response.headers or {}).items():
                if str(key).lower() == _WAF_ACTION_HEADER:
                    waf_action = str(value).strip()
                    break
        except (AttributeError, TypeError):
            waf_action = ""
        if waf_action and waf_action.lower() not in ("allow", "none", ""):
            return (
                f"AWS WAF {waf_action} response (HTTP {response.status_code}, "
                f"{_WAF_ACTION_HEADER}: {waf_action})"
            )

        ctype = (response.headers.get("Content-Type") or "").lower()
        body = ""
        full_length = 0
        if "html" in ctype or "text" in ctype or not ctype:
            try:
                text = response.text or ""
            except (UnicodeDecodeError, ValueError):
                text = ""
            full_length = len(text)
            body = text[:_SNIFF_BYTES].lower()

        for marker in _STRONG_BLOCK_MARKERS:
            if marker in body:
                return f"anti-bot interstitial detected (matched {marker!r})"

        # Weak markers are ordinary English, so one of two corroborating signals
        # must also hold before we throw a body away:
        #   * the status is an error (a real page would be 2xx/3xx), or
        #   * the body is too small to be a product page (i.e. an interstitial).
        # Every anti-bot interstitial in the wild satisfies one of these -- they
        # are a few KB of challenge script, not a 300 KB storefront page. A
        # <title> test was considered and rejected: a book genuinely *called*
        # "Access Denied" would trip it, and a false positive here is far more
        # damaging than a false negative (it silently discards real content and
        # marks the host as hostile for the rest of the run, whereas a missed
        # interstitial merely produces "selector not found" warnings).
        weak_hit = next((m for m in _WEAK_BLOCK_MARKERS if m in body), None)
        if weak_hit is not None:
            small_body = 0 < full_length <= _INTERSTITIAL_MAX_CHARS
            error_status = response.status_code >= 400
            if small_body or error_status:
                why = (
                    f"body only {full_length} chars" if small_body
                    else f"HTTP {response.status_code}"
                )
                return f"anti-bot interstitial detected (matched {weak_hit!r}, {why})"
            if verbose():
                print('  Ignoring block marker %r in a %d-char HTTP %s body from %s: it reads as page content, not an interstitial' % (weak_hit, full_length, response.status_code, response.url), file=sys.stderr)

        if response.status_code in (401, 403, 451):
            return f"HTTP {response.status_code} wall"
        if response.status_code == 429:
            return "HTTP 429 rate limit"
        return None

    # -- core request loop ---------------------------------------------------

    @staticmethod
    def _retry_after_seconds(response: requests.Response) -> Optional[float]:
        """Parse ``Retry-After`` (delta-seconds or HTTP-date) into seconds."""
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        raw = raw.strip()
        if raw.isdigit():
            return float(raw)
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        delta = (when - datetime.now(timezone.utc)).total_seconds()
        return delta if delta > 0 else None

    def _backoff(self, attempt: int, response: Optional[requests.Response]) -> Optional[float]:
        """Seconds to wait before attempt ``attempt`` + 1, or ``None`` to give up.

        A ``Retry-After`` beyond :data:`_MAX_RETRY_AFTER_SECONDS` returns ``None``:
        the host has told us to come back much later, so burning
        ``max_retries * 60s`` on one URL helps nobody. (The previous code silently
        capped such a hint at 60s and slept anyway, which contradicted its own
        "give up instead" comment.)
        """
        if response is not None:
            hinted = self._retry_after_seconds(response)
            if hinted is not None:
                if hinted > _MAX_RETRY_AFTER_SECONDS:
                    print('warning: Retry-After of %.0fs exceeds the %.0fs budget; giving up on this URL instead of waiting' % (hinted, _MAX_RETRY_AFTER_SECONDS), file=sys.stderr)
                    return None
                return hinted
        base = max(self.min_delay, 1.0) * (2 ** (attempt - 1))
        return min(base + random.uniform(0.0, 0.75), 30.0)

    def _request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Optional[requests.Response]:
        """Perform one logical request, recording how it turned out.

        A thin wrapper around :meth:`_request_inner`, kept so callers have one
        entry point.
        """

        response = self._request_inner(method, url, **kwargs)

        return response

    def _request_inner(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        referer: Optional[str] = None,
        allow_redirects: bool = True,
        json_payload: Any = None,
        data: Any = None,
        stream: bool = False,
    ) -> Optional[requests.Response]:
        """Perform one logical request with throttling, retries and block checks.

        Returns the successful :class:`requests.Response`, or ``None`` for any
        failure whatsoever. Never raises.
        """
        if not url or not isinstance(url, str):
            print('warning: Refusing to fetch empty/non-string URL %r' % (url,), file=sys.stderr)
            return None
        # urlparse raises ValueError('Invalid IPv6 URL') on an unbalanced bracket
        # in the netloc, which a scraped <img src="//cdn]host/x.jpg"> really does
        # produce. host_of() already guards for this; so must we.
        try:
            scheme = urlparse(url).scheme
        except ValueError as exc:
            print('warning: Refusing to fetch unparseable URL %r (%s)' % (url, exc), file=sys.stderr)
            return None
        if not scheme.startswith("http"):
            print('warning: Refusing to fetch non-HTTP(S) URL %r' % (url,), file=sys.stderr)
            return None

        if not self._robots_allows(url):
            print('warning: robots.txt disallows %s -- skipping (respect_robots=True)' % (url,), file=sys.stderr)
            return None

        request_headers: Dict[str, str] = {}
        if referer:
            request_headers["Referer"] = referer
            request_headers["Sec-Fetch-Site"] = "same-origin"
        if headers:
            request_headers.update(headers)

        attempts = self.max_retries + 1
        last_error: Optional[str] = None

        for attempt in range(1, attempts + 1):
            self._last_retries = attempt - 1
            self._throttle(url)
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    headers=request_headers or None,
                    timeout=self.timeout,
                    allow_redirects=allow_redirects,
                    json=json_payload,
                    data=data,
                    stream=stream,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                wait = self._backoff(attempt, None) if attempt < attempts else None
                if wait is not None:
                    print('warning: %s %s failed (%s); retrying in %.1fs (attempt %d/%d)' % (method, url, type(exc).__name__, wait, attempt, attempts), file=sys.stderr)
                    time.sleep(wait)
                    continue
                break
            except requests.RequestException as exc:
                # Malformed URL, too many redirects, bad SSL, ... not retryable.
                print('warning: %s %s failed unrecoverably: %s' % (method, url, exc), file=sys.stderr)
                return None
            except (ValueError, OSError) as exc:
                print('warning: %s %s raised %s: %s' % (method, url, type(exc).__name__, exc), file=sys.stderr)
                return None

            status = response.status_code

            if status in _RETRY_STATUS and attempt < attempts:
                wait = self._backoff(attempt, response)
                if wait is None:
                    self._record_block(
                        url, f"HTTP {status} with a Retry-After beyond our wait budget"
                    )
                    return None
                print('warning: %s %s -> HTTP %s; retrying in %.1fs (attempt %d/%d)' % (method, url, status, wait, attempt, attempts), file=sys.stderr)
                time.sleep(wait)
                last_error = f"HTTP {status}"
                continue

            reason = self._looks_blocked(response)
            if reason is not None:
                self._record_block(url, reason)
                print('warning: %s %s -> %s; not parsing this body' % (method, url, reason), file=sys.stderr)
                return None

            if status >= 400:
                print('warning: %s %s -> HTTP %s' % (method, url, status), file=sys.stderr)
                return None

            if verbose():
                print('  %s %s -> HTTP %s (%s bytes)' % (method, url, status, response.headers.get('Content-Length') or '?'), file=sys.stderr)
            return response

        # Fell out of the retry loop: every attempt timed out or reset.
        print('warning: %s %s gave up after %d attempt(s): %s' % (method, url, attempts, last_error), file=sys.stderr)
        return None

    # -- public fetchers -----------------------------------------------------

    def get(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        referer: Optional[str] = None,
        allow_redirects: bool = True,
    ) -> Optional[requests.Response]:
        """GET ``url``. Returns the response, or ``None`` on any failure."""
        return self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            referer=referer,
            allow_redirects=allow_redirects,
        )

    def get_soup(
        self,
        url: str,
        **kw: Any,
    ) -> Optional[BeautifulSoup]:
        """GET ``url`` and parse it into a :class:`~bs4.BeautifulSoup` tree.

        Accepts the same keywords as :meth:`get`, plus ``force_browser=True``
        to route this one page through Selenium. With ``browser='always'``
        every call here goes through Selenium automatically.
        """
        force_browser = bool(kw.pop("force_browser", False))
        if force_browser or self.browser == "always":
            soup = self.get_rendered_soup(url, wait_css=kw.pop("wait_css", None))
            if soup is not None:
                return soup
            if verbose():
                print('  Browser fetch of %s unavailable; falling back to requests' % (url,), file=sys.stderr)
            kw.pop("wait_css", None)

        kw.pop("wait_css", None)
        response = self.get(url, **kw)
        if response is None:
            return None
        return self.soup_from_response(response)

    def soup_from_response(self, response: requests.Response) -> Optional[BeautifulSoup]:
        """Parse an already-fetched response body. ``lxml`` with an html.parser fallback."""
        try:
            text = response.text
        except (UnicodeDecodeError, ValueError) as exc:
            print('warning: Could not decode body of %s: %s' % (response.url, exc), file=sys.stderr)
            return None
        if text and "<" not in text[:4096]:
            # Almost certainly an undecoded Content-Encoding or a binary body.
            print('warning: Body of %s does not look like HTML (Content-Encoding: %s, Content-Type: %s); refusing to parse it' % (response.url, response.headers.get('Content-Encoding') or 'none', response.headers.get('Content-Type') or 'unknown'), file=sys.stderr)
            return None
        for parser in ("lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:  # bs4 raises FeatureNotFound and friends
                if verbose():
                    print('  BeautifulSoup parser %s unavailable/failed: %s' % (parser, exc), file=sys.stderr)
        print('warning: No usable HTML parser for %s' % (response.url,), file=sys.stderr)
        return None

    def get_json(self, url: str, **kw: Any) -> Optional[Any]:
        """GET ``url`` expecting JSON. Returns the decoded object or ``None``."""
        headers = dict(kw.pop("headers", None) or {})
        headers.setdefault("Accept", "application/json, text/plain, */*")
        headers.setdefault("Sec-Fetch-Dest", "empty")
        headers.setdefault("Sec-Fetch-Mode", "cors")
        response = self.get(url, headers=headers, **kw)
        if response is None:
            return None
        return self._decode_json(response)

    def post_json(
        self,
        url: str,
        payload: Any,
        *,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Optional[Any]:
        """POST ``payload`` as JSON to ``url``; return the decoded reply or ``None``."""
        merged = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
        }
        if headers:
            merged.update(headers)
        response = self._request("POST", url, headers=merged, json_payload=payload)
        if response is None:
            return None
        return self._decode_json(response)

    def _decode_json(self, response: requests.Response) -> Optional[Any]:
        try:
            return response.json()
        except ValueError as exc:
            snippet = (response.text or "")[:160].replace("\n", " ")
            print('warning: Non-JSON body from %s (%s): %s' % (response.url, exc, snippet), file=sys.stderr)
            return None

    def download_binary(self, url: str, *, referer: Optional[str] = None) -> Optional[bytes]:
        """Download raw bytes (cover images). Returns ``None`` on failure."""
        result = self.download_binary_with_type(url, referer=referer)
        return None if result is None else result[0]

    def download_binary_with_type(
        self, url: str, *, referer: Optional[str] = None
    ) -> Optional[Tuple[bytes, Optional[str]]]:
        """Like :meth:`download_binary` but also returns the ``Content-Type``.

        Additive convenience so callers can name the file from the server's own
        content type rather than guessing from the URL.
        """
        headers = {
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
        }
        # stream=True so an oversized asset is abandoned mid-flight rather than
        # buffered whole: a scraped "cover" URL can redirect to a huge TIFF, PDF
        # or video, and response.content would read all of it into memory first.
        response = self._request("GET", url, headers=headers, referer=referer,
                                 stream=True)
        if response is None:
            return None

        ctype = response.headers.get("Content-Type")
        if ctype and "html" in ctype.lower():
            print('warning: %s returned HTML (%s), not binary data' % (url, ctype), file=sys.stderr)
            response.close()
            return None

        declared = response.headers.get("Content-Length")
        if declared and declared.strip().isdigit() and int(declared) > MAX_COVER_BYTES:
            print('warning: %s declares %s bytes, over the %d-byte cover ceiling; not downloading it' % (url, declared.strip(), MAX_COVER_BYTES), file=sys.stderr)
            response.close()
            return None

        chunks: List[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_COVER_BYTES:
                    print('warning: %s exceeded the %d-byte cover ceiling mid-download; aborting' % (url, MAX_COVER_BYTES), file=sys.stderr)
                    return None
                chunks.append(chunk)
        except (requests.RequestException, OSError, ValueError) as exc:
            print('warning: Could not read body of %s: %s' % (url, exc), file=sys.stderr)
            return None
        finally:
            response.close()

        data = b"".join(chunks)
        if not data:
            print('warning: Empty body from %s' % (url,), file=sys.stderr)
            return None
        return data, ctype

    # -- optional Selenium ---------------------------------------------------

    def _ensure_driver(self) -> Any:
        """Lazily build a headless Selenium driver, or return ``None``.

        Selenium is imported here and nowhere else, so an environment without
        it (or without a browser/driver) is a warning, not an ImportError.

        Called from :meth:`get_rendered_soup` and :meth:`browser_login` (as
        :meth:`get_rendered_soup` does), which is what stops a parallel batch
        from starting one browser per thread.
        """
        if self.browser == "never":
            if verbose():
                print("  browser='never': not starting a browser", file=sys.stderr)
            return None
        if self._driver is not None:
            return self._driver
        if self._browser_unavailable:
            if verbose():
                print('  Browser previously unavailable (%s); not retrying' % (self._browser_reason,), file=sys.stderr)
            return None

        try:
            from selenium import webdriver  # noqa: PLC0415 - deliberately lazy
            from selenium.common.exceptions import WebDriverException
        except ImportError as exc:
            self._browser_unavailable = True
            self._browser_reason = f"selenium is not installed ({exc})"
            print("warning: Selenium is not installed, so JavaScript-rendered pages will be skipped. Install the optional extra with 'pip install -r requirements-optional.txt' if you need them. Continuing with requests+BeautifulSoup only.", file=sys.stderr)
            return None

        builders: List[Tuple[str, Any]] = []

        def _chrome() -> Any:
            options = webdriver.ChromeOptions()
            # 'eager' returns from driver.get() at DOMContentLoaded instead of
            # waiting for every third-party tracker to finish. Tracker-heavy
            # storefronts (Kobo, for one) never fire 'load' at all, and 'normal'
            # would turn every such page into a page-load timeout.
            options.page_load_strategy = "eager"
            for flag in (
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1440,2400",
                "--disable-blink-features=AutomationControlled",
                "--lang=en-US",
                f"--user-agent={self.user_agent}",
            ):
                options.add_argument(flag)
            return webdriver.Chrome(options=options)

        def _firefox() -> Any:
            options = webdriver.FirefoxOptions()
            options.page_load_strategy = "eager"
            options.add_argument("-headless")
            options.set_preference("general.useragent.override", self.user_agent)
            options.set_preference("intl.accept_languages", "en-US, en")
            return webdriver.Firefox(options=options)

        builders.append(("Chrome", _chrome))
        builders.append(("Firefox", _firefox))

        problems: List[str] = []
        for label, builder in builders:
            try:
                driver = builder()
            except WebDriverException as exc:
                problems.append(f"{label}: {str(exc).splitlines()[0]}")
                continue
            except (OSError, ValueError, RuntimeError) as exc:
                problems.append(f"{label}: {exc}")
                continue
            driver.set_page_load_timeout(max(self.timeout, 30))
            self._driver = driver
            print('Started headless %s for JavaScript-rendered pages' % (label,), file=sys.stderr)
            return driver

        self._browser_unavailable = True
        self._browser_reason = "; ".join(problems) or "no supported browser found"
        print('warning: No usable browser driver (%s). JavaScript-rendered pages will be skipped; static parsing continues.' % (self._browser_reason,), file=sys.stderr)
        return None

    def get_rendered_soup(
        self,
        url: str,
        *,
        wait_css: Optional[str] = None,
        wait_seconds: int = 8,
        scroll_passes: int = 0,
    ) -> Optional[BeautifulSoup]:
        """Load ``url`` in a real browser and return the rendered DOM.

        :param wait_css: CSS selector to wait for before reading the DOM.
        :param wait_seconds: how long to wait for ``wait_css``.
        :param scroll_passes: how many times to scroll to the bottom (for
            lazily-loaded review lists).

        Returns ``None`` -- with a clear warning -- whenever Selenium or a
        driver is missing, the mode is ``'never'``, or the page fails to load.
        Callers must treat rendering as a bonus, never a requirement.

        There is one driver for the whole run, reused across books: starting a
        browser costs seconds, and a 10 000-book batch must not pay that 10 000
        times.
        """
        if self.browser == "never":
            if verbose():
                print("  browser='never': skipping rendered fetch of %s" % (url,), file=sys.stderr)
            return None
        if self._browser_unavailable:
            if verbose():
                print('  Browser unavailable (%s); skipping rendered fetch of %s' % (self._browser_reason, url), file=sys.stderr)
            return None
        if not self._robots_allows(url):
            print('warning: robots.txt disallows %s -- skipping rendered fetch' % (url,), file=sys.stderr)
            return None

        driver = self._ensure_driver()
        if driver is None:
            return None
        return self._render_with(driver, url, wait_css, wait_seconds, scroll_passes)

    def browser_login(
        self,
        login_url: str,
        steps: Sequence[Tuple[str, str]],
        success_css: str,
        label: str = "the site",
        wait_seconds: int = 20,
    ) -> bool:
        """Sign in through the real browser, then share its cookies with ``requests``.

        The login itself has to happen in Chrome: these forms are multi-step and
        JavaScript-driven, and a plain POST would fail on the first hidden token.
        Once Chrome holds the session, the cookies are copied onto the
        :class:`requests.Session` so ordinary static fetches are authenticated too --
        which is the point, since paging reviews with ``requests`` is far cheaper
        than rendering every page.

        :param steps: ``(css_selector, text)`` pairs typed in order. ``text`` may be
            ``"\\n"`` to submit, which is how multi-page forms advance.
        :param success_css: a selector that only exists once signed in.
        :returns: True on success. On failure it warns and returns False -- the
            caller must carry on anonymously rather than treat this as fatal.

        **This is opt-in and off by default.** Credentials come only from the
        environment, never a flag or a file, so they cannot end up in shell history
        or a screenshot. A CAPTCHA or a 2FA prompt stops the attempt: nothing here
        solves either.
        """
        if self._logged_in.get(label):
            return True
        driver = self._ensure_driver()
        if driver is None:
            print("warning: cannot sign in to %s: no browser is available"
                  % label, file=sys.stderr)
            return False
        symbols = self._selenium_symbols()
        if symbols is None:
            return False
        WebDriverException = symbols["WebDriverException"]

        try:
            self._throttle(login_url)
            driver.get(login_url)
            for selector, text in steps:
                field = self._await_element(driver, selector, wait_seconds)
                if field is None:
                    print("warning: sign-in to %s stalled: %r never appeared. The "
                          "form may have changed, or a CAPTCHA/2FA prompt is in "
                          "the way -- neither is solved here."
                          % (label, selector), file=sys.stderr)
                    return False
                try:
                    field.clear()
                except WebDriverException:
                    # A field can be present but not yet interactable (Amazon
                    # renders the password input before revealing it). Typing
                    # still works once it is visible, so a failed clear on an
                    # empty field is not worth aborting for.
                    pass
                field.send_keys(text)
                time.sleep(random.uniform(0.6, 1.4))

            if self._await_element(driver, success_css, wait_seconds) is None:
                print("warning: sign-in to %s did not complete (no %r after "
                      "submitting). Continuing anonymously."
                      % (label, success_css), file=sys.stderr)
                return False

            moved = 0
            for cookie in driver.get_cookies():
                name, value = cookie.get("name"), cookie.get("value")
                if not name or value is None:
                    continue
                self.session.cookies.set(
                    name, value,
                    domain=cookie.get("domain") or "",
                    path=cookie.get("path") or "/",
                )
                moved += 1
        except WebDriverException as exc:
            print("warning: sign-in to %s failed: %s"
                  % (label, str(exc).splitlines()[0]), file=sys.stderr)
            return False

        self._logged_in[label] = True
        print("Signed in to %s; %d cookie(s) shared with the static session"
              % (label, moved), file=sys.stderr)
        return True

    def _await_element(self, driver: Any, selector: str, seconds: int) -> Any:
        """Poll for one element, returning it or ``None``. Never raises."""
        symbols = self._selenium_symbols()
        if symbols is None:
            return None
        By = symbols["By"]
        WebDriverException = symbols["WebDriverException"]
        deadline = time.monotonic() + max(1, seconds)
        while time.monotonic() < deadline:
            try:
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    # Must be *visible*, not merely present: Amazon renders the
                    # password input before revealing it, and Selenium refuses to
                    # type into a hidden field ("element not interactable"). Waiting
                    # for visibility is what makes a multi-step form work.
                    if element.is_displayed():
                        return element
            except WebDriverException:
                return None
            time.sleep(0.4)
        return None

    def _selenium_symbols(self) -> Optional[Dict[str, Any]]:
        """The few Selenium names the render path needs, imported once and cached.

        Still lazy -- nothing here is imported until a rendered fetch actually
        happens -- but resolved in one place so the render helpers can share them
        without each repeating the import and its ``ImportError`` handling.
        """
        if self._selenium_cache is not None:
            return self._selenium_cache
        try:
            from selenium.common.exceptions import (
                TimeoutException,
                WebDriverException,
            )
            from selenium.webdriver.common.by import By
        except ImportError as exc:  # pragma: no cover - a driver existed, so this is odd
            print('warning: Selenium support modules unavailable: %s' % (exc,), file=sys.stderr)
            return None
        self._selenium_cache = {
            "TimeoutException": TimeoutException,
            "WebDriverException": WebDriverException,
            "By": By,
        }
        return self._selenium_cache

    def _await_selector(
        self, driver: Any, url: str, wait_css: str, wait_seconds: int
    ) -> bool:
        """Wait for ``wait_css``, but stop early once the page has clearly settled.

        Returns ``True`` if the selector appeared. The return value is advisory:
        the caller parses the DOM either way, exactly as before.

        Why this is not one long ``WebDriverWait``
        ------------------------------------------
        It used to be, and that made a *rotted selector* cost the full timeout on
        every fetch. Measured on BookBub, whose ``[data-book-json]`` attribute the
        site has since removed: three renders x 20 s of pure waiting, 64 s of a
        98 s run, for pages that then parsed fine from the DOM that was already
        there. The wait was never load-bearing -- on timeout the old code logged
        and parsed anyway -- so the seconds bought nothing at all.

        So: poll in short steps, and once the document has finished loading give
        the selector only :data:`RENDER_SETTLE_SECONDS` more before concluding it
        is simply not on this page. A page that is genuinely still loading keeps
        the caller's full ``wait_seconds`` budget, so nothing that used to work
        stops working; only the hopeless case gets cheap.
        """
        symbols = self._selenium_symbols()
        if symbols is None:
            return False
        By = symbols["By"]
        WebDriverException = symbols["WebDriverException"]

        deadline = time.monotonic() + max(1.0, float(wait_seconds))
        settle_deadline: Optional[float] = None
        step = 0.25

        while True:
            try:
                if driver.find_elements(By.CSS_SELECTOR, wait_css):
                    return True
            except WebDriverException as exc:
                # An invalid selector or a dead session is not worth retrying.
                if verbose():
                    print('  Selector probe %r on %s failed: %s' % (wait_css, url, str(exc).splitlines()[0]), file=sys.stderr)
                return False

            now = time.monotonic()
            if now >= deadline:
                if verbose():
                    print('  Selector %r never appeared on %s within its full %ss budget; parsing whatever rendered' % (wait_css, url, wait_seconds), file=sys.stderr)
                return False

            if settle_deadline is None and self._document_ready(driver):
                # The document is complete, so anything still missing is either
                # injected late or gone from the page. Give it a short grace.
                settle_deadline = min(now + RENDER_SETTLE_SECONDS, deadline)
            if settle_deadline is not None and now >= settle_deadline:
                if verbose():
                    print('  Selector %r is absent %.1fs after %s finished loading; it has most likely moved or been removed. Parsing the rendered DOM instead of waiting out the remaining %.0fs' % (wait_css, RENDER_SETTLE_SECONDS, url, deadline - now), file=sys.stderr)
                return False

            time.sleep(min(step, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _document_ready(driver: Any) -> bool:
        """True when ``document.readyState`` is ``'complete'``. Never raises."""
        try:
            return driver.execute_script("return document.readyState") == "complete"
        except Exception:  # noqa: BLE001 - a probe must never break a fetch
            return False

    def _render_with(
        self,
        driver: Any,
        url: str,
        wait_css: Optional[str],
        wait_seconds: int,
        scroll_passes: int,
    ) -> Optional[BeautifulSoup]:
        """Drive ``driver`` through one page load and return the parsed DOM."""
        symbols = self._selenium_symbols()
        if symbols is None:
            return None
        TimeoutException = symbols["TimeoutException"]
        WebDriverException = symbols["WebDriverException"]

        self._throttle(url)
        try:
            driver.get(url)
        except TimeoutException as exc:
            # A page-load timeout does NOT mean the DOM is unusable: plenty of
            # sites hold a connection open for analytics long after the content
            # is complete. Carry on to the wait_css / page_source steps and let
            # those decide, rather than throwing away a perfectly good tree.
            print('warning: Browser page-load timed out on %s (%s); reading whatever has rendered so far' % (url, str(exc).splitlines()[0]), file=sys.stderr)
        except WebDriverException as exc:
            print('warning: Browser could not load %s: %s' % (url, str(exc).splitlines()[0]), file=sys.stderr)
            return None

        if wait_css:
            self._await_selector(driver, url, wait_css, wait_seconds)

        for index in range(max(0, int(scroll_passes))):
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except WebDriverException as exc:
                if verbose():
                    print('  Scroll pass %d on %s failed: %s' % (index + 1, url, exc), file=sys.stderr)
                break
            time.sleep(random.uniform(0.8, 1.6))

        try:
            html = driver.page_source
        except WebDriverException as exc:
            print('warning: Could not read rendered DOM of %s: %s' % (url, exc), file=sys.stderr)
            return None

        if not html:
            print('warning: Browser returned an empty DOM for %s' % (url,), file=sys.stderr)
            return None

        lowered = html[:_SNIFF_BYTES].lower()
        for marker in _BLOCK_MARKERS:
            if marker in lowered:
                self._record_block(url, f"anti-bot interstitial in rendered DOM ({marker!r})")
                return None

        for parser in ("lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception as exc:
                if verbose():
                    print('  Parser %s failed on rendered %s: %s' % (parser, url, exc), file=sys.stderr)
        return None

    @property
    def browser_available(self) -> bool:
        """True if a rendered fetch has a realistic chance of succeeding."""
        return self.browser != "never" and not self._browser_unavailable

    # -- misc ----------------------------------------------------------------

    def absolutise(self, base: str, url: str) -> str:
        """Resolve ``url`` against ``base``; returns ``url`` unchanged on failure."""
        if not url:
            return ""
        try:
            return urljoin(base or "", url)
        except ValueError:
            return url

    def close(self) -> None:
        """Release the session and any browser. Safe to call more than once."""
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception as exc:  # driver teardown is famously noisy
                if verbose():
                    print('  Browser quit raised %s: %s' % (type(exc).__name__, exc), file=sys.stderr)
            self._driver = None
        try:
            self.session.close()
        except (requests.RequestException, OSError) as exc:
            if verbose():
                print('  Session close raised %s: %s' % (type(exc).__name__, exc), file=sys.stderr)

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
