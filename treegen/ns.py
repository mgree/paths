"""Linux namespaces and mounts, for fixtures that declare [[mounts]].

Mounting needs CAP_SYS_ADMIN. An unprivileged process gets it inside a new
user namespace, so mount fixtures are built (and their strategies run) in a
forked child that owns new user, mount and PID namespaces.

The child itself holds every capability in its namespace, so it can mount, and
it also bypasses permission checks on our own files. Strategies must therefore
run as programs it executes, never as code inside it: the child maps our uid
and gid to themselves, not to root, so an executed program drops the
capabilities and gets ordinary permission checks (EACCES on a mode-000
directory).
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import pickle
import struct
import sys
import tempfile
import traceback
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import NoReturn, TypeVar

MS_BIND = 0x1000
MS_REC = 0x4000
MS_PRIVATE = 1 << 18
MNT_DETACH = 2

FS_IOC_GETFLAGS = 0x80086601
FS_IOC_SETFLAGS = 0x40086602
FS_CASEFOLD_FL = 0x40000000

# tmpfs takes the encoding with a Unicode version; plain "utf8" is rejected.
CASEFOLD_OPTIONS = "casefold=utf8-12.1.0"

_libc = ctypes.CDLL(None, use_errno=True)


def _arg(x: str | Path | None) -> bytes | None:
    return None if x is None else os.fsencode(x)


def mount(
    source: str | Path | None,
    target: str | Path,
    fstype: str | None,
    flags: int = 0,
    data: str | None = None,
) -> None:
    r = _libc.mount(_arg(source), _arg(target), _arg(fstype), ctypes.c_ulong(flags), _arg(data))
    if r != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"mount {fstype or source} on {target}: {os.strerror(e)}")


def umount(target: str | Path) -> None:
    if _libc.umount2(_arg(target), MNT_DETACH) != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"umount {target}: {os.strerror(e)}")


def _flags(fd: int) -> int:
    return struct.unpack("i", fcntl.ioctl(fd, FS_IOC_GETFLAGS, bytes(4)))[0]


def set_casefold(fd: int) -> None:
    """Make an empty directory case-insensitive (chattr +F)."""
    fcntl.ioctl(fd, FS_IOC_SETFLAGS, struct.pack("i", _flags(fd) | FS_CASEFOLD_FL))


def is_casefold(fd: int) -> bool:
    return bool(_flags(fd) & FS_CASEFOLD_FL)


T = TypeVar("T")


def run_in_namespace(fn: Callable[..., T], *args) -> T:
    """Run fn(*args) as PID 1 of new user, mount and PID namespaces; return its
    result, or raise its exception here. The result must pickle; an exception
    that does not comes back as a RuntimeError holding its traceback."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: never returns
        os.close(r)
        _child(w, fn, args)
    os.close(w)
    with os.fdopen(r, "rb") as f:
        data = f.read()
    _, status = os.waitpid(pid, 0)
    if not data:
        raise RuntimeError(f"namespace child exited without a result (status {status})")
    ok, value = pickle.loads(data)
    if ok:
        return value
    raise value


def _child(w: int, fn: Callable, args: tuple) -> None:
    code = 1
    try:
        uid, gid = os.getuid(), os.getgid()
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS | os.CLONE_NEWPID)
        Path("/proc/self/setgroups").write_text("deny")
        Path("/proc/self/uid_map").write_text(f"{uid} {uid} 1")
        Path("/proc/self/gid_map").write_text(f"{gid} {gid} 1")
        mount(None, "/", None, MS_REC | MS_PRIVATE)  # nothing propagates out
        pid = os.fork()  # the grandchild is PID 1 of the new PID namespace
        if pid == 0:
            try:
                result = fn(*args)
            except BaseException as e:
                _send_error(w, e)
            _send(w, (True, result))
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
    except BaseException as e:
        _send_error(w, e)
    finally:
        os._exit(code)


def _send(w: int, payload: tuple[bool, object]) -> NoReturn:
    """Write the payload for run_in_namespace, then exit this process."""
    try:
        data = pickle.dumps(payload)
    except Exception as e:
        _send_error(w, e)
    with os.fdopen(w, "wb") as f:
        f.write(data)
    os._exit(0)


def _send_error(w: int, e: BaseException) -> NoReturn:
    tb = "".join(traceback.format_exception(e))
    e.add_note("raised in the namespace child:\n" + tb)
    try:
        data = pickle.dumps((False, e))
    except Exception:
        data = pickle.dumps((False, RuntimeError(tb)))
    with os.fdopen(w, "wb") as f:
        f.write(data)
    os._exit(0)


def _probe() -> None:
    """Every mount type the fixtures use, on scratch directories."""
    with tempfile.TemporaryDirectory() as d:
        bind, proc, ci = (Path(d, name) for name in ("bind", "proc", "ci"))
        for target in (bind, proc, ci):
            target.mkdir()
        mount(d, bind, None, MS_BIND)
        mount("proc", proc, "proc")
        mount("tmpfs", ci, "tmpfs", 0, CASEFOLD_OPTIONS)
        for target in (bind, proc, ci):
            umount(target)


# Kernel switches that block unprivileged user namespaces: (sysctl, the value
# that blocks, what it means). The fix is to set the other value.
BLOCKING_SYSCTLS = (
    ("kernel.apparmor_restrict_unprivileged_userns", "1", "0", "AppArmor restricts them"),
    ("kernel.unprivileged_userns_clone", "0", "1", "they are switched off"),
    ("user.max_user_namespaces", "0", "15000", "their limit is zero"),
)


def _read_sysctl(name: str) -> str | None:
    try:
        return Path("/proc/sys", *name.split(".")).read_text().strip()
    except OSError:
        return None


def known_blocker(read_sysctl: Callable[[str], str | None] = _read_sysctl) -> str | None:
    """A reason mounting cannot work that is visible without trying, with the
    fix where there is one; None if nothing rules it out."""
    if sys.platform != "linux":
        return "user namespaces are Linux-only"
    if not Path("/proc/self/ns/user").exists():
        return "this kernel has no user namespaces"
    for name, blocking, allowing, what in BLOCKING_SYSCTLS:
        if read_sysctl(name) == blocking:
            return (
                f"unprivileged user namespaces are blocked: {what}. To allow them until "
                f"reboot: sudo sysctl -w {name}={allowing}"
            )
    return None


@cache
def why_no_mounts() -> str | None:
    """None if this host lets an unprivileged process mount in a user namespace;
    otherwise why not. Known blockers are checked first; only if none applies
    do we try."""
    blocker = known_blocker()
    if blocker is not None:
        return blocker
    try:
        run_in_namespace(_probe)
        return None
    except OSError as e:
        return f"cannot mount in an unprivileged user namespace: {str(e).splitlines()[0]}"


def mounts_available() -> bool:
    return why_no_mounts() is None
