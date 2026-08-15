"""Optional headless-browser rendering, for the two sites that need JavaScript.

Selenium is a **soft** dependency: it is imported inside :meth:`Browser._driver`
and nowhere else, so a machine without it (or without Chrome/Firefox) prints a
warning and the run continues on ``requests`` alone. Kobo and BookBub produce
nothing that way, which is why selenium is in ``requirements.txt`` -- but nothing
here is allowed to be load-bearing for the program starting up.

One driver serves the whole run: starting a browser costs seconds, and a
10 000-book batch must not pay that 10 000 times.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Optional

from .transport import UA, warn

#: Grace a ``wait_css`` selector gets *after* the document finishes loading
#: before we conclude it is not on the page. Without it, one selector the site
#: had removed cost the full timeout on every fetch -- measured at 64 s of a 98 s
#: BookBub run, for pages that then parsed fine from the DOM already present.
SETTLE = 3.0


class Browser:
    """Lazily-started headless browser. ``mode`` mirrors ``HttpClient.browser``."""

    def __init__(self, mode: str = "auto", timeout: int = 25) -> None:
        self.mode = mode
        self.timeout = timeout
        self._session: Any = None
        self._unavailable: Optional[str] = None

    @property
    def available(self) -> bool:
        """True if a rendered fetch has a realistic chance of succeeding."""
        return self.mode != "never" and self._unavailable is None

    def _driver(self) -> Any:
        """The one driver for this run, starting it on first use, or ``None``."""
        if not self.available:
            return None
        if self._session is not None:
            return self._session
        try:
            from selenium import webdriver
            from selenium.common.exceptions import WebDriverException
        except ImportError as exc:
            self._unavailable = f"selenium is not installed ({exc})"
            warn("warning: selenium is missing, so JavaScript-rendered pages are "
                 "skipped; static parsing continues")
            return None

        problems = []
        for label, build in (("Chrome", self._chrome), ("Firefox", self._firefox)):
            try:
                driver = build(webdriver)
            except (WebDriverException, OSError, ValueError, RuntimeError) as exc:
                problems.append(f"{label}: {str(exc).splitlines()[0]}")
                continue
            driver.set_page_load_timeout(max(self.timeout, 30))
            self._session = driver
            warn(f"Started headless {label} for JavaScript-rendered pages")
            return driver
        self._unavailable = "; ".join(problems) or "no supported browser found"
        warn(f"warning: no usable browser driver ({self._unavailable}); "
             "JavaScript-rendered pages will be skipped")
        return None

    @staticmethod
    def _chrome(webdriver: Any) -> Any:
        options = webdriver.ChromeOptions()
        # 'eager' returns at DOMContentLoaded. Tracker-heavy storefronts (Kobo)
        # never fire 'load' at all, so 'normal' times out on every page.
        options.page_load_strategy = "eager"
        for flag in ("--headless=new", "--disable-gpu", "--no-sandbox",
                     "--disable-dev-shm-usage", "--window-size=1440,2400",
                     "--disable-blink-features=AutomationControlled",
                     "--lang=en-US", f"--user-agent={UA}"):
            options.add_argument(flag)
        return webdriver.Chrome(options=options)

    @staticmethod
    def _firefox(webdriver: Any) -> Any:
        options = webdriver.FirefoxOptions()
        options.page_load_strategy = "eager"
        options.add_argument("-headless")
        options.set_preference("general.useragent.override", UA)
        options.set_preference("intl.accept_languages", "en-US, en")
        return webdriver.Firefox(options=options)

    def html(self, url: str, throttle: Callable[[str], None], *,
             wait_css: Optional[str] = None, wait_seconds: int = 8,
             scrolls: int = 0) -> Optional[str]:
        """Load ``url`` and return the rendered HTML, or ``None``.

        ``throttle`` is the client's per-host courtesy delay, so a rendered fetch
        is exactly as polite as a static one.
        """
        driver = self._driver()
        if driver is None:
            return None
        from selenium.common.exceptions import TimeoutException, WebDriverException

        throttle(url)
        try:
            driver.get(url)
        except TimeoutException:
            # A page-load timeout does not mean the DOM is unusable: sites hold
            # connections open for analytics long after the content is complete.
            warn(f"warning: browser page-load timed out on {url}; reading what rendered")
        except WebDriverException as exc:
            warn(f"warning: browser could not load {url}: {str(exc).splitlines()[0]}")
            return None

        if wait_css:
            self._await(driver, wait_css, wait_seconds)
        for _ in range(max(0, scrolls)):
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except WebDriverException:
                break
            time.sleep(random.uniform(0.8, 1.6))
        try:
            return driver.page_source or None
        except WebDriverException:
            return None

    def _await(self, driver: Any, wait_css: str, wait_seconds: int) -> None:
        """Poll for ``wait_css``, giving up early once the page has settled."""
        from selenium.common.exceptions import WebDriverException
        from selenium.webdriver.common.by import By

        deadline = time.monotonic() + max(1.0, wait_seconds)
        settle: Optional[float] = None
        while True:
            try:
                if driver.find_elements(By.CSS_SELECTOR, wait_css):
                    return
                ready = driver.execute_script("return document.readyState") == "complete"
            except WebDriverException:
                return  # invalid selector or dead session; not worth retrying
            now = time.monotonic()
            if now >= deadline or (settle is not None and now >= settle):
                return
            if settle is None and ready:
                settle = min(now + SETTLE, deadline)
            time.sleep(0.25)

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.quit()
            except Exception:  # noqa: BLE001 - driver teardown is famously noisy
                pass
            self._session = None
