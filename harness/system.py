"""This module checks that a benchmark is running in bench mode, and describes
the machine for the results file.

tools/benchmode sets up bench mode, and `benchmode run` starts a command
inside the cgroup paths-bench, which can use only the CPUs benchmode set
aside. A process can be in that cgroup only if benchmode put it there, so
being in the cgroup is the check.

Like benchmode, this module works only on Linux."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

CGROUP_NAME = "paths-bench"

# The fields of pyperf's metadata that describe the machine a benchmark ran on.
# cpu_affinity appears only when this process may not use every CPU.
PYPERF_FIELDS = (
    "hostname",
    "platform",
    "cpu_model_name",
    "cpu_count",
    "cpu_affinity",
    "cpu_config",
    "cpu_freq",
    "cpu_temp",
    "load_avg_1min",
    "runnable_threads",
    "aslr",
)


def utc_now() -> str:
    """Return the time now in UTC, to the second, in ISO 8601 form, such as
    2026-11-23T20:56:06Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def in_bench_cgroup() -> bool:
    """Return whether this process is in the cgroup paths-bench."""
    try:
        cgroup = Path("/proc/self/cgroup").read_text().strip()
    except OSError:
        return False
    return cgroup == f"0::/{CGROUP_NAME}"


def _pyperf_commit() -> dict:
    """Return the repository and commit pyperf was installed from. pyperf's own
    metadata gives only its version number, which doesn't change between
    releases. Return an empty dictionary if pyperf wasn't installed from git."""
    direct = importlib.metadata.distribution("pyperf").read_text("direct_url.json")
    url = json.loads(direct) if direct else {}
    if "vcs_info" not in url:
        return {}
    return {"url": url["url"], "commit": url["vcs_info"]["commit_id"]}


def describe(pinned: bool) -> dict:
    """Describe the machine for the results file. Pass pinned=True only when
    the run is in bench mode.

    Most of the description comes from pyperf's collect_metadata, for the CPUs
    this process may run on, keeping only the fields in PYPERF_FIELDS. To that
    it adds the time, whether the run was pinned, the kernel command line,
    which shows the rcu_nocbs= setting bench mode depends on, and the commit
    pyperf came from."""
    time = utc_now()
    cpus = ",".join(map(str, sorted(os.sched_getaffinity(0))))
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "metadata.json"
        subprocess.run(
            [sys.executable, "-m", "pyperf", "collect_metadata", "--affinity", cpus, "-o", out],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        metadata = json.loads(out.read_text())["metadata"]
    return {
        "time": time,
        "pinned": pinned,
        "cmdline": Path("/proc/cmdline").read_text().strip(),
        "pyperf": _pyperf_commit(),
        **{field: metadata[field] for field in PYPERF_FIELDS if field in metadata},
    }


def require_bench_mode(unpinned: bool) -> dict:
    """Stop the run, and explain why, if this process isn't in bench mode. An
    unpinned run goes ahead anyway. Either way, return the description of the
    machine for the results file."""
    if unpinned:
        return describe(pinned=False)
    if not in_bench_cgroup():
        raise SystemExit(
            f"This process isn't in the cgroup {CGROUP_NAME}, so it isn't in bench mode. "
            "Start bench mode with 'sudo tools/benchmode on' and run under "
            "'sudo tools/benchmode run -- ...', or pass --unpinned to time anyway "
            "and mark the results as unpinned."
        )
    return describe(pinned=True)
