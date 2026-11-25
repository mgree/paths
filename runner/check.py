"""The correctness runner: every fixture against every available strategy.

    python -m runner.check [--driver c] [--strategy NAME ...] [--fixture NAME ...]
                           [--root-shape SHAPE ... | --root-shape all]

Each attempt gets a fresh sandbox (built inside a namespace when the fixture
has mounts), an empty working directory outside the root, and a snapshot of
everything outside the root taken before and after the driver's one call. The
attempt is judged against the fixture's expectations for the strategy's
semantics, and recorded in full, outcome and all, so that a strategy's
outcomes across the suite can be read as its fingerprint.

The root's shape (see RootShape) spells the path to the root differently,
as real configurations do, without changing what is inside it.

Results go to results/raw/check-<start>.jsonl, with the start time in UTC. The
first line says how the run was made, and each attempt's line is written as the
attempt finishes.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import itertools
import json
import math
import multiprocessing
import os
import platform
import stat
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from multiprocessing.context import BaseContext
from pathlib import Path
from typing import Literal

from protocol import client
from treegen import ns
from treegen.build import (
    DIR_FLAGS,
    Sandbox,
    build_fixture,
    free_name,
    open_dir,
    opened,
    scan,
    snapshot_outside,
    working_dir,
)
from treegen.spec import Expect, Fixture, Op, Semantics, TreeName, all_fixtures

Verdict = Literal["pass", "fail", "skip"]

RESULTS = client.REPO / "results" / "raw"
DEFAULT_BASE = Path("/dev/shm")
RACER_DIR = client.REPO / "runner" / "racer"
RACER = RACER_DIR / "build" / "racer"

# Run memcheck with descriptor tracking. A leaked descriptor, a memory error or
# a memory leak makes valgrind exit with VALGRIND_ERROR_EXITCODE, which the
# runner records as a driver failure. Valgrind can't run the sanitizer build, so
# it runs the optimized one.
VALGRIND_ERROR_EXITCODE = 99
VALGRIND = (
    "valgrind",
    "-q",
    "--track-fds=yes",
    "--leak-check=full",
    f"--error-exitcode={VALGRIND_ERROR_EXITCODE}",
)


@dataclass(frozen=True)
class Task:
    fixture: Fixture
    driver: str
    command: tuple[str, ...]  # exactly what runs, valgrind included
    strategy: str
    semantics: Semantics
    root_shape: RootShape
    base: str
    timeout: float  # beyond the race, for a race fixture
    race_seconds: float  # how long a race fixture's driver races


@dataclass
class Seen:
    """One distinct outcome of the driver's calls, and how many calls had it."""

    count: int
    ok: bool
    label: str | None  # error label, normalized
    identity: list[int] | None  # (dev, ino) the driver reported
    reached: str | None  # what an ok outcome opened, as tree:path, if declared


@dataclass
class Attempt:
    """What happened in one attempt, before it is judged. A strategy's
    fingerprint is made of these. Without a race the driver makes one call, so
    there is one outcome; with one, there is an outcome for each different
    thing its calls did."""

    failure: str | None  # the driver or the racer failed (crash, timeout, bad output)
    outcomes: list[Seen]
    expected_ids: dict[str, list[int] | None]  # identity of each expected file
    outside_changed: list[str]
    swaps: int | None  # how often the racer swapped its names; None without a race
    stderr: str


@dataclass
class Result:
    fixture: str
    hazard: str
    driver: str
    strategy: str
    semantics: Semantics
    root_shape: RootShape
    op: Op
    verdict: Verdict
    reason: str
    attempt: Attempt | None


# --- one attempt ---------------------------------------------------------------


def _identity(sb: Sandbox, tree: TreeName, rel: str) -> list[int] | None:
    """Return the (dev, ino) of a regular file, or None. It is looked up one
    directory at a time, without following symlinks. Mount points are crossed
    like any other directory."""
    head, _, name = rel.rpartition("/")
    with opened(sb.base(tree), DIR_FLAGS) as fd:
        try:
            with open_dir(fd, head) as d:
                st = os.stat(name, dir_fd=d, follow_symlinks=False)
        except OSError:
            return None
    return [st.st_dev, st.st_ino] if stat.S_ISREG(st.st_mode) else None


class RootShape(StrEnum):
    """How the runner spells the path to the root. Real configurations name
    roots in all these ways, and a correct strategy behaves the same under
    every one."""

    PLAIN = "plain"
    SYMLINKED = "symlinked"  # through a symlink to the directory above the root
    DOTTED = "dotted"  # with `.`, `..` and `//`
    ROOT_IS_SYMLINK = "root-is-symlink"
    # The root is the top of a mount, as a container's root filesystem is.
    MOUNTED = "mounted"

    @property
    def needs_mount(self) -> bool:
        """Return whether this shape mounts something, and so has to run in a
        namespace of its own."""
        return self is RootShape.MOUNTED

    def path(self, sb: Sandbox, link: Path) -> str:
        """Make any symlink or mount this shape needs, at link beside the root,
        and return the path to give the driver."""
        root = sb.root.name
        match self:
            case RootShape.PLAIN:
                return str(sb.root)
            case RootShape.SYMLINKED:
                os.symlink(".", link)
                return str(link / root)
            case RootShape.DOTTED:
                return f"{sb.path}/.//{root}/../{root}"
            case RootShape.ROOT_IS_SYMLINK:
                os.symlink(root, link)
                return str(link)
            case RootShape.MOUNTED:
                # Bind the root onto a new, empty directory beside it, and give
                # the driver that directory. The root itself isn't mounted on,
                # so the build, the racer and the outside snapshot keep using
                # the root's own path. The racer couldn't go through the mount
                # anyway: renameat2 refuses to swap entries across mounts. The
                # bind is recursive, so the fixture's own mounts in the root
                # show through it.
                os.mkdir(link)
                ns.mount(sb.root, link, None, ns.MS_BIND | ns.MS_REC)
                sb.outer_mounts[link.name] = "bind"
                return str(link)


def _changed(before: dict, after: dict) -> list[str]:
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def _expected_ids(fx: Fixture, sb: Sandbox) -> dict[str, list[int] | None]:
    expected = {e.file for e in fx.expect + fx.expect_in_root if e.file is not None}
    return {f: _identity(sb, "root", f) for f in sorted(expected)}


def _names(sb: Sandbox) -> dict[tuple[int, int], str]:
    """The tree:path of each regular file in the sandbox, by (dev, ino)."""
    names: dict[tuple[int, int], str] = {}
    for r in scan(sb):
        if r.kind == "file":
            names.setdefault(r.ino, f"{r.tree}:{r.path}")
    return names


# The two CPUs a race worker runs its races on: the driver on the first, the
# racer on the second. _claim_cpus sets it in each race worker. It is None
# elsewhere, and when there are too few CPUs to pair.
_race_cpus: tuple[int, int] | None = None


def cpu_pairs() -> list[tuple[int, int]]:
    """The CPUs this process may run on, in disjoint pairs. With an odd number,
    the last CPU is left out."""
    cpus = sorted(os.sched_getaffinity(0))
    return list(zip(cpus[0::2], cpus[1::2], strict=False))


def _claim_cpus(claims) -> None:
    """Take this worker's pair of CPUs, or None, from the queue."""
    global _race_cpus
    _race_cpus = claims.get()


def _attempt_in_sandbox(task: Task) -> Attempt:
    fx = task.fixture
    with build_fixture(fx, task.base) as sb:
        cwd = working_dir(fx, sb)
        root = task.root_shape.path(sb, sb.path / free_name(fx, "via"))
        # The racer moves names around, so a race fixture's files are looked
        # up before it starts. Without a race they are looked up afterwards, to
        # find a file the call created.
        expected_ids, names = (_expected_ids(fx, sb), _names(sb)) if fx.race else ({}, {})
        before = snapshot_outside(sb)
        args = (task.strategy, fx.request.op, root, fx.request.path)
        swaps, racer_failure, racer_err = None, None, ""
        if fx.race:
            driver_limit = task.race_seconds + task.timeout
            # The runner stops the racer once the driver is done. The limit only
            # matters if the runner dies first, so it is well past the driver's.
            limit = math.ceil(driver_limit + task.timeout)
            with ExitStack() as stack:
                racer_args, fds = [], []
                for tree, path in fx.race.exchange:
                    # Hand the racer each entry's parent as a descriptor, so the
                    # entries can sit deeper than PATH_MAX.
                    parent, _, name = path.rpartition("/")
                    top = stack.enter_context(opened(sb.base(tree), DIR_FLAGS))
                    fd = stack.enter_context(open_dir(top, parent))
                    racer_args += [str(fd), name]
                    fds.append(fd)
                # The kernel's spurious EXDEV needs a swap in the middle of one
                # lookup, which is rare unless the driver and the racer run at
                # the same time. So each gets its own CPU, inherited from this
                # process: the racer is started while this process is on the
                # racer's CPU, and the driver after it moves to the driver's.
                if _race_cpus is not None:
                    os.sched_setaffinity(0, {_race_cpus[1]})
                racer = subprocess.Popen(
                    [str(RACER), str(limit), *racer_args],
                    pass_fds=fds,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if _race_cpus is not None:
                    os.sched_setaffinity(0, {_race_cpus[0]})
            try:
                run = client.race(
                    task.command, task.race_seconds, *args, cwd=cwd, timeout=driver_limit
                )
            finally:
                racer.terminate()
                try:
                    out, racer_err = racer.communicate(timeout=task.timeout)
                except subprocess.TimeoutExpired:
                    racer.kill()
                    out, racer_err = racer.communicate()
                    racer_err += "racer: did not stop on SIGTERM\n"
            if racer.returncode == 0:
                swaps = int(out)
            elif racer.returncode < 0:
                racer_failure = f"racer signal {-racer.returncode}"
            else:
                racer_failure = f"racer exit status {racer.returncode}"
        else:
            run = client.once(task.command, *args, cwd=cwd, timeout=task.timeout)
        after = snapshot_outside(sb)
        if not fx.race:
            expected_ids, names = _expected_ids(fx, sb), _names(sb)
        outcomes = [
            Seen(
                count=o.count,
                ok=o.ok,
                label=o.label,
                identity=list(o.identity) if o.identity else None,
                reached=names.get(o.identity) if o.identity else None,
            )
            for o in run.outcomes
        ]
        return Attempt(
            failure=run.failure or racer_failure,
            outcomes=outcomes,
            expected_ids=expected_ids,
            outside_changed=_changed(before, after),
            swaps=swaps,
            stderr=run.stderr + racer_err,
        )


def attempt(task: Task) -> Attempt:
    if task.fixture.mounts or task.root_shape.needs_mount:
        return ns.run_in_namespace(_attempt_in_sandbox, task)
    return _attempt_in_sandbox(task)


# --- judging -------------------------------------------------------------------


def _wanted(e: Expect) -> str:
    return e.result if e.result in ("ok", "refused") else f"error {e.errno}"


def _got(o: Seen) -> str:
    """What an outcome was, in a few words. The attempt itself keeps the
    details: the path reached, the full failure, the files changed."""
    if not o.ok:
        return f"error {o.label}"
    if o.reached is None:
        return "ok elsewhere"
    return "ok outside" if o.reached.startswith("outside:") else "ok in root"


def _matches(e: Expect, a: Attempt, o: Seen) -> bool:
    if e.result == "ok":
        return o.ok and o.identity is not None and o.identity == a.expected_ids.get(e.file or "")
    if e.result == "refused":
        return not o.ok
    return not o.ok and o.label is not None and client.same_error(o.label, e.errno or "")


def judge(fx: Fixture, semantics: Semantics, a: Attempt) -> tuple[Verdict, str]:
    """The verdict, and a short reason such as "wanted refused, got ok outside".
    Every one of the driver's calls must have an accepted outcome, and nothing
    outside may change."""
    expects = fx.expect if semantics == "beneath" else fx.expect_in_root
    wanted = f"wanted {' or '.join(_wanted(e) for e in expects)}"
    if a.failure:
        return "fail", f"{wanted}, got {a.failure.partition(':')[0]}"
    calls = sum(o.count for o in a.outcomes)

    def got(o: Seen) -> str:
        return _got(o) if calls == 1 else f"{_got(o)} ({o.count} of {calls} calls)"

    bad = [o for o in a.outcomes if not any(_matches(e, a, o) for e in expects)]
    wrote = [p for p in a.outside_changed if p not in fx.outside_changes]
    reasons = [f"{wanted}, got {got(bad[0])}"] if bad else []
    if wrote:
        reasons.append(f"wrote outside: {', '.join(wrote)}")
    if reasons:
        return "fail", ", ".join(reasons)
    return "pass", "got " + ", ".join(got(o) for o in a.outcomes)


def skip_reason(fx: Fixture) -> str | None:
    if "linux" in fx.requires and sys.platform != "linux":
        return "needs Linux"
    if "nonroot" in fx.requires and os.geteuid() == 0:
        return "needs a non-root user"
    if "userns" in fx.requires and not ns.mounts_available():
        return ns.why_no_mounts() or "needs user namespaces"
    return None


def run_task(task: Task) -> Result:
    fx = task.fixture
    result = functools.partial(
        Result,
        fixture=fx.name,
        hazard=fx.hazard,
        driver=task.driver,
        strategy=task.strategy,
        semantics=task.semantics,
        root_shape=task.root_shape,
        op=fx.request.op,
    )
    why = skip_reason(fx)
    if not why and task.root_shape.needs_mount and not ns.mounts_available():
        why = ns.why_no_mounts() or "needs user namespaces"
    if why:
        return result(verdict="skip", reason=why, attempt=None)
    a = attempt(task)
    verdict, reason = judge(fx, task.semantics, a)
    return result(verdict=verdict, reason=reason, attempt=a)


# --- the suite -----------------------------------------------------------------


def build_racer() -> None:
    subprocess.run(["make", "-s", "-C", str(RACER_DIR)], check=True, capture_output=True)


def plan(
    drivers: list[client.Driver],
    fixtures: list[Fixture],
    strategies: set[str] | None,
    base: str,
    timeout: float,
    *,
    valgrind: bool,
    root_shapes: tuple[RootShape, ...],
    race_seconds: float,
) -> tuple[list[Task], list[Result]]:
    """Return the attempts to make, and a result for each strategy this host
    can't run."""
    tasks, unavailable = [], []
    for d in drivers:
        if valgrind:
            command = (*VALGRIND, *client.absolute(d.command))
        else:
            command = tuple(client.absolute(d.check_command))
        for s in client.list_strategies(d.check_command):
            if strategies and s.name not in strategies:
                continue
            for shape in root_shapes:
                for fx in fixtures:
                    if s.unavailable:
                        unavailable.append(
                            Result(
                                fx.name, fx.hazard, d.name, s.name, s.semantics, shape,
                                fx.request.op, "skip", f"unavailable: {s.unavailable}", None,
                            )
                        )  # fmt: skip
                        continue
                    tasks.append(
                        Task(
                            fx,
                            d.name,
                            command,
                            s.name,
                            s.semantics,
                            shape,
                            base,
                            timeout,
                            race_seconds,
                        )
                    )
    return tasks, unavailable


# Fixtures that run after all the others, with nothing else running beside
# them. Linux counts a symlink chain's links twice when a lookup in RCU mode
# fails with -ECHILD and is retried, so a chain of 21 to 40 links can fail with
# ELOOP. A mount or unmount anywhere on the system can cause that failure, and
# fixtures with mounts mount in their own namespaces while they run. The fix is
# pending upstream:
# https://lore.kernel.org/linux-fsdevel/87zeuz5o4j.fsf@hippogriff/T/#u
# Once kernels with it are common, this can go.
ALONE = {"symlink-chain-40"}


def run_all(tasks: list[Task], jobs: int) -> Iterator[Result]:
    """Run the tasks in parallel: first the ordinary fixtures' tasks, then the
    race fixtures', then the tasks of the fixtures in ALONE. Results come back
    in that order, each as soon as it and every task before it have finished.

    A racer's renames make the kernel fail scoped `..` steps with EAGAIN
    anywhere on the system, so a race running beside another fixture can change
    that fixture's result. Races may run beside each other: every race fixture
    accepts EAGAIN. Each race takes two CPUs, one for the driver and one for the
    racer, so at most one race runs per pair of CPUs."""
    # forkserver: workers start single-threaded, so the namespace helper may
    # fork and unshare inside them.
    ctx = multiprocessing.get_context("forkserver")
    ordinary = [t for t in tasks if not t.fixture.race and t.fixture.name not in ALONE]
    races = [t for t in tasks if t.fixture.race and t.fixture.name not in ALONE]
    alone = [t for t in tasks if t.fixture.name in ALONE]
    with ProcessPoolExecutor(max_workers=jobs, mp_context=ctx) as pool:
        yield from pool.map(run_task, ordinary, chunksize=1)
    yield from _run_races(ctx, races, jobs)
    # The ALONE fixtures neither mount nor race, so they may run beside each
    # other, except under a root shape that mounts. Those tasks run one at a
    # time, last. A task's own mount goes on before its driver starts and comes
    # off after the driver ends, so it can't disturb the task's own lookup.
    quiet = [t for t in alone if not t.root_shape.needs_mount]
    mounting = [t for t in alone if t.root_shape.needs_mount]
    with ProcessPoolExecutor(max_workers=jobs, mp_context=ctx) as pool:
        yield from pool.map(run_task, quiet, chunksize=1)
    with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
        yield from pool.map(run_task, mounting, chunksize=1)


def _run_races(ctx: BaseContext, races: list[Task], jobs: int) -> Iterator[Result]:
    """Run the race fixtures' tasks, at most one per pair of CPUs."""
    if not races:
        return
    pairs = cpu_pairs()[:jobs]
    claims = ctx.SimpleQueue()
    for pair in pairs or [None]:
        claims.put(pair)
    with ProcessPoolExecutor(
        max_workers=len(pairs) or 1,
        mp_context=ctx,
        initializer=_claim_cpus,
        initargs=(claims,),
    ) as pool:
        yield from pool.map(run_task, races, chunksize=1)


def header(started: datetime, tasks: list[Task], options: dict) -> dict:
    """The first line of a results file: when the run started, the code and host
    it ran on, its options, and the command each driver ran with. The commands
    come from the tasks themselves, so they are what actually ran."""

    def git(*args: str) -> str:
        r = subprocess.run(["git", *args], cwd=client.REPO, capture_output=True, text=True)
        return r.stdout.strip()

    def sha256(path: str) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    commands = {t.driver: t.command for t in tasks}
    return {
        "kind": "check",
        "started": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "options": options,
        "drivers": {
            name: {
                "command": list(command),
                "sha256": {a: sha256(a) for a in command if os.path.isfile(a)},
            }
            for name, command in commands.items()
        },
    }


def driver_failures(results: list[Result]) -> list[Result]:
    """Attempts the driver itself failed, other than by hanging: a crash, a
    sanitizer or valgrind report, bad output. Unlike a failed verdict, which is
    a finding about a strategy, these are bugs in our code."""
    return [r for r in results if r.attempt and r.attempt.failure not in (None, "timeout")]


def report(results: list[Result]) -> str:
    """Per strategy: how many fixtures it passed, and what went wrong where."""
    lines = []
    by_strategy: dict[tuple[str, str, RootShape], list[Result]] = {}
    for r in results:
        by_strategy.setdefault((r.driver, r.strategy, r.root_shape), []).append(r)
    width = max((len(r.fixture) for r in results if r.verdict == "fail"), default=0)
    for (d, s, shape), rs in by_strategy.items():
        judged = [r for r in rs if r.verdict != "skip"]
        passed = sum(r.verdict == "pass" for r in judged)
        skipped = len(rs) - len(judged)
        note = f", {skipped} skipped" if skipped else ""
        label = f"{d}/{s}" if shape is RootShape.PLAIN else f"{d}/{s} (root shape: {shape})"
        lines.append(f"{label}: passes {passed}/{len(judged)}{note}")
        for r in judged:
            if r.verdict == "fail":
                lines.append(f"  {r.fixture:<{width}}  {r.reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    started = datetime.now(timezone.utc)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--driver", action="append", help="driver name (default: all that run here)")
    p.add_argument("--strategy", action="append", help="strategy name (default: all)")
    p.add_argument("--fixture", action="append", help="fixture name (default: all)")
    p.add_argument(
        "--root-shape",
        action="append",
        choices=[*RootShape, "all"],
        help="how to spell the path to the root (default: plain)",
    )
    p.add_argument("--timeout", type=float, default=5.0, help="seconds per attempt (default 5)")
    p.add_argument(
        "--race-seconds", type=float, default=1.0, help="how long each race lasts (default 1)"
    )
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    p.add_argument("--base", default=str(DEFAULT_BASE), help="where sandboxes go")
    p.add_argument("--no-build", action="store_true", help="do not build the drivers first")
    p.add_argument(
        "--valgrind", action="store_true", help="run the optimized drivers under valgrind"
    )
    p.add_argument(
        "--out", type=Path, help="results file (default: results/raw/check-<start>.jsonl)"
    )
    args = p.parse_args(argv)

    def check_names(kind: str, given: list[str] | None, known: list[str]) -> None:
        """Reject names that match nothing, which would otherwise run nothing."""
        unknown = sorted(set(given or ()) - set(known))
        if unknown:
            p.error(f"unknown {kind} {', '.join(unknown)} (known: {', '.join(sorted(set(known)))})")

    registered = client.load_drivers()
    check_names("driver", args.driver, [d.name for d in registered])
    drivers = [d for d in registered if d.runs_here()]
    elsewhere = sorted(set(args.driver or ()) - {d.name for d in drivers})
    if elsewhere:
        p.error(f"driver {', '.join(elsewhere)} doesn't run on this host ({sys.platform})")
    if args.driver:
        drivers = [d for d in drivers if d.name in args.driver]
    fixtures = all_fixtures()
    check_names("fixture", args.fixture, [f.name for f in fixtures])
    if args.fixture:
        fixtures = [f for f in fixtures if f.name in args.fixture]
    if not args.no_build:
        for d in drivers:
            client.build(d)
        build_racer()
    offered = [s.name for d in drivers for s in client.list_strategies(d.check_command)]
    check_names("strategy", args.strategy, offered)
    strategies = set(args.strategy) if args.strategy else None

    shapes = args.root_shape or [RootShape.PLAIN]
    root_shapes = (
        tuple(RootShape) if "all" in shapes else tuple(map(RootShape, dict.fromkeys(shapes)))
    )
    tasks, unavailable = plan(
        drivers,
        fixtures,
        strategies,
        args.base,
        args.timeout,
        valgrind=args.valgrind,
        root_shapes=root_shapes,
        race_seconds=args.race_seconds,
    )
    out = args.out or RESULTS / f"check-{started:%Y%m%dT%H%M%SZ}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    results = []
    with out.open("w") as f:
        f.write(json.dumps(header(started, tasks, vars(args)), default=str) + "\n")
        for r in itertools.chain(run_all(tasks, args.jobs), unavailable):
            f.write(json.dumps(asdict(r)) + "\n")
            results.append(r)
    print(report(results))
    print(f"\nresults: {out}")
    broken = driver_failures(results)
    if broken:
        print(f"\n{len(broken)} driver failure(s), bugs in the driver or a strategy:")
        for r in broken:
            assert r.attempt is not None
            print(f"  {r.driver}/{r.strategy} on {r.fixture}: {r.attempt.failure}")
            for line in r.attempt.stderr.strip().splitlines()[:20]:
                print(f"      {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
