"""tangier CLI."""

from __future__ import annotations

import argparse
import os
import sys

from tangier.commands import changemap_cmds, deploy_cmds, image_cmds
from tangier.config import ConfigError, read_config
from tangier.deploy import DeployError
from tangier.image import ImageError

# The config lives in the repo tangier runs from — each project carries its own
# pipeline.toml at its root. TANGIER_CONFIG lets the parity harness point both
# implementations at the same file without copying or symlinking it.
DEFAULT_CONFIG = os.environ.get("TANGIER_CONFIG") or "pipeline.toml"


def cli_main() -> None:
    """Console-script entry point."""
    raise SystemExit(main())


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        config = read_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    try:
        return args.func(config, args) or 0
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except (ImageError, DeployError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _add_diff_args(p: argparse.ArgumentParser) -> None:
    _ = p.add_argument("--base", default="origin/main")
    _ = p.add_argument("--head", default="HEAD")


def _add_no_expand(p: argparse.ArgumentParser) -> None:
    _ = p.add_argument(
        "--no-expand",
        action="store_true",
        help="don't expand matched tags via depends (debug)",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tangier",
        description="content-addressed CI/deploy pipeline toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ = p.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"path to TOML config (default: {DEFAULT_CONFIG})",
    )
    sub = p.add_subparsers(dest="group")
    _add_changemap_parser(sub)
    image_cmds.add_parsers(sub)
    deploy_cmds.add_parsers(sub)
    return p


def _add_changemap_parser(sub: argparse._SubParsersAction) -> None:
    cm = sub.add_parser("changemap", help="which parts of the repo does this diff touch?")
    cmsub = cm.add_subparsers(dest="cmd", required=True)

    lp = cmsub.add_parser("list", help="print all tags and their globs")
    _ = lp.add_argument(
        "--graph",
        action="store_true",
        help="print each tag with its depends entries indented underneath",
    )
    lp.set_defaults(func=changemap_cmds.cmd_list)

    sp = cmsub.add_parser("sha", help="git-tree content hash for a SHA bucket")
    _ = sp.add_argument("bucket", nargs="?")
    _ = sp.add_argument("--all", action="store_true")
    _ = sp.add_argument("--github-notice", action="store_true")
    _ = sp.add_argument("--head", default="HEAD", help="git ref to hash at (default: HEAD)")
    sp.set_defaults(func=changemap_cmds.cmd_sha)

    ip = cmsub.add_parser("items", help="project the expanded changed set into a named item list")
    _ = ip.add_argument("name", help="items name (e.g. `unittest`, `e2e`)")
    _add_diff_args(ip)
    _add_no_expand(ip)
    ip.set_defaults(func=changemap_cmds.cmd_items)

    li = cmsub.add_parser(
        "list-ignored",
        help="list changed files in the diff that matched no tag — sanity-check helper",
    )
    _add_diff_args(li)
    li.set_defaults(func=changemap_cmds.cmd_list_ignored)

    ep = cmsub.add_parser("explain", help="show modified tags, dependents, and resulting CI invocations")
    _add_diff_args(ep)
    _add_no_expand(ep)
    _ = ep.add_argument(
        "--files",
        action="append",
        metavar="GROUP=CSV",
        help="csv of changed files for a file-set group, forwarded into that runner's --files arg (repeatable)",
    )
    ep.set_defaults(func=_explain_with_files)

    gp = cmsub.add_parser(
        "github-outputs",
        help="emit the answer set (SHAs, items, touched, file-sets) as $GITHUB_OUTPUT lines",
    )
    _add_diff_args(gp)
    _add_no_expand(gp)
    gp.set_defaults(func=changemap_cmds.cmd_github_outputs)


def _explain_with_files(config, args: argparse.Namespace) -> int:
    """Validate `--files GROUP=CSV` before running explain, so a malformed pair exits 2."""
    try:
        args.files_map = changemap_cmds.parse_files_args(args.files)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return changemap_cmds.cmd_explain(config, args)
