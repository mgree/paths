"""This module decides whether one benchmark session can be used, and, if it
can, describes its times.

A session is one invocation of harness.bench, and it writes one results
file. A run is one driver process within it: a warm-up, then a measured
stretch. A session makes several runs of each cell (a strategy on a target),
each a fresh process on the same tree. This module looks at one session on
its own. It doesn't compare sessions with each other.

A session qualifies when all of these hold:

  - It finished, so the file ends with an end record.
  - It ran in bench mode, so the header says it was pinned.
  - Every run each strategy was meant to make is in the file and didn't
    fail.
  - Every timed call reached the file.
  - Every run recorded the frequency the firmware guaranteed the pinned CPU,
    and the whole session ran at one such frequency. Comparing strategies
    only makes sense at one speed, and a session split between two speeds
    would make the change of speed look like a difference between
    strategies.
  - The hardware didn't throttle the pinned CPU's core or package during any
    run, and every run recorded the throttle counters.

If the session doesn't qualify, this module prints every reason and nothing
else, and exits with status 1. If it qualifies, it prints the frequency the
session ran at and a line for each cell, and exits with status 0. A cell's
line describes all of its runs' calls together: their median, their 99th
percentile, and the slowest call. Beside each figure is how far apart the
runs are on it: the standard deviation of the runs' own figures. With --runs
it also prints each run's own figures.

The figures are read from histograms, which only say which bucket each call
fell in. The protocol fixes the buckets, so the runs' histograms add up to
the cell's. The median and 99th percentile assume the calls in their bucket
are spread evenly across it. The slowest call is shown as the top of its
bucket, so the true slowest time is at most the figure shown. A bucket is at
most 1/16 as wide as its lower bound, so each figure is within about 6% of
the true one.

So the runs' figures can't be told apart more finely than their buckets. If
their standard deviation is less than half the width of the widest bucket
they fall in, the histograms can't distinguish it from zero, and the module
prints that half-width as a bound instead: "±<512" means the runs agree to
within the buckets, which are 1024 ns wide there."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from common.color import RED, fade, paint, use_color

THROTTLE_MS = ("core_throttle_ms", "package_throttle_ms")


def bucket_width(lower: int) -> int:
    """Return the width of the protocol's histogram bucket that starts at
    lower: 1 ns below 16 ns, and 1/16 of the power of two below it above."""
    if lower < 16:
        return 1
    return 1 << (lower.bit_length() - 1 - 4)


def quantile(hist: list[list[int]], q: float) -> float:
    """Return quantile q of the calls in hist, in nanoseconds, assuming the
    calls in each bucket are spread evenly across it."""
    n = sum(count for _, count in hist)
    rank = q * (n - 1)
    seen = 0
    for lower, count in hist:
        if rank < seen + count:
            return lower + bucket_width(lower) * (rank - seen + 0.5) / count
        seen += count
    raise ValueError("the histogram is empty")


def base_khz(run: dict) -> int | None:
    """Return the guaranteed frequency a run ran at, in kHz, or None if the
    file doesn't record it before and after the run, or it changed between
    the two readings."""
    before, after = run.get("before") or {}, run.get("after") or {}
    khz = before.get("base_khz")
    return khz if khz is not None and khz == after.get("base_khz") else None


def throttled_ms(run: dict) -> int | None:
    """Return how many milliseconds the hardware throttled the pinned CPU's
    core or package while the run went on, or None if the file doesn't record
    the counters before and after the run."""
    before, after = run.get("before") or {}, run.get("after") or {}
    total = 0
    for k in THROTTLE_MS:
        b, a = before.get(k), after.get(k)
        if b is None or a is None:
            return None
        total += a - b
    return total


def problems(records: list[dict]) -> list[str]:
    """Return every reason the session can't be used. The session qualifies
    if there are none."""
    header = records[0]
    runs = [r for r in records if r["kind"] == "run"]
    found = []

    if records[-1]["kind"] != "end":
        found.append("the session didn't finish: the file has no end record")
    if not header["machine"]["pinned"]:
        found.append("the session ran unpinned, outside bench mode")

    options = header["options"]
    made = defaultdict(set)
    for r in runs:
        made[r["target"], r["strategy"]].add(r["run"])
    for target in options["targets"]:
        for strategy in options["strategies"]:
            missing = set(range(1, options["runs"] + 1)) - made[target, strategy]
            if missing:
                numbers = ", ".join(map(str, sorted(missing)))
                found.append(
                    f"{target} {strategy}: run {numbers} is missing"
                    if len(missing) == 1
                    else f"{target} {strategy}: runs {numbers} are missing"
                )

    frequencies = set()
    for r in runs:
        where = f"{r['target']} run {r['run']} {r['strategy']}"
        if r["failure"]:
            found.append(f"{where}: failed: {r['failure']}")
            continue
        errors = [o for o in r["outcomes"] if not o["ok"]]
        if errors:
            found.append(
                f"{where}: {sum(o['count'] for o in errors)} calls failed "
                f"({', '.join(o['label'] for o in errors)})"
            )
        khz = base_khz(r)
        if khz is None:
            found.append(f"{where}: the guaranteed frequency isn't recorded")
        else:
            frequencies.add(khz)
        ms = throttled_ms(r)
        if ms is None:
            found.append(f"{where}: the throttle counters aren't recorded")
        elif ms:
            found.append(f"{where}: throttled for {ms} ms")
    if len(frequencies) > 1:
        mhz = ", ".join(f"{k // 1000}" for k in sorted(frequencies))
        found.append(f"the session ran at more than one guaranteed frequency: {mhz} MHz")
    return found


def slowest_ns(hist: list[list[int]]) -> int:
    """Return the top of the slowest call's bucket. The slowest call took at
    most that long."""
    lower = hist[-1][0]
    return lower + bucket_width(lower)


def merged(runs: list[dict]) -> list[list[int]]:
    """Return one histogram of all the calls in runs."""
    counts: dict[int, int] = defaultdict(int)
    for r in runs:
        for lower, count in r["hist"]:
            counts[lower] += count
    return [[lower, counts[lower]] for lower in sorted(counts)]


def figures(hist: list[list[int]]) -> list[tuple[float, int]]:
    """Return a histogram's median, 99th percentile and slowest call, each with
    the width of the bucket it comes from."""
    p50, p99 = quantile(hist, 0.5), quantile(hist, 0.99)
    return [
        (p50, bucket_width(int(p50))),
        (p99, bucket_width(int(p99))),
        (slowest_ns(hist), bucket_width(hist[-1][0])),
    ]


def spread(values: list[float], widths: list[int]) -> str:
    """Return the standard deviation of the runs' values, or, if it is less
    than half the widest of their buckets, that half-width as a bound."""
    if len(values) < 2:
        return "±-"
    half = max(widths) / 2
    sd = statistics.stdev(values)
    return f"±<{half:.0f}" if sd < half else f"±{sd:.0f}"


def describe(runs: list[dict], each_run: bool, colored: bool) -> None:
    """Print a line for each cell: the median, 99th percentile and slowest of
    all its calls, each beside how far apart its runs are on it. Targets are
    printed apart, since their times aren't comparable. If colored is true,
    color each figure from green to red by how it compares with the same
    column's figures on the same target. If each_run is true, follow each
    cell's line with its runs' own figures."""
    cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in runs:
        cells[r["target"], r["strategy"]].append(r)
    totals = {cell: figures(merged(group)) for cell, group in cells.items()}
    heads = " ".join(f"{h:>6} {'':6}" for h in ("p50 ns", "p99 ns", "max ns"))
    head = f"{'target':6} {'strategy':22} {heads}".rstrip()
    print(paint(head, colored, bold=True))
    targets = list(dict.fromkeys(target for target, _ in cells))
    for n, target in enumerate(targets):
        if n:
            print()
        mine = [cell for cell in cells if cell[0] == target]
        bounds = [
            (min(totals[c][i][0] for c in mine), max(totals[c][i][0] for c in mine))
            for i in range(3)
        ]
        for cell in mine:
            each = [figures(r["hist"]) for r in cells[cell]]
            shown = []
            for i, (value, _) in enumerate(totals[cell]):
                number = fade(f"{value:6.0f}", colored, value, *bounds[i])
                sd = spread([e[i][0] for e in each], [e[i][1] for e in each])
                shown.append(f"{number} {sd:<6}")
            print(f"{target:6} {cell[1]:22} {' '.join(shown)}".rstrip())
            if each_run:
                for r, e in zip(cells[cell], each, strict=True):
                    values = " ".join(f"{value:6.0f} {'':6}" for value, _ in e).rstrip()
                    print(
                        f"{'':6}   run {r['run']:<16} {values}"
                        + (f"  after {r['discarded']} discarded" if r["discarded"] else "")
                    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Decide whether a benchmark session can be used.")
    p.add_argument("--runs", action="store_true", help="also show each run's figures")
    p.add_argument("results", type=Path)
    args = p.parse_args(argv)

    records = [json.loads(line) for line in args.results.read_text().splitlines()]
    found = problems(records)
    if found:
        print(args.results.name)
        print("This session doesn't qualify:")
        colored = use_color()
        for why in found:
            print(f"  {paint(why, colored, RED, bold=True)}")
        sys.exit(1)

    # The session qualified, so every run succeeded at the same recorded
    # frequency.
    header = records[0]
    runs = [r for r in records if r["kind"] == "run"]
    mhz = (base_khz(runs[0]) or 0) // 1000
    cells = len({(r["target"], r["strategy"]) for r in runs})
    discarded = sum(r["discarded"] for r in runs)
    print(args.results.name)
    print(
        f"This session qualifies: {mhz} MHz, {header['options']['runs']} runs "
        f"of {cells} cells, started {header['started']}"
        + (f", {discarded} tries discarded" if discarded else "")
    )
    print()
    describe(runs, args.runs, use_color())


if __name__ == "__main__":
    main()
