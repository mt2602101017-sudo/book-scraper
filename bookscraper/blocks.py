"""Detecting bot walls -- and never fighting them.

CAPTCHA interstitials, Cloudflare managed challenges, 403 walls and AWS WAF's
HTTP-202 JavaScript challenge are *recognised and recorded* so an adapter can say
"blocked" instead of parsing an error page as if it were a book. Nothing here
solves, forges or routes around a challenge.

Why "blocked" and "empty" must never be merged: "Kobo does not sell this 1980s
paperback as an ebook" is a finding, and "Kobo walled us" is a problem to act on.
Merging them once wrote off 629 WAF-challenged Goodreads books as "not on
Goodreads"; every one resolved on a later attempt.
"""

from __future__ import annotations

from typing import Optional

import requests

#: Substrings that can only be an anti-bot interstitial -- vendor script names,
#: challenge cookie names, CAPTCHA widget markup. None of these can plausibly
#: occur in a book title, blurb or review, so a bare substring hit is conclusive.
HARD = (
    "enter the characters you see below", "type the characters you see in this image",
    "/errors/validatecaptcha", "captcha-delivery.com", "g-recaptcha", "h-captcha",
    "hcaptcha.com", "cf-browser-verification", "checking if the site connection is secure",
    "attention required! | cloudflare", "request unsuccessful. incapsula", "px-captcha",
    "perimeterx",
    # AWS WAF's JavaScript challenge arrives as HTTP 202 with a ~2.4 KB body, so
    # no status-code check would ever see it. Without these markers a challenge
    # page is handed to adapters as a success and parsed as content.
    "awswafcookiedomainlist", "awswafintegration", "token.awswaf.com",
    "we need to verify that you're not a robot",
)

#: Substrings that are *ordinary English* and so appear in real content: a book
#: can be called "Access Denied", and a review can say "got access denied on
#: download". Matching these bare threw away 30 legitimate Kobo reviews and then
#: poisoned the host for the rest of the run, so they need corroboration.
SOFT = ("robot check", "are you a robot", "just a moment...", "pardon our interruption",
        "unusual traffic from your computer", "access denied", "you have been blocked")

#: A real product page is hundreds of KB; a challenge is a few KB of script, so a
#: body this small carrying a soft marker is an interstitial rather than content.
_INTERSTITIAL_MAX = 20480

#: Header AWS WAF sets when it issues a challenge, on any status code.
_WAF_HEADER = "x-amzn-waf-action"


def classify(response: requests.Response) -> Optional[str]:
    """Why this response is a bot wall, or ``None`` if it is real content."""
    action = (response.headers.get(_WAF_HEADER) or "").strip()
    if action and action.lower() not in ("allow", "none"):
        return f"AWS WAF {action} response (HTTP {response.status_code})"

    body, length = "", 0
    ctype = (response.headers.get("Content-Type") or "").lower()
    if "html" in ctype or "text" in ctype or not ctype:
        try:
            length = len(response.text or "")
            body = response.text[:8192].lower()
        except (UnicodeDecodeError, ValueError):
            pass

    if hit := next((m for m in HARD if m in body), None):
        return f"anti-bot interstitial detected (matched {hit!r})"
    if hit := next((m for m in SOFT if m in body), None):
        # One of two corroborating signals must hold before real content is
        # discarded. A <title> test was rejected: a book genuinely called "Access
        # Denied" would trip it, and a false positive here is far more damaging
        # than a false negative -- it silently drops content and marks the host
        # hostile for the whole run, whereas a missed wall only yields
        # "selector not found" warnings.
        if 0 < length <= _INTERSTITIAL_MAX:
            return f"anti-bot interstitial detected ({hit!r}, body only {length} chars)"
        if response.status_code >= 400:
            return f"anti-bot interstitial detected ({hit!r}, HTTP {response.status_code})"
    if response.status_code in (401, 403, 451):
        return f"HTTP {response.status_code} wall"
    if response.status_code == 429:
        return "HTTP 429 rate limit"
    return None


def in_rendered(html: str) -> Optional[str]:
    """Why a rendered DOM is a bot wall, or ``None``.

    The browser follows the challenge, so there is no status code to read -- only
    the markup it ended up on.
    """
    lowered = (html or "")[:8192].lower()
    if hit := next((m for m in HARD + SOFT if m in lowered), None):
        return f"anti-bot interstitial in rendered DOM ({hit!r})"
    return None
