"""Health checks for the hand-written correctness fixtures."""

import os
import signal
import tomllib
from contextlib import contextmanager
from pathlib import Path

import pytest
from treegen import ns
from treegen.build import (
    DIR_FLAGS,
    Sandbox,
    build_fixture,
    expand,
    opened,
    read_unlocked,
    scan,
    snapshot_outside,
)
from treegen.spec import FIXTURE_DIR, Fixture, SpecError, all_fixtures, parse_fixture, split_ref

FIXTURES = all_fixtures()
IDS = [f.name for f in FIXTURES]

REPO = Path(__file__).resolve().parents[2]
# Builds are compared on tmpfs and on the repo's own filesystem (ext4 here).
BASES = {"tmpfs": Path("/dev/shm"), "repo-fs": REPO / "build" / "sandboxes"}


def check_build(fx: Fixture, sb: Sandbox) -> None:
    """The sandbox holds exactly the declared entries, each as declared: kind,
    mode, symlink text, hard links as one file, mounts really mounted."""
    got = {(r.tree, r.path): r for r in scan(sb)}
    declared = {("root", p): e for p, e in fx.root.entries.items()}
    declared |= {("outside", p): e for p, e in fx.outside.entries.items()}
    assert sorted(got) == sorted(declared), "missing or undeclared entries"
    mounts = {m.at: m for m in fx.mounts}
    top_dev = os.stat(sb.path).st_dev
    files: dict[tuple[str, str], tuple[int, int]] = {}  # each file's first name -> inode
    for key, e in declared.items():
        r = got[key]
        m = mounts.get(e.path) if key[0] == "root" else None
        if m is not None and m.type != "casefold":
            assert r.kind == m.type, key
            if m.source is not None:  # bind: the mount point shows the source itself
                assert r.ino == got[m.source].ino, f"{key} is not a mount of {m.source}"
            else:
                assert r.ino[0] != top_dev, f"{key} is not a mount"
            continue
        assert r.kind == ("file" if e.kind == "hardlink" else e.kind), key
        assert r.mode == e.effective_mode(), key
        if m is not None:  # casefold: its own filesystem, case-insensitive at the top
            assert r.ino[0] != top_dev, f"{key} is not a mount"
            with opened(sb.root / e.path, DIR_FLAGS) as fd:
                assert ns.is_casefold(fd), f"{key} is not case-insensitive"
        if e.kind == "symlink":
            assert r.target == expand(e.link, sb.root, sb.path), key
        if e.kind in ("file", "hardlink"):
            name = split_ref(e.link) if e.kind == "hardlink" else key
            assert files.setdefault(name, r.ino) == r.ino, f"{key} is not one file with {name}"
    assert len(set(files.values())) == len(files), "two declared files share an inode"


def in_sandbox(fx: Fixture, base: Path, fn):
    """Build fx and return fn(fx, sb), inside a namespace if fx has mounts."""

    def go():
        with build_fixture(fx, base) as sb:
            return fn(fx, sb)

    if not fx.mounts:
        return go()
    if not ns.mounts_available():
        pytest.skip(ns.why_no_mounts() or "")
    return ns.run_in_namespace(go)


def test_fixtures_were_found():
    """Every fixture test is parametrized over FIXTURES: with none found, they
    would all silently vanish instead of failing."""
    assert FIXTURES


@pytest.mark.parametrize("base", BASES.values(), ids=BASES.keys())
@pytest.mark.parametrize("fx", FIXTURES, ids=IDS)
def test_build_matches_spec(fx, base):
    base.mkdir(parents=True, exist_ok=True)
    in_sandbox(fx, base, check_build)


def declared_file(fx: Fixture, path: str):
    """The declared entry a root-relative path names, looking through bind mounts."""
    for m in fx.mounts:
        if m.source is not None and path.startswith(m.at + "/"):
            tree, src = m.source
            rest = path[len(m.at) + 1 :]
            return (fx.root if tree == "root" else fx.outside).entries.get(f"{src}/{rest}")
    return fx.root.entries.get(path)


@pytest.mark.parametrize("fx", FIXTURES, ids=IDS)
def test_expectations_name_real_entries(fx):
    for e in fx.expect + fx.expect_in_root:
        if e.result != "ok":
            continue
        assert e.file is not None
        entry = declared_file(fx, e.file)
        if fx.request.op == "read":
            assert entry is not None and entry.kind in ("file", "hardlink"), e.file
        elif fx.request.op == "newfile":
            assert entry is None, f"newfile target {e.file!r} already exists"
        else:  # write: creates a new file or truncates an existing one
            assert entry is None or entry.kind in ("file", "hardlink"), e.file


@pytest.mark.parametrize("fx", FIXTURES, ids=IDS)
def test_has_outside_or_not_an_escape(fx):
    """A read escape needs an outside file to land on, or the runner cannot tell
    an escape from a miss. (newfile and write escapes create something outside,
    which the outside snapshot catches even when [outside] is empty.) NB that
    /proc is an escape hatch here."""
    target = fx.root.entries.get(fx.request.path)
    names_own_entry = target is not None and target.kind != "symlink"  # e.g. a FIFO
    escapes = (
        fx.request.op == "read"
        and all(e.result == "refused" for e in fx.expect)
        and not names_own_entry
    )
    links_out = any((e.target or "").startswith("{sandbox}") for e in fx.root.entries.values())
    # A proc mount's magic links reach the driver's own files, such as its
    # program (self/exe), so a request through one has something to land on.
    path = fx.request.path
    through_proc = any(
        m.type == "proc" and (path == m.at or path.startswith(m.at + "/")) for m in fx.mounts
    )
    targets_outside = bool(fx.outside.entries) or links_out or path.startswith("/") or through_proc
    assert not escapes or targets_outside


MINIMAL = {
    "hazard": "h",
    "about": "a",
    "request": {"op": "read", "path": "x"},
    "expect": {"result": "refused"},
}


NS = {"root": {"dirs": ["p"]}}
RACE = {
    "root": {"files": ["f"], "symlinks": {"l": "f"}},
    "race": {"exchange": ["root:f", "root:l"]},
}


@pytest.mark.parametrize(
    "patch, msg",
    [
        ({"root": {"files": ["../x"]}}, "normal relative"),
        ({"root": {"files": ["/x"]}}, "normal relative"),
        ({"root": {"files": ["a//b"]}}, "normal relative"),
        ({"root": {"files": ["a", "a/b"]}}, "has children"),
        ({"root": {"symlinks": {"l": "x"}, "modes": {"l": 0}}}, "mode on symlink"),
        (
            {
                "root": {
                    "files": ["f"],
                    "hardlinks": {"h": "root:f"},
                    "modes": {"f": 0, "h": 0o600},
                },
            },
            "conflicting modes",
        ),
        ({"expect": {"result": "refused", "in_root": "error"}}, "in_root must be a table"),
        ({"root": {"modes": {"nothere": 0}}}, "undeclared"),
        ({"root": {"files": ["a/x"], "modes": {"a/y": 0}}}, "undeclared"),
        ({"root": {"hardlinks": {"h": "/etc/passwd"}}}, "root:<path> or outside:<path>"),
        ({"root": {"hardlinks": {"h": "outside:missing"}}}, "needs a file"),
        ({"root": {"dirs": ["d"], "hardlinks": {"h": "root:d"}}}, "needs a file"),
        ({"root": {"bogus": []}}, "unknown keys"),
        ({"expect": {"result": "error", "errno": "ENOTANERRNO"}}, "unknown errno"),
        ({"expect": {"result": "ok"}}, "needs file"),
        ({"requires": ["root"]}, "unknown requirement"),
        ({"request": {"op": "open", "path": "x"}}, "request needs op"),
        (
            {"expect": {"result": "refused", "outside_changes": ["nope"]}},
            "not an \\[outside\\] file",
        ),
        (
            {"outside": {"dirs": ["d"]}, "expect": {"result": "refused", "outside_changes": ["d"]}},
            "not an \\[outside\\] file",
        ),
        ({"expect": {"accept": [{"result": "refused"}]}}, "at least two"),
        ({"expect": {"accept": [{"result": "refused"}, {"result": "refused"}]}}, "twice"),
        ({"expect": {"result": "refused", "accept": []}}, "cannot be mixed"),
        ({"root_name": "a/b"}, "one component"),
        ({"root_name": "r", "outside": {"files": ["r/x"]}}, "collides"),
        ({**NS, "mounts": {"at": "p", "type": "proc"}}, "list of tables"),
        ({**NS, "mounts": [{"at": "p", "type": "nfs"}]}, "type must be one of"),
        ({**NS, "mounts": [{"at": "nothere", "type": "proc"}]}, "'nothere' is not declared"),
        ({**NS, "mounts": [{"type": "proc"}]}, "directory; missing"),
        (
            {"root": {"files": ["f"]}, "mounts": [{"at": "f", "type": "proc"}]},
            "'f' is a file in \\[root\\]",
        ),
        (
            {"outside": {"dirs": ["d"]}, "mounts": [{"at": "d", "type": "proc"}]},
            "'d' is an \\[outside\\] entry",
        ),
        ({**NS, "mounts": [{"at": "p", "type": "proc"}] * 2}, "mounted twice"),
        (
            {**NS, "root": {"files": ["p/x"]}, "mounts": [{"at": "p", "type": "proc"}]},
            "hidden by the mount",
        ),
        ({**NS, "mounts": [{"at": "p", "type": "bind"}]}, "needs from"),
        (
            {**NS, "mounts": [{"at": "p", "type": "bind", "from": "outside:no"}]},
            "'outside:no' is not a declared directory",
        ),
        (
            {
                "root": {"files": ["ci/f"], "hardlinks": {"h": "root:ci/f"}},
                "mounts": [{"at": "ci", "type": "casefold"}],
            },
            "different filesystems",
        ),
        (
            # The outer mount is declared first, so a first match would put
            # both files on it.
            {
                "root": {"files": ["ci/f", "ci/in/g"], "hardlinks": {"ci/in/h": "root:ci/f"}},
                "mounts": [{"at": "ci", "type": "casefold"}, {"at": "ci/in", "type": "casefold"}],
            },
            "different filesystems",
        ),
        ({**NS, "mounts": [{"at": "p", "type": "proc", "from": "root:p"}]}, "only bind"),
        ({**RACE, "race": {"exchange": ["root:f"]}}, "two entries"),
        ({**RACE, "race": {"exchange": ["root:f", "root:no"]}}, "root:no, which is not declared"),
        ({**RACE, "race": {"exchange": ["root:f", "root:f"]}}, "twice"),
        ({**RACE, "request": {"op": "newfile", "path": "f"}}, "cannot use newfile"),
        (
            {"root": {"files": ["d/f"]}, "race": {"exchange": ["root:d", "root:d/f"]}},
            "one contains the other",
        ),
        (
            {
                "root": {"dirs": ["p", "q"]},
                "mounts": [{"at": "p", "type": "proc"}],
                "race": {"exchange": ["root:q", "root:p"]},
            },
            "one is a mount point",
        ),
        (
            {
                "root": {"files": ["ci/f", "g"]},
                "mounts": [{"at": "ci", "type": "casefold"}],
                "race": {"exchange": ["root:ci/f", "root:g"]},
            },
            "different filesystems",
        ),
        (
            {
                **RACE,
                "expect": {"in_root": {"result": "error", "errno": "ENOENT"}, "result": "refused"},
            },
            "must accept EAGAIN",
        ),
    ],
)
def test_spec_rejects(patch, msg):
    with pytest.raises(SpecError, match=msg):
        parse_fixture({**MINIMAL, **patch}, "t")


DEEP = "/".join(["d" * 200] * 25)  # about 5,000 bytes, past PATH_MAX


@pytest.mark.parametrize("base", BASES.values(), ids=BASES.keys())
@pytest.mark.parametrize(
    "trees",
    [
        {"root": {"files": ["f"]}, "outside": {"hardlinks": {"h": "root:f"}}},
        {"root": {"files": [f"{DEEP}/f"], "hardlinks": {"h": f"root:{DEEP}/f"}}},
        {
            "root": {"files": ["f"]},
            "outside": {"hardlinks": {"h": "root:f"}, "modes": {"h": 0o600}},
        },
    ],
    ids=["outside-link-to-root-file", "link-source-past-path-max", "mode-on-link"],
)
def test_hard_links_build_as_declared(trees, base):
    """Hard-link cases that no committed fixture covers: a source in the other
    tree, which is built second, and a source path too long to name in one
    string."""
    fx = parse_fixture({**MINIMAL, **trees}, "t")
    base.mkdir(parents=True, exist_ok=True)
    in_sandbox(fx, base, check_build)


@pytest.mark.parametrize("listed", [[], ["userns"], ["nonroot", "linux", "userns"]])
def test_mounts_imply_their_requirements(listed):
    """Implied requirements are added once, whether or not they are also listed."""
    mounts = [{"at": "p", "type": "proc"}]
    fx = parse_fixture({**MINIMAL, **NS, "requires": listed, "mounts": mounts}, "t")
    assert sorted(fx.requires) == sorted(set(listed) | {"userns", "linux"})


def test_fixture_files_are_ascii():
    """Non-ASCII names are written as escapes (\\u00e9, e\\u0301): spellings that differ
    only in normalization look identical in an editor."""
    for p in FIXTURE_DIR.glob("*.toml"):
        assert p.read_bytes().isascii(), f"{p.name}: write non-ASCII characters as escapes"


def test_file_contents_identify_the_file(tmp_path):
    """Every regular file holds '<fixture> <tree>:<path>', distinct across the suite."""

    def contents(fx, sb):
        """Map each file to its names and its contents. Hard links to one file
        count as one file."""
        files: dict[tuple[int, int], tuple[list[str], bytes]] = {}
        for r in scan(sb):
            if r.kind == "file":
                names, _ = files.setdefault(r.ino, ([], read_unlocked(sb.base(r.tree), r.path)))
                names.append(f"{r.tree}:{r.path}")
        return list(files.values())

    seen: dict[bytes, str] = {}
    for fx in FIXTURES:
        if fx.mounts and not ns.mounts_available():
            continue
        for names, data in in_sandbox(fx, tmp_path, contents):
            assert data in {f"{fx.name} {n}\n".encode() for n in names}, (names, data)
            assert data not in seen, f"{data!r} also in {seen[data]}"
            seen[data] = fx.name
    assert seen


def test_format_example_is_valid():
    """The example in FORMAT.md must load and build like a real fixture."""
    text = (FIXTURE_DIR.parent / "FORMAT.md").read_text()
    block = text.split("```toml\n", 1)[1].split("```", 1)[0]
    fx = parse_fixture(tomllib.loads(block), "format-example", "FORMAT.md")
    in_sandbox(fx, Path("/dev/shm"), check_build)


SNAPSHOT_FX = {
    "hazard": "h",
    "about": "a",
    "outside": {
        "files": ["secret", "locked/inner"],
        "symlinks": {"l": "secret"},
        "modes": {"locked": 0},
    },
    "request": {"op": "read", "path": "x"},
    "expect": {"result": "refused"},
}


def test_snapshot_outside_is_stable_and_restores_modes(tmp_path):
    fx = parse_fixture(SNAPSHOT_FX, "snap")
    with build_fixture(fx, tmp_path) as sb:
        before = snapshot_outside(sb)
        assert before["locked/inner"].data == b"snap outside:locked/inner\n"
        assert snapshot_outside(sb) == before
        check_build(fx, sb)  # modes put back


@pytest.mark.parametrize(
    "change",
    [
        lambda p: (p / "secret").write_bytes(b""),  # truncation: names and modes unchanged
        lambda p: (p / "created").write_bytes(b"x"),
        lambda p: os.chmod(p / "secret", 0o600),
        lambda p: (os.unlink(p / "l"), os.symlink("elsewhere", p / "l")),
    ],
    ids=["truncate", "create", "chmod", "relink"],
)
def test_snapshot_outside_sees_changes(tmp_path, change):
    fx = parse_fixture(SNAPSHOT_FX, "snap")
    with build_fixture(fx, tmp_path) as sb:
        before = snapshot_outside(sb)
        change(sb.path)
        assert snapshot_outside(sb) != before


@contextmanager
def deadline(seconds: int):
    """Fail instead of hanging: an accidental open of a FIFO blocks forever."""

    def expired(signum, frame):
        raise TimeoutError(f"still running after {seconds} s")

    old = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


FIFO_FX = {
    **MINIMAL,
    "root": {"fifos": ["p"]},
    "outside": {"files": ["f"], "fifos": ["q", "locked/r"], "modes": {"locked": 0}},
}


def test_fifos_are_never_opened(tmp_path):
    """Build, scan, snapshot and removal finish with FIFOs on both sides, even
    under a mode-000 directory; reading one is refused, not waited on."""
    fx = parse_fixture(FIFO_FX, "t")
    with deadline(5):
        with build_fixture(fx, tmp_path) as sb:
            check_build(fx, sb)
            snap = snapshot_outside(sb)
            assert snap["q"].kind == "fifo" and snap["q"].data is None
            assert snap["f"].data == b"t outside:f\n"
            with pytest.raises(ValueError, match="not a regular file"):
                read_unlocked(sb.path, "q")


def _open_fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


def test_build_failure_removes_sandbox(tmp_path, monkeypatch):
    fx = all_fixtures()[0]
    fds = _open_fds()

    def boom(*args):
        raise OSError("injected")

    monkeypatch.setattr("treegen.build._apply_modes", boom)
    with pytest.raises(OSError, match="injected"):
        build_fixture(fx, tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert _open_fds() == fds


def test_built_sandbox_outlives_build(tmp_path):
    fds = _open_fds()
    with build_fixture(all_fixtures()[0], tmp_path) as sb:
        assert _open_fds() == fds
        assert sb.root.is_dir()
    assert list(tmp_path.iterdir()) == []


def test_scan_and_snapshot_close_their_descriptors(tmp_path):
    fx = next(f for f in all_fixtures() if f.name == "permission-mid-path")  # has a 000 dir
    with build_fixture(fx, tmp_path) as sb:
        fds = _open_fds()
        scan(sb)
        snapshot_outside(sb)
        assert _open_fds() == fds
