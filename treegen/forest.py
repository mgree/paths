"""Generate benchmark trees, and the request lists for working-set levels.

A tree is a stack of directory levels that branches as a shape says: every
directory at level k holds fanout[k] directories of the next level, each with
freshly drawn names, and every directory at the last level holds one target
file. A request is the path from the tree's root to one target file. See
FOREST.md for how these trees serve the benchmark's sweeps.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .names import NameGen
from .spec import Entry, SpecError, Tree

# The character mix FOREST.md gives for generated names: about 10% non-ASCII.
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

# The surveyed source for name lengths, when a shape doesn't fix one.
SURVEY = "linux-7.3.4"


@dataclass(frozen=True)
class Shape:
    """How a tree branches. fanout[k] is how many directories each directory
    at level k holds, where level 0 is the tree's root, so fanout has one entry
    for each level of directories on a request's path. Each path ends with a
    target file below its last directory. names is a surveyed source of name
    lengths, or one byte length for every name."""

    fanout: tuple[int, ...]
    names: str | int = SURVEY

    def __post_init__(self) -> None:
        if not self.fanout:
            raise SpecError("a shape needs at least one level of directories")
        if any(f < 1 for f in self.fanout):
            raise SpecError("every fan-out must be at least 1")

    @property
    def depth(self) -> int:
        """Return the number of directories on each request's path."""
        return len(self.fanout)

    @property
    def leaves(self) -> int:
        """Return how many target files, and so distinct requests, the tree has."""
        return math.prod(self.fanout)

    @property
    def entries(self) -> int:
        """Return how many entries the tree has below its root: every directory
        at every level, and the target files."""
        total, width = 0, 1
        for f in self.fanout:
            width *= f
            total += width
        return total + self.leaves


def generate(shape: Shape, seed: int) -> tuple[Tree, list[str]]:
    """Return the tree that shape and seed determine, and the path of each of
    its target files, in the order they were drawn."""
    names = NameGen(random.Random(seed), shape.names, DEFAULT_CHARS)
    level = [""]  # the directories at the current level, as paths from the root
    for f in shape.fanout:
        level = [
            f"{parent}/{name}" if parent else name
            for parent in level
            for name in (names.fresh(parent, "dir") for _ in range(f))
        ]
    tree = Tree()
    files = []
    for directory in level:
        path = f"{directory}/{names.fresh(directory, 'file')}"
        tree.add(Entry(path, "file"))  # adds every directory above it too
        files.append(path)
    return tree, files


def dentries_of(paths: list[str]) -> set[str]:
    """Return every entry a lookup of each path touches: each of its prefixes,
    the path itself included. The paths must have no symlinks or .. in them."""
    touched: set[str] = set()
    for path in paths:
        parts = path.split("/")
        touched.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return touched


def working_set(files: list[str], dentries: int, seed: int) -> tuple[list[str], int]:
    """Return target files chosen at random from files, using seed, adding one
    at a time until their paths touch at least dentries distinct entries, and
    the exact number they touch. Raise SpecError if all of files touch fewer."""
    order = list(files)
    random.Random(seed).shuffle(order)
    touched: set[str] = set()
    for n, path in enumerate(order, 1):
        touched |= dentries_of([path])
        if len(touched) >= dentries:
            return order[:n], len(touched)
    raise SpecError(
        f"the tree's {len(files)} paths touch {len(touched)} entries, fewer than {dentries}"
    )
