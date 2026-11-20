"""Tree and fixture descriptions: loading and validation.

See FORMAT.md for the file format. Everything here is pure data; building on
disk lives in build.py.
"""

from __future__ import annotations

import errno as errno_mod
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

DIR_MODE = 0o755
FILE_MODE = 0o644

KINDS = ("dir", "file", "fifo", "symlink", "hardlink")
# The two trees a fixture declares: the root the strategy is scoped to, and
# the outside around it.
TreeName = Literal["root", "outside"]
TREES: tuple[TreeName, ...] = get_args(TreeName)
REQUIRES = ("nonroot", "userns", "linux")
# read: O_RDONLY, the file must exist. newfile: O_WRONLY|O_CREAT|O_EXCL, the
# name must not exist. write: O_WRONLY|O_CREAT|O_TRUNC, as creat(2) and the
# shell's > do. It creates or truncates the file, and follows a final symlink.
Op = Literal["read", "newfile", "write"]
OPS: tuple[Op, ...] = get_args(Op)
ExpectedResult = Literal["ok", "refused", "error"]
RESULTS: tuple[ExpectedResult, ...] = get_args(ExpectedResult)
# Which expectations judge a strategy: beneath picks [expect], and in_root picks
# [expect.in_root]. The driver protocol's `list` names each strategy's.
Semantics = Literal["beneath", "in_root"]
SEMANTICS: tuple[Semantics, ...] = get_args(Semantics)
# bind: another declared directory's contents; proc: a procfs; casefold: an
# empty case-insensitive tmpfs that the fixture's entries are built inside.
MOUNT_TYPES = ("bind", "proc", "casefold")
# The requirements [[mounts]] implies: an unprivileged process can mount only
# inside a Linux user namespace.
MOUNT_REQUIRES = ("userns", "linux")
# What [race] implies: the racer swaps names with renameat2's RENAME_EXCHANGE.
RACE_REQUIRES = ("linux",)


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    path: str  # normal relative path within its tree
    kind: str  # one of KINDS
    target: str | None = None  # symlink text (verbatim), or hard-link source (<tree>:<path>)
    mode: int | None = None  # None: default for the kind; symlinks have none

    @property
    def link(self) -> str:
        """The target of a symlink or hard link."""
        if self.target is None:
            raise SpecError(f"{self.path!r} ({self.kind}) has no target")
        return self.target

    def effective_mode(self) -> int | None:
        if self.kind == "symlink":
            return None
        if self.mode is not None:
            return self.mode
        return DIR_MODE if self.kind == "dir" else FILE_MODE


@dataclass
class Tree:
    """Entries of one tree (root or outside), keyed by path, parents included."""

    entries: dict[str, Entry] = field(default_factory=dict)

    def add(self, entry: Entry) -> None:
        check_entry_path(entry.path)
        parts = entry.path.split("/")
        for i in range(1, len(parts)):
            self._add_dir(("/".join(parts[:i])))
        old = self.entries.get(entry.path)
        if old is not None and not (old.kind == entry.kind == "dir"):
            raise SpecError(f"{entry.path!r} declared twice ({old.kind}, {entry.kind})")
        self.entries[entry.path] = entry

    def _add_dir(self, path: str) -> None:
        old = self.entries.get(path)
        if old is None:
            self.entries[path] = Entry(path, "dir")
        elif old.kind != "dir":
            raise SpecError(f"{path!r} is a {old.kind} but has children")

    def set_mode(self, path: str, mode: int) -> None:
        old = self.entries.get(path)
        if old is None:
            raise SpecError(f"mode for undeclared path {path!r}")
        if old.kind == "symlink":
            raise SpecError(f"mode on symlink {path!r}")
        if not 0 <= mode <= 0o7777:
            raise SpecError(f"bad mode {mode:o} for {path!r}")
        self.entries[path] = Entry(old.path, old.kind, old.target, mode)

    def sorted(self) -> list[Entry]:
        return [self.entries[p] for p in sorted(self.entries)]


@dataclass(frozen=True)
class Request:
    op: Op
    path: str


@dataclass(frozen=True)
class Expect:
    result: ExpectedResult
    file: str | None = None
    errno: str | None = None


@dataclass(frozen=True)
class Mount:
    at: str  # a declared [root] directory
    type: str  # one of MOUNT_TYPES
    source: tuple[TreeName, str] | None = None  # bind only: (tree, path) of a declared directory


@dataclass
class Fixture:
    name: str
    hazard: str
    about: str
    root_name: str
    requires: tuple[str, ...]
    root: Tree
    outside: Tree
    request: Request
    expect: tuple[Expect, ...]  # accepted outcomes, RESOLVE_BENEATH-style strategies
    expect_in_root: tuple[Expect, ...]  # accepted outcomes, RESOLVE_IN_ROOT-style
    outside_changes: tuple[str, ...] = ()  # [outside] files allowed to change; default none
    mounts: tuple[Mount, ...] = ()  # in declaration order
    race: Race | None = None


@dataclass(frozen=True)
class Race:
    """Two declared entries that trade places, over and over, while the
    strategy runs. Each is (tree, path)."""

    exchange: tuple[tuple[TreeName, str], tuple[TreeName, str]]


def check_entry_path(path: str) -> None:
    if not isinstance(path, str) or not path:
        raise SpecError(f"bad entry path {path!r}")
    for part in path.split("/"):
        if part in ("", ".", ".."):
            raise SpecError(f"entry path {path!r} is not a normal relative path")
        if "\0" in part:
            raise SpecError(f"NUL in entry path {path!r}")


def split_ref(text: str) -> tuple[TreeName, str]:
    """Parse a file reference of the form <tree>:<path>, where tree is root or
    outside."""
    tree, sep, path = text.partition(":")
    if not sep or tree not in TREES:
        raise SpecError(f"{text!r} must be root:<path> or outside:<path>")
    check_entry_path(path)
    return tree, path


def _keys(table: dict, allowed: set[str], where: str) -> None:
    extra = set(table) - allowed
    if extra:
        raise SpecError(f"unknown keys in {where}: {sorted(extra)}")


def _str_list(v, where: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise SpecError(f"{where} must be a list of strings")
    return v


def _str_map(v, where: str) -> dict[str, str]:
    if not isinstance(v, dict) or not all(isinstance(x, str) for x in v.values()):
        raise SpecError(f"{where} must be a table of strings")
    return v


def parse_tree(table: dict, where: str) -> Tree:
    _keys(table, {"dirs", "files", "fifos", "symlinks", "hardlinks", "modes"}, where)
    tree = Tree()
    for p in _str_list(table.get("dirs", []), f"{where}.dirs"):
        tree.add(Entry(p, "dir"))
    for p in _str_list(table.get("files", []), f"{where}.files"):
        tree.add(Entry(p, "file"))
    for p in _str_list(table.get("fifos", []), f"{where}.fifos"):
        tree.add(Entry(p, "fifo"))
    for p, t in _str_map(table.get("symlinks", {}), f"{where}.symlinks").items():
        if not t:
            raise SpecError(f"empty symlink target for {p!r}")
        tree.add(Entry(p, "symlink", t))
    for p, t in _str_map(table.get("hardlinks", {}), f"{where}.hardlinks").items():
        split_ref(t)
        tree.add(Entry(p, "hardlink", t))
    modes = table.get("modes", {})
    if not isinstance(modes, dict) or not all(isinstance(m, int) for m in modes.values()):
        raise SpecError(f"{where}.modes must map paths to integers")
    for p, m in modes.items():
        tree.set_mode(p, m)
    return tree


def parse_expect(table: dict, where: str) -> Expect:
    _keys(table, {"result", "file", "errno"}, where)
    result = table.get("result")
    if result not in RESULTS:
        raise SpecError(f"{where}.result must be one of {RESULTS}")
    file, err = table.get("file"), table.get("errno")
    if result == "ok":
        if file is None or err is not None:
            raise SpecError(f"{where}: result ok needs file and no errno")
        check_entry_path(file)
    elif result == "error":
        if err is None or file is not None:
            raise SpecError(f"{where}: result error needs errno and no file")
        if not hasattr(errno_mod, err):
            raise SpecError(f"{where}: unknown errno {err!r}")
    elif file is not None or err is not None:
        raise SpecError(f"{where}: result refused takes no file or errno")
    return Expect(result, file, err)


def parse_outcomes(table: dict, where: str) -> tuple[Expect, ...]:
    """Parse the expected outcome: one outcome (result, file, errno), or
    several under accept = [...]."""
    if "accept" not in table:
        return (parse_expect(table, where),)
    if set(table) != {"accept"}:
        raise SpecError(f"{where}: accept cannot be mixed with result/file/errno")
    alts = table["accept"]
    if not isinstance(alts, list) or len(alts) < 2 or not all(isinstance(a, dict) for a in alts):
        raise SpecError(f"{where}.accept must list at least two outcome tables")
    out = tuple(parse_expect(a, f"{where}.accept[{i}]") for i, a in enumerate(alts))
    if len(set(out)) != len(out):
        raise SpecError(f"{where}.accept lists an outcome twice")
    return out


def _check_hardlinks(root: Tree, outside: Tree, source: str) -> None:
    """Check that each hard link names a declared file. A link and its source
    are one file with one mode, so a fixture may give the mode on any of its
    names. The mode is copied to all of them, and two different modes for one
    file are an error."""
    trees: dict[TreeName, Tree] = {"root": root, "outside": outside}
    names: dict[tuple[TreeName, str], list[tuple[TreeName, str]]] = {}  # source -> its names
    for label, tree in trees.items():
        for e in tree.entries.values():
            if e.kind == "hardlink":
                src_tree, src = split_ref(e.link)
                src_entry = trees[src_tree].entries.get(src)
                if src_entry is None or src_entry.kind != "file":
                    raise SpecError(f"{source}: hard link {label}:{e.path} needs a file {e.link!r}")
                names.setdefault((src_tree, src), [(src_tree, src)]).append((label, e.path))
    for group in names.values():
        modes = {m for t, p in group if (m := trees[t].entries[p].mode) is not None}
        if len(modes) > 1:
            listed = ", ".join(f"{t}:{p}" for t, p in group)
            raise SpecError(f"{source}: conflicting modes for one file ({listed})")
        if modes:
            mode = modes.pop()
            for t, p in group:
                trees[t].set_mode(p, mode)


def parse_mounts(items, root: Tree, outside: Tree, source: str) -> tuple[Mount, ...]:
    """The [[mounts]] list, checked against the trees it mounts on and from."""
    if not isinstance(items, list) or not all(isinstance(m, dict) for m in items):
        raise SpecError(f"{source}: mounts must be a list of tables ([[mounts]])")
    trees: dict[TreeName, Tree] = {"root": root, "outside": outside}
    out: list[Mount] = []
    for i, m in enumerate(items):
        where = f"{source}: mounts[{i}]"
        _keys(m, {"at", "type", "from"}, where)
        at, kind = m.get("at"), m.get("type")
        if kind not in MOUNT_TYPES:
            raise SpecError(f"{where}: type must be one of {MOUNT_TYPES}")
        entry = root.entries.get(at) if isinstance(at, str) else None
        if entry is None or entry.kind != "dir":
            if not isinstance(at, str):
                why = "missing" if at is None else f"not a string: {at!r}"
            elif entry is not None:
                why = f"{at!r} is a {entry.kind} in [root]"
            elif at in outside.entries:
                why = f"{at!r} is an [outside] entry"
            else:
                why = f"{at!r} is not declared"
            raise SpecError(f"{where}: at must name a declared [root] directory; {why}")
        if any(x.at == at for x in out):
            raise SpecError(f"{where}: {at!r} is mounted twice")
        if kind != "casefold" and any(p.startswith(at + "/") for p in root.entries):
            raise SpecError(f"{where}: entries under {at!r} would be hidden by the mount")
        src = None
        if kind == "bind":
            if not isinstance(m.get("from"), str):
                raise SpecError(f"{where}: a bind mount needs from = <tree>:<path>")
            src = split_ref(m["from"])
            src_entry = trees[src[0]].entries.get(src[1])
            if src_entry is None or src_entry.kind != "dir":
                raise SpecError(f"{where}: from {m['from']!r} is not a declared directory")
        elif "from" in m:
            raise SpecError(f"{where}: only bind mounts take from")
        out.append(Mount(at, kind, src))
    _check_links_stay_on_one_filesystem(out, root, outside, source)
    return tuple(out)


def _filesystem(mounts: Iterable[Mount], tree: TreeName, path: str) -> str | None:
    """The casefold mount a declared entry is on, or None for the sandbox's own
    filesystem. A casefold mount is its own filesystem. Bind and proc mounts
    are too, but no declared entry lies under one. Casefold mounts can nest, so
    the entry is on the innermost one containing it: the longest match."""
    if tree != "root":
        return None
    containing = (
        m.at
        for m in mounts
        if m.type == "casefold" and (path == m.at or path.startswith(m.at + "/"))
    )
    return max(containing, key=len, default=None)


def _check_links_stay_on_one_filesystem(
    mounts: list[Mount], root: Tree, outside: Tree, source: str
) -> None:
    """A hard link cannot cross from one filesystem to another."""
    trees: dict[TreeName, Tree] = {"root": root, "outside": outside}
    for label, tree in trees.items():
        for e in tree.entries.values():
            if e.kind != "hardlink":
                continue
            if _filesystem(mounts, label, e.path) != _filesystem(mounts, *split_ref(e.link)):
                raise SpecError(
                    f"{source}: hard link {label}:{e.path} and its source {e.link!r} are on "
                    "different filesystems (a casefold mount lies between them)"
                )


def parse_race(
    table: object, root: Tree, outside: Tree, mounts: tuple[Mount, ...], source: str
) -> Race:
    """The [race] table, checked against the trees it names entries in, and
    against the exchanges renameat2 refuses."""
    if not isinstance(table, dict):
        raise SpecError(f"{source}: [race] must be a table")
    _keys(table, {"exchange"}, "race")
    names = table.get("exchange")
    if not isinstance(names, list) or len(names) != 2 or not all(isinstance(n, str) for n in names):
        raise SpecError(f"{source}: race.exchange must be two entries, as <tree>:<path>")
    trees: dict[TreeName, Tree] = {"root": root, "outside": outside}
    a, b = (split_ref(n) for n in names)
    for tree, path in (a, b):
        if path not in trees[tree].entries:
            raise SpecError(f"{source}: race.exchange names {tree}:{path}, which is not declared")
    if a == b:
        raise SpecError(f"{source}: race.exchange names {names[0]} twice")
    where = f"{source}: race.exchange cannot swap {names[0]} and {names[1]}"
    for x, y in ((a, b), (b, a)):
        if x[0] == y[0] and y[1].startswith(x[1] + "/"):
            raise SpecError(f"{where}: one contains the other")
    if any(m.at == path for tree, path in (a, b) if tree == "root" for m in mounts):
        raise SpecError(f"{where}: one is a mount point")
    if _filesystem(mounts, *a) != _filesystem(mounts, *b):
        raise SpecError(f"{where}: they are on different filesystems")
    return Race((a, b))


def _accepts_eagain(expects: tuple[Expect, ...]) -> bool:
    return any(e.result == "refused" or e.errno == "EAGAIN" for e in expects)


def parse_fixture(data: dict, name: str, source: str = "<fixture>") -> Fixture:
    """name is the fixture's identifier: its file name without .toml."""
    _keys(
        data,
        {
            "hazard",
            "about",
            "root_name",
            "requires",
            "root",
            "outside",
            "request",
            "expect",
            "mounts",
            "race",
        },
        source,
    )
    for k in ("hazard", "about"):
        if not isinstance(data.get(k), str) or not data[k]:
            raise SpecError(f"{source}: {k} is required")
    root_name = data.get("root_name", "root")
    check_entry_path(root_name)
    if "/" in root_name:
        raise SpecError(f"{source}: root_name must be one component")
    requires = tuple(_str_list(data.get("requires", []), "requires"))
    for r in requires:
        if r not in REQUIRES:
            raise SpecError(f"{source}: unknown requirement {r!r}")
    root = parse_tree(data.get("root", {}), "root")
    outside = parse_tree(data.get("outside", {}), "outside")
    if root_name in outside.entries or any(p.startswith(root_name + "/") for p in outside.entries):
        raise SpecError(f"{source}: [outside] collides with root_name {root_name!r}")
    _check_hardlinks(root, outside, source)
    mounts = parse_mounts(data.get("mounts", []), root, outside, source)
    if mounts:
        requires += tuple(r for r in MOUNT_REQUIRES if r not in requires)
    race = parse_race(data["race"], root, outside, mounts, source) if "race" in data else None
    if race:
        requires += tuple(r for r in RACE_REQUIRES if r not in requires)
        if isinstance(data.get("request"), dict) and data["request"].get("op") == "newfile":
            raise SpecError(f"{source}: a race cannot use newfile, whose second call fails")
    req = data.get("request")
    if not isinstance(req, dict):
        raise SpecError(f"{source}: [request] is required")
    _keys(req, {"op", "path"}, "request")
    if req.get("op") not in OPS or not isinstance(req.get("path"), str):
        raise SpecError(f"{source}: request needs op in {OPS} and a path")
    exp = data.get("expect")
    if not isinstance(exp, dict):
        raise SpecError(f"{source}: [expect] is required")
    in_root_table = exp.get("in_root")
    if in_root_table is not None and not isinstance(in_root_table, dict):
        raise SpecError(f"{source}: expect.in_root must be a table")
    changes = tuple(_str_list(exp.get("outside_changes", []), "expect.outside_changes"))
    for c in changes:
        e = outside.entries.get(c)
        if e is None or e.kind not in ("file", "hardlink"):
            raise SpecError(f"{source}: outside_changes names {c!r}, not an [outside] file")
    base = parse_outcomes(
        {k: v for k, v in exp.items() if k not in ("in_root", "outside_changes")}, "expect"
    )
    in_root = parse_outcomes(in_root_table, "expect.in_root") if in_root_table else base
    if race and not all(_accepts_eagain(x) for x in (base, in_root)):
        raise SpecError(
            f"{source}: a race must accept EAGAIN, in [expect] and [expect.in_root]: races "
            "running beside it can make any lookup with .. give up"
        )
    return Fixture(
        name=name,
        hazard=data["hazard"],
        about=data["about"],
        root_name=root_name,
        requires=requires,
        root=root,
        outside=outside,
        request=Request(req["op"], req["path"]),
        expect=base,
        expect_in_root=in_root,
        outside_changes=changes,
        mounts=mounts,
        race=race,
    )


def load_fixture(path: str | Path) -> Fixture:
    path = Path(path)
    with path.open("rb") as f:
        data = tomllib.load(f)
    return parse_fixture(data, path.stem, str(path))


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def all_fixtures() -> list[Fixture]:
    return [load_fixture(p) for p in sorted(FIXTURE_DIR.glob("*.toml"))]
