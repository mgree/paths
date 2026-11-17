"""Health checks for the hand-written correctness fixtures."""

import os
import stat
import tomllib
from pathlib import Path

import pytest
from treegen.build import build_fixture, listing, predict, scan, snapshot_outside
from treegen.spec import FIXTURE_DIR, SpecError, all_fixtures, parse_fixture

FIXTURES = all_fixtures()
IDS = [f.name for f in FIXTURES]

REPO = Path(__file__).resolve().parents[2]
# Builds are compared on tmpfs and on the repo's own filesystem (ext4 here).
BASES = {"tmpfs": Path("/dev/shm"), "repo-fs": REPO / "build" / "sandboxes"}


def test_fixture_dir_nonempty():
    assert len(FIXTURES) == len(list(FIXTURE_DIR.glob("*.toml"))) > 0


@pytest.mark.parametrize("base", BASES.values(), ids=BASES.keys())
@pytest.mark.parametrize("fx", FIXTURES, ids=IDS)
def test_build_matches_prediction(fx, base):
    base.mkdir(parents=True, exist_ok=True)
    with build_fixture(fx, base) as sb:
        assert listing(scan(sb)) == listing(predict(fx))


@pytest.mark.parametrize("fx", FIXTURES, ids=IDS)
def test_expectations_name_real_entries(fx):
    for e in fx.expect + fx.expect_in_root:
        if e.result != "ok":
            continue
        entry = fx.root.entries.get(e.file)
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
    which the outside snapshot catches even when [outside] is empty.)"""
    escapes = fx.request.op == "read" and all(e.result == "refused" for e in fx.expect)
    links_out = any((e.target or "").startswith("{sandbox}") for e in fx.root.entries.values())
    targets_outside = bool(fx.outside.entries) or links_out or fx.request.path.startswith("/")
    assert not escapes or targets_outside


MINIMAL = {
    "hazard": "h",
    "about": "a",
    "request": {"op": "read", "path": "x"},
    "expect": {"result": "refused"},
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
def test_hard_links_build_as_predicted(trees, base):
    """Link cases no committed fixture has: a source in the other tree, built
    second, and a source path too long to name in one string."""
    fx = parse_fixture({**MINIMAL, **trees}, "t")
    base.mkdir(parents=True, exist_ok=True)
    expected = listing(predict(fx))
    with build_fixture(fx, base) as sb:
        assert listing(scan(sb)) == expected


def test_every_file_parses_as_toml():
    for p in FIXTURE_DIR.glob("*.toml"):
        with p.open("rb") as f:
            tomllib.load(f)


def test_file_contents_identify_the_file(tmp_path):
    """Every regular file holds '<fixture> <tree>:<path>', distinct across the suite."""
    seen: dict[bytes, str] = {}
    for fx in FIXTURES:
        with build_fixture(fx, tmp_path) as sb:
            for r in scan(sb):
                if r.kind != "file":
                    continue
                base = sb.root if r.tree == "root" else sb.path
                try:
                    data = (base / r.path).read_bytes()
                except PermissionError:
                    continue  # under a mode-000 directory; scan already saw it
                assert data == f"{fx.name} {r.tree}:{r.path}\n".encode()
                assert data not in seen, f"{data!r} also in {seen[data]}"
                seen[data] = fx.name
    assert seen


def test_format_example_is_valid():
    """The example in FORMAT.md must load and build like a real fixture."""
    text = (FIXTURE_DIR.parent / "FORMAT.md").read_text()
    block = text.split("```toml\n", 1)[1].split("```", 1)[0]
    fx = parse_fixture(tomllib.loads(block), "format-example", "FORMAT.md")
    with build_fixture(fx, "/dev/shm") as sb:
        assert listing(scan(sb)) == listing(predict(fx))


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
        assert before["locked/inner"][3] == b"snap outside:locked/inner\n"
        assert snapshot_outside(sb) == before
        assert listing(scan(sb)) == listing(predict(fx))  # modes put back


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


def _open_fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


def test_build_failure_removes_sandbox(tmp_path, monkeypatch):
    fx = all_fixtures()[0]
    fds = _open_fds()

    def boom(*args):
        raise OSError("injected")

    monkeypatch.setattr("treegen.build.apply_modes", boom)
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


def test_mode_on_a_hard_link_reaches_the_file(tmp_path):
    trees = {
        "root": {"files": ["f"]},
        "outside": {"hardlinks": {"h": "root:f"}, "modes": {"h": 0o600}},
    }
    with build_fixture(parse_fixture({**MINIMAL, **trees}, "t"), tmp_path) as sb:
        assert stat.S_IMODE(os.stat(sb.root / "f").st_mode) == 0o600
