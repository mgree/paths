"""Check that every driver that runs on this host follows the protocol, using
both of its commands. The rules tested here come from PROTOCOL.md, not from
any one driver."""

import os
from datetime import timedelta

import pytest
from protocol import client
from treegen.build import Sandbox


def _commands(d: client.Driver):
    yield pytest.param(d, d.command, id=d.name)
    if d.check_command != d.command:
        yield pytest.param(d, d.check_command, id=f"{d.name}-check")


TIMEOUT = timedelta(seconds=10)

COMMANDS = [p for d in client.load_drivers() if d.runs_here() for p in _commands(d)]


def some_strategy(driver: client.Driver, command: tuple[str, ...]) -> str:
    """Return any strategy this host can run. The tests don't depend on which
    one."""
    client.build(driver)
    return next(s.name for s in client.list_strategies(command) if s.unavailable is None)


def bucket(v: int) -> int:
    """Compute the bucket rule from PROTOCOL.md. It is written separately from
    the drivers' code, so the test doesn't just compare a driver with itself."""
    if v < 16:
        return v
    width = 1 << (v.bit_length() - 5)
    return v - v % width


@pytest.mark.parametrize("driver, command", COMMANDS)
def test_every_mode_speaks_the_protocol(tmp_path, driver, command):
    strategy = some_strategy(driver, command)
    with Sandbox(tmp_path) as sb:
        (sb.root / "f").write_text("x")
        ino = os.stat(sb.root / "f").st_ino
        args = [strategy, "read", str(sb.root), "f"]
        one = client.invoke(command, "once", args, cwd=tmp_path, timeout=TIMEOUT)
        assert one.failure is None and one.outcomes[0].ino == ino and one.time_ns
        race = client.invoke(command, "race", ["0.05", *args], cwd=tmp_path, timeout=TIMEOUT)
        assert race.failure is None and {o.ino for o in race.outcomes} == {ino}


@pytest.mark.parametrize("warmup_passes", [0, 2])
@pytest.mark.parametrize("driver, command", COMMANDS)
def test_time_calls_every_request_once_a_pass(tmp_path, driver, command, warmup_passes):
    """With requests that reach two files and one that is missing, each
    outcome's count is the number of passes times the requests that have it,
    whatever order the shuffle puts them in, with or without a warm-up, which
    isn't counted."""
    strategy = some_strategy(driver, command)
    with Sandbox(tmp_path) as sb:
        (sb.root / "f").write_text("x")
        (sb.root / "g").write_text("y")
        requests = tmp_path / "requests"
        client.write_requests(requests, ["f", "missing", "g", "f"])
        run = client.time(
            command,
            warmup_passes,
            5,  # passes
            7,  # seed
            strategy,
            "read",
            str(sb.root),
            requests,
            cwd=tmp_path,
            timeout=TIMEOUT,
        )
        assert run.failure is None and run.time_ns
        assert {(o.ok, o.count) for o in run.outcomes} == {(True, 15), (False, 5)}
        for o in run.outcomes:
            for lower, _ in o.hist:  # every bucket is named by its own lower bound
                assert bucket(lower) == lower


MISUSES = {
    "unknown-strategy": lambda s: ["once", "no-such-strategy", "read", "/", "x"],
    "unknown-op": lambda s: ["once", s, "open", "/", "x"],
    "relative-root": lambda s: ["once", s, "read", "relative", "x"],
    "newfile-in-race": lambda s: ["race", "1", s, "newfile", "/", "x"],
}


@pytest.mark.parametrize("misuse", MISUSES.values(), ids=MISUSES.keys())
@pytest.mark.parametrize("driver, command", COMMANDS)
def test_misuse_is_a_failure_not_an_outcome(tmp_path, driver, command, misuse):
    """A driver given bad arguments must exit with a nonzero status, explain the
    problem on stderr, and report no outcome. The protocol doesn't say which
    nonzero status."""
    mode, *args = misuse(some_strategy(driver, command))
    run = client.invoke(command, mode, args, cwd=tmp_path, timeout=TIMEOUT)
    assert run.failure is not None and run.failure.startswith("exit status ")
    assert not run.outcomes and run.stderr
