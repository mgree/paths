"""Name generation for synthetic trees: character classes, byte-length
distributions, and fold-safe uniqueness among siblings.

Lengths are in UTF-8 bytes, because NAME_MAX and PATH_MAX are byte limits and
the kernel hashes bytes.
"""

from __future__ import annotations

import random
import string
import tomllib
import unicodedata
from functools import cache
from pathlib import Path

from .spec import SpecError

DATA_DIR = Path(__file__).parent / "data"
NAME_MAX = 255


def _pool(lo: int, hi: int, categories: tuple[str, ...]) -> str:
    out = []
    for cp in range(lo, hi + 1):
        c = chr(cp)
        if unicodedata.category(c) in categories and unicodedata.normalize("NFC", c) == c:
            out.append(c)
    return "".join(out)


LETTERS = ("Lu", "Ll", "Lt", "Lo")

# Each class is a fixed pool of single code points. Every pool character is
# NFC-stable and is a letter or digit (emoji: a symbol), never a combining mark,
# format character or control.
CLASSES: dict[str, str] = {
    "lower": string.ascii_lowercase,
    "upper": string.ascii_uppercase,
    "digit": string.digits,
    "punct": "._-",
    "latin": _pool(0x00C0, 0x024F, LETTERS),  # accents, NFC vs NFD; includes ß
    "greek": _pool(0x0391, 0x03C9, LETTERS),  # includes Σ, with two lowercase forms
    "cyrillic": _pool(0x0410, 0x044F, LETTERS),
    "cjk": _pool(0x4E00, 0x4FFF, ("Lo",)),  # 3 bytes, no case
    "emoji": _pool(0x1F300, 0x1F5FF, ("So",)),  # 4 bytes, astral (UTF-16 surrogates)
}
ASCII_FILL = string.ascii_lowercase  # completes a name when a wide char won't fit


def fold_key(name: str) -> str:
    """Compatibility caseless form (Unicode D146). Two siblings with the same
    key could alias on some filesystem (APFS, ext4/tmpfs casefold, NTFS)."""
    x = unicodedata.normalize("NFD", name).casefold()
    x = unicodedata.normalize("NFKD", x).casefold()
    return unicodedata.normalize("NFKD", x)


@cache
def load_lengths(source: str) -> dict[str, tuple[tuple[int, int], ...]]:
    """A surveyed distribution: names-<source>.toml with [value, count] pairs
    for dir and file name lengths and for width (entries per directory)."""
    path = DATA_DIR / f"names-{source}.toml"
    if not path.exists():
        raise SpecError(f"no name-length data {path.name}")
    with path.open("rb") as f:
        data = tomllib.load(f)
    out = {}
    for kind in ("dir", "file", "width"):
        pairs = tuple((int(k), int(v)) for k, v in data[kind])
        top = NAME_MAX if kind != "width" else 1 << 20
        low = 1 if kind != "width" else 0
        if not pairs or any(not low <= k <= top or v <= 0 for k, v in pairs):
            raise SpecError(f"{path.name}: bad {kind} data")
        out[kind] = pairs
    return out


def dir_share(source: str) -> float:
    """Fraction of surveyed entries that are directories."""
    d = load_lengths(source)
    dirs, files = (sum(c for _, c in d[k]) for k in ("dir", "file"))
    return dirs / (dirs + files)


class NameGen:
    """Draws names for one tree. `lengths` is a surveyed source name, or one
    exact byte length for every name; `chars` maps class names to weights."""

    def __init__(self, rng: random.Random, lengths: str | int, chars: dict[str, float]):
        self.rng = rng
        if isinstance(lengths, str):
            dist = load_lengths(lengths)
            self._len = {k: ([n for n, _ in v], [c for _, c in v]) for k, v in dist.items()}
            self.fixed = 0
        else:
            if not 1 <= lengths <= NAME_MAX:
                raise SpecError(f"name length {lengths} is outside 1 to {NAME_MAX} bytes")
            self._len = None
            self.fixed = lengths
        unknown = set(chars) - set(CLASSES)
        if unknown:
            raise SpecError(f"unknown character classes {sorted(unknown)}")
        if not any(w > 0 for w in chars.values()) or any(w < 0 for w in chars.values()):
            raise SpecError("character weights must be non-negative, not all zero")
        self.classes = [c for c, w in chars.items() if w > 0]
        self.weights = [chars[c] for c in self.classes]
        self.taken: dict[str, set[str]] = {}

    def length(self, kind: str) -> int:
        if self._len is None:
            return self.fixed
        lens, counts = self._len[kind]
        return self.rng.choices(lens, counts)[0]

    def _draw(self, nbytes: int) -> str:
        out: list[str] = []
        left = nbytes
        while left:
            cls = self.rng.choices(self.classes, self.weights)[0]
            c = self.rng.choice(CLASSES[cls])
            n = len(c.encode())
            if n > left:
                c = self.rng.choice(ASCII_FILL)
                n = 1
            out.append(c)
            left -= n
        return "".join(out)

    def fresh(self, directory: str, kind: str) -> str:
        """A new name in directory that folds to no existing sibling."""
        keys = self.taken.setdefault(directory, set())
        for _ in range(10_000):
            name = self._draw(self.length(kind))
            if name in (".", ".."):
                continue
            key = fold_key(name)
            if key not in keys:
                keys.add(key)
                return name
        raise SpecError(f"could not find a fresh name in {directory!r}; widen lengths or classes")


def classes_of(name: str) -> set[str]:
    """Which classes a name's characters come from (fill counts as lower)."""
    found = set()
    for c in name:
        hit = [k for k, pool in CLASSES.items() if c in pool]
        if not hit:
            raise ValueError(f"character {c!r} is in no class")
        found.update(hit)
    return found
