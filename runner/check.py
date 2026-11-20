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
import multiprocessing
import os
import platform
import stat
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal

from protocol import client
from treegen import ns
from treegen.build import (
    DIR_FLAGS,
    Sandbox,
    build_fixture,
    open_dir,
    opened,
    scan,
    snapshot_outside,
)
from treegen.spec import Expect, Fixture, Op, Semantics, all_fixtures

Verdict = Literal["pass", "fail", "skip"]

RESULTS = client.REPO / "results" / "raw"
DEFAULT_BASE = Path("/dev/shm")

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
    timeout: float


@dataclass
class Attempt:
    """What happened in one attempt, before it is judged. A strategy's
    fingerprint is made of these."""

    failure: str | None  # the driver failed (crash, timeout, bad output)
    ok: bool
    label: str | None  # error label, normalized
    reached: str | None  # what an ok outcome opened, as tree:path, if declared
    expected_ids: dict[str, list[int] | None]  # identity of each expected file
    identity: list[int] | None  # (dev, ino) the driver reported
    outside_changed: list[str]
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


def _identity(sb: Sandbox, tree: str, rel: str) -> list[int] | None:
    """Return the (dev, ino) of a regular file, or None. It is looked up one
    directory at a time, without following symlinks. Mount points are crossed
    like any other directory."""
    base = sb.root if tree == "root" else sb.path
    head, _, name = rel.rpartition("/")
    with opened(base, DIR_FLAGS) as fd:
        try:
            with open_dir(fd, head) as d:
                st = os.stat(name, dir_fd=d, follow_symlinks=False)
        except OSError:
            return None
    return [st.st_dev, st.st_ino] if stat.S_ISREG(st.st_mode) else None


def _free_name(fx: Fixture, name: str) -> str:
    """A name for a new entry in the sandbox, outside the root, that collides
    with nothing the fixture declares."""
    while name in fx.outside.entries or name == fx.root_name:
        name = "_" + name
    return name


def _working_dir(fx: Fixture, sb: Sandbox) -> Path:
    """Make an empty directory in the sandbox, outside the root."""
    path = sb.path / _free_name(fx, "cwd")
    os.mkdir(path, 0o755)
    return path


class RootShape(StrEnum):
    """How the runner spells the path to the root. Real configurations name
    roots in all these ways, and a correct strategy behaves the same under
    every one."""

    PLAIN = "plain"
    SYMLINKED = "symlinked"  # through a symlink to the directory above the root
    DOTTED = "dotted"  # with `.`, `..` and `//`
    ROOT_IS_SYMLINK = "root-is-symlink"

    def path(self, sb: Sandbox, link: Path) -> str:
        """Make any symlink this shape needs, at link beside the root, and
        return the path to give the driver."""
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


def _changed(before: dict, after: dict) -> list[str]:
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def _attempt_in_sandbox(task: Task) -> Attempt:
    fx = task.fixture
    with build_fixture(fx, task.base) as sb:
        cwd = _working_dir(fx, sb)
        root = task.root_shape.path(sb, sb.path / _free_name(fx, "via"))
        before = snapshot_outside(sb)
        run = client.once(
            task.command,
            task.strategy,
            fx.request.op,
            root,
            fx.request.path,
            cwd=cwd,
            timeout=task.timeout,
        )
        after = snapshot_outside(sb)
        expected = {e.file for e in fx.expect + fx.expect_in_root if e.file is not None}
        expected_ids = {f: _identity(sb, "root", f) for f in sorted(expected)}
        outcome = run.outcomes[0] if run.outcomes else None
        identity = list(outcome.identity) if outcome and outcome.identity else None
        reached = None
        if identity is not None:
            for r in scan(sb):
                if r.kind == "file" and list(r.ino) == identity:
                    reached = f"{r.tree}:{r.path}"
                    break
        return Attempt(
            failure=run.failure,
            ok=bool(outcome and outcome.ok),
            label=outcome.label if outcome else None,
            reached=reached,
            expected_ids=expected_ids,
            identity=identity,
            outside_changed=_changed(before, after),
            stderr=run.stderr,
        )


def attempt(task: Task) -> Attempt:
    if task.fixture.mounts:
        return ns.run_in_namespace(_attempt_in_sandbox, task)
    return _attempt_in_sandbox(task)


# --- judging -------------------------------------------------------------------


def _wanted(e: Expect) -> str:
    return e.result if e.result in ("ok", "refused") else f"error {e.errno}"


def _got(a: Attempt) -> str:
    """What the attempt did, in a few words. The attempt itself keeps the
    details: the path reached, the full failure, the files changed."""
    if a.failure:
        return a.failure.partition(":")[0]
    if not a.ok:
        return f"error {a.label}"
    if a.reached is None:
        return "ok elsewhere"
    return "ok outside" if a.reached.startswith("outside:") else "ok in root"


def _matches(e: Expect, a: Attempt) -> bool:
    if e.result == "ok":
        return a.ok and a.identity is not None and a.identity == a.expected_ids.get(e.file or "")
    if e.result == "refused":
        return not a.ok
    return not a.ok and a.label is not None and client.same_error(a.label, e.errno or "")


def judge(fx: Fixture, semantics: Semantics, a: Attempt) -> tuple[Verdict, str]:
    """The verdict, and a short reason such as "wanted refused, got ok outside"."""
    expects = fx.expect if semantics == "beneath" else fx.expect_in_root
    got = f"got {_got(a)}"
    wanted = f"wanted {' or '.join(_wanted(e) for e in expects)}"
    if a.failure:
        return "fail", f"{wanted}, {got}"
    if any(p not in fx.outside_changes for p in a.outside_changed):
        return "fail", f"{wanted}, {got}, wrote outside"
    if any(_matches(e, a) for e in expects):
        return "pass", got
    return "fail", f"{wanted}, {got}"


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
    if why:
        return result(verdict="skip", reason=why, attempt=None)
    a = attempt(task)
    verdict, reason = judge(fx, task.semantics, a)
    return result(verdict=verdict, reason=reason, attempt=a)


# --- the suite -----------------------------------------------------------------


def plan(
    drivers: list[client.Driver],
    fixtures: list[Fixture],
    strategies: set[str] | None,
    base: str,
    timeout: float,
    *,
    valgrind: bool,
    root_shapes: tuple[RootShape, ...],
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
                        Task(fx, d.name, command, s.name, s.semantics, shape, base, timeout)
                    )
    return tasks, unavailable


def run_all(tasks: list[Task], jobs: int) -> Iterator[Result]:
    """Run the tasks in parallel. Results come back in task order, each as soon
    as it and every task before it have finished."""
    # forkserver: workers start single-threaded, so the namespace helper may
    # fork and unshare inside them.
    ctx = multiprocessing.get_context("forkserver")
    with ProcessPoolExecutor(max_workers=jobs, mp_context=ctx) as pool:
        yield from pool.map(run_task, tasks, chunksize=1)


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
        "started": started.isoformat(timespec="seconds"),
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
