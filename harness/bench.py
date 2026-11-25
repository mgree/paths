"""This module times every strategy on one benchmark cell, and writes the
results to a JSONL file. One invocation is a session, and writes one file. A
session makes several runs of each strategy on each target, and each run is
one driver process.

Run it inside bench mode, so that it and every driver it starts are in the
cgroup paths-bench:

    sudo tools/benchmode on
    sudo tools/benchmode run -- .venv/bin/python -m harness.bench
    sudo tools/benchmode off

Outside bench mode it refuses to run, unless it is given --unpinned, which
marks the results as unpinned.

Each run is a fresh driver process in the protocol's time mode: a warm-up
that isn't measured, then a measured stretch, with every call timed into a
histogram. The runs happen one at a time, so no two drivers compete for a
core, its caches, or the directories they all walk. The harness pins itself,
and so every driver, to one CPU, and leaves that CPU's hyperthread sibling
idle. It runs the first run of every strategy, then the second of every
strategy, and so on, so a slow drift in the machine is spread over all the
strategies instead of landing on one.

The file starts with a header and ends with an end record. A file without
the end record is from a session that didn't finish."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TextIO

from harness import system
from protocol import client
from treegen.build import build_fixture, working_dir
from treegen.spec import Entry, Expect, Fixture, Request, Tree

RESULTS = client.REPO / "results" / "raw"

# How many times to try a run whose guaranteed frequency keeps changing.
TRIES = 3

# Where each target puts its trees. tmpfs keeps everything in memory, and ext4
# is the main local filesystem on the machine this study runs on.
TARGETS = {
    "tmpfs": Path("/dev/shm"),
    "ext4": client.REPO / "build" / "bench",
}


@dataclass(frozen=True)
class Cell:
    """The settings one benchmark cell holds fixed. Each one-axis sweep in the
    design changes one of these."""

    depth: int = 16
    tree: str = "fixed"  # which tree: "fixed" until the forest generator exists

    def fixture(self) -> Fixture:
        """Return the cell's tree as a treegen fixture: a chain of directories
        ending in a file, with a read request that must open the file."""
        path = "/".join([f"dir{i:02}" for i in range(1, self.depth + 1)] + ["file"])
        root = Tree()
        root.add(Entry(path, "file"))
        reaches = (Expect("ok", file=path),)
        return Fixture(
            name=f"bench-{self.tree}-depth{self.depth}",
            hazard="none",
            about="The baseline benchmark tree: no symlinks, no .., no mounts.",
            root_name="root",
            requires=(),
            root=root,
            outside=Tree(),
            request=Request("read", path),
            expect=reaches,
            expect_in_root=reaches,
        )


BASELINE = Cell()


def pin_to_one_cpu() -> dict:
    """Pin this process, and so every driver it starts, to the lowest CPU it
    may use. Return that CPU and its hyperthread siblings. In bench mode
    nothing else runs on the partition's CPUs, so the siblings stay idle."""
    cpu = min(os.sched_getaffinity(0))
    os.sched_setaffinity(0, {cpu})
    siblings = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list")
    try:
        sibling_list = siblings.read_text().strip()
    except OSError:
        sibling_list = None
    return {"cpu": cpu, "thread_siblings": sibling_list}


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
    """A cell's tree, built on one target, as the driver is given it."""

    target: str
    root: str
    cwd: Path
    request: str
    expected: tuple[int, int]  # the device and inode of the file the request must open


def build_trees(cell: Cell, targets: list[str], stack: ExitStack) -> list[Built]:
    """Build the cell's tree on each target. stack removes them when it
    closes."""
    trees = []
    for target in targets:
        fx = cell.fixture()
        base = TARGETS[target]
        base.mkdir(parents=True, exist_ok=True)
        sb = stack.enter_context(build_fixture(fx, base))
        st = os.stat(sb.root / fx.request.path)
        trees.append(
            Built(
                target, str(sb.root), working_dir(fx, sb), fx.request.path, (st.st_dev, st.st_ino)
            )
        )
    return trees


def check(
    driver: client.Driver, strategies: list[client.StrategyInfo], trees: list[Built]
) -> list[str]:
    """Make one call with each strategy on each tree, using the binary that
    will be timed. Return a line for each call that didn't open the file."""
    failed = []
    for t in trees:
        for s in strategies:
            run = client.once(
                driver.command, s.name, "read", t.root, t.request, cwd=t.cwd, timeout=30
            )
            if run.failure:
                failed.append(f"{t.target} {s.name}: {run.failure}")
            elif [(o.ok, o.dev, o.ino) for o in run.outcomes] != [(True, *t.expected)]:
                failed.append(f"{t.target} {s.name}: reached {run.outcomes}, not {t.expected}")
    return failed


def bench(
    driver: client.Driver,
    strategies: list[client.StrategyInfo],
    trees: list[Built],
    cell: Cell,
    runs: int,
    warmup: float,
    seconds: float,
    cpu: int,
    out: TextIO,
) -> None:
    """Time each strategy on each tree, and write a record for each run. cpu is
    the CPU the runs are pinned to."""
    timeout = warmup + seconds + 30
    for t in trees:
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
                        driver.command, warmup, seconds, s.name, "read", t.root, t.request,
                        cwd=t.cwd, timeout=timeout,
                    )  # fmt: skip
                    after = conditions(cpu)
                    if before["base_khz"] == after["base_khz"]:
                        break
                    discarded += 1
                    print(
                        f"{t.target} run {n} {s.name}: guaranteed frequency changed from "
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
                        "cell": asdict(cell),
                        "strategy": s.name,
                        "semantics": s.semantics,
                        "run": n,
                        "discarded": discarded,
                        "failure": run.failure,
                        "outcomes": [
                            {"ok": o.ok, "label": o.label, "count": o.count} for o in run.outcomes
                        ],
                        "time_ns": run.time_ns,
                        "hist": [list(b) for b in run.hist],
                        "before": before,
                        "after": after,
                    },
                )
                calls = sum(o.count for o in run.outcomes)
                mean = f"{run.time_ns / calls:.0f} ns mean" if calls and run.time_ns else ""
                print(
                    f"{t.target} run {n} {s.name}: {run.failure or f'{calls} calls, {mean}'}",
                    file=sys.stderr,
                )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Time every strategy on one benchmark cell.")
    p.add_argument("--unpinned", action="store_true", help="run outside bench mode")
    p.add_argument("--strategy", action="append", help="time only this strategy (repeatable)")
    p.add_argument("--target", action="append", choices=list(TARGETS), help="(repeatable)")
    p.add_argument("--runs", type=int, default=3, help="runs per strategy and target")
    p.add_argument("--warmup", type=float, default=0.5, help="unmeasured seconds per run")
    p.add_argument("--seconds", type=float, default=1.0, help="measured seconds per run")
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

    stamp = started.replace("-", "").replace(":", "")
    out_path = args.out or RESULTS / f"bench-{stamp}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        trees = build_trees(BASELINE, targets, stack)
        # Every strategy must open the file on every tree. If one doesn't,
        # stop before timing anything, and write no results file.
        failed = check(driver, strategies, trees)
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
                    "warmup": args.warmup,
                    "seconds": args.seconds,
                    "targets": targets,
                    "strategies": [s.name for s in strategies],
                },
            },
        )
        bench(
            driver, strategies, trees, BASELINE, args.runs, args.warmup, args.seconds,
            pinned_to["cpu"], out,
        )  # fmt: skip
        _write(out, {"kind": "end", "finished": system.utc_now()})
    print(out_path)


if __name__ == "__main__":
    main()
