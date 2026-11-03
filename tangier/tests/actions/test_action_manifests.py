"""No action manifest has `${{` in a `description:` value.

The runner evaluates expressions in input descriptions, and a composite action
has no `vars` context, so `${{ vars.X }}` in a description rejects the whole
manifest before any step runs. The ban covers every expression and every
description the runner reads (the action's, each input's and each output's),
not only `vars` in inputs. A line scan, not a YAML parser: tangier is
stdlib-only.
"""

import glob
import os
import unittest

from tangier.tests.support import REPO_ROOT


def description_expressions(text: str) -> list[int]:
    """Line numbers (1-based) of `${{` inside a `description:` the runner reads.

    Those are the action's own `description:` (indent 0) and each input's or
    output's (indent 4 under top-level `inputs:` or `outputs:`). One opens a
    region that holds the rest of its line and every following line that is
    blank or indented deeper.
    """
    found = []
    section = None
    region_indent = None
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped and indent == 0:
            section = stripped.split(":", 1)[0]
        if region_indent is not None and stripped and indent <= region_indent:
            region_indent = None
        read_by_runner = indent == 0 or (indent == 4 and section in ("inputs", "outputs"))
        if region_indent is None and read_by_runner and stripped.startswith("description:"):
            region_indent = indent
        if region_indent is not None and "${{" in line:
            found.append(number)
    return found


MANIFEST = """\
name: example
description: >-
  Summary.

  From `${{ github.repository }}`.
inputs:
  x:
    description: >-
      Pass
      `${{ vars.X }}` here.

      Or `${{ vars.Z }}` after a blank line.
    required: true
  y:
    description: Pass `${{ vars.Y }}`.
outputs:
  z:
    description: From `${{ steps.a.outputs.z }}`.
    value: ${{ steps.a.outputs.z }}
runs:
  using: composite
  steps:
    - id: a
      uses: ./other
      with:
        x: ${{ inputs.x }}
        description: ${{ inputs.y }}
      env:
        Y: ${{ inputs.y }}
"""


class TestDescriptionExpressions(unittest.TestCase):
    def test_reports_only_the_descriptions_the_runner_reads(self) -> None:
        # Not `value:` right after a description, nor anything under `runs:`,
        # including a `with:` key that happens to be named `description`.
        self.assertEqual(description_expressions(MANIFEST), [5, 10, 12, 15, 18])

    def test_no_action_in_the_checkout_has_one(self) -> None:
        actions = os.path.join(REPO_ROOT, ".github", "actions", "*")
        paths = sorted(glob.glob(os.path.join(actions, "action.yaml")) + glob.glob(os.path.join(actions, "action.yml")))
        self.assertTrue(paths)
        found = {}
        for path in paths:
            with open(path, encoding="utf-8") as f:
                lines = description_expressions(f.read())
            if lines:
                found[os.path.relpath(path, REPO_ROOT)] = lines
        self.assertEqual(found, {})


class TestTailscaleArgs(unittest.TestCase):
    def test_no_action_passes_advertise_tags_itself(self) -> None:
        # tailscale/github-action@v4 adds `--advertise-tags` from its `tags:`
        # input whenever `audience:` is set. A second one in `args:` makes
        # `tailscale up` exit 2 with "flag provided multiple times".
        actions = os.path.join(REPO_ROOT, ".github", "actions", "*", "action.y*ml")
        paths = sorted(glob.glob(actions))
        self.assertTrue(paths)
        found = []
        for path in paths:
            with open(path, encoding="utf-8") as f:
                if "--advertise-tags" in f.read():
                    found.append(os.path.relpath(path, REPO_ROOT))
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
