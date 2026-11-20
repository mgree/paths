"""The runner's side of PROTOCOL.md: the driver registry, invoking a driver,
and parsing what it prints.

Parsing is strict. Anything a driver prints that the protocol does not allow,
and any exit status but 0, is a driver failure, never an outcome.
"""

from __future__ import annotations

import errno
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from treegen.spec import SEMANTICS, Op, Semantics

REPO = Path(__file__).resolve().parents[1]
REGISTRY = Path(__file__).with_name("drivers.toml")

# The sys.platform values that Python documents. On other Unix systems, and on
# FreeBSD before Python 3.14, the value has the OS's major version appended, so
# hosts are matched by prefix. https://docs.python.org/3/library/sys.html#sys.platform
PLATFORMS = (
    "aix",
    "android",
    "cygwin",
    "darwin",
    "emscripten",
    "freebsd",
    "ios",
    "linux",
    "wasi",
    "win32",
)


class ProtocolError(Exception):
    """A driver printed something the protocol does not allow."""


@dataclass(frozen=True)
class Driver:
    name: str
    build: tuple[str, ...]
    command: tuple[str, ...]  # for timing
    check_command: tuple[str, ...]  # for correctness
    platforms: tuple[str, ...]

    def runs_here(self) -> bool:
        return any(sys.platform.startswith(p) for p in self.platforms)


def load_drivers(path: Path = REGISTRY) -> list[Driver]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    drivers = []
    for name, t in data.items():
        if "platforms" not in t:
            raise ValueError(f"{path}: [{name}] has no platforms")
        for platform in t["platforms"]:
            if platform not in PLATFORMS:
                raise ValueError(
                    f"{path}: [{name}] platform {platform!r} is not a sys.platform value"
                    f" (one of {', '.join(PLATFORMS)})"
                )
        command = tuple(t["command"])
        drivers.append(
            Driver(
                name=name,
                build=tuple(t.get("build", ())),
                command=command,
                check_command=tuple(t.get("check_command", command)),
                platforms=tuple(t["platforms"]),
            )
        )
    return drivers


def build(driver: Driver) -> None:
    if driver.build:
        subprocess.run(driver.build, cwd=REPO, check=True, capture_output=True)


def absolute(command: tuple[str, ...]) -> list[str]:
    """The command with its program resolved against the repository root."""
    program = Path(command[0])
    if not program.is_absolute() and "/" in command[0]:
        program = REPO / program
    return [str(program), *command[1:]]


# --- what drivers print --------------------------------------------------------


@dataclass(frozen=True)
class StrategyInfo:
    name: str
    semantics: Semantics
    unavailable: str | None = None  # why this host cannot run it


@dataclass(frozen=True)
class Outcome:
    ok: bool
    count: int
    dev: int | None = None  # set for ok outcomes from once and race, which fstat the file
    ino: int | None = None
    label: str | None = None  # error, normalized (see normalize_label)

    @property
    def identity(self) -> tuple[int, int] | None:
        return None if self.dev is None or self.ino is None else (self.dev, self.ino)


@dataclass(frozen=True)
class Run:
    """Output from a driver invocation: either a failure, or outcomes."""

    failure: str | None = None  # e.g. "timeout", "exit status 2", "bad output: ..."
    outcomes: tuple[Outcome, ...] = ()
    time_ns: int | None = None
    hist: tuple[tuple[int, int], ...] = ()  # (bucket lower bound, count)
    stderr: str = field(default="", compare=False)


def normalize_label(label: str) -> str:
    """Return one name for each errno on this host, so that aliases such as
    EWOULDBLOCK and EAGAIN compare equal. A label like E11 gets its name if the
    host has one. Other labels are returned unchanged."""
    value = getattr(errno, label, None) if label.startswith("E") else None
    m = re.fullmatch(r"E(\d+)", label)
    if m:
        value = int(m.group(1))
    if isinstance(value, int) and value in errno.errorcode:
        return errno.errorcode[value]
    return label


def same_error(label: str, expected: str) -> bool:
    return normalize_label(label) == normalize_label(expected)


_NUM = r"(0|[1-9][0-9]*)"
_OK_ID = re.compile(rf"OUTCOME {_NUM} ok {_NUM} {_NUM}")
_OK = re.compile(rf"OUTCOME {_NUM} ok")
_ERROR = re.compile(rf"OUTCOME {_NUM} error (\S+)")
_TIME = re.compile(rf"TIME {_NUM}")
_HIST = re.compile(rf"HIST {_NUM} {_NUM}")
_SEMANTICS = "|".join(SEMANTICS)
_STRATEGY = re.compile(rf"STRATEGY ([a-z0-9-]+) ({_SEMANTICS})")
_UNAVAILABLE = re.compile(rf"UNAVAILABLE ([a-z0-9-]+) ({_SEMANTICS}) (.+)")


def parse_run(stdout: str, mode: str) -> Run:
    """Parse the output of once, race or time. Raise ProtocolError if the
    output breaks the protocol."""
    outcomes: list[Outcome] = []
    time_ns, hist = None, []
    for line in stdout.splitlines():
        if m := _OK_ID.fullmatch(line):
            outcomes.append(Outcome(True, int(m[1]), dev=int(m[2]), ino=int(m[3])))
        elif m := _OK.fullmatch(line):
            outcomes.append(Outcome(True, int(m[1])))
        elif m := _ERROR.fullmatch(line):
            outcomes.append(Outcome(False, int(m[1]), label=normalize_label(m[2])))
        elif (m := _TIME.fullmatch(line)) and time_ns is None:
            time_ns = int(m[1])
        elif m := _HIST.fullmatch(line):
            hist.append((int(m[1]), int(m[2])))
        else:
            raise ProtocolError(f"unexpected line {line!r}")
    if not outcomes or any(o.count == 0 for o in outcomes):
        raise ProtocolError("no outcome")
    identified = mode in ("once", "race")
    for o in outcomes:
        if o.ok and (o.identity is not None) != identified:
            raise ProtocolError(
                f"{mode}: ok outcomes {'need' if identified else 'take no'} dev ino"
            )
    calls = sum(o.count for o in outcomes)
    if mode == "once" and calls != 1:
        raise ProtocolError("once makes exactly one call")
    if (time_ns is None) != (mode == "race"):
        raise ProtocolError(f"{mode}: TIME is {'not ' if mode == 'race' else ''}expected")
    if bool(hist) != (mode == "time"):
        raise ProtocolError(f"{mode}: HIST lines are {'' if mode == 'time' else 'not '}expected")
    if mode == "time" and sum(c for _, c in hist) != calls:
        raise ProtocolError("HIST counts do not add up to the OUTCOME counts")
    return Run(outcomes=tuple(outcomes), time_ns=time_ns, hist=tuple(hist))


def parse_list(stdout: str) -> list[StrategyInfo]:
    """Parse the output of list. Raise ProtocolError if the output breaks the
    protocol."""
    out = []
    for line in stdout.splitlines():
        if m := _STRATEGY.fullmatch(line):
            out.append(StrategyInfo(m[1], cast(Semantics, m[2])))
        elif m := _UNAVAILABLE.fullmatch(line):
            out.append(StrategyInfo(m[1], cast(Semantics, m[2]), m[3]))
        else:
            raise ProtocolError(f"unexpected line {line!r}")
    names = [s.name for s in out]
    if len(set(names)) != len(names):
        raise ProtocolError("a strategy is listed twice")
    return out


# --- invoking drivers ----------------------------------------------------------


def list_strategies(command: tuple[str, ...], timeout: float = 30) -> list[StrategyInfo]:
    r = subprocess.run(
        [*absolute(command), "list"],
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        raise ProtocolError(f"list: exit status {r.returncode}: {r.stderr.strip()}")
    return parse_list(r.stdout)


def invoke(
    command: tuple[str, ...], mode: str, args: list[str], *, cwd: Path, timeout: float
) -> Run:
    """Run one driver command. Failures and crashes return Run(failure=...)."""
    try:
        r = subprocess.run(
            [*absolute(command), mode, *args],
            capture_output=True,
            cwd=cwd,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        return Run(failure="timeout", stderr=_text(e.stderr))
    stderr = _text(r.stderr)
    if r.returncode < 0:
        return Run(failure=f"signal {-r.returncode}", stderr=stderr)
    if r.returncode != 0:
        return Run(failure=f"exit status {r.returncode}", stderr=stderr)
    try:
        run = parse_run(_text(r.stdout), mode)
    except ProtocolError as e:
        return Run(failure=f"bad output: {e}", stderr=stderr)
    return Run(outcomes=run.outcomes, time_ns=run.time_ns, hist=run.hist, stderr=stderr)


def once(
    command: tuple[str, ...],
    strategy: str,
    op: Op,
    root: str,  # passed as spelled: a Path would drop `.` and `//`
    path: str,
    *,
    cwd: Path,
    timeout: float,
) -> Run:
    return invoke(command, "once", [strategy, op, root, path], cwd=cwd, timeout=timeout)


def _text(b: bytes | str | None) -> str:
    if b is None:
        return ""
    return b if isinstance(b, str) else b.decode(errors="replace")
