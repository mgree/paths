"""This module colors terminal output, for the harness's and the runner's
reports.

Colors are 24-bit, written as ANSI escape sequences. Every function here
returns its text unchanged when enabled is false, so callers can format the
same way whether or not they color."""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass

RGB = tuple[int, int, int]

GREEN: RGB = (80, 200, 80)
YELLOW: RGB = (230, 200, 60)
RED: RGB = (230, 70, 70)


@dataclass(frozen=True)
class Style:
    """How to show text: in a color, and bold or dim."""

    rgb: RGB | None = None
    bold: bool = False
    dim: bool = False


def use_color() -> bool:
    """Return whether to color standard output. A non-empty NO_COLOR turns
    color off (https://no-color.org). Otherwise a non-empty FORCE_COLOR turns
    it on (https://force-color.org), and without either, the output is colored
    only on a terminal. NO_COLOR is checked first, as CPython does for its own
    colored output."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def paint(
    text: str, enabled: bool, rgb: RGB | None = None, *, bold: bool = False, dim: bool = False
) -> str:
    """Return text in the color rgb, bold or dim as asked, if enabled is true."""
    codes = (["1"] if bold else []) + (["2"] if dim else [])
    if rgb is not None:
        codes.append("38;2;{};{};{}".format(*rgb))
    if not enabled or not codes:
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def on_scale(t: float) -> RGB:
    """Return the color for t, from 0 to 1, on a scale from green at 0 through
    yellow at 0.5 to red at 1."""
    t = min(max(t, 0.0), 1.0)
    a, b, u = (GREEN, YELLOW, 2 * t) if t < 0.5 else (YELLOW, RED, 2 * t - 1)
    r, g, b_ = (round(x + (y - x) * u) for x, y in zip(a, b, strict=True))
    return (r, g, b_)


def styled(text: str, enabled: bool, style: Style) -> str:
    """Return text in style, if enabled is true."""
    return paint(text, enabled, style.rgb, bold=style.bold, dim=style.dim)


def fade(text: str, enabled: bool, value: float, lo: float, hi: float) -> str:
    """Return text colored by where value lies between lo and hi on a log
    scale, if enabled is true: green at lo, yellow halfway, red at hi. The
    scale is logarithmic, so equal ratios get equal steps in color."""
    t = 0.0 if hi <= lo else math.log(value / lo) / math.log(hi / lo)
    return paint(text, enabled, on_scale(t))
