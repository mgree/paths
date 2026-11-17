"""Health checks for name generation: character pools, fold keys, surveyed
length data, and the generator's guarantees."""

import random
import unicodedata

import pytest

from treegen.names import CLASSES, NameGen, dir_share, fold_key, load_lengths
from treegen.spec import SpecError

DEFAULT_CHARS = {
    "lower": 60.0,
    "upper": 10.0,
    "digit": 10.0,
    "punct": 10.0,
    "latin": 4.0,
    "greek": 2.0,
    "cyrillic": 2.0,
    "cjk": 1.0,
    "emoji": 1.0,
}


def test_class_pools_are_clean():
    for cls, pool in CLASSES.items():
        assert len(set(pool)) == len(pool) > 0, cls
        for c in pool:
            assert unicodedata.normalize("NFC", c) == c, (cls, c)
            assert unicodedata.category(c)[0] in "LNS" or c in "._-", (cls, c)
            assert c not in "/\0", (cls, c)


def test_fold_key_catches_known_aliases():
    for a, b in [
        ("Foo", "foo"),
        ("straße", "STRASSE"),
        ("\u00e9", "e\u0301"),  # precomposed vs decomposed
        ("ΣΑΣ", "σας"),
        ("ﬁle", "file"),
        ("Ǆ", "dž"),
    ]:
        assert fold_key(a) == fold_key(b), (a, b)
    assert fold_key("a") != fold_key("b")


def test_length_data_matches_survey_shape():
    d = load_lengths("linux-7.3.4")
    dirs, files = (sum(c for _, c in d[k]) for k in ("dir", "file"))
    assert dirs > 1000 and files > 10 * dirs

    def mean(k):
        return sum(n * c for n, c in d[k]) / sum(c for _, c in d[k])

    assert mean("dir") < mean("file")
    assert 0.03 < dir_share("linux-7.3.4") < 0.1
    assert sum(c for _, c in d["width"]) == dirs + 1  # every directory, plus the top


@pytest.mark.parametrize("lengths", ["linux-7.3.4", 4, 200, 255])
@pytest.mark.parametrize("chars", [DEFAULT_CHARS, {"emoji": 1.0, "cjk": 1.0}, {"lower": 1.0}])
def test_generated_names(lengths, chars):
    gen = NameGen(random.Random(1), lengths, chars)
    allowed = set().union(*(CLASSES[c] for c in chars)) | set(CLASSES["lower"])
    if isinstance(lengths, str):
        d = load_lengths(lengths)
        support = {k: {n for n, _ in d[k]} for k in ("dir", "file")}
    else:
        support = {k: {lengths} for k in ("dir", "file")}
    keys = set()
    for i in range(300):
        kind = "dir" if i % 3 else "file"
        name = gen.fresh("d", kind)
        n = len(name.encode())
        assert n in support[kind], (name, n)
        assert set(name) <= allowed, name
        assert name not in (".", "..")
        assert fold_key(name) not in keys, f"{name!r} folds onto an earlier sibling"
        keys.add(fold_key(name))


def test_deterministic():
    a = NameGen(random.Random(7), "linux-7.3.4", DEFAULT_CHARS)
    b = NameGen(random.Random(7), "linux-7.3.4", DEFAULT_CHARS)
    assert [a.fresh("", "file") for _ in range(50)] == [b.fresh("", "file") for _ in range(50)]


def test_siblings_only_within_a_directory():
    gen = NameGen(random.Random(1), 1, {"digit": 1.0})
    first = [gen.fresh("a", "file") for _ in range(10)]
    second = [gen.fresh("b", "file") for _ in range(10)]
    assert sorted(first) == sorted(second) == list("0123456789")


def test_exhausted_name_space_is_an_error():
    gen = NameGen(random.Random(1), 1, {"digit": 1.0})
    for _ in range(10):
        gen.fresh("d", "file")
    with pytest.raises(SpecError, match="fresh name"):
        gen.fresh("d", "file")


@pytest.mark.parametrize(
    "lengths, chars, msg",
    [
        ("no-such-survey", DEFAULT_CHARS, "no name-length data"),
        (4, {"klingon": 1.0}, "unknown character classes"),
        (4, {"lower": 0.0}, "not all zero"),
        (4, {"lower": -1.0}, "non-negative"),
        (0, DEFAULT_CHARS, "outside 1 to 255"),
        (256, DEFAULT_CHARS, "outside 1 to 255"),
    ],
)
def test_bad_settings(lengths, chars, msg):
    with pytest.raises(SpecError, match=msg):
        NameGen(random.Random(1), lengths, chars)
