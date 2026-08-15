"""The client adapters actually hold: typed fetchers over the polite transport.

:class:`HttpClient` adds "what to do with the bytes" -- parse HTML, decode JSON,
stream a cover, render with a browser -- to the politeness, retry and bot-wall
handling in :class:`~bookscraper.transport.Transport`. One instance per run,
shared by every book, so the delay clock and the single browser are the run's.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from . import blocks
from .render import Browser
from .transport import UA, Transport, warn

#: A book cover is tens of KB; anything vastly larger is a redirect to the wrong
#: asset, and buffering it whole would be a memory-exhaustion footgun.
MAX_COVER_BYTES = 12 * 1024 * 1024

__all__ = ["HttpClient", "soup_of", "UA", "MAX_COVER_BYTES"]


def soup_of(text: str, url: str = "") -> Optional[BeautifulSoup]:
    """Parse HTML with ``lxml``, falling back to ``html.parser``.

    ``None`` when the body is not HTML at all -- almost always an undecoded
    ``Content-Encoding``, which would otherwise make every selector miss silently.
    """
    if text and "<" not in text[:4096]:
        warn(f"warning: body of {url} does not look like HTML; refusing to parse it")
        return None
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception:  # noqa: BLE001 - bs4 raises FeatureNotFound and friends
            continue
    warn(f"warning: no usable HTML parser for {url}")
    return None


class HttpClient(Transport):
    """One shared HTTP client per run.

    :param browser: ``'auto'`` (render only where an adapter asks -- Kobo and
        BookBub), ``'never'`` (rendering off), ``'always'`` (route HTML through
        the browser).
    """

    def __init__(self, min_delay: float = 1.0, max_delay: float = 2.0,
                 timeout: int = 25, retries: int = 3, browser: str = "auto") -> None:
        super().__init__(min_delay, max_delay, timeout, retries)
        self.mode = browser if browser in ("auto", "never", "always") else "auto"
        self.browser = Browser(self.mode, self.timeout)

    def get(self, url: str, **kw: Any) -> Optional[requests.Response]:
        """GET ``url``; the response, or ``None`` on any failure."""
        return self.request("GET", url, **kw)

    def soup(self, url: str, *, render: bool = False, wait_css: Optional[str] = None,
             scrolls: int = 0, wait_seconds: int = 8, **kw: Any) -> Optional[BeautifulSoup]:
        """GET ``url`` and parse it. ``render=True`` routes it through Selenium.

        A failed render falls back to a plain fetch rather than giving up, because
        rendering is a bonus for most sites even where it is essential for two.
        """
        if render or self.mode == "always":
            rendered = self.rendered(url, wait_css=wait_css, scrolls=scrolls,
                                     wait_seconds=wait_seconds)
            if rendered is not None:
                return rendered
        response = self.get(url, **kw)
        return None if response is None else soup_of(response.text, str(response.url))

    def rendered(self, url: str, **kw: Any) -> Optional[BeautifulSoup]:
        """The browser-rendered DOM of ``url``, or ``None``.

        The browser follows a challenge redirect, so the rendered markup is sniffed
        for a wall too -- there is no status code left to read.
        """
        html = self.browser.html(url, self.throttle, **kw)
        if not html:
            return None
        if reason := blocks.in_rendered(html):
            self.record_block(url, reason)
            return None
        return soup_of(html, url)

    def json(self, url: str, **kw: Any) -> Optional[Any]:
        """GET ``url`` expecting JSON; the decoded object or ``None``."""
        headers = {"Accept": "application/json, text/plain, */*",
                   "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                   **dict(kw.pop("headers", None) or {})}
        return self._decode(self.get(url, headers=headers, **kw))

    def post_json(self, url: str, payload: Any, **kw: Any) -> Optional[Any]:
        """POST ``payload`` as JSON; the decoded reply or ``None``.

        Goodreads pages its reviews through a GraphQL endpoint, which is the only
        reason this exists.
        """
        headers = {"Accept": "application/json, text/plain, */*",
                   "Content-Type": "application/json", "Sec-Fetch-Dest": "empty",
                   "Sec-Fetch-Mode": "cors", **dict(kw.pop("headers", None) or {})}
        return self._decode(self.request("POST", url, headers=headers,
                                         json_body=payload, **kw))

    @staticmethod
    def _decode(response: Optional[requests.Response]) -> Optional[Any]:
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            snippet = (response.text or "")[:160].replace("\n", " ")
            warn(f"warning: non-JSON body from {response.url}: {snippet}")
            return None

    def download(self, url: str, *, referer: Optional[str] = None
                 ) -> Optional[Tuple[bytes, Optional[str]]]:
        """A cover as ``(bytes, content_type)``, streamed and size-capped.

        Streamed so an oversized asset is abandoned mid-flight rather than
        buffered whole: a scraped "cover" URL can redirect to a huge TIFF, a PDF
        or a video, and ``response.content`` would read all of it into memory.
        """
        response = self.request("GET", url, stream=True, referer=referer, headers={
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors"})
        if response is None:
            return None
        ctype = response.headers.get("Content-Type")
        declared = (response.headers.get("Content-Length") or "").strip()
        try:
            if ctype and "html" in ctype.lower():
                warn(f"warning: {url} returned HTML ({ctype}), not binary data")
                return None
            if declared.isdigit() and int(declared) > MAX_COVER_BYTES:
                warn(f"warning: {url} declares {declared} bytes, over the "
                     f"{MAX_COVER_BYTES}-byte cover ceiling")
                return None
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > MAX_COVER_BYTES:
                    warn(f"warning: {url} exceeded the cover ceiling mid-download")
                    return None
                chunks.append(chunk)
        except (requests.RequestException, OSError, ValueError) as exc:
            warn(f"warning: could not read the body of {url}: {exc}")
            return None
        finally:
            response.close()
        if not chunks:
            warn(f"warning: empty body from {url}")
            return None
        return b"".join(chunks), ctype

    def close(self) -> None:
        """Release the browser and the session. Safe to call more than once."""
        self.browser.close()
        self.session.close()
