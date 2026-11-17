# Driver protocol

A *driver* runs one language's path-handling strategies and reports what
happened. The correctness runner and the benchmark harness build the trees,
invoke drivers, and judge and record the results. Drivers know nothing about
fixtures, expectations or statistics.

## Commands

A given driver command `DRIVER` takes the following command-line arguments:

```
DRIVER list
DRIVER once                    <strategy> <op> <root> <path>
DRIVER race          <seconds> <strategy> <op> <root> <path>
DRIVER time <warmup> <seconds> <strategy> <op> <root> <path>
```

There are four modes:

- `list` names the driver's strategies.
- `once` makes one call: correctness checks, and cold-cache timing.
- `race` makes calls until `<seconds>` have passed; this mode is used when the
  runner changes the tree underneath.
- `time` makes calls for `<warmup>` seconds unmeasured, then for `<seconds>`
  measured; this mode is used for benchmarking.

Arguments:

- `<strategy>` is one of the implemented strategies (i.e., from `driver list`).
- `<op>` is the operation, as in fixtures.
  + `read`: `O_RDONLY`.
  + `newfile`: `O_WRONLY|O_CREAT|O_EXCL`.
  + `write`: `O_WRONLY|O_CREAT|O_TRUNC`.

  Created files get mode `0644` exactly: drivers clear their umask.
  `race` and `time` do not take `newfile`, whose second call would fail with
  `EEXIST`.
- `<root>` is the absolute path of the root. Before the first call, the driver
  prepares whatever the strategy keeps (a root descriptor, a string).
- `<path>` is the request, raw bytes, handed to the strategy unchanged. It is
  always the last argument and never parsed as an option, so it may be empty,
  begin with `-`, or be absolute.
- `<seconds>` and `<warmup>` are decimal, and may have a fractional part. Both
  are in seconds.

## A single call

The strategy does just one `op` request per _call_: resolve `<path>` under the
root, open it with the `op`'s flags, and close the descriptor. In `once` mode
the driver makes one call; the `race` and `time` modes repeat the same call.

In `once` and `race` modes the driver also runs `fstat` on the descriptor
before closing it, to identify the file reached. `time` mode skips the
`fstat`, which would otherwise be measured; `once` mode stops its clock for it.

## Output

Standard output carries only these lines. Standard error is free text (error
messages, etc.): the runner records it but never parses it.

```
OUTCOME <count> ok <dev> <ino>
OUTCOME <count> ok
OUTCOME <count> error <label>
TIME <nanoseconds>
HIST <lower> <count>
```

- One `OUTCOME` line per distinct outcome. The counts add up to the number of
  measured calls; `once` prints a single line with count 1.
  + `ok <dev> <ino>`: the open succeeded. `<dev>` and `<ino>` are the opened
    file's `st_dev` and `st_ino`, in decimal.
  + `ok` alone: the open succeeded, in `time`, which does not `fstat`.
  + `error <label>`: the strategy failed. `<label>` names the error, without
    spaces. It is the errno's name (`ENOENT`) whenever the language exposes
    an errno, even one wrapped inside another error; otherwise it is the
    language's own stable identifier, such as the Java exception
    `NoSuchFileException`.
  + Any of an errno's names will do where it has several (`EAGAIN` and
    `EWOULDBLOCK`, say): the runner compares errno labels by their value on
    the host. An errno the driver cannot name is `E<number>`, such as `E133`.
- `TIME` (from `once` and `time`) is the total time of the measured calls, in
  nanoseconds, measured by the driver around the calls alone. The mean is
  `TIME` divided by the `OUTCOME` counts' sum.
- `HIST` (from `time` only) describes the distribution: `time` reads the clock
  around every call, and prints one line per nonempty bucket, in increasing
  order of `<lower>`. `<count>` is the number of calls in that bucket.
  + Buckets are fixed by the protocol, so histograms from any driver can be
    compared and merged. Below 16 ns each bucket is 1 ns wide; within
    `[2^k, 2^(k+1))` for k >= 4 each is `2^(k-4)` ns wide, 16 to a power of
    two, so a bucket is at most 1/16 as wide as its lower bound.
  + A call taking `v` ns falls in the bucket whose `<lower>` is `v` rounded
    down to a multiple of its width.

`list` prints one line per strategy:

```
STRATEGY <name> <semantics>
UNAVAILABLE <name> <semantics> <reason>
```

- `<name>` uses `a-z`, `0-9` and `-`, and is unique within the driver.
- `<semantics>` picks the expectations that judge the strategy: `beneath`
  (a fixture's `[expect]`) or `in_root` (`[expect.in_root]`).
- `UNAVAILABLE` marks a strategy this host cannot run (no `openat2`, io_uring
  switched off); `<reason>` is the rest of the line.

## Exit status

- 0: the command ran, and standard output holds its result. A strategy's
  errors are outcomes, not failures.
- Anything else, including death by a signal or a timeout: a driver failure
  (bad arguments, an unavailable strategy, a crash). The runner records a
  failure, never an outcome, so a strategy that crashes cannot pass a fixture
  that accepts `refused`.

## Environment

- Drivers run with an empty working directory in the sandbox, outside the
  root, so a strategy that ignores `<root>` fails the controls, and anything
  it creates shows up as a change outside the root.
- For fixtures with mounts, the runner starts the driver inside the fixture's
  namespace. Nothing changes for the driver.

## Not yet specified

Threads, retry counts (`openat2`'s `EAGAIN`), `newfile` in loops, and requests
containing NUL bytes.
