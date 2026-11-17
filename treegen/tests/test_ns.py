"""The namespace helper that mount fixtures are built and run in."""

import os
import subprocess
import sys

import pytest
from treegen import ns

needs_mounts = pytest.mark.skipif(not ns.mounts_available(), reason=ns.why_no_mounts() or "")


@needs_mounts
def test_runs_as_pid_1_with_our_own_ids():
    ids = ns.run_in_namespace(lambda: (os.getpid(), os.getuid(), os.getgid()))
    assert ids == (1, os.getuid(), os.getgid())


@needs_mounts
def test_exceptions_come_back_with_their_traceback():
    def boom():
        raise ValueError("inside")

    with pytest.raises(ValueError, match="inside") as info:
        ns.run_in_namespace(boom)
    assert any("namespace child" in note for note in info.value.__notes__)


@needs_mounts
@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses permission checks anyway")
def test_programs_run_inside_keep_permission_checks(tmp_path):
    """Not mapped to root, so an exec'd strategy still gets EACCES."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        cmd = ["ls", str(locked)]
        rc = ns.run_in_namespace(lambda: subprocess.run(cmd, capture_output=True).returncode)
    finally:
        locked.chmod(0o700)
    assert rc != 0


@needs_mounts
def test_mounts_stay_inside(tmp_path):
    target = tmp_path / "t"
    target.mkdir()
    dev = ns.run_in_namespace(lambda: (ns.mount("tmpfs", target, "tmpfs"), target.stat().st_dev))
    assert dev[1] != tmp_path.stat().st_dev  # mounted in there
    assert target.stat().st_dev == tmp_path.stat().st_dev  # not out here


@pytest.mark.skipif(sys.platform != "linux", reason="the switches are Linux sysctls")
@pytest.mark.parametrize(
    "sysctls, hint",
    [
        (
            {"kernel.apparmor_restrict_unprivileged_userns": "1"},
            "apparmor_restrict_unprivileged_userns=0",
        ),
        ({"kernel.unprivileged_userns_clone": "0"}, "unprivileged_userns_clone=1"),
        ({"user.max_user_namespaces": "0"}, "max_user_namespaces=15000"),
    ],
)
def test_blocking_switch_is_named_with_its_fix(sysctls, hint):
    blocker = ns.known_blocker(sysctls.get)
    assert blocker is not None and hint in blocker


@pytest.mark.skipif(sys.platform != "linux", reason="the switches are Linux sysctls")
@pytest.mark.parametrize("sysctls", [{}, {"kernel.unprivileged_userns_clone": "1"}])
def test_allowing_switches_are_no_blocker(sysctls):
    assert ns.known_blocker(sysctls.get) is None
