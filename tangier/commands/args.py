"""Arguments that the `changemap` and `gate` parsers share, so each flag is defined once."""

from __future__ import annotations

import argparse


def add_diff_args(p: argparse.ArgumentParser, *, head: bool = True) -> None:
    """`--base`, and `--head` unless the command keys the working tree."""
    _ = p.add_argument("--base", default="origin/main")
    if head:
        _ = p.add_argument("--head", default="HEAD")
