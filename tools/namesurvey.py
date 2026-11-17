"""Survey name lengths and shapes in a directory tree or a tar listing.

    python tools/namesurvey.py --dir /usr
    curl -sL URL | xz -d | tar -t | python tools/namesurvey.py --tar-list - --label linux

Reports per-entry statistics: name length in bytes, depth, path length,
character classes, and case-fold collisions between siblings.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import unicodedata
from collections import Counter, defaultdict


def fold_key(name: str) -> str:
    """Compatibility caseless form (Unicode D146): NFKD(cf(NFKD(cf(NFD(x)))))."""
    x = unicodedata.normalize("NFD", name).casefold()
    x = unicodedata.normalize("NFKD", x).casefold()
    return unicodedata.normalize("NFKD", x)


class Stats:
    def __init__(self) -> None:
        self.lens: dict[str, Counter] = defaultdict(Counter)  # kind -> byte length -> n
        self.depths: Counter = Counter()
        self.pathlens: list[int] = []
        self.classes: Counter = Counter()
        self.siblings: dict[bytes, list[bytes]] = defaultdict(list)
        self.dirs: set[bytes] = {b""}  # the surveyed top counts as a directory
        self.n = 0

    def add(self, rel: bytes, kind: str) -> None:
        rel = rel.rstrip(b"/")
        if not rel:
            return
        head, _, name = rel.rpartition(b"/")
        self.n += 1
        self.lens[kind][len(name)] += 1
        self.lens["all"][len(name)] += 1
        self.depths[rel.count(b"/") + 1] += 1
        self.pathlens.append(len(rel))
        self.siblings[head].append(name)
        if kind == "dir":
            self.dirs.add(rel)
        c = self.classes
        if any(b >= 0x80 for b in name):
            c["non-ASCII"] += 1
            try:
                name.decode("utf-8")
            except UnicodeDecodeError:
                c["invalid UTF-8"] += 1
        if any(0x41 <= b <= 0x5A for b in name):
            c["uppercase ASCII"] += 1
        if any(0x30 <= b <= 0x39 for b in name):
            c["digit"] += 1
        for ch, label in ((b".", "dot"), (b"-", "dash"), (b"_", "underscore"), (b" ", "space")):
            if ch in name:
                c[label] += 1
        if name.startswith(b"."):
            c["leading dot"] += 1

    def widths(self) -> Counter:
        """Entries per directory, empty directories included."""
        return Counter(len(self.siblings.get(d, ())) for d in self.dirs)

    def fold_collisions(self) -> tuple[int, int]:
        """Directories with siblings that fold together, and names involved."""
        dirs = names = 0
        for kids in self.siblings.values():
            groups = Counter(fold_key(k.decode("utf-8", "surrogateescape")) for k in kids)
            clash = sum(v for v in groups.values() if v > 1)
            if clash:
                dirs += 1
                names += clash
        return dirs, names


def pct(counter: Counter, qs) -> list[int]:
    total = sum(counter.values())
    out, acc, it = [], 0, iter(sorted(counter.items()))
    k, v = next(it)
    for q in qs:
        while acc + v < q * total:
            acc += v
            k, v = next(it)
        out.append(k)
    return out


def pct_list(values: list[int], qs) -> list[int]:
    s = sorted(values)
    return [s[min(len(s) - 1, int(q * len(s)))] for q in qs]


def walk_dir(top: str, st: Stats) -> int:
    errors = 0
    top_b = os.fsencode(top)
    stack = [b""]
    while stack:
        rel = stack.pop()
        try:
            with os.scandir(os.path.join(top_b, rel) if rel else top_b) as it:
                entries = list(it)
        except OSError:
            errors += 1
            continue
        for e in entries:
            r = rel + b"/" + e.name if rel else e.name
            try:
                mode = e.stat(follow_symlinks=False).st_mode
            except OSError:
                errors += 1
                continue
            if stat.S_ISLNK(mode):
                st.add(r, "symlink")
            elif stat.S_ISDIR(mode):
                st.add(r, "dir")
                stack.append(r)
            else:
                st.add(r, "file")
    return errors


def read_tar_list(f, st: Stats) -> None:
    lines = [ln.rstrip(b"\n") for ln in f]
    # Drop the archive's single top-level directory (linux-x.y.z/).
    tops = {ln.split(b"/", 1)[0] for ln in lines if ln}
    strip = len(tops) == 1
    for ln in lines:
        if strip:
            ln = ln.split(b"/", 1)[1] if b"/" in ln else b""
        if ln:
            st.add(ln, "dir" if ln.endswith(b"/") else "file")


QS = (0.10, 0.25, 0.50, 0.75, 0.90, 0.99, 1.0)


def report(label: str, st: Stats, errors: int) -> None:
    print(f"== {label}: {st.n} entries" + (f" ({errors} unreadable, skipped)" if errors else ""))
    print(
        f"   {'name bytes':<16}" + "".join(f"{f'p{int(q * 100)}':>6}" for q in QS) + f"{'mean':>7}"
    )
    for kind in ("all", "dir", "file", "symlink"):
        c = st.lens.get(kind)
        if not c:
            continue
        mean = sum(k * v for k, v in c.items()) / sum(c.values())
        print(
            f"   {kind + f' ({sum(c.values())})':<16}"
            + "".join(f"{x:>6}" for x in pct(c, QS))
            + f"{mean:>7.1f}"
        )
    print(f"   {'dir entries':<16}" + "".join(f"{x:>6}" for x in pct(st.widths(), QS)))
    print(f"   {'depth':<16}" + "".join(f"{x:>6}" for x in pct(st.depths, QS)))
    print(f"   {'path bytes':<16}" + "".join(f"{x:>6}" for x in pct_list(st.pathlens, QS)))
    shares = ", ".join(f"{k} {100 * v / st.n:.1f}%" for k, v in st.classes.most_common())
    print(f"   names containing: {shares}")
    d, n = st.fold_collisions()
    print(f"   case-fold collisions: {n} names in {d} directories")
    c = st.lens["all"]
    buckets = [
        (1, 4),
        (5, 8),
        (9, 12),
        (13, 16),
        (17, 24),
        (25, 32),
        (33, 64),
        (65, 128),
        (129, 255),
    ]
    print(
        "   histogram (all): "
        + ", ".join(
            f"{lo}-{hi}: {100 * sum(v for k, v in c.items() if lo <= k <= hi) / st.n:.1f}%"
            for lo, hi in buckets
        )
    )


def print_toml(label: str, st: Stats) -> None:
    print(
        f"# Name lengths in bytes and directory widths, surveyed from {label} by tools/namesurvey.py --toml."
    )
    print("# Each pair is [length, count]. Generated once and committed; do not edit by hand.")
    print(f'source = "{label}"')
    for kind in ("dir", "file"):
        pairs = ", ".join(f"[{k}, {v}]" for k, v in sorted(st.lens[kind].items()))
        print(f"{kind} = [{pairs}]")
    pairs = ", ".join(f"[{k}, {v}]" for k, v in sorted(st.widths().items()))
    print("# Entries per directory, empty directories included.")
    print(f"width = [{pairs}]")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dir")
    g.add_argument("--tar-list", metavar="FILE", help="tar -t output; - for stdin")
    ap.add_argument("--label")
    ap.add_argument(
        "--toml",
        action="store_true",
        help="print dir and file name-length histograms as a TOML snippet to commit",
    )
    a = ap.parse_args()
    st = Stats()
    errors = 0
    if a.dir:
        errors = walk_dir(a.dir, st)
    else:
        if a.tar_list == "-":
            read_tar_list(sys.stdin.buffer, st)
        else:
            with open(a.tar_list, "rb") as f:
                read_tar_list(f, st)
    if a.toml:
        print_toml(a.label or a.dir or "tar", st)
    else:
        report(a.label or a.dir or "tar", st, errors)


if __name__ == "__main__":
    main()
