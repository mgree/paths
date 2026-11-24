"""Tests for tools/benchmode, run with --dry-run against a fake kernel command
line and /sys tree, so they need no root and change nothing. They check how
benchmode chooses the CPUs to set aside; only a real run shows that on, run and
off work."""

import os
import re
import subprocess
from pathlib import Path

import pytest

BENCHMODE = Path(__file__).resolve().parents[1] / "benchmode"


def fake_sys(root: Path, cores: int = 6, online: str | None = None) -> Path:
    """A /sys tree for a machine with two hyperthreads per core, numbered as
    this project's laptop numbers them: core c has CPUs c and c + cores."""
    sys_ = root / "sys"
    cpu_dir = sys_ / "devices" / "system" / "cpu"
    cpu_dir.mkdir(parents=True)
    (cpu_dir / "online").write_text((online or f"0-{2 * cores - 1}") + "\n")
    for cpu in range(2 * cores):
        core = cpu % cores
        topology = cpu_dir / f"cpu{cpu}" / "topology"
        topology.mkdir(parents=True)
        (topology / "thread_siblings_list").write_text(f"{core},{core + cores}\n")
    cgroup = sys_ / "fs" / "cgroup"
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.subtree_control").write_text("cpu memory pids\n")
    return sys_


def benchmode(tmp_path: Path, cmdline: str, *args: str, **env: str):
    sys_ = tmp_path / "sys" if (tmp_path / "sys").exists() else fake_sys(tmp_path)
    (tmp_path / "cmdline").write_text(cmdline + "\n")
    return subprocess.run(
        [str(BENCHMODE), "--dry-run", *args],
        env={
            **os.environ,
            "BENCHMODE_CMDLINE": str(tmp_path / "cmdline"),
            "BENCHMODE_SYS": str(sys_),
            "PYTHON": "python",
            **env,
        },
        capture_output=True,
        text=True,
    )


BOOT = "BOOT_IMAGE=/boot/vmlinuz root=UUID=x ro quiet"


def set_aside(r: subprocess.CompletedProcess[str]) -> str:
    """The CPUs a dry run of on says it sets aside."""
    match = re.search(r"CPUs (\S+) are set aside", r.stdout)
    assert match, r.stdout + r.stderr
    return match.group(1)


@pytest.mark.parametrize("cpus", ["4,5,10,11", "4-5,10-11", "10-11,4,5"])
def test_every_spelling_of_a_cpu_list_sets_aside_the_same_cpus(tmp_path, cpus):
    r = benchmode(tmp_path, f"{BOOT} rcu_nocbs={cpus}", "on")
    assert r.returncode == 0, r.stderr
    assert set_aside(r) == "4,5,10,11"
    assert r.stderr == ""


def test_the_last_rcu_nocbs_wins(tmp_path):
    r = benchmode(tmp_path, f"{BOOT} rcu_nocbs=0-1 rcu_nocbs=5,11", "on")
    assert r.returncode == 0, r.stderr
    assert set_aside(r) == "5,11"


def test_without_rcu_nocbs_on_explains_the_fix(tmp_path):
    r = benchmode(tmp_path, BOOT, "on")
    assert r.returncode == 1
    assert "has no rcu_nocbs=" in r.stderr
    # It suggests both hyperthreads of the two highest-numbered cores.
    assert "rcu_nocbs=4,5,10,11" in r.stderr
    assert "/etc/default/grub.d/rcu-nocbs.cfg" in r.stderr
    assert "--unpinned" in r.stderr
    assert r.stdout == ""


@pytest.mark.parametrize("word", ["rcu_nocbs", "rcu_nocbs=", "rcu_nocbs=all"])
def test_every_cpu_leaves_none_for_the_rest(tmp_path, word):
    r = benchmode(tmp_path, f"{BOOT} {word}", "on")
    assert r.returncode == 1
    assert "every CPU" in r.stderr


def test_naming_every_online_cpu_leaves_none_for_the_rest(tmp_path):
    r = benchmode(tmp_path, f"{BOOT} rcu_nocbs=0-11", "on")
    assert r.returncode == 1
    assert "every online CPU" in r.stderr


def test_a_split_core_is_a_warning(tmp_path):
    r = benchmode(tmp_path, f"{BOOT} rcu_nocbs=4,5", "on")
    assert r.returncode == 0, r.stderr
    assert "its hyperthread sibling, CPU 10, is not" in r.stderr
    assert "its hyperthread sibling, CPU 11, is not" in r.stderr
    assert set_aside(r) == "4,5"


def test_an_offline_cpu_is_an_error(tmp_path):
    fake_sys(tmp_path, online="0-9")
    r = benchmode(tmp_path, f"{BOOT} rcu_nocbs=4,5,10,11", "on")
    assert r.returncode == 1
    assert "CPU 10, which is not online" in r.stderr


@pytest.mark.parametrize("cpus", ["4-x", "5-4", "4:2"])
def test_an_unreadable_cpu_list_is_an_error(tmp_path, cpus):
    r = benchmode(tmp_path, f"{BOOT} rcu_nocbs={cpus}", "on")
    assert r.returncode == 1
    assert "cannot read CPU list" in r.stderr
