"""The version rules that bin/release-bump and bin/release-plan share.

A release version is plain X.Y.Z, and `pyproject.toml` and
`tangier/__init__.py` must name the same one. Both scripts import this from
their own directory, which Python puts first on `sys.path`.
"""

import re
import sys
import tomllib

PLAIN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse(version: str) -> tuple[int, int, int] | None:
    """The version as a sort key, or None when it is not plain X.Y.Z."""
    match = PLAIN.match(version)
    if not match:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def require_plain(version: str) -> tuple[int, int, int]:
    # Plain X.Y.Z only: a pre-release would move the `@v0` alias onto an rc.
    key = parse(version)
    if key is None:
        sys.exit(f"error: version must look like 1.2.3, got: {version}")
    return key


def package_version(pyproject: str, module: str, where: str = "") -> str:
    """The version both files name, from their text. `where` qualifies the error."""
    project = tomllib.loads(pyproject)["project"]["version"]
    found = re.search(r'^__version__ = "([^"]+)"', module, re.M)
    module_version = found.group(1) if found else None
    if project != module_version:
        sys.exit(
            f"error: the package versions differ{where}\n"
            f"  pyproject.toml:      {project}\n"
            f"  tangier.__version__: {module_version}"
        )
    return project
