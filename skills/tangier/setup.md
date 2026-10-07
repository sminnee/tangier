# Set up or extend `pipeline.toml`

Map the repo into tags, then add what CI reads from them. Every key is in
[reference/pipeline-toml.md](reference/pipeline-toml.md).

## Add tangier to a repo

1. Copy the starter config from the tangier repo, `pipeline.example.toml`, to `pipeline.toml` at
   the repo root. Delete the sections the repo has no use for.
2. [Map each package](#add-or-change-a-tag): one tag per deployable service, shared library,
   frontend app and cross-cutting area such as `.github/**` or `k8s/**`.
3. Add `depends` from each tag to the libraries it imports.
4. Add `sha = true` to each tag that ships as an image, and an `[image.<tag>]` table for it.
5. [Add the items lists](#add-selective-tests) that the test runners need.
6. [Add a gate](#add-a-gate) for each check a developer runs before pushing.
7. Run the [checks](#checks).
8. Write the workflow: follow [ci.md](ci.md).

Done when the checks pass and `changemap explain --base HEAD~5` matches what a person would run.

## Add or change a tag

1. Write `[<name>]` with `paths` covering the package's files. When `<name>` is a reserved name,
   write `[tags.<name>]`.
2. When the tag feeds a SHA bucket or a gate scope, use only literal paths and `dir/**` globs.
3. Add `depends` for each tag whose change can break this one.
4. Add an output: `sha`, `touched`, a `<name>_items`, or a gate scope entry. A tag with none warns.
5. Run the [checks](#checks).

Follow these rules:

- **Split a subsystem's backend and UI.** `<name>` holds the backend paths and `unittest_items`.
  `<name>-ui` holds the UI paths and `e2e_items`, and depends on `<name>` and the shared `e2e`
  tag. A backend change then runs its e2e suite. A UI-only change runs no backend tests.
- **Make one `e2e` hub.** The e2e harness and shared frontend code form one tag that every
  `<name>-ui` depends on. A harness change then runs every e2e suite.
- **Let `depends` carry library changes.** A service depends on each library it imports, and a
  library on the libraries it imports. Never list a library's paths in a service's `paths`.
- **Exclude test files from subsystem tags, not from bucket tags.** `exclude = ["**/*_test.py"]` on a
  subsystem tag stops a test-only change from running e2e. On a tag that feeds a SHA bucket, or one
  of that bucket's `depends`, it changes the image tag and stops images rebuilding when a test file
  changes.
- **Leave unmapped files unmapped.** A file no tag claims runs nothing. That is correct for docs and
  tooling no test reads.

## Add selective tests

1. Add `<runner>_items = "<dir>"` to each tag whose tests live in `<dir>`. The runner gets the
   union of the items of every selected tag.
2. Add `[runners] <runner> = { cmd = "bin/<runner>" }`. The runner script takes `--dirs a,b,c`.
3. When a changed test file must run even though its tag is not selected, add a file-set:
   `[<runner>-files]` with `files = true` and globs over the test files. Add `files =
   "<runner>-files"` to the runner. The script then also takes `--files x,y`, and runs the union.
4. Keep a file-set's globs to tests the runner's job can run. Leave out a service with its own job.

Done when `changemap explain` on a sample diff prints the invocation you expect.

## Add a gate

1. Write `[gate.<name>]` with `cmd` and `scope`. Each command runs without a shell. Put shell logic
   in a `bin/` script.
2. Use `{<runner>-items}` and `{<file-set>}` placeholders for selective commands. Each is a whole
   argument.
3. Put each SHA bucket or tag the commands test in `scope`.
4. Add a `[<name>-inputs]` tag with no `sha`, listing every other file the commands read: the
   lockfile, `pyproject.toml`, `pipeline.toml`, the runner scripts and tool config. Add it to
   `scope`. A file missing from the scope gives a false pass.
5. When the commands read different inputs, or one is slow, make a group: `[gate.<name>]` with
   `scope` and no `cmd`, and one `[gate.<name>.<member>]` with `cmd` per command.
6. When the commands can write a JUnit XML report, set `junit` to its path, under a path git
   ignores. `gate stats` then lists the tests that fail most.
7. Add the gate to the repo's pre-push hook.
8. Add its CI job: follow [ci.md](ci.md).
9. Run `tangier gate run <name>`.

Done when `gate run <name>` passes and a second run prints `verified`.

When the gate needs CI services, secrets or hardware, it is CI-only. Leave it out of the pre-push
hook, and give its CI job `--accept ci`.

## Checks

Run each one. Done when all of them pass.

1. `tangier changemap list` prints no warning on stderr.
2. `tangier changemap list --graph` shows the `depends` you meant.
3. `tangier changemap list-ignored --base <older commit>` lists no file that should run tests.
4. `tangier changemap explain --base <older commit>` prints the runner invocations a person would
   choose for that diff.
5. `tangier changemap build-matrix --full` lists every image.
6. `tangier gate run --all --dry-run` plans every gate without an error.
