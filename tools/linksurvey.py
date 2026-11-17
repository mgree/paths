"""Survey symlinks in a directory tree or a verbose tar listing.

    python tools/linksurvey.py --dir /usr
    curl -sL URL | xz -d | tar -tv | python tools/linksurvey.py --tar-list - --label linux

For each link: absolute or relative; how far its target climbs (leading ..)
and descends; tree distance from the link's directory to the target; whether
the target leaves the surveyed tree; and, for directories on disk, whether it
dangles, how long its chain of links-to-links is, and whether lexical and
physical resolution of the target disagree.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import stat
import sys
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class Stats:
    n: int = 0
    kinds: Counter = field(default_factory=Counter)  # absolute / relative
    relation: Counter = field(default_factory=Counter)  # sibling, descendant, ...
    up: Counter = field(default_factory=Counter)  # leading .. in relative targets
    distance: Counter = field(default_factory=Counter)  # up + down, lexical
    comps: Counter = field(default_factory=Counter)  # components in the target text
    flags: Counter = field(default_factory=Counter)
    chain: Counter = field(default_factory=Counter)
    target_kind: Counter = field(default_factory=Counter)


def relation(link_dir: str, target: str) -> tuple[str, int, int]:
    """Lexical relation of a target (relative to the top, normalized) to the
    link's directory: (name, up, down)."""
    a = [p for p in link_dir.split("/") if p]
    b = [p for p in target.split("/") if p]
    common = 0
    while common < min(len(a), len(b)) and a[common] == b[common]:
        common += 1
    up, down = len(a) - common, len(b) - common
    if up == 0 and down == 0:
        name = "self (the link's own directory)"
    elif up == 0 and down == 1:
        name = "sibling"
    elif up == 0:
        name = "descendant of a sibling"
    elif down == 0:
        name = "ancestor directory"
    elif down == 1 and up == 1:
        name = "parent's sibling"
    else:
        name = "cousin (up, then down)"
    return name, up, down


def classify(st: Stats, link_rel: str, text: str, top_abs: str | None) -> str | None:
    """Record the text-only properties; return the target relative to the
    top (lexically normalized), or None when it is outside the tree."""
    st.n += 1
    link_dir = posixpath.dirname(link_rel)
    parts = [p for p in text.split("/") if p not in ("", ".")]
    st.comps[min(len(parts), 10)] += 1
    if text.endswith("/"):
        st.flags["trailing slash in target"] += 1
    seen_name = False
    for p in parts:
        if p == "..":
            if seen_name:
                st.flags["'..' after a name inside the target"] += 1
                break
        else:
            seen_name = True
    if text.startswith("/"):
        st.kinds["absolute"] += 1
        if top_abs is None:
            st.relation["absolute (tar: cannot place)"] += 1
            return None
        norm = posixpath.normpath(text)
        if norm != top_abs and not norm.startswith(top_abs.rstrip("/") + "/"):
            st.relation["absolute, outside the surveyed tree"] += 1
            return None
        target = posixpath.relpath(norm, top_abs)
        target = "" if target == "." else target
        st.flags["absolute but inside the surveyed tree"] += 1
    else:
        st.kinds["relative"] += 1
        lead = 0
        for p in parts:
            if p != "..":
                break
            lead += 1
        st.up[min(lead, 10)] += 1
        target = (
            posixpath.normpath(posixpath.join(link_dir, text))
            if link_dir
            else posixpath.normpath(text)
        )
        if target == ".":
            target = ""
        if target == ".." or target.startswith("../"):
            st.relation["relative, climbs out of the surveyed tree"] += 1
            return None
    name, up, down = relation(link_dir, target)
    st.relation[name] += 1
    st.distance[min(up + down, 12)] += 1
    return target


def walk_dir(top: str, st: Stats, dirs_only: bool = False) -> int:
    top_abs = os.path.realpath(top)
    errors = 0
    for dirpath, dirnames, filenames in os.walk(top_abs, onerror=lambda _: None):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            try:
                if not stat.S_ISLNK(os.lstat(path).st_mode):
                    continue
                text = os.readlink(path)
            except OSError:
                errors += 1
                continue
            rel = os.path.relpath(path, top_abs)
            if dirs_only:
                try:
                    if not stat.S_ISDIR(os.stat(path).st_mode):
                        continue
                except OSError:
                    continue
            classify(st, rel, text, top_abs)
            # On-disk extras: dangling, chain length, target kind, lexical vs physical.
            hops, p = 0, path
            while hops <= 40:
                try:
                    if not stat.S_ISLNK(os.lstat(p).st_mode):
                        break
                    t = os.readlink(p)
                except OSError:
                    break
                p = t if t.startswith("/") else os.path.join(os.path.dirname(p), t)
                hops += 1
            st.chain[min(hops, 6) if hops <= 40 else "loop/over 40"] += 1
            try:
                s = os.stat(path)
                st.target_kind["directory" if stat.S_ISDIR(s.st_mode) else "file"] += 1
                physical = os.path.realpath(path)
                lexical = os.path.realpath(posixpath.normpath(os.path.join(dirpath, text)))
                if physical != lexical:
                    st.flags["lexical and physical resolution disagree"] += 1
            except OSError:
                st.target_kind["dangling"] += 1
    return errors


def read_tar(f, st: Stats) -> None:
    rows = []
    for raw in f:
        line = raw.decode("utf-8", "surrogateescape").rstrip("\n")
        if not line.startswith("l") or " -> " not in line:
            continue
        # perms owner size date time path -> target
        head, target = line.split(" -> ", 1)
        path = head.split(None, 5)[5]
        rows.append((path, target))
    tops = {p.split("/", 1)[0] for p, _ in rows}
    strip = len(tops) == 1
    for path, text in rows:
        if strip:
            path = path.split("/", 1)[1]
        classify(st, path, text, None)


def show(title: str, c: Counter, n: int) -> None:
    if not c:
        return
    items = sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))
    print(f"   {title}: " + ", ".join(f"{k} {100 * v / n:.1f}%" for k, v in items))


def report(label: str, st: Stats, errors: int) -> None:
    print(f"== {label}: {st.n} symlinks" + (f" ({errors} unreadable)" if errors else ""))
    if not st.n:
        return
    show("target", st.kinds, st.n)
    show("relation", st.relation, st.n)
    rel = st.kinds["relative"]
    if rel:
        show(
            "leading '..' (relative links)",
            Counter({f"{k}{'+' if k == 10 else ''}": v for k, v in st.up.items()}),
            rel,
        )
    placed = sum(st.distance.values())
    if placed:
        show("tree distance up+down (placed targets)", st.distance, placed)
    show("components in target text", st.comps, st.n)
    show("chain length (links to links)", st.chain, st.n)
    show("target kind", st.target_kind, st.n)
    for k, v in st.flags.most_common():
        print(f"   {k}: {v} ({100 * v / st.n:.1f}%)")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dir")
    g.add_argument("--tar-list", metavar="FILE", help="tar -tv output; - for stdin")
    ap.add_argument("--label")
    ap.add_argument(
        "--dirs-only",
        action="store_true",
        help="only links whose target is a directory (what a path traverses mid-way)",
    )
    a = ap.parse_args()
    st = Stats()
    errors = 0
    if a.dir:
        errors = walk_dir(a.dir, st, a.dirs_only)
    elif a.tar_list == "-":
        read_tar(sys.stdin.buffer, st)
    else:
        with open(a.tar_list, "rb") as f:
            read_tar(f, st)
    report(a.label or a.dir or "tar", st, errors)


if __name__ == "__main__":
    main()
