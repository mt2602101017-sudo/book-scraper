"""The ``--verbose`` switch, and nothing else.

There is no logging framework here: progress and warnings are plain
``print(..., file=sys.stderr)`` calls at the point where something happens.

Two conventions the whole project follows:

* **stderr for progress, stdout for the report.** Only the summary renderers in
  ``pipeline.py`` and ``batch.py`` use a bare ``print()``. Everything else passes
  ``file=sys.stderr``, so ``main.py books.csv 2>/dev/null`` still gives you just
  the summary tables and piping the report to a file keeps it clean.

* **Detail is opt-in.** Roughly a hundred of those prints are diagnostics --
  per-request throttle notes, parser-fallback chatter, purge counts. On a
  10 000-ISBN run they would bury the lines that actually matter (a WAF challenge,
  a weak title match), so each sits behind ``if VERBOSE:`` and appears only with
  ``--verbose``.

``VERBOSE`` is read through this module rather than copied into each file
(``from .verbosity import VERBOSE`` binds a *value*, so a later assignment would
not be seen). Call sites therefore use ``if verbosity.VERBOSE:`` -- or the
:func:`verbose` helper, which reads the current value.
"""

from __future__ import annotations

__all__ = ["VERBOSE", "set_verbose", "verbose"]

#: True once ``--verbose`` is passed. Read it via :func:`verbose` from code that
#: imported this module early, so the current value is always seen.
VERBOSE = False


def set_verbose(enabled: bool) -> None:
    """Turn detailed output on or off. Called once, from ``main.py``."""
    global VERBOSE
    VERBOSE = bool(enabled)


def verbose() -> bool:
    """Current value of :data:`VERBOSE`."""
    return VERBOSE
