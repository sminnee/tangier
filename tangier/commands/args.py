"""Arguments that the `changemap` and `gate` parsers share, so each flag is defined once."""

from __future__ import annotations

import argparse


def add_diff_args(p: argparse.ArgumentParser, *, head: bool = True) -> None:
    """`--base`, and `--head` unless the command keys the working tree."""
    _ = p.add_argument("--base", default="origin/main")
    if head:
        _ = p.add_argument("--head", default="HEAD")


def add_full(
    p: argparse.ArgumentParser, help: str = "answer as if every tag changed; reads no diff, so --base is ignored"
) -> None:
    _ = p.add_argument("--full", action="store_true", help=help)
