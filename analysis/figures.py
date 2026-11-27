"""This module draws the figures for one qualified benchmark session.

Each figure is one Vega-Lite spec, written with Altair, so that the paper and
the blog post draw the same chart: the paper includes its SVG, and the blog
embeds its spec. For each figure, the module writes <name>.svg, <name>.png
and <name>.vl.json to the output directory.

Times are per call, and all are read from the session's histograms, so each
is within a bucket of the true value. A median alone hides the long tails, so
the figures show whole distributions as quantile dots, or show P50 with P99
and the slowest call beside it.

The main figures show one strategy of each family: the -beneath strategy of
each pair that differs only in semantics. The supplementary figure in-root
shows that each -in-root strategy costs what its -beneath twin does.

    python -m analysis.figures results/sessions/bench-<time>.jsonl [--out DIR]
"""

from __future__ import annotations

import argparse
import html
import json
import math
import random
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import altair as alt
import vl_convert as vlc

from harness.qualify import bucket_width, cell_name, figures, merged, problems, quantile
from protocol.client import REPO

OUT = REPO / "build" / "figures"

# Each family's color, in the fixed order the categorical palette assigns
# them. The order is the one that passes the palette checks for lines, which
# a later family must not change: a family keeps its color in every figure.
FAMILIES = {
    "string-concat": "#257de0",
    "logical": "#f47230",
    "realpath": "#00b894",
    "walk": "#f2a600",
    "openat2": "#ee7fac",
    "openat2-walk": "#008a17",
}
INK = "#191919"
GRID = "#e7e7e7"
# The ink for a label that names an error: the palette's critical red, dark
# enough to read on the pale red background.
ERROR_INK = "#dc3c3d"
# About how wide one character of a 13-point bold title, and of an 11-point
# key's words, is, in pixels.
TITLE_CHAR_WIDTH = 7.5
KEY_SMALL_CHAR_WIDTH = 5.6
KEY_CHAR_WIDTH = 6.2
# The background behind results that are failures: the pale red the
# palette's surfaces allow, which a label always accompanies.
FAIL_BACKGROUND = "#fbebeb"
SECONDARY_INK = "#666666"


def family(strategy: str) -> tuple[str, str]:
    """Return a strategy's family and the semantics its name gives: the walk,
    openat2 and openat2-walk families come in -beneath and -in-root pairs."""
    for suffix in ("-beneath", "-in-root"):
        if strategy.endswith(suffix):
            return strategy.removesuffix(suffix), suffix[1:]
    return strategy, "beneath"


def rows(records: list[dict]) -> list[dict]:
    """Return a row for each target, cell and strategy in a session: its
    settings, its family, whether its calls open the file or fail the cell's
    fixture, and its P50, P99 and slowest call over all its runs."""
    header = records[0]
    cells = {cell_name(c): c for c in header["cells"]}
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in records:
        if r["kind"] == "run":
            groups[r["target"], cell_name(r["cell"]), r["strategy"]].append(r)
    out = []
    for (target, name, strategy), runs in groups.items():
        cell = cells[name]
        ok = strategy not in cell["fails"]
        hist = merged(runs, ok)
        (p50, _), (p99, _), (slowest, _) = figures(hist)
        fam, semantics = family(strategy)
        out.append(
            {
                "target": target,
                "cell": name,
                "depth": cell["depth"],
                "working_set": cell["working_set"],
                "names": cell["names"],
                "entries": cell["entries"],
                "sweeps": cell["sweeps"],
                "strategy": strategy,
                "family": fam,
                "semantics": semantics,
                "outcome": "opens the file" if ok else "fails",
                "p50": p50,
                "p99": p99,
                "max": slowest,
                # The arithmetic mean time per call, exact, from the driver's
                # own totals; and the geometric mean, from the histogram.
                "arith_mean": sum(r["time_ns"] for r in runs)
                / sum(o["count"] for r in runs for o in r["outcomes"]),
                "geomean": geomean(hist),
                "hist": hist,
            }
        )
    return out


def geomean(hist: list[list[int]]) -> float:
    """Return the geometric mean of the calls in hist, in nanoseconds. The
    histogram doesn't record each call's time, so, as the quantiles do, the
    calls in each bucket are taken to be spread evenly across it, from a to b.
    The mean of ln t over that spread is (b ln b - a ln a) / (b - a) - 1."""
    total = sum(count for _, count in hist)
    logs = 0.0
    for lower, count in hist:
        a, b = lower, lower + bucket_width(lower)
        # A bucket below 1 ns would start at 0, where ln is undefined; none
        # holds a real call, but the formula must not fail on one.
        mean_log = math.log(b) - 1 if a == 0 else (b * math.log(b) - a * math.log(a)) / (b - a) - 1
        logs += count * mean_log
    return math.exp(logs / total)


def values(picked: list[dict]) -> alt.Data:
    """Return picked rows as a chart's data, without their histograms, which
    only the CDF uses and which would make every spec far bigger."""
    return alt.Data(values=[{k: v for k, v in r.items() if k != "hist"} for r in picked])


# The order targets appear in: tmpfs first, as the one with no storage under it.
TARGETS = ["tmpfs", "ext4"]


def _family_color(legend: alt.Legend | None = None) -> alt.Color:
    """Return the color encoding for families, with legend, or with none. A
    legend should list only the families a chart shows (its values), but every
    family keeps its color."""
    return alt.Color(
        "family:N",
        scale=alt.Scale(domain=list(FAMILIES), range=list(FAMILIES.values())),
        legend=legend,
    )


def _boxed_legend(families: list[str], offset: int = 12) -> alt.Legend:
    """Return a legend for families inside a panel's top-left corner, offset
    pixels from its edges, on a white box with a hairline border, so the grid
    doesn't show through behind it."""
    return alt.Legend(
        title=None,
        values=families,
        orient="top-left",
        offset=offset,
        fillColor="#ffffff",
        strokeColor=GRID,
        padding=6,
        cornerRadius=2,
    )


def _shown(rows: list[dict]) -> list[str]:
    """Return the families in rows, in the order they appear."""
    return list(dict.fromkeys(r["family"] for r in rows))


def _log_domain(values: list[float]) -> list[float]:
    """Return a log scale's domain from a little below the lowest of values to
    a little above the highest, rather than out to whole powers of ten."""
    return [min(values) / 1.25, max(values) * 1.25]


def _log_ticks(domain: list[float]) -> list[float]:
    """Return 1, 2 and 5 times each power of ten inside domain, for a log
    axis's labels and gridlines."""
    ticks, power = [], 10.0 ** math.floor(math.log10(domain[0]))
    while power <= domain[1]:
        ticks += [m * power for m in (1, 2, 5) if domain[0] <= m * power <= domain[1]]
        power *= 10
    return ticks


def _log_scale(values: list[float]) -> tuple[alt.Scale, alt.Axis]:
    """Return a log scale fitted to values, and an axis with gridlines at 1, 2
    and 5 times each power of ten."""
    domain = _log_domain(values)
    return alt.Scale(type="log", domain=domain, nice=False), alt.Axis(values=_log_ticks(domain))


# A glyph in a key: given a function that places marks at (x, y) points, the
# glyph's left edge and its width, return the marks that draw it.
Glyph = Callable[[Callable[[list[tuple[float, float]]], alt.Chart], float, float], list[alt.Chart]]


def key_row(
    entries: list[tuple[Glyph, str]], columns: int | None = None
) -> alt.LayerChart | alt.FacetChart:
    """Return a key: each entry's glyph in gray, then its words. Entries run in
    one row, or, with columns, fill a grid of that many columns row by row,
    each column as wide as its longest entry. Entries are spaced by the length
    of their words, so short ones leave no wide gaps. Gray keeps the key from
    suggesting a family."""
    glyph, gap, row_height = 30, 18, 18
    columns = columns or len(entries)
    rows = math.ceil(len(entries) / columns)

    def entry_width(w: str) -> float:
        return glyph + 6 + KEY_CHAR_WIDTH * len(w)

    widths = [max(entry_width(w) for _, w in entries[c::columns]) for c in range(columns)]
    column_starts = [sum(widths[:c]) + gap * c for c in range(columns)]
    width = sum(widths) + gap * (columns - 1)
    height = row_height * rows
    x = alt.X("x:Q", scale=alt.Scale(domain=[0, width]), axis=None)
    y = alt.Y("y:Q", scale=alt.Scale(domain=[0, rows]), axis=None)

    def placer(row: int) -> Callable[[list[tuple[float, float]]], alt.Chart]:
        """Return a function that places marks within row, where a glyph's y
        runs from 0 at the row's bottom to 1 at its top."""
        bottom = rows - 1 - row

        def at(points: list[tuple[float, float]]) -> alt.Chart:
            values = [{"x": a, "y": bottom + b} for a, b in points]
            return alt.Chart(alt.Data(values=values)).encode(x=x, y=y)

        return at

    marks, words = [], []
    for i, (draw, w) in enumerate(entries):
        row, column = divmod(i, columns)
        start = column_starts[column]
        marks += draw(placer(row), start, glyph)
        words.append({"x": start + glyph + 6, "y": rows - 1 - row + 0.5, "text": w})
    text = (
        alt.Chart(alt.Data(values=words))
        .mark_text(align="left", baseline="middle", fontSize=11, color=SECONDARY_INK)
        .encode(x=x, y=y, text="text:N")
    )
    return alt.layer(*marks, text, width=width, height=height)


def key() -> alt.LayerChart | alt.FacetChart:
    """Return a key to the three marks that show a time along a time axis
    that runs left to right: a dot at P50, a line from P50 to P99, and a
    right-pointing triangle at the slowest call."""
    gray = SECONDARY_INK

    def p50(at: Callable, x: float, w: float) -> list[alt.Chart]:
        return [at([(x + w / 2, 0.5)]).mark_point(filled=True, size=60, color=gray)]

    def p99(at: Callable, x: float, w: float) -> list[alt.Chart]:
        return [at([(x, 0.5), (x + w, 0.5)]).mark_line(strokeWidth=1, color=gray)]

    def slowest(at: Callable, x: float, w: float) -> list[alt.Chart]:
        mark = at([(x + w / 2, 0.5)]).mark_point(
            shape="triangle-right", filled=False, size=50, color=gray
        )
        return [mark]

    return key_row([(p50, "P50"), (p99, "P50 to P99"), (slowest, "slowest call (PMAX)")])


def headed(
    title: str, legend: alt.LayerChart | alt.FacetChart, body: alt.LayerChart | alt.FacetChart
) -> alt.VConcatChart:
    """Return body below title, with legend to the title's right. A Vega-Lite
    title can't share its row with a chart, so the title is drawn as text, in
    the size and weight of a chart's title."""
    heading = (
        alt.Chart(alt.Data(values=[{}]))
        .mark_text(align="left", baseline="middle", fontSize=13, fontWeight="bold", color=INK)
        .encode(x=alt.value(0), y=alt.value(9), text=alt.value(title))
        .properties(width=TITLE_CHAR_WIDTH * len(title), height=18)
    )
    return alt.vconcat(alt.hconcat(heading, legend, spacing=40), body)


def _with_slowdowns(picked: list[dict]) -> list[dict]:
    """Return picked with each row's slowdown: its geometric mean over
    string-concat's, on the same target in the same cell, with a label such as
    "×2.4" for it; and its slowdown at P99, its P99 over string-concat's."""
    base = {
        (r["target"], r["cell"]): r["geomean"] for r in picked if r["family"] == "string-concat"
    }
    base_p99 = {
        (r["target"], r["cell"]): r["p99"] for r in picked if r["family"] == "string-concat"
    }
    out = []
    for r in picked:
        slowdown = r["geomean"] / base[r["target"], r["cell"]]
        label = "×1" if r["family"] == "string-concat" else f"×{slowdown:.1f}"
        p99 = r["p99"] / base_p99[r["target"], r["cell"]]
        out.append({**r, "slowdown": slowdown, "p99_slowdown": p99, "label": label})
    return out


# How many quantile dots each row of the dot figure has: each stands for an
# equal share of the calls.
DOTS = 200


def quantile_dots(
    picked: list[dict],
    level: Callable[[dict], str],
    levels: list[str],
    levels_title: str,
    title: str,
) -> alt.FacetChart:
    """Return a quantile dot plot for each family in each of picked's cells:
    DOTS dots at evenly spaced quantiles of its calls, so that they crowd
    where calls are common and thin out in the tail, as one dot per call
    would. Cells are panels, one row of panels per level (level names a row's
    cell, levels gives their order) and a column per target. Each family's row
    marks its geometric mean with a black tick, and gives its slowdown against
    string-concat at the row's right end. Every panel shares one log scale for
    time, so levels compare directly."""
    picked = _with_slowdowns(picked)
    # A fixed vertical jitter for each dot, the same in every row and every
    # drawing, so the figure doesn't change from one drawing to the next.
    jitter = random.Random(0)
    offsets = [jitter.uniform(-0.35, 0.35) for _ in range(DOTS)]
    dots = []
    for r in picked:
        for i, offset in enumerate(offsets):
            # Rounded, since thousands of dots go into the spec.
            dots.append(
                {
                    "kind": "dot",
                    "target": r["target"],
                    "level": level(r),
                    "family": r["family"],
                    "ns": round(quantile(r["hist"], (i + 0.5) / DOTS)),
                    "jitter": round(offset, 3),
                }
            )
    # The dots stop at the 99.75th percentile, so the scale fits them alone.
    scale, axis = _log_scale([d["ns"] for d in dots])
    right_edge = _log_domain([d["ns"] for d in dots])[1]
    y = alt.Y(
        "family:N",
        sort=list(FAMILIES),
        title=None,
        axis=alt.Axis(labelColor=INK),
        scale=alt.Scale(paddingInner=0.1),
    )
    x = alt.X("ns:Q", scale=scale, axis=axis, title="time per call (ns)")
    # One dataset holds the dots and each row's means, told apart by kind: a
    # faceted chart's layers must share their data.
    points = (
        alt.Chart()
        .transform_filter(alt.datum.kind == "dot")
        .mark_circle(size=14, opacity=0.35)
        .encode(
            x=x,
            y=y,
            yOffset=alt.YOffset("jitter:Q", scale=alt.Scale(domain=[-0.5, 0.5]), title=None),
            color=_family_color(),
        )
    )
    means = alt.Chart().transform_filter(alt.datum.kind == "mean")
    ticks = means.mark_tick(color=INK, thickness=2, size=16).encode(
        x=alt.X("geomean:Q", scale=scale),
        y=y,
        tooltip=[
            "strategy:N",
            "target:N",
            "level:N",
            alt.Tooltip("geomean:Q", format=",.0f", title="geometric mean (ns)"),
            alt.Tooltip("slowdown:Q", format=".2f"),
        ],
    )
    labels = means.mark_text(align="right", baseline="middle", fontSize=10, color=INK).encode(
        x=alt.datum(right_edge), y=y, text="label:N"
    )
    rows = [
        {
            "kind": "mean",
            "level": level(r),
            **{k: r[k] for k in ("target", "family", "strategy", "geomean", "slowdown", "label")},
        }
        for r in picked
    ]
    return (
        alt.layer(points, ticks, labels, data=alt.Data(values=dots + rows))
        .properties(width=420, height=150)
        .facet(
            row=alt.Row(
                "level:N",
                sort=levels,
                title=levels_title,
                header=alt.Header(labelColor=INK, labelAngle=0, labelAlign="left"),
            ),
            column=alt.Column("target:N", sort=TARGETS, title=None),
        )
        .properties(
            title=alt.Title(
                title,
                subtitle="every dot is 0.5% of the calls; black tick: geometric mean; at right: "
                "its slowdown against string-concat; openat2 and openat2-walk coincide",
                offset=12,
            )
        )
    )


def depth_dots(data: list[dict]) -> alt.FacetChart:
    """Return the depth sweep's quantile dots, a row of panels per depth."""
    picked = [
        r for r in data
        if "depth" in r["sweeps"] and r["names"] is None and r["semantics"] == "beneath"
    ]  # fmt: skip
    depths = [str(d) for d in sorted({r["depth"] for r in picked})]
    return quantile_dots(
        picked,
        lambda r: str(r["depth"]),
        depths,
        "directories on the path",
        "Time per call at each depth (working set half the L3)",
    )


def working_set(data: list[dict]) -> alt.FacetChart:
    """Return the working-set sweep's quantile dots, a row of panels per
    working set, smallest first."""
    picked = [r for r in data if "working-set" in r["sweeps"] and r["semantics"] == "beneath"]

    def level(r: dict) -> str:
        return f"{r['working_set']}: {r['entries']:,} entries"

    levels = [level(r) for r in sorted(picked, key=lambda r: r["entries"])]
    return quantile_dots(
        picked,
        level,
        list(dict.fromkeys(levels)),
        "working set",
        "Time per call at each working set (depth 16)",
    )


# A glyph in an inset key: the mark to draw ("point" or "tick") and its
# options, such as filled, shape and size.
InsetGlyph = tuple[str, dict]


def inset_key(
    rows: list[tuple[str | None, list[tuple[InsetGlyph, str]]]],
    right: float,
    bottom: float,
) -> list[alt.Chart]:
    """Return the marks of a key drawn inside a plot, in a white box with a
    hairline border whose bottom-right corner is at (right, bottom), in
    pixels from the plot's top-left. Each row has an optional title, then
    glyphs, each followed by its words. Everything is placed in pixels, so the
    key doesn't touch the plot's scales."""
    pad, gap, font = 8, 12, 10

    def diameter(g: InsetGlyph) -> float:
        mark, opts = g
        return 16 if mark == "tick" else 2 * math.sqrt(opts.get("size", 60) / math.pi)

    def item_width(g: InsetGlyph, words: str) -> float:
        return max(14, diameter(g)) + 4 + KEY_SMALL_CHAR_WIDTH * len(words)

    heights = [max(16, *(diameter(g) + 4 for g, _ in items)) for _, items in rows]
    # The entries of rows without a title line up in columns, each as wide as
    # its widest entry.
    untitled = [items for title, items in rows if title is None]
    columns = max((len(items) for items in untitled), default=0)
    column_widths = [
        max(item_width(*items[i]) for items in untitled if i < len(items)) for i in range(columns)
    ]
    widths = []
    for title, items in rows:
        if title is None:
            widths.append(sum(column_widths[: len(items)]) + gap * (len(items) - 1))
        else:
            widths.append(
                KEY_SMALL_CHAR_WIDTH * len(title)
                + 6
                + sum(item_width(*i) + gap for i in items)
                - gap
            )
    box_w, box_h = max(widths) + 2 * pad, sum(heights) + 2 * pad
    left, top = right - box_w, bottom - box_h

    def at(x: float, y: float) -> alt.Chart:
        return alt.Chart(alt.Data(values=[{}])).encode(x=alt.value(x), y=alt.value(y))

    box = (
        alt.Chart(alt.Data(values=[{}]))
        .mark_rect(fill="#ffffff", stroke=GRID, cornerRadius=2)
        .encode(x=alt.value(left), x2=alt.value(right), y=alt.value(top), y2=alt.value(bottom))
    )
    marks: list[alt.Chart] = [box]
    y = top + pad
    for (title, items), h in zip(rows, heights, strict=True):
        middle = y + h / 2
        x = left + pad
        if title:
            marks.append(
                at(x, middle).mark_text(
                    text=title, align="left", baseline="middle", fontSize=font, color=SECONDARY_INK
                )
            )
            x += KEY_SMALL_CHAR_WIDTH * len(title) + 6
        for i, ((mark, opts), words) in enumerate(items):
            start = x
            w = max(14, diameter((mark, opts)))
            if mark == "tick":
                marks.append(at(x + w / 2, middle).mark_tick(orient="vertical", size=14, **opts))
            else:
                marks.append(at(x + w / 2, middle).mark_point(**opts))
            marks.append(
                at(x + w + 4, middle).mark_text(
                    text=words, align="left", baseline="middle", fontSize=font, color=SECONDARY_INK
                )
            )
            step = column_widths[i] if title is None else item_width((mark, opts), words)
            x = start + step + gap
        y += h
    return marks


# The width of the slowdown figure's plot, in pixels.
SLOWDOWNS_HEIGHT = 300
# The area of the dots for each depth, smallest at the shallowest.
DEPTH_SIZES = (8, 30, 90, 220)
SLOWDOWNS_WIDTH = 560


def slowdowns(data: list[dict]) -> alt.LayerChart | alt.FacetChart:
    """Return each family's slowdown against string-concat at every depth on
    every target, one dot each: too few values for a box or a violin, so
    every one is shown. Filled dots are slowdowns in the geometric mean of
    the calls, and hollow ones slowdowns at P99. Ticks mark each row's
    geometric mean of each kind, the summary for ratios, whose answer doesn't
    depend on which strategy is ×1."""
    picked = [
        r for r in _with_slowdowns(
            [
                r for r in data
                if "depth" in r["sweeps"] and r["names"] is None and r["semantics"] == "beneath"
            ]
        )
        if r["family"] != "string-concat"
    ]  # fmt: skip
    families = [f for f in FAMILIES if f != "string-concat"]
    depths = sorted({r["depth"] for r in picked})

    def gmean(values: list[float]) -> float:
        return math.exp(sum(math.log(v) for v in values) / len(values))

    means = []
    for f in families:
        mine = [r for r in picked if r["family"] == f]
        g, g99 = gmean([r["slowdown"] for r in mine]), gmean([r["p99_slowdown"] for r in mine])
        means.append(
            {
                "family": f,
                "slowdown": g,
                "p99_slowdown": g99,
                "label": f"×{g:.1f}",
                "p99_label": f"×{g99:.1f}",
            }
        )
    domain = _log_domain([1.0] + [r[k] for r in picked for k in ("slowdown", "p99_slowdown")])
    scale = alt.Scale(type="log", domain=domain, nice=False)
    ticks_at = [t for t in (1, 1.5, 2, 3, 5, 8, 12) if domain[0] <= t <= domain[1]]
    x_axis = alt.Axis(values=ticks_at, format="~g", labelExpr="'×' + datum.label")
    y = alt.Y("family:N", sort=families, title=None, axis=alt.Axis(labelColor=INK))
    shape = alt.Shape(
        "target:N",
        scale=alt.Scale(domain=TARGETS, range=["circle", "square"]),
        legend=None,
    )
    # Each dot's area comes from DEPTH_SIZES, drawn as is. A size scale
    # would be a point scale here, which spreads the depths between the first
    # two areas and ignores the rest, so the dots wouldn't match the key.
    area = dict(zip(depths, DEPTH_SIZES, strict=True))
    for r in picked:
        r["dot_size"] = area[r["depth"]]
    size = alt.Size("dot_size:Q", scale=None, legend=None)
    tooltip = [
        "strategy:N",
        "target:N",
        "depth:Q",
        alt.Tooltip("slowdown:Q", format=".2f", title="slowdown in geometric mean"),
        alt.Tooltip("p99_slowdown:Q", format=".2f", title="slowdown at P99"),
    ]
    cells = alt.Chart(values(picked))
    filled = cells.mark_point(filled=True, opacity=0.7).encode(
        x=alt.X(
            "slowdown:Q",
            scale=scale,
            axis=x_axis,
            title="time per call as a multiple of string-concat's",
        ),
        y=y,
        color=_family_color(),
        shape=shape,
        size=size,
        tooltip=tooltip,
    )
    hollow = cells.mark_point(filled=False, opacity=0.8, strokeWidth=1.5).encode(
        x=alt.X("p99_slowdown:Q", scale=scale),
        y=y,
        color=_family_color(),
        shape=shape,
        size=size,
        tooltip=tooltip,
    )
    summary = alt.Chart(alt.Data(values=means))
    tick = summary.mark_tick(color=INK, thickness=2, size=22).encode(
        x=alt.X("slowdown:Q", scale=scale), y=y
    )
    hollow_tick = summary.mark_tick(
        color=INK, filled=False, thickness=4, size=22, strokeWidth=1.2
    ).encode(x=alt.X("p99_slowdown:Q", scale=scale), y=y)
    # Each row's geometric mean is written above its filled tick, and its
    # geometric mean at P99 below its hollow one, so the two stay apart even
    # where the ticks almost meet.
    labels = summary.mark_text(baseline="bottom", dy=-12, fontSize=9, color=INK).encode(
        x=alt.X("slowdown:Q", scale=scale), y=y, text="label:N"
    )
    p99_labels = summary.mark_text(baseline="top", dy=12, fontSize=9, color=INK).encode(
        x=alt.X("p99_slowdown:Q", scale=scale), y=y, text="p99_label:N"
    )
    one = alt.Chart(alt.Data(values=[{"slowdown": 1.0}]))
    rule = one.mark_rule(color="#c2c2c2", strokeWidth=1).encode(x=alt.X("slowdown:Q", scale=scale))
    # The rule is string-concat's own row, at ×1, so it's labeled with its
    # name, just above the plot.
    rule_label = one.mark_text(baseline="bottom", dy=-4, fontSize=10, color=SECONDARY_INK).encode(
        x=alt.X("slowdown:Q", scale=scale), y=alt.value(0), text=alt.value("string-concat")
    )
    # The key goes inside the plot, in its bottom-right corner, which is empty:
    # the bottom rows' dots end well short of it, and the walk's row is above.
    gray = SECONDARY_INK
    inset = inset_key(
        [
            (None, [(("point", {"filled": True, "size": 60, "color": gray}),
                      "typical slowdown (at geomean)"),
                     (("point", {"filled": False, "size": 60, "color": gray, "strokeWidth": 1.5}),
                      "worst-case slowdown (at P99)")]),
            (None, [(("tick", {"thickness": 2, "color": INK}), "row mean, typical"),
                     (("tick", {"filled": False, "thickness": 4, "strokeWidth": 1.2, "color": INK}),
                      "row mean, P99")]),
            ("depth", [(("point", {"filled": True, "size": a, "color": gray}), str(d))
                       for a, d in zip(DEPTH_SIZES, depths, strict=True)]),
            ("filesystem", [(("point", {"filled": True, "size": 60, "color": gray, "shape": sh}), t)
                            for sh, t in (("circle", "tmpfs"), ("square", "ext4"))]),
        ],
        right=SLOWDOWNS_WIDTH - 6,
        bottom=SLOWDOWNS_HEIGHT - 6,
    )  # fmt: skip
    # Tall enough that a row's label below and the next row's above don't meet.
    return alt.layer(
        rule, rule_label, hollow, filled, hollow_tick, tick, labels, p99_labels, *inset
    ).properties(
        width=SLOWDOWNS_WIDTH,
        height=SLOWDOWNS_HEIGHT,
        title=alt.Title(
            "Slowdown against string-concat at every depth and on both filesystems",
            subtitle="dots: one depth on one filesystem, against string-concat's same call; "
            "ticks: geometric mean of a row's eight dots",
            anchor="start",
            offset=20,
        ),
    )


def past_path_max(data: list[dict]) -> alt.VConcatChart:
    """Return the times past PATH_MAX, one row per family: first those that
    open the file, then, on a pale red band, those that fail with
    ENAMETOOLONG, whose times are times to fail."""
    picked = [r for r in data if r["names"] is not None and r["semantics"] == "beneath"]
    picked.sort(key=lambda r: (r["outcome"] != "opens the file", -r["p50"]))
    scale, axis = _log_scale([r[k] for r in picked for k in ("p50", "p99", "max")])
    lo, hi = _log_domain([r[k] for r in picked for k in ("p50", "p99", "max")])
    # No padding between rows, so the failing rows' backgrounds join into one
    # band.
    y = alt.Y(
        "family:N",
        sort=list(dict.fromkeys(r["family"] for r in picked)),
        scale=alt.Scale(paddingInner=0),
        title=None,
    )
    x = alt.X("p50:Q", scale=scale, axis=axis, title="time per call (ns)")
    base = alt.Chart().encode(y=y)
    fails = alt.datum.outcome == "fails"
    band = (
        base.transform_filter(fails)
        .mark_rect(color=FAIL_BACKGROUND)
        .encode(x=alt.datum(lo), x2=alt.datum(hi))
    )
    # The band's label goes in its bottom-right corner, on the bottom row, the
    # fastest failing family, whose marks end well short of the right edge. So
    # the color isn't the only thing that says these calls fail.
    fastest_failure = min((r for r in picked if r["outcome"] == "fails"), key=lambda r: r["p50"])
    label = (
        base.transform_filter(alt.datum.family == fastest_failure["family"])
        .transform_aggregate(rows="count()", groupby=["family"])
        .mark_text(align="right", baseline="bottom", dx=-4, dy=9, fontSize=10, fontWeight="bold")
        .encode(x=alt.datum(hi), text=alt.value("ENAMETOOLONG"), color=alt.value(ERROR_INK))
    )
    colored = base.encode(color=_family_color())
    whisker = colored.mark_rule(strokeWidth=1).encode(x=x, x2="p99:Q")
    p50 = colored.mark_point(filled=True, size=60, opacity=1).encode(
        x=x, tooltip=["strategy:N", "target:N", "outcome:N", "p50:Q", "p99:Q", "max:Q"]
    )
    slowest = colored.mark_point(shape="triangle-right", filled=False, size=50).encode(
        x=alt.X("max:Q", scale=scale)
    )
    panels = alt.layer(band, label, whisker, p50, slowest, data=values(picked)).facet(
        column=alt.Column("target:N", sort=TARGETS, title=None)
    )
    return headed("Past PATH_MAX (paths of 5,024 bytes)", key(), panels)


def side_by_side(
    panels: list[tuple[str, alt.LayerChart | alt.FacetChart]], title: alt.Title | str
) -> alt.HConcatChart:
    """Return panels side by side, each headed by its name like a facet's
    header. Unlike facets, each panel owns its legend, so a legend can sit
    inside one panel, where a faceted chart would put it outside them all."""
    headed_panels = [
        chart.properties(
            title=alt.Title(name, fontSize=11, fontWeight="normal", anchor="middle", color=INK)
        )
        for name, chart in panels
    ]
    return alt.hconcat(*headed_panels).resolve_legend(color="independent").properties(title=title)


def in_root(data: list[dict]) -> alt.HConcatChart:
    """Return a quantile-quantile plot of each -in-root strategy against its
    -beneath twin, in every cell: for each percentile from the 1st to the
    99th, the -beneath strategy's time at that percentile against the -in-root
    strategy's. Two strategies' buckets can't be paired call by call, but
    their quantiles can. Points on the diagonal mean the two distributions
    match, tails included, not just their medians."""
    by_key = {(r["target"], r["cell"], r["family"], r["semantics"]): r for r in data}
    points = []
    for (t, c, f, s), r in by_key.items():
        twin = by_key.get((t, c, f, "in-root"))
        if s != "beneath" or twin is None:
            continue
        for pct in range(1, 100):
            points.append(
                {
                    "target": t,
                    "cell": c,
                    "family": f,
                    "percentile": pct,
                    "beneath": round(quantile(r["hist"], pct / 100)),
                    "in_root": round(quantile(twin["hist"], pct / 100)),
                }
            )
    scale, axis = _log_scale([p[k] for p in points for k in ("beneath", "in_root")])

    def panel(target: str, with_legend: bool) -> alt.LayerChart | alt.FacetChart:
        base = alt.Chart(alt.Data(values=[p for p in points if p["target"] == target]))
        legend = _boxed_legend(_shown(points))
        dots = base.mark_circle(size=16, opacity=0.35).encode(
            x=alt.X("beneath:Q", scale=scale, axis=axis, title="-beneath time per call (ns)"),
            # Only the first panel's axis is titled; the second shares it.
            y=alt.Y(
                "in_root:Q",
                scale=scale,
                axis=axis,
                title="-in-root time per call (ns)" if with_legend else None,
            ),
            color=_family_color(legend if with_legend else None),
            tooltip=["family:N", "target:N", "cell:N", "percentile:Q", "beneath:Q", "in_root:Q"],
        )
        # The diagonal, drawn through each point's x at y = x.
        diagonal = base.mark_line(color=SECONDARY_INK, strokeWidth=1).encode(
            x=alt.X("beneath:Q", scale=scale), y=alt.Y("beneath:Q", scale=scale)
        )
        return alt.layer(diagonal, dots)

    # The legend goes in the first panel's top-left corner, which is empty
    # because the points lie along the diagonal.
    targets = [t for t in TARGETS if any(p["target"] == t for p in points)]
    return side_by_side(
        [(t, panel(t, i == 0)) for i, t in enumerate(targets)],
        alt.Title(
            "Each -in-root strategy against its -beneath twin, quantile by quantile",
            subtitle="one dot per percentile, 1st to 99th, in every cell",
        ),
    )


# The cell the CDF shows: the baseline, depth 16 at half the L3.
BASELINE = {"depth": 16, "working_set": "half-L3", "names": None}


def cdf(data: list[dict]) -> alt.HConcatChart:
    """Return, for each family in the baseline cell, the share of calls that
    took each time or less. Each bucket's top edge gets the share of calls up
    to and including that bucket, joined by straight lines: the calls in a
    bucket are taken to be spread evenly across it, as the quantiles do."""
    picked = [
        r for r in data
        if all(r[k] == v for k, v in BASELINE.items()) and r["semantics"] == "beneath"
    ]  # fmt: skip
    points = []
    for r in picked:
        total = sum(count for _, count in r["hist"])
        seen = 0
        common = {"family": r["family"], "target": r["target"], "strategy": r["strategy"]}
        points.append({**common, "ns": r["hist"][0][0], "share": 0.0})
        for lower, count in r["hist"]:
            seen += count
            points.append({**common, "ns": lower + bucket_width(lower), "share": seen / total})
    scale, axis = _log_scale([p["ns"] for p in points])
    right_edge = _log_domain([p["ns"] for p in points])[1]
    targets = [t for t in TARGETS if any(p["target"] == t for p in points)]

    def panel(target: str, with_legend: bool) -> alt.LayerChart | alt.FacetChart:
        base = alt.Chart().encode(
            x=alt.X("ns:Q", scale=scale, axis=axis, title="time per call (ns)")
        )
        # Close to the axis, to leave the curves as much room as it can.
        legend = _boxed_legend(_shown(picked), offset=4)
        lines = base.mark_line(strokeWidth=2).encode(
            y=alt.Y(
                "share:Q",
                scale=alt.Scale(domain=[0, 1]),
                axis=alt.Axis(format="%", values=[0, 0.25, 0.5, 0.75, 1]),
                # Only the first panel's axis is titled; the second shares it.
                title="calls taking this long or less" if target == targets[0] else None,
            ),
            color=_family_color(legend if with_legend else None),
            detail="strategy:N",
            tooltip=["strategy:N", "target:N", "ns:Q", alt.Tooltip("share:Q", format=".1%")],
        )
        # Faint rules at the median and P99 tie the curves to the other
        # figures. Each rule is labeled at its right end, rather than on the
        # axis, where a label at 99% would sit on top of the one at 100%. The
        # panel's data would draw a rule and a label for every one of its rows,
        # so each is drawn from those rows counted into one.
        rules = []
        once = alt.Chart().transform_aggregate(rows="count()")
        for share, name in ((0.5, "P50"), (0.99, "P99")):
            rules.append(once.mark_rule(color="#c2c2c2", strokeWidth=1).encode(y=alt.datum(share)))
            rules.append(
                once.mark_text(align="right", baseline="bottom", dx=-2, dy=-2, fontSize=10).encode(
                    x=alt.datum(right_edge),
                    y=alt.datum(share),
                    text=alt.datum(name),
                    color=alt.value(SECONDARY_INK),
                )
            )
        mine = [p for p in points if p["target"] == target]
        return alt.layer(*rules, lines, data=alt.Data(values=mine))

    # The legend goes in the first panel's top-left corner, which is empty:
    # above about 80%, every curve is already well to the right.
    return side_by_side(
        [(t, panel(t, i == 0)) for i, t in enumerate(targets)],
        alt.Title(
            "Share of calls taking each time or less (depth 16, working set half the L3)",
            subtitle="openat2 and openat2-walk coincide",
        ),
    )


# The figures in the order the preview shows them: the headline results
# first, then the sweeps, then the supplementary in-root check.
FIGURES = {
    "slowdowns": slowdowns,
    "cdf": cdf,
    "depth-dots": depth_dots,
    "working-set": working_set,
    "past-path-max": past_path_max,
    "in-root": in_root,
}


def write(chart: alt.TopLevelMixin, out: Path, name: str) -> None:
    """Write chart as name.svg, name.png and name.vl.json in out."""
    spec = chart.to_json()
    (out / f"{name}.vl.json").write_text(spec)
    (out / f"{name}.svg").write_text(vlc.vegalite_to_svg(spec))
    (out / f"{name}.png").write_bytes(vlc.vegalite_to_png(spec, scale=2))


def preview_page(session: str, names: list[str]) -> str:
    """Return a page that shows every figure's PNG, in order, each under its
    name, for looking at the figures together."""
    figures_html = "\n".join(
        f'<section id="{n}"><h2><a href="#{n}">{n}</a></h2><img src="{n}.png" alt="{n}"></section>'
        for n in names
    )
    contents = " · ".join(f'<a href="#{n}">{n}</a>' for n in names)
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>Figures: {html.escape(session)}</title>
<style>
  body {{ margin: 0 auto; padding: 1rem; max-width: 100rem; background: #fff; color: {INK};
         font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
  h1 {{ font-size: 1.1rem; }}
  h2 {{ font-size: 1rem; margin: 2rem 0 0.5rem; }}
  a {{ color: inherit; }}
  img {{ max-width: 100%; height: auto; border: 1px solid #e7e7e7; }}
</style>
<h1>Figures from {html.escape(session)}</h1>
<nav>{contents}</nav>
{figures_html}
</html>
"""


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Draw the figures for a qualified benchmark session.")
    p.add_argument("session", type=Path)
    p.add_argument("--out", type=Path, default=OUT, help=f"where to write them (default {OUT})")
    args = p.parse_args(argv)

    records = [json.loads(line) for line in args.session.read_text().splitlines()]
    found = problems(records)
    if found:
        raise SystemExit(
            f"{args.session} doesn't qualify, so it has no figures:\n"
            + "\n".join(f"  {why}" for why in found)
        )
    data = rows(records)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, draw in FIGURES.items():
        chart = (
            draw(data)
            .configure_view(stroke=None)
            .configure_axis(
                gridColor=GRID,
                domainColor="#c2c2c2",
                labelColor=SECONDARY_INK,
                titleColor=SECONDARY_INK,
            )
        )
        write(chart, args.out, name)
        print(args.out / f"{name}.svg")
    preview = args.out / "preview.html"
    preview.write_text(preview_page(args.session.name, list(FIGURES)))
    print(preview)


if __name__ == "__main__":
    main()
