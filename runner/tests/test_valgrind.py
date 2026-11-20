"""The runner's valgrind mode must catch a leaked descriptor. Valgrind only
counts descriptor leaks as errors in recent versions, so check the one we have
rather than trust it."""

import shutil
import subprocess

import pytest
from protocol import client
from runner import check

pytestmark = pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind not installed")

SOURCE = client.REPO / "strategies" / "c" / "tests" / "fdleak.c"


@pytest.mark.parametrize(
    "args, status, meaning",
    [
        (
            [],  # no arg, fd leaks
            check.VALGRIND_ERROR_EXITCODE,
            "Valgrind on this host doesn't report a leaked descriptor, so the runner's"
            " valgrind mode can't tell us whether the drivers leak descriptors.",
        ),
        (
            ["close"],  # closes the fd, no leak
            0,
            "Valgrind on this host reports an error for a program that leaks nothing,"
            " so the runner's valgrind mode would fail every driver.",
        ),
    ],
    ids=["leaks", "closes"],
)
def test_valgrind_mode_fails_on_a_leaked_descriptor(tmp_path, args, status, meaning):
    assert check.VALGRIND_ERROR_EXITCODE != 0, (
        "--error-exitcode must be nonzero, or errors pass as success"
    )
    fdleak = tmp_path / "fdleak"
    subprocess.run(["cc", "-g", "-o", str(fdleak), str(SOURCE)], check=True)
    r = subprocess.run([*check.VALGRIND, str(fdleak), *args], capture_output=True, text=True)
    assert r.returncode == status, f"{meaning}\n{r.stderr}"
