"""Glob matching with `**` support.

Load-bearing for SHA parity: `_glob_to_regex` decides which files enter a
bucket's content hash. `**` converts to `.*`, which matches the empty string —
so `**/README.md` excludes a root-level `README.md` as well as nested ones.
Changing that changes every bucket SHA in every consuming repo.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Compiling the same glob repeatedly is the inner loop of a bucket walk over
# thousands of ls-tree lines, so the compiled patterns are memoised.
_CACHE: dict[str, re.Pattern[str]] = {}


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Convert a glob to an anchored regex.

    `**` matches any characters including `/`; `*` matches any character except
    `/`; `?` matches exactly one non-`/` character.
    """
    cached = _CACHE.get(glob)
    if cached is not None:
        return cached
    out: list[str] = []
    i = 0
    while i < len(glob):
        ch = glob[i]
        if ch == "*" and i + 1 < len(glob) and glob[i + 1] == "*":
            out.append(".*")
            i += 2
            # Swallow the separator after `**` so `dir/**` matches `dir` itself.
            if i < len(glob) and glob[i] == "/":
                i += 1
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch in ".+()[]{}|^$\\":
            out.append("\\" + ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    pattern = re.compile("^" + "".join(out) + "$")
    _CACHE[glob] = pattern
    return pattern


def matches(path: str, globs: Iterable[str]) -> bool:
    """Whether `path` matches any of `globs`."""
    # An explicit loop rather than `any(...)`: this runs once per ls-tree line
    # per contributing tag during a bucket walk, and the generator overhead is
    # measurable there.
    for g in globs:  # noqa: SIM110
        if glob_to_regex(g).match(path):
            return True
    return False


def globs_to_ls_tree_paths(globs: Iterable[str]) -> list[str]:
    """Reduce globs to path prefixes `git ls-tree -r` matches natively.

    `dir/**` -> `dir`; everything else passes through unchanged. This is what
    keeps bucket SHAs byte-identical to the legacy `git ls-tree -r HEAD <dir>`
    pipeline — do not "improve" it.
    """
    paths: list[str] = []
    for g in globs:
        if g.endswith("/**"):
            paths.append(g[: -len("/**")])
        else:
            paths.append(g)
    return paths
