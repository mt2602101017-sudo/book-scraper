"""Is this actually the right book?

Three of the six sources index no ISBN at all -- Kobo, Audible and BookBub are
storefronts with their own product ids -- so they can only be found by title and
author. That turns "is this the book we asked for?" into a scraping problem, and
getting it wrong files someone else's book under the requested ISBN.

Every adapter used to carry its own copy of this, and they had drifted. The
differences that *matter* survive as arguments; the rest was accident.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, List, Optional, Sequence, Tuple

_PUNCT = re.compile(r"[^a-z0-9 ]+")
#: Where a subtitle starts, for comparing main titles.
SUBTITLE = re.compile(r"\s*(?::|;|\s[-–—]\s|\(|\[)\s*")

#: Number words that turn one title into its sequel. "Ready Player One" versus
#: "Ready Player Two" scores ~0.88 -- over any sane threshold -- so a differing
#: ordinal needs an explicit veto or the wrong book gets written.
ORDINALS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
            "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
            "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
            "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
            **{str(n): str(n) for n in range(1, 11)}}

#: Study guides, summaries and box sets that fuzzy-match a real title almost
#: perfectly. Every store's search results are saturated with them, so this
#: rejection is load-bearing rather than defensive.
DERIVATIVES = (
    "study guide", "studyguide", "summary of", "summary and analysis", "a summary",
    "summaries", "analysis of", "conversation starters", "trivia on books",
    "trivia-on-books", "digest & review", "digest and review", "workbook",
    "quicklet", "cliffs notes", "cliffsnotes", "sparknotes", "joosr", "instaread",
    "book club kit", "book club questions", "book club", "a guide to",
    "key takeaways", "reading guide", "review and analysis", "shortcut",
    "boxed set", "box set", "books collection")


def fold(value: object) -> str:
    """Strip accents and punctuation for comparison.

    ``"Brontë, C."`` -> ``"bronte c"``. Accent folding matters both ways: it is
    what lets an accented title reach a store's own ASCII slug spelling.
    """
    plain = unicodedata.normalize("NFKD", str(value or ""))
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", plain.casefold()).strip())


def main_part(value: object) -> str:
    """The folded title with any subtitle removed."""
    return fold(SUBTITLE.split(str(value or ""), maxsplit=1)[0])


def is_sequel(left: object, right: object) -> bool:
    """True when two titles differ *only* in a sequel ordinal -- so reject them."""
    a, b = fold(left).split(), fold(right).split()
    if len(a) != len(b) or not a:
        return False
    differing = [(x, y) for x, y in zip(a, b) if x != y]
    if len(differing) != 1:
        return False
    x, y = differing[0]
    return x in ORDINALS and y in ORDINALS and ORDINALS[x] != ORDINALS[y]


def is_derivative(candidate: object, wanted: object = "") -> bool:
    """True when the candidate is a study guide, summary or box set.

    A marker already present in what we asked for does not count -- a book really
    called *A Guide to Birdsong* must stay findable.
    """
    low, want = fold(candidate), fold(wanted)
    return any(m in low and m not in want
               for m in (fold(marker) for marker in DERIVATIVES))


def surname(author: object) -> str:
    """The last token **longer than two characters**, for comparison.

    Not simply the last token: "Celeste Ng" would yield an unusably short "ng",
    and dropping her entirely made her unmatchable on two stores.
    """
    tokens = [t for t in fold(author).split() if len(t) > 2]
    return tokens[-1] if tokens else ""


def authors_agree(wanted: Iterable[object], found: Iterable[object]) -> bool:
    """True when any wanted author plausibly names any author on the page.

    Empty on either side means there is nothing to disagree about. Stores differ
    on initials, middle names and transliteration, so a shared surname plus a
    reasonable similarity is the test -- "J.R.R. Tolkien" and "John Ronald Reuel
    Tolkien" are one person.
    """
    keys = [fold(a) for a in wanted if fold(a)]
    targets = [fold(a) for a in found if fold(a)]
    if not keys or not targets:
        return True
    joined = " ".join(targets)
    for key in keys:
        if key in joined or (surname(key) and surname(key) in joined):
            return True
        for target in targets:
            if surname(key) and surname(key) == surname(target) \
                    and SequenceMatcher(None, key, target).ratio() >= 0.72:
                return True
    return False


def title_score(wanted: object, candidate: object, *, prefix_is_exact: bool = False,
                additive_ceiling: Optional[float] = None) -> float:
    """Score two titles, 0.0-1.0, with a sequel veto.

    Containment is deliberately **asymmetric**, and must not be collapsed to a
    symmetric ``max()``: a candidate that *drops* a subtitle is the same book
    ("Educated" for "Educated: A Memoir"), while one that *adds* words is probably
    its sequel ("Dune" -> "Dune Messiah"). A bare substring test is exactly how
    Dune Messiah once got filed under Dune's ISBN.

    :param prefix_is_exact: treat a candidate that is our title minus its subtitle
        as a perfect match. Audible and Kobo both truncate subtitles in listings.
    :param additive_ceiling: cap the score when the candidate adds words, so it can
        still win as the only candidate but never look confident.
    """
    want, cand = fold(wanted), fold(candidate)
    if not want or not cand:
        return 0.0
    if want == cand:
        return 1.0
    if is_sequel(want, cand):
        return 0.0
    score = max(SequenceMatcher(None, want, cand).ratio(),
                SequenceMatcher(None, main_part(wanted), main_part(candidate)).ratio())
    if cand in want or (len(cand) >= 6 and want.startswith(cand)):
        return 1.0 if prefix_is_exact else max(score, 0.85)
    if want in cand or (len(want) >= 6 and cand.startswith(want)):
        raised = max(score, 0.85)
        return min(raised, additive_ceiling) if additive_ceiling is not None else raised
    return score


def best(wanted: str, authors: Sequence[object],
         candidates: Iterable[Tuple[object, object, object]], *,
         floor: float = 0.85, require_author: bool = True,
         **scoring: object) -> List[Tuple[float, object]]:
    """Score ``(title, authors, payload)`` candidates and return the acceptable ones.

    Sorted best first. Ties break towards the *shorter* title, because listings
    omit subtitles -- so "Sapiens" the book and "Sapiens: A Graphic History" both
    arrive as "Sapiens", and the adaptations carry the longer titles.
    """
    scored: List[Tuple[float, int, int, object]] = []
    for index, (title, found, payload) in enumerate(candidates):
        if is_derivative(title, wanted):
            continue
        score = title_score(wanted, title, **scoring)  # type: ignore[arg-type]
        if score < floor:
            continue
        if require_author and authors and not authors_agree(authors, found or []):
            continue
        scored.append((-score, len(fold(title)), index, payload))
    return [(-negated, payload) for negated, _, _, payload in sorted(scored)]
