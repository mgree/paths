"""Tests for the protocol client: strict parsing, label normalization, and
reporting a hang as a timeout. test_conformance.py checks whether drivers
follow the protocol."""

import errno
import os

import pytest
from protocol import client
from treegen.build import Sandbox


@pytest.mark.parametrize(
    "stdout, mode",
    [
        ("OUTCOME 1 ok 29 1234\nTIME 5\n", "once"),
        ("OUTCOME 1 error ENOENT\nTIME 5\n", "once"),
        ("OUTCOME 3 ok 29 1\nOUTCOME 2 error escape\n", "race"),
        ("OUTCOME 4 ok\nTIME 400\nHIST 96 3\nHIST 104 1\n", "time"),
    ],
)
def test_parses_what_the_protocol_allows(stdout, mode):
    run = client.parse_run(stdout, mode)
    assert run.outcomes


@pytest.mark.parametrize(
    "stdout, mode, msg",
    [
        ("", "once", "no outcome"),
        ("OUTCOME 1 ok 29 1234\n", "once", "TIME is expected"),
        ("OUTCOME 2 ok 29 1234\nTIME 5\n", "once", "exactly one call"),
        ("OUTCOME 1 ok\nTIME 5\n", "once", "need dev ino"),
        ("OUTCOME 1 ok 29 1\nTIME 5\nHIST 1 1\n", "once", "HIST lines are not"),
        ("OUTCOME 1 ok 29 1\nTIME 5\n", "race", "TIME is not"),
        ("OUTCOME 4 ok 29 1\nTIME 400\nHIST 96 4\n", "time", "take no dev ino"),
        ("OUTCOME 4 ok\nTIME 400\nHIST 96 3\n", "time", "do not add up"),
        ("OUTCOME 1 ok 29 1\nTIME 5\nhello\n", "once", "unexpected line"),
        ("OUTCOME 01 ok 29 1\nTIME 5\n", "once", "unexpected line"),
        ("OUTCOME 1 error has space\nTIME 5\n", "once", "unexpected line"),
        ("OUTCOME 0 error ENOENT\nTIME 5\n", "once", "no outcome"),
    ],
)
def test_rejects_anything_else(stdout, mode, msg):
    with pytest.raises(client.ProtocolError, match=msg):
        client.parse_run(stdout, mode)


def test_list_parses_both_kinds_of_line():
    out = client.parse_list(
        "STRATEGY walk-beneath beneath\nUNAVAILABLE openat2-in-root in_root no openat2 here\n"
    )
    assert out == [
        client.StrategyInfo("walk-beneath", "beneath"),
        client.StrategyInfo("openat2-in-root", "in_root", "no openat2 here"),
    ]


@pytest.mark.parametrize(
    "stdout, msg",
    [
        ("STRATEGY a beneath\nSTRATEGY a in_root\n", "listed twice"),
        ("STRATEGY a sideways\n", "unexpected line"),
        ("STRATEGY Walk_Beneath beneath\n", "unexpected line"),
        ("UNAVAILABLE a beneath\n", "unexpected line"),  # no reason
    ],
)
def test_list_rejects_anything_else(stdout, msg):
    with pytest.raises(client.ProtocolError, match=msg):
        client.parse_list(stdout)


@pytest.mark.parametrize(
    "table, msg",
    [
        ('[x]\ncommand = ["x"]\n', "has no platforms"),
        ('[x]\ncommand = ["x"]\nplatforms = ["macos"]\n', "not a sys.platform value"),
    ],
)
def test_registry_rejects_a_driver_that_could_never_run(tmp_path, table, msg):
    """A driver with no platforms, or with a name that sys.platform never has,
    would silently never run."""
    path = tmp_path / "drivers.toml"
    path.write_text(table)
    with pytest.raises(ValueError, match=msg):
        client.load_drivers(path)


def test_labels_compare_by_errno_value():
    assert client.same_error("EWOULDBLOCK", "EAGAIN")
    assert client.same_error(f"E{errno.ENOENT}", "ENOENT")
    assert not client.same_error("ENOENT", "ENOTDIR")
    assert client.normalize_label("escape") == "escape"
    assert client.normalize_label("NoSuchFileException") == "NoSuchFileException"
    assert client.normalize_label("E99999") == "E99999"  # no such errno: kept as is


def test_a_hang_is_a_timeout(tmp_path):
    """string-concat blocks when it opens a FIFO that has no writer. This checks
    that the client reports the hang as a timeout."""
    c = next(d for d in client.load_drivers() if d.name == "c")
    client.build(c)
    with Sandbox(tmp_path) as sb:
        os.mkfifo(sb.root / "p")
        run = client.once(
            c.check_command, "string-concat", "read", sb.root, "p", cwd=tmp_path, timeout=0.5
        )
    assert run.failure == "timeout"
