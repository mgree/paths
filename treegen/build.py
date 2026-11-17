"""Build trees on disk, scan them back, and render canonical listings.

All filesystem work goes through directory descriptors, one component at a
time, so trees deeper than PATH_MAX build and scan like any other.
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

from .spec import Entry, Fixture, Tree, split_ref

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


def expand(text: str, root: Path, sandbox: Path) -> str:
    """Replace the {root} and {sandbox} placeholders in symlink text."""
    return text.replace("{root}", str(root)).replace("{sandbox}", str(sandbox))


def unexpand(text: str, root: Path, sandbox: Path) -> str:
    """Put the placeholders back; the root, being longer, wins over the sandbox."""
    for real, ph in ((str(root), "{root}"), (str(sandbox), "{sandbox}")):
        if text == real or text.startswith(real + "/"):
            return ph + text[len(real) :]
    return text


class Sandbox:
    """A fresh directory holding <root_name>/ and the outside entries."""

    def __init__(self, base: str | Path | None = None, root_name: str = "root"):
        self.path = Path(tempfile.mkdtemp(prefix="pt-", dir=base)).resolve()
        self.root_name = root_name
        self.root = self.path / root_name
        os.mkdir(self.root, 0o755)
        os.chmod(self.root, 0o755)

    def remove(self) -> None:
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


def _unlock(stack: ExitStack, dir_fd: int, name: str, need: int, grant: int) -> int:
    """If name lacks the owner bits in need, add grant, and have stack restore
    the old mode on exit. Returns the old mode."""
    mode = stat.S_IMODE(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    if mode & need != need:
        os.chmod(name, mode | grant, dir_fd=dir_fd)
        stack.callback(os.chmod, name, mode, dir_fd=dir_fd)
    return mode


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


def file_contents(fixture: str, tree: str, path: str) -> bytes:
    """One line, unique across the suite: '<fixture> <tree>:<path>'."""
    return f"{fixture} {tree}:{path}\n".encode()


def build_trees(fds: dict[str, int], fx: Fixture, sb: Sandbox) -> None:
    """Create both trees in phases, one kind of entry at a time across both:
    every directory and file exists before any hard link names it as a source,
    whichever tree it is in. fds maps "root" and "outside" to their bases."""
    trees = (("outside", fx.outside), ("root", fx.root))
    for kind in ("dir", "file", "hardlink", "symlink"):
        for label, tree in trees:
            for e in tree.sorted():  # parents sort before children
                if e.kind == kind:
                    _create(fds, label, e, fx.name, sb)


def _create(fds: dict[str, int], label: str, e: Entry, fixture: str, sb: Sandbox) -> None:
    head, name = _split(e.path)
    with open_dir(fds[label], head) as fd:
        if e.kind == "dir":
            os.mkdir(name, 0o700, dir_fd=fd)
        elif e.kind == "file":
            with opened(name, NEW_FILE_FLAGS, 0o600, dir_fd=fd) as ffd:
                os.write(ffd, file_contents(fixture, label, e.path))
        elif e.kind == "hardlink":
            src_tree, src = split_ref(e.link)
            src_head, src_name = _split(src)
            with open_dir(fds[src_tree], src_head) as sfd:
                os.link(src_name, name, src_dir_fd=sfd, dst_dir_fd=fd, follow_symlinks=False)
        else:
            os.symlink(expand(e.link, sb.root, sb.path), name, dir_fd=fd)


def apply_modes(base_fd: int, tree: Tree) -> None:
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
            build_trees({"outside": top, "root": root}, fx, sb)
            apply_modes(top, fx.outside)
            apply_modes(root, fx.root)
        stack.pop_all()  # success: keep the sandbox, now the caller's
        return sb


# --- listings -------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    tree: str  # "outside" or "root"
    path: str
    kind: str  # dir, file, symlink, hardlink
    mode: int | None
    target: str | None  # symlink text (placeholder form) or hard-link source (<tree>:<path>)

    def line(self) -> str:
        k = {"dir": "d", "file": "f", "symlink": "l", "hardlink": "h"}[self.kind]
        m = "----" if self.mode is None else f"{self.mode:04o}"
        s = f"{self.tree:<7} {k} {m}  {self.path}"
        if self.kind == "symlink":
            s += f" -> {self.target}"
        elif self.kind == "hardlink":
            s += f" == {self.target}"
        return s


def _ref(tree: str, path: str) -> str:
    return f"{tree}:{path}"


def _canonical_hardlinks(
    records: list[Record], groups: list[list[tuple[str, str]]]
) -> list[Record]:
    """Within each group of names for one inode, the first in order is the file."""
    by_key = {(r.tree, r.path): r for r in records}
    for group in groups:
        group = sorted(group)
        first = by_key[group[0]]
        mode = next((by_key[k].mode for k in group if by_key[k].kind == "file"), first.mode)
        by_key[group[0]] = Record(first.tree, first.path, "file", mode, None)
        for k in group[1:]:
            by_key[k] = Record(k[0], k[1], "hardlink", mode, _ref(*group[0]))
    return [by_key[k] for k in sorted(by_key)]


def predict(fx: Fixture) -> list[Record]:
    """The listing a correct build of fx produces, computed from the spec alone."""
    # Placeholders round-trip through made-up paths, as scan's do through real
    # ones, so text such as {sandbox}/root/x predicts scan's {root}/x.
    sandbox = Path("/nonexistent-sandbox")
    root = sandbox / fx.root_name
    records, links = [], []
    for label, tree in (("outside", fx.outside), ("root", fx.root)):
        for e in tree.sorted():
            target = e.target
            if e.kind == "symlink":
                target = unexpand(expand(e.link, root, sandbox), root, sandbox)
            if e.kind == "hardlink":
                links.append(((label, e.path), split_ref(e.link)))
            records.append(Record(label, e.path, e.kind, e.effective_mode(), target))
    groups = _union(links)
    return _canonical_hardlinks(records, groups)


def _union(pairs: list[tuple[tuple, tuple]]) -> list[list[tuple]]:
    parent: dict = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    for a, b in pairs:
        parent[find(a)] = find(b)
    groups: dict = {}
    for x in list(parent):
        groups.setdefault(find(x), []).append(x)
    return list(groups.values())


def scan(sb: Sandbox) -> list[Record]:
    """Read the sandbox back from disk as a listing."""
    records: list[Record] = []
    inodes: dict[tuple[int, int], list[tuple[str, str]]] = {}
    with opened(sb.path, DIR_FLAGS) as top:
        _scan_dir(top, "", sb, records, inodes)
    groups = [g for g in inodes.values() if len(g) > 1]
    return _canonical_hardlinks(records, groups)


def _scan_dir(fd, prefix, sb, records, inodes) -> None:
    for name in sorted(os.listdir(fd)):
        rel = f"{prefix}{name}"
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if rel == sb.root_name or rel.startswith(sb.root_name + "/"):
            tree, path = "root", rel[len(sb.root_name) + 1 :]
        else:
            tree, path = "outside", rel
        mode = stat.S_IMODE(st.st_mode)
        if stat.S_ISLNK(st.st_mode):
            text = os.readlink(name, dir_fd=fd)
            records.append(Record(tree, path, "symlink", None, unexpand(text, sb.root, sb.path)))
        elif stat.S_ISREG(st.st_mode):
            records.append(Record(tree, path, "file", mode, None))
            inodes.setdefault((st.st_dev, st.st_ino), []).append((tree, path))
        elif stat.S_ISDIR(st.st_mode):
            if path:  # the root directory itself is not an entry
                records.append(Record(tree, path, "dir", mode, None))
            with ExitStack() as stack:
                _unlock(stack, fd, name, 0o500, 0o700)
                child = stack.enter_context(opened(name, DIR_FLAGS, dir_fd=fd))
                _scan_dir(child, rel + "/", sb, records, inodes)
        else:
            raise ValueError(f"unexpected file type at {rel!r}: {st.st_mode:o}")


def snapshot_outside(sb: Sandbox) -> dict[str, tuple]:
    """Everything outside the root that a strategy could change: per entry its
    kind, mode, link text and, for files, contents. Compare before and after."""
    snap: dict[str, tuple] = {}
    for r in scan(sb):
        if r.tree != "outside":
            continue
        data = None
        if r.kind in ("file", "hardlink"):
            data = _read_unlocked(sb.path, r.path)
        snap[r.path] = (r.kind, r.mode, r.target, data)
    return snap


def _read_unlocked(top: Path, rel: str) -> bytes:
    """Read a file even under mode-000 directories, restoring their modes."""
    head, name = _split(rel)
    with ExitStack() as stack:  # unwinds innermost first: close, then restore
        fd = stack.enter_context(opened(top, DIR_FLAGS))
        for part in head.split("/") if head else []:
            _unlock(stack, fd, part, 0o500, 0o700)
            fd = stack.enter_context(opened(part, DIR_FLAGS, dir_fd=fd))
        _unlock(stack, fd, name, 0o400, 0o400)
        with opened(name, READ_FLAGS, dir_fd=fd) as ffd:
            return b"".join(iter(lambda: os.read(ffd, 1 << 16), b""))


def listing(records: list[Record]) -> str:
    return "".join(r.line() + "\n" for r in records)
