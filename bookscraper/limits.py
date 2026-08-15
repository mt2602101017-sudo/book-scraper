"""How hard each host may be hit, and what to do when one stops answering.

Rate-limit *policy*, kept apart from the request loop that enforces it, so adding a
host is a one-line data change rather than a code change.

The default courtesy delay suits storefronts that tolerate a request every second or
two. Some hosts do not, and they say so in different ways: Open Library answers HTTP
503 under sustained load, its cover service asks for a documented maximum rate, and
archive.org -- which is where a large Open Library cover actually comes from -- is
simply slow, because it extracts the image from a ZIP on demand.
"""

from __future__ import annotations

from typing import Dict, Tuple

#: ``host -> (min_delay, max_delay, timeout)``. An entry matches the host itself or
#: any subdomain of it, and the **longest** match wins, so
#: ``covers.openlibrary.org`` gets its own limit rather than ``openlibrary.org``'s.
HOST_LIMITS: Dict[str, Tuple[float, float, int]] = {
    # Two requests per book, and it 503s under load.
    "openlibrary.org": (2.5, 4.0, 30),
    # Asks for no more than ~100 cover requests per IP per 5 minutes: one per 3 s.
    "covers.openlibrary.org": (3.5, 5.0, 45),
    # Serves a -L cover out of a ZIP on demand: 7-14 s observed when it works, so it
    # needs a long timeout rather than a short retry.
    "archive.org": (3.5, 5.0, 60),
}

#: Consecutive transport failures before a host is given a cooling-off period.
#: Retrying a URL four times against a host that is refusing connections outright
#: spends the whole retry budget on every book; pausing the host lets it recover.
STRIKES_BEFORE_COOLDOWN = 3
#: Base cooling-off period, multiplied as failures continue and capped by
#: :data:`MAX_COOLDOWN_STEPS`, so one dead host cannot wedge a long run.
COOLDOWN_SECONDS = 30.0
MAX_COOLDOWN_STEPS = 4


def for_host(host: str, default: Tuple[float, float, int]) -> Tuple[float, float, int]:
    """This host's ``(min_delay, max_delay, timeout)``, or ``default``."""
    matches = [(len(known), limits) for known, limits in HOST_LIMITS.items()
               if host == known or host.endswith("." + known)]
    return max(matches)[1] if matches else default


def cooldown_for(strikes: int) -> float:
    """How long to pause a host that has failed ``strikes`` times in a row.

    Zero until the failures look like a pattern rather than bad luck.
    """
    if strikes < STRIKES_BEFORE_COOLDOWN:
        return 0.0
    steps = min(strikes - STRIKES_BEFORE_COOLDOWN + 1, MAX_COOLDOWN_STEPS)
    return COOLDOWN_SECONDS * steps
