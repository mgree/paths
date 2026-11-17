# Fixture description format

A fixture is one TOML file in `treegen/fixtures/`, named by its file name
without `.toml`. It describes a small tree, one request to resolve a path
inside it and perform some operation, and the outcome a correct strategy must
produce.

## The sandbox

Every fixture is built in a fresh sandbox directory:

```
<sandbox>/
  <root_name>/     the root handed to the strategy ([root] entries)
  ...              everything else ([outside] entries), siblings of the root
```

Escapes are aimed at `[outside]` entries, so the runner can check that
nothing outside the root was opened, created or changed.

## Keys

```toml
hazard = "Symlink escape"            # row in the doc's hazard catalog
about  = "One line on what this checks."
root_name = "root"                   # optional, default "root"
requires  = ["nonroot"]              # optional: nonroot, userns, linux

[root]                               # entries inside the root
dirs      = ["a", "real", "locked", "m"]
files     = ["real/f"]               # contents: see below
fifos     = ["pipe"]                 # named pipes (mkfifo)
symlinks  = { "a/link" = "../../x" } # path = link text, written verbatim
hardlinks = { "h" = "outside:secret" }  # path = an existing file, as <tree>:<path>
modes     = { "locked" = 0o000 }     # permission bits; default 0o755 dirs, 0o644 files

[outside]                            # same keys, relative to the sandbox
files = ["x", "secret", "d/y"]

[[mounts]]                           # optional, one table per mount
at   = "m"                           # a declared [root] directory
type = "bind"                        # bind | proc | casefold
from = "outside:d"                   # bind only: a declared directory, as <tree>:<path>

[request]
op   = "read"                        # read | newfile | write (see below)
path = "a/link"                      # raw string; anything goes

[expect]                             # outcome under RESOLVE_BENEATH-style roots
result = "refused"                   # ok | refused | error
# file  = "real/f"                   # with ok: root-relative path of the file reached
# errno = "ENOENT"                   # with error: the one errno that is correct
# outside_changes = ["x"]            # [outside] files allowed to change; default none

[expect.in_root]                     # optional override for RESOLVE_IN_ROOT-style
result = "error"                     # strategies, which re-anchor absolute paths
errno  = "ENOENT"
```

Fixture metadata:

- `hazard` (required) names the row of the doc's hazard catalog this fixture
  covers, for example `"Symlink escape"`.
- `about` (required) is one line on what the fixture checks.
- `root_name` (optional, default `"root"`) is the root directory's name: one
  path component, not used by any `[outside]` entry.
- `requires` (optional, default none) lists conditions the fixture needs; the
  runner skips it elsewhere.
  + `nonroot`: running as root bypasses permission checks.
  + `userns`: needs to mount inside an unprivileged user namespace.
  + `linux`: depends on Linux-only behavior.
  + `[[mounts]]` implies `userns` and `linux`, so a fixture with mounts need
    not list them.

The trees, `[root]` and `[outside]`, take the same keys. `[root]` paths are
relative to the root, `[outside]` paths to the sandbox. All keys are optional.

- Entry paths must be normal relative paths (no `.`, `..`, empty or absolute
  components). The request path is the only place hostile syntax goes.
- Parent directories are implied: `files = ["a/b/c"]` creates `a` and `a/b`.
- `dirs` lists directories. A directory needs listing only when no other
  entry implies it, as with an empty one.
- `files` lists regular files. Every one holds one line,
  `<fixture> <tree>:<path>` (for example `symlink-escape outside:secret/x`),
  unique across the suite. The runner identifies what a strategy reached by
  (dev, inode); the contents make stray output readable when debugging.
- `fifos` lists named pipes. Opening one blocks until the other end is opened,
  so nothing in our own tools ever opens them.
- `symlinks` maps a link's path to its text, written verbatim. The text may
  dangle, climb with `..` or be absolute.
  + `{root}` and `{sandbox}` expand to the absolute paths of this build, for
    links that must spell a host path. Nothing else is templated.
- `hardlinks` maps a link's path to a file declared in either tree, written
  `root:<path>` or `outside:<path>`. The link and its source are one file
  with one mode.
- `modes` maps a declared directory, file, FIFO or hard link (not a symlink)
  to its permission bits; the defaults are `0o755` for directories and
  `0o644` for the rest. Modes are set after the whole tree exists, so a
  directory can hold entries and still be unsearchable (`0o000` gives
  EACCES).
  + A mode on a hard link or on its source sets the one file's mode. Two
    different modes for one file are an error.

`[[mounts]]` (optional) is a list of mounts inside the root. A fixture with
mounts is built, and its strategies run, in a fresh user, mount and PID
namespace, mapped to the runner's own uid (not root, so permission checks
still apply).

- `at` names a declared `[root]` directory, mounted on at most once.
- `type` is the kind of mount.
  + `bind`: the directory shows another declared directory, named by `from`.
    Nothing may be declared under `at`, since the mount would hide it.
  + `proc`: a procfs, so `<at>/self/root` and `<at>/self/fd/N` are magic
    links. Nothing may be declared under `at`.
  + `casefold`: an empty case-insensitive tmpfs (Unicode case folding, which
    also equates NFC and NFD spellings). It goes on as soon as `at` exists, so
    the entries declared under `at` are built inside it. It is a separate
    filesystem, so no hard link may join a file inside it to one outside.
- Bind and proc mounts go on after modes are set, so a mode never reaches
  through one to its source.

The `[request]` is what the strategy is asked to do; both keys are required.

- `request.op` is the operation to perform at `request.path`.
  + `read` is `O_RDONLY` and the file must exist.
  + `newfile` is `O_WRONLY|O_CREAT|O_EXCL`: the name must not exist, and a
    final symlink is never followed (EEXIST).
  + `write` is `O_WRONLY|O_CREAT|O_TRUNC`, what `creat(2)` and a shell `>`
    do: it creates or truncates, and follows a final symlink.
- `request.path` is a raw string, resolved against the root and handed to
  the strategy unchanged. Anything goes: absolute, `..`, repeated slashes, a
  trailing slash.

The `[expect]` table is the outcome a correct strategy must produce.

- `expect.result` is the expected result.
  + `ok`: the op succeeds. `file` (required) is the root-relative path of the
    file operated on: read, created or truncated. For a `write` through a
    symlink, that is the link's target.
  + `refused`: any error, provided nothing outside the root was touched.
  + `error`: one specific errno, given as `errno` (required, for example
    `"EACCES"`), for fixtures where *which* error matters (a permission error
    must not read as "missing").
- `expect.accept` replaces `result` when more than one outcome is acceptable:
  a list of at least two outcomes, each written as above, for example
  `accept = [{ result = "refused" }, { result = "ok", file = "real/f" }]`.
  Use it for policy choices, and say in `about` what each choice costs.
- `expect.outside_changes` (optional, default none) lists `[outside]` files
  the correct outcome may change. After every fixture the runner compares a
  snapshot of everything outside the root (names, kinds, modes, link text and
  file contents) with the one taken before; nothing else may change. Use it
  only where the correct outcome changes an outside file (a write through a
  hard link).
- `[expect.in_root]` (optional) gives the outcome for strategies with
  RESOLVE_IN_ROOT semantics, which re-anchor absolute paths and `..` at the
  root instead of refusing them. It takes `result` (with `file` or `errno`)
  or `accept`, as above. Without it, those strategies get `[expect]`.
