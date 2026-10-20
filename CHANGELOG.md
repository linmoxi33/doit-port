# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [0.1.1] — 2026-10-20

Fixes found by the CI matrix on the first published run — none of them were
visible on the single Windows machine used during development.

### Fixed

- **Backslashes were not normalised to posix on POSIX systems.**
  `Path("build\out.txt").as_posix()` keeps the backslash where it is a legal
  filename character, so the same project produced the keys `build\out.txt` on
  Linux and `build/out.txt` on Windows: a cache could never cross platforms.
  `config.relpath()` now replaces `\` with `/` explicitly. Regression test added.
- **The example required Python ≥ 3.11** because `dodo.py` imported `tomllib`
  directly. It now falls back to `tomli` with an actionable error message, and
  the `toml310` extra is documented and used by CI.
- **Four tests assumed `.doit.db` exists.** Which DBM files doit writes depends
  on the interpreter's dbm backend, and a run may write none. They now call
  `tests/support.py::drop_doit_state()`, which globs `.doit.db*`.

### Changed

- The revert test no longer pins an exact task list. Whether doit treats a
  reverted file as stale is doit's bookkeeping, not this layer's behaviour; the
  test now asserts the invariants that must hold on every platform.

## [0.1.0] — 2026-10-20

First release. Scope: portable build receipts for `doit`, and nothing else.

### Added

- `port_task()` — build a plain `doit` task dict whose up-to-date decision is
  backed by a content-addressed receipt. Accepts `name` or `basename`,
  `actions`, `targets`, `inputs`, `task_dep`, `key_salt`, `uptodate`, and any
  documented doit attribute. Rejects `file_dep`, absolute targets, targets
  outside the project, directory targets, and tasks without targets.
- `port_group()` — group subtasks into a `doit` generator group when the list
  comes from a config file. Re-files each receipt spec under the name doit
  actually registers (`lint:<module>`).
- `setup()` — configure the layer from `dodo.py`.
- Receipts: immutable JSON, keyed on a sha256 of inputs, upstream artefact
  digests, action bytecode, target list, flavour and toolchain versions. Stored
  under the key, so a receipt can be verified against its own name.
- Object store: content-addressed blobs, atomic writes, re-hash on read,
  garbage collection, crash-leftover cleanup, safe under concurrent writers.
- Restore: verify every blob before writing a byte, atomic per-file writes,
  parent-directory creation, rollback on failure.
- Drift classification: missing vs. stale-but-legal (revert) vs. tampered, with
  `on_drift = rebuild | warn | restore`.
- Early cutoff: a downstream task's key uses the *contents* of its upstream
  artefacts, so an upstream re-run that produced identical bytes stops there.
- `doit port` sub-command (`status`, `verify [bundle]`, `gc`, `export`,
  `import`, `forget`), registered through the `doit.COMMAND` entry point.
- Bundles: `manifest.json` + receipts + objects, with a root hash; import is
  verify-then-write and rejects traversal, absolute paths and tampering.
- Example `examples/codeaudit`: a four-stage AST audit pipeline with a real
  failure path (a budget gate) and 18 CLI-level acceptance tests.
- `tools/mutation_check.py`: 21 injected defects, all caught.
- `tools/build_archive.py`: reproducible source ZIP (fixed mtimes, LF
  normalisation, generated state excluded).

### Known limits

- `doit --parallel` is untested.
- A revert re-runs the affected stage once instead of replaying it; downstream
  stages then keep their receipts (early cutoff).
- `doit --always` writes no receipts, by design.
- No remote cache transport; `export` / `import` are the seam.
- No age-based pruning; `gc` keeps everything any receipt references.
