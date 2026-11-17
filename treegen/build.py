"""Build fixtures on disk and scan them back.

All filesystem work goes through directory descriptors, one component at a
time, so trees deeper than PATH_MAX build and scan like any other. Fixtures
with mounts exist only inside the namespace they were built in, so build and
use them inside ns.run_in_namespace.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import ns
from .spec import Entry, Fixture, Tree, split_ref

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK: a FIFO opens at once instead of waiting for a writer (we refuse it after).
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def expand(text: str, root: Path, sandbox: Path) -> str:
    """Replace the {root} and {sandbox} placeholders in symlink text."""
    return text.replace("{root}", str(root)).replace("{sandbox}", str(sandbox))


class Sandbox:
    """A fresh directory holding <root_name>/ and the outside entries."""

    def __init__(self, base: str | Path | None = None, root_name: str = "root"):
        self.path = Path(tempfile.mkdtemp(prefix="pt-", dir=base)).resolve()
        self.root_name = root_name
        self.root = self.path / root_name
        self.mounts: dict[str, str] = {}  # root-relative mount point -> type, in mount order
        os.mkdir(self.root, 0o755)
        os.chmod(self.root, 0o755)

    def remove(self) -> None:
        for at in reversed(list(self.mounts)):  # unmount first: never delete through one
            ns.umount(self.root / at)
        self.mounts.clear()
        _make_traversable(self.path)
        shutil.rmtree(self.path)

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.remove()


@contextmanager
def opened(
    path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
) -> Generator[int]:
    """os.open as a context manager: the descriptor is closed on exit. mode
    matters only with O_CREAT."""
    fd = os.open(path, flags, mode, dir_fd=dir_fd)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def open_dir(base_fd: int, rel: str) -> Generator[int]:
    """Open a directory below base_fd by walking each component, no symlinks."""
    fd = os.dup(base_fd)
    try:
        for part in rel.split("/") if rel else []:
            nfd = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
        yield fd
    finally:
        os.close(fd)


def _unlock(stack: ExitStack, dir_fd: int, name: str, need: int, grant: int) -> None:
    """If name lacks the owner bits in need, add grant, and have stack restore
    the old mode on exit."""
    mode = stat.S_IMODE(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    if mode & need != need:
        os.chmod(name, mode | grant, dir_fd=dir_fd)
        stack.callback(os.chmod, name, mode, dir_fd=dir_fd)


def _make_traversable(top: Path) -> None:
    """Give the owner rwx on every directory, top-down, so rmtree can finish."""
    with opened(top, DIR_FLAGS) as fd:
        _fix_dir(fd)


def _fix_dir(fd: int) -> None:
    for name in os.listdir(fd):
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            os.chmod(name, stat.S_IMODE(st.st_mode) | 0o700, dir_fd=fd)
            with opened(name, DIR_FLAGS, dir_fd=fd) as child:
                _fix_dir(child)


def _split(path: str) -> tuple[str, str]:
    head, _, name = path.rpartition("/")
    return head, name


def _file_contents(fixture: str, tree: str, path: str) -> bytes:
    """One line, unique across the suite: '<fixture> <tree>:<path>'."""
    return f"{fixture} {tree}:{path}\n".encode()


def _build_trees(fds: dict[str, int], fx: Fixture, sb: Sandbox) -> None:
    """Create both trees in phases, one kind of entry at a time across both:
    every directory and file exists before any hard link names it as a source,
    whichever tree it is in. A casefold mount goes on its directory as soon as
    the directory exists, so everything under it is built inside the mount.
    fds maps "root" and "outside" to their bases."""
    casefold = {m.at for m in fx.mounts if m.type == "casefold"}
    trees = (("outside", fx.outside), ("root", fx.root))
    for kind in ("dir", "file", "fifo", "hardlink", "symlink"):
        for label, tree in trees:
            for e in tree.sorted():  # parents sort before children
                if e.kind == kind:
                    _create(fds, label, e, fx.name, sb)
                    if label == "root" and e.path in casefold:
                        _mount_casefold(fds["root"], e.path, sb)


def _create(fds: dict[str, int], label: str, e: Entry, fixture: str, sb: Sandbox) -> None:
    head, name = _split(e.path)
    with open_dir(fds[label], head) as fd:
        if e.kind == "dir":
            os.mkdir(name, 0o700, dir_fd=fd)
        elif e.kind == "file":
            with opened(name, NEW_FILE_FLAGS, 0o600, dir_fd=fd) as ffd:
                os.write(ffd, _file_contents(fixture, label, e.path))
        elif e.kind == "fifo":
            os.mkfifo(name, 0o600, dir_fd=fd)
        elif e.kind == "hardlink":
            src_tree, src = split_ref(e.link)
            src_head, src_name = _split(src)
            with open_dir(fds[src_tree], src_head) as sfd:
                os.link(src_name, name, src_dir_fd=sfd, dst_dir_fd=fd, follow_symlinks=False)
        else:
            os.symlink(expand(e.link, sb.root, sb.path), name, dir_fd=fd)


def _mount_casefold(root_fd: int, at: str, sb: Sandbox) -> None:
    ns.mount("tmpfs", sb.root / at, "tmpfs", 0, ns.CASEFOLD_OPTIONS)
    sb.mounts[at] = "casefold"
    with open_dir(root_fd, at) as fd:  # opened after mounting: the tmpfs's top
        ns.set_casefold(fd)


def _mount_late(fx: Fixture, sb: Sandbox) -> None:
    """Bind and proc mounts go on after modes, so chmod never reaches through
    one to its source."""
    for m in fx.mounts:
        target = sb.root / m.at
        if m.type == "bind":
            assert m.source is not None
            tree, path = m.source
            ns.mount((sb.root if tree == "root" else sb.path) / path, target, None, ns.MS_BIND)
        elif m.type == "proc":
            ns.mount("proc", target, "proc")
        if m.type != "casefold":  # casefold went on during the build
            sb.mounts[m.at] = m.type


def _apply_modes(base_fd: int, tree: Tree) -> None:
    """chmod every non-symlink entry, deepest first, so a 000 directory is last."""
    for e in sorted(tree.entries.values(), key=lambda e: -e.path.count("/")):
        mode = e.effective_mode()
        if mode is None or e.kind == "hardlink":
            continue  # a hard link shares its source's mode
        head, name = _split(e.path)
        with open_dir(base_fd, head) as fd:
            os.chmod(name, mode, dir_fd=fd)


def build_fixture(fx: Fixture, base: str | Path | None = None) -> Sandbox:
    """Build fx in a fresh sandbox. On success the caller owns the sandbox (use
    it as a context manager); if building fails, it is removed."""
    with ExitStack() as stack:
        sb = stack.enter_context(Sandbox(base, fx.root_name))
        with opened(sb.path, DIR_FLAGS) as top, opened(sb.root, DIR_FLAGS) as root:
            _build_trees({"outside": top, "root": root}, fx, sb)
            _apply_modes(top, fx.outside)
            _apply_modes(root, fx.root)
        _mount_late(fx, sb)
        stack.pop_all()  # success: keep the sandbox, now the caller's
        return sb


# --- reading back ---------------------------------------------------------


@dataclass(frozen=True)
class Record:
    """One entry as found on disk."""

    tree: str  # "outside" or "root"
    path: str
    kind: str  # dir, file, fifo, symlink, or bind / proc (a mount point, not descended into)
    mode: int | None  # None for symlinks
    target: str | None  # symlink text as on disk
    ino: tuple[int, int]  # (st_dev, st_ino); hard links share it


def scan(sb: Sandbox) -> list[Record]:
    """Every entry in the sandbox, sorted by (tree, path). Descends into
    casefold mounts (the fixture's own entries are there) but not into bind or
    proc mounts, whose contents are another directory's or the kernel's."""
    records: list[Record] = []
    with opened(sb.path, DIR_FLAGS) as top:
        _scan_dir(top, "", sb, records)
    return sorted(records, key=lambda r: (r.tree, r.path))


def _scan_dir(fd: int, prefix: str, sb: Sandbox, records: list[Record]) -> None:
    for name in sorted(os.listdir(fd)):
        rel = f"{prefix}{name}"
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if rel == sb.root_name or rel.startswith(sb.root_name + "/"):
            tree, path = "root", rel[len(sb.root_name) + 1 :]
        else:
            tree, path = "outside", rel
        mode, ino = stat.S_IMODE(st.st_mode), (st.st_dev, st.st_ino)
        mount = sb.mounts.get(path) if tree == "root" else None
        if stat.S_ISLNK(st.st_mode):
            records.append(Record(tree, path, "symlink", None, os.readlink(name, dir_fd=fd), ino))
        elif stat.S_ISREG(st.st_mode):
            records.append(Record(tree, path, "file", mode, None, ino))
        elif stat.S_ISFIFO(st.st_mode):  # never opened: an open would block
            records.append(Record(tree, path, "fifo", mode, None, ino))
        elif mount in ("bind", "proc"):
            records.append(Record(tree, path, mount, mode, None, ino))
        elif stat.S_ISDIR(st.st_mode):
            if path:  # the root directory itself is not an entry
                records.append(Record(tree, path, "dir", mode, None, ino))
            with ExitStack() as stack:
                _unlock(stack, fd, name, 0o500, 0o700)
                child = stack.enter_context(opened(name, DIR_FLAGS, dir_fd=fd))
                _scan_dir(child, rel + "/", sb, records)
        else:
            raise ValueError(f"unexpected file type at {rel!r}: {st.st_mode:o}")


def snapshot_outside(sb: Sandbox) -> dict[str, tuple]:
    """Everything outside the root that a strategy could change: per entry its
    kind, mode, link text, identity and, for regular files, contents (only
    those are read: opening a FIFO blocks). Compare before and after."""
    snap: dict[str, tuple] = {}
    for r in scan(sb):
        if r.tree != "outside":
            continue
        data = read_unlocked(sb.path, r.path) if r.kind == "file" else None
        snap[r.path] = (r.kind, r.mode, r.target, r.ino, data)
    return snap


def read_unlocked(top: Path, rel: str) -> bytes:
    """Read a regular file even under mode-000 directories, restoring their
    modes. Anything else is refused without blocking."""
    head, name = _split(rel)
    with ExitStack() as stack:  # unwinds innermost first: close, then restore
        fd = stack.enter_context(opened(top, DIR_FLAGS))
        for part in head.split("/") if head else []:
            _unlock(stack, fd, part, 0o500, 0o700)
            fd = stack.enter_context(opened(part, DIR_FLAGS, dir_fd=fd))
        _unlock(stack, fd, name, 0o400, 0o400)
        with opened(name, READ_FLAGS, dir_fd=fd) as ffd:
            if not stat.S_ISREG(os.fstat(ffd).st_mode):
                raise ValueError(f"{rel!r} is not a regular file")
            return b"".join(iter(lambda: os.read(ffd, 1 << 16), b""))
