"""Tests for the correctness runner, using the real C strategies. They check
our predictions for each strategy, the fixtures' negative control, and the
judging rules."""

import functools

import pytest
from protocol import client
from runner import check
from treegen import ns
from treegen.spec import all_fixtures, parse_fixture

C = next(d for d in client.load_drivers() if d.name == "c")
FIXTURES = all_fixtures()


@functools.cache
def shape_results() -> list[check.Result]:
    """One run of the suite under every root shape, shared by the tests."""
    client.build(C)
    tasks, unavailable = check.plan(
        [C],
        FIXTURES,
        None,
        "/dev/shm",
        timeout=2,
        valgrind=False,
        root_shapes=tuple(check.RootShape),
    )
    return [*check.run_all(tasks, jobs=16), *unavailable]


def results() -> list[check.Result]:
    """The results under the plain root."""
    return [r for r in shape_results() if r.root_shape is check.RootShape.PLAIN]


def verdicts(strategy: str) -> dict[str, check.Result]:
    """The strategy's result on each fixture. A strategy with no results fails
    here, so a test about it can't pass by checking nothing."""
    found = {r.fixture: r for r in results() if r.strategy == strategy}
    assert len(found) == len(FIXTURES), f"{strategy} has results for {len(found)} fixtures"
    return found


# The predictions below are made under the plain root only. That is enough,
# because test_root_shape_changes_only_the_symlink_budget checks that every
# other shape gives the same outcomes, apart from exceptions none of these
# predictions involves.


@pytest.mark.parametrize(
    "strategy", ["walk-beneath", "walk-in-root", "openat2-walk-beneath", "openat2-walk-in-root"]
)
def test_correct_strategies_pass_everything(strategy):
    failed = {f: r.reason for f, r in verdicts(strategy).items() if r.verdict == "fail"}
    assert failed == {}


@pytest.mark.parametrize("strategy", ["openat2-beneath", "openat2-in-root"])
def test_pure_openat2_fails_only_past_path_max(strategy):
    failed = {f: r for f, r in verdicts(strategy).items() if r.verdict == "fail"}
    assert set(failed) == {"over-long-path", "over-long-path-dotdot"}
    assert all(r.attempt and r.attempt.label == "ENAMETOOLONG" for r in failed.values())


def test_string_concat_fails_every_refusal_fixture():
    """The negative control for the fixtures. string-concat checks nothing, so
    it must fail every fixture that accepts only a refusal. If it passes one,
    that fixture tests nothing."""
    concat = verdicts("string-concat")
    for fx in FIXTURES:
        if all(e.result == "refused" for e in fx.expect):
            assert concat[fx.name].verdict == "fail", fx.name


def test_no_strategy_makes_the_driver_fail():
    """A crash, a sanitizer report or bad output is a bug in our code, whichever
    strategy or root shape it happens under, including the strategies no
    prediction covers."""
    failures = check.driver_failures(shape_results())
    assert [(r.root_shape, r.strategy, r.fixture) for r in failures] == []


@pytest.mark.parametrize(
    "args",
    [["--driver", "cc"], ["--fixture", "no-such-fixture"], ["--strategy", "walk-beneth"]],
    ids=["driver", "fixture", "strategy"],
)
def test_runner_rejects_unknown_names(args, capsys):
    """A misspelled name would otherwise match nothing and run nothing."""
    client.build(C)
    with pytest.raises(SystemExit) as e:
        check.main(["--no-build", *args])
    assert e.value.code == 2 and "unknown" in capsys.readouterr().err


def outcome(r: check.Result):
    a = r.attempt
    return (r.verdict, a and (a.failure, a.ok, a.label, a.reached, a.outside_changed))


def test_root_shape_changes_only_the_symlink_budget():
    """The path to the root changes no outcome, with one exception. The kernel
    counts the symlinks on the way to the root toward its limit of 40, so the
    strategies that hand it root and request together have one hop fewer, and
    the 40-link chain fails for them with ELOOP."""
    plain = {(r.strategy, r.fixture): r for r in results()}
    changed = {
        (r.root_shape, r.strategy, r.fixture): r.reason
        for r in shape_results()
        if outcome(r) != outcome(plain[r.strategy, r.fixture])
    }
    assert changed == {
        (shape, strategy, "symlink-chain-40"): "wanted ok, got error ELOOP"
        for shape in (check.RootShape.SYMLINKED, check.RootShape.ROOT_IS_SYMLINK)
        for strategy in ("string-concat", "logical")
    }


def test_nothing_is_skipped_where_mounts_work():
    if not ns.mounts_available():
        pytest.skip(ns.why_no_mounts() or "")
    assert [r for r in shape_results() if r.verdict == "skip"] == []


REFUSE = parse_fixture(
    {
        "hazard": "h",
        "about": "a",
        "outside": {"files": ["secret"]},
        "request": {"op": "read", "path": "x"},
        "expect": {"result": "refused"},
    },
    "t",
)


def attempt(**kw):
    base = dict(
        failure=None,
        ok=False,
        label="EXDEV",
        reached=None,
        expected_ids={},
        identity=None,
        outside_changed=[],
        stderr="",
    )
    return check.Attempt(**{**base, **kw})


@pytest.mark.parametrize("failure", ["timeout", "signal 11", "exit status 1", "bad output: x"])
def test_a_driver_failure_is_never_a_refusal(failure):
    verdict, reason = check.judge(REFUSE, "beneath", attempt(failure=failure, label=None))
    assert verdict == "fail" and failure.partition(":")[0] in reason


def test_any_change_outside_fails_even_a_matching_outcome():
    verdict, reason = check.judge(REFUSE, "beneath", attempt(outside_changed=["secret"]))
    assert verdict == "fail" and "wrote outside" in reason


def test_ok_must_reach_the_expected_file():
    fx = parse_fixture(
        {
            "hazard": "h",
            "about": "a",
            "root": {"files": ["f", "g"]},
            "request": {"op": "read", "path": "f"},
            "expect": {"result": "ok", "file": "f"},
        },
        "t",
    )
    right = attempt(ok=True, label=None, identity=[1, 2], expected_ids={"f": [1, 2]})
    wrong = attempt(ok=True, label=None, identity=[1, 3], expected_ids={"f": [1, 2]})
    assert check.judge(fx, "beneath", right)[0] == "pass"
    assert check.judge(fx, "beneath", wrong)[0] == "fail"


def test_errors_compare_by_value():
    fx = parse_fixture(
        {
            "hazard": "h",
            "about": "a",
            "request": {"op": "read", "path": "x"},
            "expect": {"result": "error", "errno": "EAGAIN"},
        },
        "t",
    )
    assert check.judge(fx, "beneath", attempt(label="EWOULDBLOCK"))[0] == "pass"
    assert check.judge(fx, "beneath", attempt(label="escape"))[0] == "fail"
