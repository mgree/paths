# Benchmark directory forests

This is a notes document, planning for future benchmarking work.

Benchmarks use two kinds of tree:

- **Realistic trees** are real filesystem contents: a Debian root built by
  mmdebstrap from a pinned snapshot.debian.org date with the popularity-contest
  top 100 packages (queries under /usr and /etc), and the Linux 7.3.4 source.
  We publish the recipe, not the image. Realistic cells use RESOLVE_IN_ROOT
  semantics, since the root is a container-style rootfs whose absolute links
  (most of /etc) mean the image's paths; host-escaping resolutions by naive
  strategies are logged as outcomes. Unprivileged extraction loses ownership,
  setuid bits and device nodes.
- **Adversarial trees** are generated, in a **directory forest**: several
  independent trees under one sandbox directory, built together from one seed.
  Names and widths follow the surveyed norms where cheap; the shapes are
  openly unrealistic where the stress needs it. The rest of this file
  describes them.

Queries are found, not built in: the query generator indexes a tree (either
kind) and searches for a path matching a query config, choosing
deterministically from the seed, or reports why none exists. Not every tree
serves every query; the sweep file pairs query configs with trees, and
validation rejects pairs the tree can't serve.

Features beyond the spine are placed only at the levels queries need
(per-tree settings such as `chain`, `failures_at`, `cousin_at`), so most
directories keep their drawn width. Target files, ENOTDIR files and detour
directories come from filler where it exists; the generator converts a filler
slot when a level needs one and has none.

## Terms

- **Tree config**: the settings that fix what a tree's entries are: name
  lengths, characters, width, maximum depth, link capacity. With a seed it
  determines exactly one tree.
- **Forest**: one tree per tree config in `treegen/forest.toml`, plus a
  shared `outside/` directory, built from one seed. We build 10 forests (10 seeds).
- **Query config**: the shape of one request against a tree: depth, symlink
  hops and where they sit, `..` steps (and how many follow a cousin link),
  last component, failure kind and position, operation (read, newfile or write).
- **Query**: a concrete path chosen in a given tree to match a query config,
  plus its expected outcome. Query validation rejects a config the tree can't
  serve (too deep, too many hops for its link capacity).
- **Cell**: tree config x query config x strategy x harness settings (cache,
  threads, churn), on one platform and filesystem. One row of results.
- **Run**: one fresh process measuring one cell on one forest: warm-up, then
  one measured second. Each cell gets 3 runs on each of the 10 forests.
  Cold-cache cells use a different protocol (drop caches, time one call,
  repeat), defined with the harness.

## Layout

```
<sandbox>/
  outside/            sentinel files; reached only by escapes
  survey/             each tree is a root handed to strategies
  len4/ len16/ ...
```

A tree is a **spine** of level directories `D0` (the tree's root) to
`D(max_depth-1)`. Each level holds the spine entry; the other **features**
appear only at the levels its tree config places them:

| Feature | Entry | Serves |
|---|---|---|
| spine | `D(k+1)/` | the path itself (not at the last level) |
| sibling chain | `l1 -> D(k+1)`, `l2 -> l1`, ... | symlink hops: entering at `lj` costs j hops |
| cousin link | `c -> D(k+1)/e` | `c/..` lands in `D(k+1)` physically, `Dk` lexically |
| empty dir | `e/` | `X/..` detours; the cousin link's target |
| target file | `f` | depth-k queries end here; `f/x` gives ENOTDIR |
| file link | `fl -> f` | queries that end on a link, the common real case |
| loop pair | `la -> lb`, `lb -> la` | ELOOP by cycling |
| escape link | `esc -> ../`x(k+1)`outside/...` | escapes: leaves the tree, lands in `outside/` |
| locked dir | `locked/`, mode 000 | EACCES |
| filler | files and empty dirs | makes up the drawn width |

Rules:

- Features count toward the level's width; filler makes up the rest. A
  level is wider than drawn only when its features alone exceed the draw.
- Links resolve inside their own tree, or (escape links only) into
  `outside/`. Never into another tree.
- Benign links are relative, stay inside the tree, and never start with `..`.
- Names in `D(k+1)` never fold onto names in `Dk`, so a lexical strategy that
  cancels `c/..` gets a clean ENOENT, never a wrong file.
- Absolute links appear only in correctness fixtures (scoped lookup refuses
  or re-anchors them, so they are a policy question).
- Chain capacity: a query of depth d with h hops spread over d-1 levels needs
  ceil(h / (d-1)) links per level; one past the limit (41 on Linux, 33 on
  macOS/FreeBSD) must be reachable where ELOOP-by-count is tested.

- Escape links climb exactly out of their tree: at level k, k+1 `../`
  components, then `outside/<dir>`. `outside/` holds only sentinel files that
  no legitimate query reaches, so reaching one is an unambiguous escape.

## Trees in a forest

| Tree | Names | Width | Max depth | Chain | Serves |
|---|---|---|---|---|---|
| `survey` | survey lengths, default chars | survey | 64 | 1 | depth sweep, low hop counts, failures, `..` and cousin links |
| `links` | survey | survey (inflated by chains, and says so) | 16 | 14 | 40 and 41 hops, the depth x symlinks grid |
| `len4`, `len16`, `len64` | fixed bytes | survey | 16 | 1 | baseline query |
| `len200` | 200 bytes | survey | 25 | 1 | over-PATH_MAX (about 5,000 bytes) |
| `ascii`, `unicode` | char mixes | survey | 16 | 1 | baseline query |
| `width16`, `width256`, `width4096` | survey | fixed | 16 | 1 | baseline query, cold cache |

Failure features (`failures_at`) sit at components 2 and 15, the doc's early
and late positions. Default character weights: lower 60, upper 10, digit 10,
punct (`._-`) 10, latin 4, greek 2, cyrillic 2, cjk 1, emoji 1, about 10%
non-ASCII. Filler is an empty directory with the survey's
share (about 6%), otherwise a file. Name lengths are UTF-8 bytes; a draw
finishes with ASCII to land exactly.

Forests also serve as the per-thread copies for multi-threaded cells.

## Queries that need care

- **ELOOP by count**: always pair the 41-hop query (must fail) with the
  40-hop query on the same levels (must succeed). ELOOP also means "final
  component is a symlink" under O_NOFOLLOW and "any symlink" under
  RESOLVE_NO_SYMLINKS, so the 41-hop failure alone can't tell a correct
  strategy from one that refuses every link.
- **`..` after a cousin link**: physical strategies continue in `D(k+1)`;
  lexical ones (S1) land in `Dk` and get ENOENT, by the no-fold rule above.
- **newfile and write queries** change the tree: they leave created files
  behind, and a write to an existing file truncates it. Unlink created names
  in batches outside the timed region, aim writes only at files no other
  query reads, and compare the tree (contents included) before and after a
  run to confirm nothing else changed.

## Changing trees

- Restore by rebuilding from the seed: same names and shape, but new inode
  numbers, block placement and ext4 hash order. If cold-cache runs vary
  between rebuilds, build each forest once into a filesystem image instead.
- Churn and race runs mutate the tree, so they get their own copy of the
  forest, never the one other cells read.
- Every results record carries the tree's checksum.

## Sanity cells

- Links present but unused: a 0-hop query on `links` vs the same query on
  `survey` (warm cache: should not differ; lookup is by hash).
- Forest size: a lone spine vs the same spine inside the full forest.

## Generator health checks (carry these over)

- Build matches spec: scan the built forest back and compare it entry for
  entry with the prediction from the config, on tmpfs and ext4.
- Determinism: the same seed gives an identical forest; another seed changes
  names. Golden listings only for generator output (a change there is a real
  event), never for anything that just mirrors its input.
- Exact widths: each level has max(drawn width, its features) entries,
  off-spine directories are empty. Check that the test fails when filler is off
  by one.
- Every link, resolved on paper, lands in its own tree or in `outside/`.
- No siblings fold together; names in `D(k+1)` don't fold onto `Dk`.
- For each query: a step-by-step walk that never lets the kernel follow
  anything confirms the depth, hops and `..` count it claims; ordinary kernel
  lookup reaches the expected file by (dev, inode), or the expected errno.
  Paths over PATH_MAX are opened in chunks split at component boundaries,
  measured in **bytes, not characters** (multi-byte names broke a
  character-count version). The over-PATH_MAX query must really fail a plain
  open with ENAMETOOLONG.
- Validation rejects queries a tree can't serve, with the reason.

## Surveyed reference data (tools/namesurvey.py, tools/linksurvey.py)

Linux 7.3.4 source: names median 5 bytes (dirs) and 12 (files); median 6
entries per directory, p99 182. Real directory links: about half siblings, a
third cousins a few levels away; chains rarely past 2. Our chains are
deliberate stress.

Width draws are uniform over directories. A path to a real file passes
through bigger directories than average, so a path-weighted distribution
would be wider; revisit only if cold-cache results turn out sensitive to
width.
