"""The only module that shells out to git.

Kept separate so tests can patch a single seam. Import it as a module
(`from tangier import git`) and call `git.changed_files(...)`, never
`from tangier.git import changed_files` — the latter creates a second binding
that `unittest.mock.patch.object` cannot reach.
"""

from __future__ import annotations

import subprocess


def _git(*args: str) -> str:
    res = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return res.stdout


def changed_files(base: str, head: str) -> list[str]:
    """Files changed between `base` and `head`, as repo-relative paths.

    `check=False` is deliberate: an unresolvable ref yields an empty diff
    ("nothing changed") rather than an error. CI depends on that — a shallow
    clone without `origin/main` must degrade to running nothing selective,
    not explode.
    """
    out = _git("diff", "--name-only", f"{base}...{head}")
    return [line for line in out.splitlines() if line]


def ls_tree(head: str, paths: list[str]) -> list[str]:
    """Raw `git ls-tree -r` lines (`<mode> <type> <sha>\\t<path>`) for `paths`.

    Returned undecoded-then-decoded as text lines; the caller hashes the joined
    lines, so the exact bytes matter.
    """
    result = subprocess.run(["git", "ls-tree", "-r", head, *paths], capture_output=True, check=False)
    return result.stdout.decode().splitlines()


def ls_tree_path(line: str) -> str:
    """The path portion of a `git ls-tree -r` line."""
    _, _, rest = line.partition("\t")
    return rest
