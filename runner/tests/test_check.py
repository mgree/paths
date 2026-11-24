"""Tests for the correctness runner, using the real C strategies. They check
our predictions for each strategy, the fixtures' negative control, and the
judging rules."""

import functools
import warnings

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
    check.build_racer()
    tasks, unavailable = check.plan(
        [C],
        FIXTURES,
        None,
        "/dev/shm",
        timeout=2,
        valgrind=False,
        root_shapes=tuple(check.RootShape),
        # Long enough to catch every weak strategy many times over: the
        # weakest, realpath, escapes about a thousand times a second.
        race_seconds=0.2,
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
# because test_root_shape_changes_only_what_we_predict checks that every
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
    assert all(r.reason == "wanted ok, got error ENAMETOOLONG" for r in failed.values())


OPENAT2 = ["openat2-beneath", "openat2-in-root", "openat2-walk-beneath", "openat2-walk-in-root"]


@functools.cache
def one_cpu_dir_out_of_root() -> dict[str, check.Result]:
    """swap-race-dir-out-of-root's results for the openat2 strategies, from
    races long enough for one CPU. There, the driver and the racer take turns,
    so a swap lands in the middle of a lookup only when the driver is preempted
    there, about twice a second. A 5-second race should miss about once in
    20,000 runs."""
    fx = next(fx for fx in FIXTURES if fx.name == "swap-race-dir-out-of-root")
    tasks, _ = check.plan(
        [C],
        [fx],
        set(OPENAT2),
        "/dev/shm",
        timeout=2,
        valgrind=False,
        root_shapes=(check.RootShape.PLAIN,),
        race_seconds=5,
    )
    return {r.strategy: r for r in check.run_all(tasks, jobs=1)}


# Slow only on one CPU, where it runs its own 5-second races.
SLOW_ON_ONE_CPU = [] if check.cpu_pairs() else [pytest.mark.slow]


@pytest.mark.parametrize("strategy", [pytest.param(s, marks=SLOW_ON_ONE_CPU) for s in OPENAT2])
def test_moving_a_directory_out_of_the_root_gives_openat2_exdev(strategy):
    """swap-race-dir-out-of-root exists to count the EXDEVs openat2 gives when
    a directory on the path moves out of the root mid-lookup. Its expectations
    accept EXDEV without requiring it, so this checks that the race really
    produces some. With one CPU it needs much longer races, so it runs its own."""
    if check.cpu_pairs():
        result = verdicts(strategy)["swap-race-dir-out-of-root"]
    else:
        warnings.warn(
            "only one CPU: this test runs its own 5-second race; skip it with -m 'not slow'",
            stacklevel=1,
        )
        result = one_cpu_dir_out_of_root()[strategy]
    attempt = result.attempt
    assert attempt is not None
    tally = {o.label or o.reached: o.count for o in attempt.outcomes}
    assert "EXDEV" in tally, f"no EXDEV in {tally}, from {attempt.swaps} swaps"


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
    kinds = a and sorted((o.ok, o.label or "", o.reached or "") for o in a.outcomes)
    return (r.verdict, a and (a.failure, kinds, a.outside_changed))


def test_the_alone_fixtures_exist_and_neither_mount_nor_race():
    """A misspelled or renamed fixture in check.ALONE would run beside the
    others again, and one that mounts or races would disturb the others in
    ALONE, which run beside each other."""
    by_name = {fx.name: fx for fx in FIXTURES}
    assert check.ALONE <= by_name.keys()
    assert [name for name in check.ALONE if by_name[name].mounts or by_name[name].race] == []


def test_root_shape_changes_only_what_we_predict():
    """The path to the root changes no outcome, with two exceptions.

    The kernel counts the symlinks on the way to the root toward its limit of
    40. The strategies that hand it root and request together have one hop
    fewer under the shapes with a symlink in the root's path, and the 40-link
    chain fails for them with ELOOP.

    Under the mounted shape, the root's path names a mount of the root, so the
    same directory has two paths. symlink-inside-absolute's link spells the
    other one. realpath compares paths as strings, sees one that isn't under
    the root it was given, and refuses. The fixture accepts that (but we see a
    different outcome than with other root shapes).

    Race fixtures are left out: which outcomes a race produces varies from run
    to run."""
    racing = {fx.name for fx in FIXTURES if fx.race}
    plain = {(r.strategy, r.fixture): r for r in results()}
    changed = {
        (r.root_shape, r.strategy, r.fixture): r.reason
        for r in shape_results()
        if r.fixture not in racing and outcome(r) != outcome(plain[r.strategy, r.fixture])
    }
    assert changed == {
        **{
            (shape, strategy, "symlink-chain-40"): "wanted ok, got error ELOOP"
            for shape in (check.RootShape.SYMLINKED, check.RootShape.ROOT_IS_SYMLINK)
            for strategy in ("string-concat", "logical")
        },
        (check.RootShape.MOUNTED, "realpath", "symlink-inside-absolute"): "got error escape",
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


def attempt(
    failure: str | None = None,
    ok: bool = False,
    label: str | None = "EXDEV",
    identity: list[int] | None = None,
    reached: str | None = None,
    **kw,
):
    """An attempt whose driver made one call with the given outcome."""
    one = check.Seen(count=1, ok=ok, label=label, identity=identity, reached=reached)
    base = dict(expected_ids={}, outside_changed=[], swaps=None, stderr="")
    return check.Attempt(failure=failure, outcomes=[] if failure else [one], **{**base, **kw})


@pytest.mark.parametrize("failure", ["timeout", "signal 11", "exit status 1", "bad output: x"])
def test_a_driver_failure_is_never_a_refusal(failure):
    verdict, reason = check.judge(REFUSE, "beneath", attempt(failure=failure, label=None))
    assert verdict == "fail" and failure.partition(":")[0] in reason


def test_any_change_outside_fails_even_a_matching_outcome():
    verdict, reason = check.judge(REFUSE, "beneath", attempt(outside_changed=["secret"]))
    assert verdict == "fail" and reason == "wrote outside: secret"


def test_one_bad_call_fails_a_race():
    """Under a race, every call must have an accepted outcome: one call that
    reached outside fails the strategy, however many others refused."""
    calls = [
        check.Seen(count=999, ok=False, label="ELOOP", identity=None, reached=None),
        check.Seen(count=1, ok=True, label=None, identity=[1, 9], reached="outside:secret"),
    ]
    a = attempt()
    a.outcomes = calls
    verdict, reason = check.judge(REFUSE, "beneath", a)
    assert verdict == "fail" and reason == "wanted refused, got ok outside (1 of 1000 calls)"


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
