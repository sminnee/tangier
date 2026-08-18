"""GitHub Actions' env-var-file protocol, in one place.

Actions passes step outputs and job summaries through files named by
`$GITHUB_OUTPUT` and `$GITHUB_STEP_SUMMARY` rather than through stdout. Both
functions here degrade to plain stdout when those variables are unset, so every
command that uses them behaves identically on a laptop and on a runner.
"""

from __future__ import annotations

import os


def emit_outputs(pairs: dict[str, str]) -> None:
    """Append `key=value` lines to `$GITHUB_OUTPUT`, and always echo to stdout.

    The echo is unconditional: it is what makes these commands readable in a
    terminal, and `changemap github-outputs` has carried that contract since
    before the extraction.
    """
    lines = [f"{key}={value}" for key, value in pairs.items()]
    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path:
        with open(out_path, "a") as fh:
            for line in lines:
                _ = fh.write(line + "\n")
    for line in lines:
        print(line)


def write_summary(markdown: str) -> bool:
    """Append `markdown` to `$GITHUB_STEP_SUMMARY`; return False when unset.

    Returning a bool rather than falling back to stdout keeps the choice with
    the caller: `deploy --summary` prints the table itself when there is no
    summary file, which is what makes one invocation work in both places.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    with open(path, "a") as fh:
        _ = fh.write(markdown if markdown.endswith("\n") else markdown + "\n")
    return True
