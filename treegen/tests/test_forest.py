"""Tests for the benchmark tree generator: that a shape and a seed determine
the tree, that the tree has the shape's size, and that a working set is the
smallest prefix of its shuffled leaves to reach its size.

The properties must hold for every seed, so each run draws fresh seeds at
random. A test's id names its seed, so a failure can be rerun with it."""

import random

import pytest
from treegen.forest import FANOUT, Shape, dentries_of, generate, shape_for, working_set
from treegen.spec import SpecError

SHAPES = [
    Shape((5,)),
    Shape((3, 1, 4, 2)),
    Shape((6, 6) + (1,) * 14),
    Shape((2, 2, 2), names=200),
]

SEEDS = [random.randrange(2**32) for _ in range(10)]

with_seeds = pytest.mark.parametrize("seed", SEEDS, ids=lambda s: f"seed={s}")
with_shapes = pytest.mark.parametrize("shape", SHAPES, ids=str)


@with_seeds
@with_shapes
def test_a_shape_and_seed_determine_the_tree(shape, seed):
    assert generate(shape, seed) == generate(shape, seed)
    assert generate(shape, seed)[1] != generate(shape, seed + 1)[1]


@with_seeds
@with_shapes
def test_the_tree_has_the_shapes_size(shape, seed):
    tree, files = generate(shape, seed)
    assert len(tree.entries) == shape.entries
    assert len(set(files)) == len(files) == shape.leaves
    assert all(len(f.split("/")) == shape.depth + 1 for f in files)
    assert all(tree.entries[f].kind == "file" for f in files)


@with_seeds
def test_a_working_set_is_the_fewest_paths_that_reach_its_size(seed):
    shape = Shape((4, 4, 4) + (1,) * 5)
    _, files = generate(shape, seed)
    rng = random.Random(seed)
    for size in (1, rng.randrange(2, shape.entries), shape.entries):
        chosen, count = working_set(files, size, seed)
        assert count == len(dentries_of(chosen)) >= size
        assert len(dentries_of(chosen[:-1])) < size


def test_a_tree_too_small_for_the_working_set_is_refused():
    shape = Shape((2, 2))
    _, files = generate(shape, 1)
    with pytest.raises(SpecError, match="fewer than"):
        working_set(files, shape.entries + 1, 1)


@pytest.mark.parametrize("depth", [1, 4, 16, 64])
@pytest.mark.parametrize("entries", [1, 37, 5_000, 150_000])
def test_shape_for_is_big_enough_and_branches_near_the_root(depth, entries):
    shape = shape_for(depth, entries)
    assert shape.depth == depth and shape.entries >= entries
    # Levels that branch come first, and only the deepest of them is either
    # narrower than FANOUT or, when nothing else is enough, wider.
    branching = [f for f in shape.fanout if f > 1]
    assert shape.fanout[: len(branching)] == tuple(branching)
    assert all(f == FANOUT for f in branching[:-1])
    # One fewer at the last branching level would be too small.
    if branching:
        k = len(branching) - 1
        narrower = shape.fanout[:k] + (shape.fanout[k] - 1,) + shape.fanout[k + 1 :]
        assert Shape(narrower).entries < entries
