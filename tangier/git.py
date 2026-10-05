"""The only module that shells out to git.

Kept separate so tests can patch a single seam. Import it as a module
(`from tangier import git`) and call `git.changed_files(...)`, never
`from tangier.git import changed_files` — the latter creates a second binding
that `unittest.mock.patch.object` cannot reach.
"""

from __future__ import annotations

import subprocess


class GitError(RuntimeError):
    """A git command failed where the caller cannot continue. Carries git's stderr."""


def _git(*args: str) -> str:
    res = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return res.stdout


def _git_checked(*args: str, input: str | None = None) -> str:
    """Run git and raise `GitError` on a non-zero exit.

    The gate commands use this path: a record must never rest on a git call
    that failed without notice.
    """
    res = subprocess.run(["git", *args], capture_output=True, text=True, check=False, input=input)
    if res.returncode != 0:
        detail = res.stderr.strip() or f"exit {res.returncode}"
        raise GitError(f"git {' '.join(args)}: {detail}")
    return res.stdout


def _git_ok(*args: str) -> bool:
    return subprocess.run(["git", *args], capture_output=True, check=False).returncode == 0


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


# ---------------------------------------------------------------------------
# Helpers for the gate commands. Each raises `GitError` when git fails, except
# `config_get` and `ref_exists`, where a miss is an ordinary answer.
# ---------------------------------------------------------------------------


def _ref_lines(out: str) -> list[tuple[str, str]]:
    """Parse `<sha><tab><ref>` lines into (sha, ref) pairs."""
    pairs: list[tuple[str, str]] = []
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref:
            pairs.append((sha, ref))
    return pairs


def rev_parse(ref: str) -> str:
    """The commit SHA `ref` names."""
    return _git_checked("rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def merge_base(base: str, head: str) -> str:
    """The commit a `base...head` diff starts from. Raises when there is none."""
    return _git_checked("merge-base", base, head).strip()


def rev_list_first_parent(head: str, stop: str) -> list[str]:
    """The commits on `head`'s first-parent line, newest first, that `stop` cannot reach."""
    return _git_checked("rev-list", "--first-parent", head, f"^{stop}").split()


def status_porcelain() -> list[str]:
    """One line per tracked change or untracked file; empty when the tree is clean.

    `--untracked-files` is explicit: a user's `status.showUntrackedFiles = no`
    would otherwise hide untracked files from the clean-tree check.
    """
    out = _git_checked("status", "--porcelain", "--untracked-files=normal")
    return [line for line in out.splitlines() if line]


def config_get(key: str) -> str | None:
    """A git config value, or None when unset. Unset is an ordinary answer, not a failure."""
    return _git("config", "--get", key).strip() or None


def hash_object(text: str) -> str:
    """Write `text` as a blob and return its SHA."""
    return _git_checked("hash-object", "-w", "--stdin", input=text).strip()


def cat_blob(sha: str) -> str:
    return _git_checked("cat-file", "blob", sha)


def update_ref(ref: str, sha: str) -> None:
    _ = _git_checked("update-ref", ref, sha)


def delete_ref(ref: str) -> None:
    _ = _git_checked("update-ref", "-d", ref)


def ref_exists(ref: str) -> bool:
    return _git_ok("show-ref", "--verify", "--quiet", ref)


def for_each_ref(prefix: str) -> list[tuple[str, str]]:
    """(sha, ref) for every local ref under `prefix`."""
    return _ref_lines(_git_checked("for-each-ref", "--format=%(objectname)%09%(refname)", prefix))


def ls_remote(remote: str, pattern: str) -> list[tuple[str, str]]:
    """(sha, ref) for every ref on `remote` that matches `pattern`."""
    return _ref_lines(_git_checked("ls-remote", remote, pattern))


def fetch(remote: str, refspec: str) -> None:
    """Fetch `refspec`, and delete local refs under it that `remote` no longer has."""
    _ = _git_checked("fetch", "--quiet", "--prune", remote, refspec)


def push(remote: str, refspecs: list[str]) -> None:
    _ = _git_checked("push", "--quiet", remote, *refspecs)
