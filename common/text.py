"""This module helps the harness's and the runner's reports write sentences
about counts of things."""

from __future__ import annotations


def plural(n: int, one: str, many: str = "") -> str:
    """Return n with the noun for one of something, or for many. many defaults
    to one with an s added."""
    return f"{n} {one if n == 1 else many or one + 's'}"
