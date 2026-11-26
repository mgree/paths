"""This module times every strategy on the cells of the benchmark's sweeps, and
writes the results to a JSONL file. One invocation is a session, and writes
one file. sweeps.toml lists the sweeps: each changes one setting of a baseline
cell, the depth of the requests or their working set. A session makes several
runs of each strategy on each cell and target, and each run is one driver
process.

Run it inside bench mode, so that it and every driver it starts are in the
cgroup paths-bench:

    sudo tools/benchmode on
    sudo tools/benchmode run -- .venv/bin/python -m harness.bench
    sudo tools/benchmode off

Outside bench mode it refuses to run, unless it is given --unpinned, which
marks the results as unpinned.

Each run is a fresh driver process in the protocol's time mode. The driver
goes through the tree's list of requests in passes, shuffled afresh each pass
from the run's seed: one pass that isn't measured, then enough measured passes
to make at least --calls calls, each timed into its outcome's histogram. Run
n of every strategy uses seed n, so the same run of different strategies sees
the same sequence of requests. The runs happen one at a time, so no two
drivers compete for a core, its caches, or the directories they all walk. The
harness pins itself, and so every driver, to one CPU, and leaves that CPU's
hyperthread sibling idle. It runs the first run of every strategy, then the
second of every strategy, and so on, so a slow drift in the machine is spread
over all the strategies instead of landing on one.

Before timing anything, the session builds one tree for each depth on each
target, from treegen.forest and the session's seed, sized for the largest
working set any cell of that depth needs. Each cell's requests are a random
subset of its tree's paths, enough to touch the cell's working set: its
share of the pinned CPU's caches, at BYTES_PER_DENTRY each. The paths have no
symlinks and no .., and the session removes the trees when it ends.

The file starts with a header and ends with an end record. A file without
the end record is from a session that didn't finish."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tomllib
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from harness import system
from protocol import client
from treegen import forest
from treegen.build import build_fixture, free_name, working_dir
from treegen.spec import Expect, Fixture, Request, Tree

RESULTS = client.REPO / "results" / "raw"

# How many times to try a run whose guaranteed frequency keeps changing.
TRIES = 3

# How many measured calls a run makes, at least: enough that a run's P50 and
# P99 are stable to within a histogram bucket. Earlier sessions' runs of
# 50,000 calls already agreed that closely; a sweep over this number should
# settle it.
CALLS = 100_000
WARMUP_PASSES = 1

# A run's timeout allows this long per call, which is far longer than any
# strategy takes, plus a margin for starting the driver and reading its
# requests.
TIMEOUT_PER_CALL = timedelta(milliseconds=1)
TIMEOUT_MARGIN = timedelta(seconds=30)
# The timeout of the one call each strategy makes before timing.
ONCE_TIMEOUT = timedelta(seconds=30)

# Where each target puts its trees. tmpfs keeps everything in memory, and ext4
# is the main local filesystem on the machine this study runs on.
TARGETS = {
    "tmpfs": Path("/dev/shm"),
    "ext4": client.REPO / "build" / "bench",
}


SWEEPS = Path(__file__).with_name("sweeps.toml")


class WorkingSet(StrEnum):
    """How many distinct entries a cell's requests touch, as a multiple of one
    of the pinned CPU's caches. sweeps.toml names them by their values."""

    cache: str
    times: float

    def __new__(cls, name: str, cache: str, times: float) -> WorkingSet:
        member = str.__new__(cls, name)
        member._value_ = name
        member.cache = cache
        member.times = times
        return member

    HALF_L2 = ("half-L2", "L2", 0.5)
    HALF_L3 = ("half-L3", "L3", 0.5)
    TWICE_L3 = ("twice-L3", "L3", 2.0)


# About how much cache one lookup's step through an entry uses: its hash
# bucket, its dentry and its inode, about four cache lines.
BYTES_PER_DENTRY = 256


@dataclass(frozen=True)
class Cell:
    """The settings one benchmark cell holds fixed. Each sweep in sweeps.toml
    changes one of them."""

    depth: int
    working_set: WorkingSet

    def __str__(self) -> str:
        return f"depth {self.depth}, {self.working_set}"

    def entries(self, caches: dict[str, dict]) -> int:
        """Return how many distinct entries the cell's requests touch, given
        the pinned CPU's caches."""
        ws = self.working_set
        return int(caches[ws.cache]["bytes"] * ws.times / BYTES_PER_DENTRY)


def load_sweeps(path: Path = SWEEPS) -> dict[str, list[Cell]]:
    """Return each sweep in path by name, with its cells in the order path
    lists them. A cell can belong to several sweeps."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    baseline = data["baseline"]
    sweeps: dict[str, list[Cell]] = {}
    for sweep in data["sweep"]:
        cells = sweeps.setdefault(sweep["name"], [])
        for level in sweep["levels"]:
            settings = {**baseline, **level}
            try:
                working_set = WorkingSet(settings["working_set"])
            except ValueError:
                known = ", ".join(WorkingSet)
                raise SystemExit(
                    f"{path}: unknown working set {settings['working_set']!r} (known: {known})"
                ) from None
            cells.append(Cell(settings["depth"], working_set))
    return sweeps


def pin_to_one_cpu() -> dict:
    """Pin this process, and so every driver it starts, to the lowest CPU it
    may use. Return that CPU, its hyperthread siblings, and the data caches it
    uses. In bench mode nothing else runs on the partition's CPUs, so the
    siblings stay idle."""
    cpu = min(os.sched_getaffinity(0))
    os.sched_setaffinity(0, {cpu})
    siblings = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list")
    try:
        sibling_list = siblings.read_text().strip()
    except OSError:
        sibling_list = None
    return {"cpu": cpu, "thread_siblings": sibling_list, "caches": system.caches(cpu)}


def conditions(cpu: int) -> dict:
    """Return what can change the pinned CPU's speed between runs.

    base_khz is the frequency the firmware currently guarantees, and with
    turbo off, intel_pstate caps the CPU there (max_khz). Under some laptop
    power profiles the firmware moves the guarantee between levels on its
    own, which changes every strategy's time by the same ratio. The throttle
    counters count, since boot, how often and for how long the hardware slowed
    the CPU's core and its package for heat or power. The harness reads all
    of this before and after each run, so the analysis can group runs by the
    state they ran in. Any value this machine doesn't offer is None."""

    def read(path: Path) -> int | None:
        try:
            return int(path.read_text())
        except (OSError, ValueError):
            return None

    freq = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq")
    throttle = Path(f"/sys/devices/system/cpu/cpu{cpu}/thermal_throttle")
    return {
        "base_khz": read(freq / "base_frequency"),
        "max_khz": read(freq / "scaling_max_freq"),
        **{
            f"{part}_throttle_{what}": read(throttle / f"{part}_throttle_{name}")
            for part in ("core", "package")
            for what, name in (("count", "count"), ("ms", "total_time_ms"))
        },
    }


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=client.REPO, capture_output=True, text=True)
    return r.stdout.strip()


def _driver_record(driver: client.Driver) -> dict:
    """Return the driver's command, with a hash of each file in it, so a result
    can be traced to the exact binary that produced it."""
    command = client.absolute(driver.command)
    return {
        "command": command,
        "sha256": {
            a: hashlib.sha256(Path(a).read_bytes()).hexdigest()
            for a in command
            if os.path.isfile(a)
        },
    }


def _write(out: TextIO, record: dict) -> None:
    out.write(json.dumps(record) + "\n")
    out.flush()


@dataclass(frozen=True)
class Built:
    """One cell on one target: its tree, as the driver is given it, and its
    requests."""

    target: str
    cell: Cell
    root: str
    cwd: Path
    request: str  # the request the check before timing makes
    expected: tuple[int, int]  # the device and inode of the file it must open
    requests: Path  # the file of requests the time mode goes through
    n_requests: int


def build_trees(
    cells: list[Cell], targets: list[str], caches: dict[str, dict], seed: int, stack: ExitStack
) -> tuple[list[Built], dict[Cell, dict]]:
    """Build a tree for each depth on each target, and write each cell's
    requests beside it. stack removes the trees when it closes. Return what
    each cell runs on, and a description of each cell for the header: its
    tree's shape, and how many entries its requests touch."""
    built, described = [], {}
    for depth in dict.fromkeys(c.depth for c in cells):
        mine = [c for c in cells if c.depth == depth]
        shape = forest.shape_for(depth, max(c.entries(caches) for c in mine))
        tree, files = forest.generate(shape, seed)
        chosen = {}
        for c in mine:
            paths, touched = forest.working_set(files, c.entries(caches), seed)
            chosen[c] = paths
            described[c] = {
                "fanout": list(shape.fanout),
                "tree_entries": shape.entries,
                "entries_wanted": c.entries(caches),
                "entries": touched,
                "requests": len(paths),
            }
        fx = Fixture(
            name=f"bench-depth{depth}",
            hazard="none",
            about="A benchmark tree: no symlinks, no .., no mounts.",
            root_name="root",
            requires=(),
            root=tree,
            outside=Tree(),
            request=Request("read", files[0]),
            expect=(Expect("ok", file=files[0]),),
            expect_in_root=(Expect("ok", file=files[0]),),
        )
        for target in targets:
            base = TARGETS[target]
            base.mkdir(parents=True, exist_ok=True)
            sb = stack.enter_context(build_fixture(fx, base))
            cwd = working_dir(fx, sb)
            for c in mine:
                # The request file goes beside the root, so no request can reach it.
                requests = sb.path / free_name(fx, f"requests-{c.working_set}")
                client.write_requests(requests, chosen[c])
                st = os.stat(sb.root / chosen[c][0])
                built.append(
                    Built(
                        target, c, str(sb.root), cwd, chosen[c][0], (st.st_dev, st.st_ino),
                        requests, len(chosen[c]),
                    )
                )  # fmt: skip
    return built, described


def check(
    driver: client.Driver, strategies: list[client.StrategyInfo], built: list[Built]
) -> list[str]:
    """Make one call with each strategy on each cell and target, using the
    binary that will be timed. Return a line for each call that didn't open
    the file."""
    failed = []
    for b in built:
        for s in strategies:
            run = client.once(
                driver.command, s.name, "read", b.root, b.request, cwd=b.cwd, timeout=ONCE_TIMEOUT
            )
            where = f"{b.target} {b.cell} {s.name}"
            if run.failure:
                failed.append(f"{where}: {run.failure}")
            elif [(o.ok, o.dev, o.ino) for o in run.outcomes] != [(True, *b.expected)]:
                failed.append(f"{where}: reached {run.outcomes}, not {b.expected}")
    return failed


def bench(
    driver: client.Driver,
    strategies: list[client.StrategyInfo],
    built: list[Built],
    runs: int,
    calls: int,
    cpu: int,
    out: TextIO,
) -> None:
    """Time each strategy on each cell and target, and write a record for each
    run. Each run makes at least calls measured calls. cpu is the CPU the runs
    are pinned to."""
    for t in built:
        passes = max(1, math.ceil(calls / t.n_requests))
        made = (WARMUP_PASSES + passes) * t.n_requests
        timeout = made * TIMEOUT_PER_CALL + TIMEOUT_MARGIN
        for n in range(1, runs + 1):
            for s in strategies:
                # A run during which the firmware changed the CPU's guaranteed
                # frequency mixes two speeds, so it is thrown away and made
                # again, up to TRIES times.
                discarded = 0
                before: dict = {}
                after: dict = {}
                while discarded < TRIES:
                    before = conditions(cpu)
                    run = client.time(
                        driver.command, WARMUP_PASSES, passes, n, s.name, "read", t.root,
                        t.requests, cwd=t.cwd, timeout=timeout,
                    )  # fmt: skip
                    after = conditions(cpu)
                    if before["base_khz"] == after["base_khz"]:
                        break
                    discarded += 1
                    print(
                        f"{t.target} {t.cell} run {n} {s.name}: guaranteed frequency changed from "
                        f"{before['base_khz']} to {after['base_khz']} kHz; running it again",
                        file=sys.stderr,
                    )
                else:
                    run = client.Run(
                        failure=f"the guaranteed frequency changed during all {TRIES} tries"
                    )
                _write(
                    out,
                    {
                        "kind": "run",
                        "target": t.target,
                        "cell": asdict(t.cell),
                        "strategy": s.name,
                        "semantics": s.semantics,
                        "run": n,
                        "seed": n,
                        "requests": t.n_requests,
                        "warmup_passes": WARMUP_PASSES,
                        "passes": passes,
                        "discarded": discarded,
                        "failure": run.failure,
                        "outcomes": [
                            {
                                "ok": o.ok,
                                "label": o.label,
                                "count": o.count,
                                "hist": [list(b) for b in o.hist],
                            }
                            for o in run.outcomes
                        ],
                        "time_ns": run.time_ns,
                        "before": before,
                        "after": after,
                    },
                )
                made = sum(o.count for o in run.outcomes)
                mean = f"{run.time_ns / made:.0f} ns mean" if made and run.time_ns else ""
                print(
                    f"{t.target} {t.cell} run {n} {s.name}: "
                    f"{run.failure or f'{made} calls, {mean}'}",
                    file=sys.stderr,
                )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Time every strategy on the benchmark's sweeps.")
    p.add_argument("--unpinned", action="store_true", help="run outside bench mode")
    p.add_argument("--strategy", action="append", help="time only this strategy (repeatable)")
    p.add_argument("--target", action="append", choices=list(TARGETS), help="(repeatable)")
    p.add_argument("--runs", type=int, default=3, help="runs per strategy, cell and target")
    p.add_argument("--seed", type=int, default=1, help="the seed the trees are built from")
    p.add_argument("--sweeps", type=Path, default=SWEEPS, help="the sweeps to time")
    p.add_argument(
        "--calls",
        type=int,
        default=CALLS,
        help=f"measured calls per run, at least (default {CALLS})",
    )
    p.add_argument(
        "--out", type=Path, help="results file (default: results/raw/bench-<time>.jsonl)"
    )
    p.add_argument("--no-build", action="store_true", help="do not build the driver first")
    args = p.parse_args(argv)

    # One time for the whole run: it names the file and goes in the header.
    started = system.utc_now()
    machine = system.require_bench_mode(args.unpinned)
    pinned_to = pin_to_one_cpu()

    driver = next(d for d in client.load_drivers() if d.name == "c")
    if not args.no_build:
        client.build(driver)
    offered = client.list_strategies(driver.command)
    names = {s.name for s in offered}
    unknown = sorted(set(args.strategy or ()) - names)
    if unknown:
        p.error(f"unknown strategy: {', '.join(unknown)}")
    strategies = [
        s for s in offered if not s.unavailable and (not args.strategy or s.name in args.strategy)
    ]
    targets = args.target or list(TARGETS)
    sweeps = load_sweeps(args.sweeps)
    # Every cell once, in the order the sweeps first list it, with the sweeps
    # it belongs to.
    member: dict[Cell, list[str]] = {}
    for name, sweep_cells in sweeps.items():
        for c in sweep_cells:
            member.setdefault(c, []).append(name)
    cells = list(member)

    stamp = started.replace("-", "").replace(":", "")
    out_path = args.out or RESULTS / f"bench-{stamp}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        built, described = build_trees(cells, targets, pinned_to["caches"], args.seed, stack)
        # Every strategy must open the file on every tree. If one doesn't,
        # stop before timing anything, and write no results file.
        failed = check(driver, strategies, built)
        if failed:
            for line in failed:
                print(line, file=sys.stderr)
            sys.exit("A strategy didn't open the benchmark file, so nothing was timed.")
        # Write the trees out now, so the kernel isn't writing them back to
        # disk in the middle of a measured run.
        os.sync()
        out = stack.enter_context(open(out_path, "x"))
        _write(
            out,
            {
                "kind": "header",
                "started": started,
                "commit": _git("rev-parse", "HEAD"),
                "dirty": bool(_git("status", "--porcelain")),
                "machine": machine,
                "pinned_to": pinned_to,
                "driver": _driver_record(driver),
                "unavailable": {s.name: s.unavailable for s in offered if s.unavailable},
                "options": {
                    "runs": args.runs,
                    "calls": args.calls,
                    "warmup_passes": WARMUP_PASSES,
                    "targets": targets,
                    "strategies": [s.name for s in strategies],
                    "seed": args.seed,
                    "bytes_per_dentry": BYTES_PER_DENTRY,
                },
                "cells": [{**asdict(c), "sweeps": member[c], **described[c]} for c in cells],
                # Each sweep's cells, in the order sweeps.toml lists them.
                "sweeps": {name: [asdict(c) for c in cs] for name, cs in sweeps.items()},
            },
        )
        bench(driver, strategies, built, args.runs, args.calls, pinned_to["cpu"], out)
        _write(out, {"kind": "end", "finished": system.utc_now()})
    print(out_path)


if __name__ == "__main__":
    main()
