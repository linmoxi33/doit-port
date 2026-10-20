# Verification record

Everything below was produced by running the code in this repository on
2026-10-20, Windows, Python 3.13.14, doit 0.37.0. Commands are reproducible; the
numbers in this file are the ones the tests assert.

## 1. Upstream project, verified not assumed

| Claim | How it was checked | Result |
|---|---|---|
| The project exists and is maintained | `codeload.github.com/pydoit/doit/tar.gz/refs/heads/master` | 664,952 bytes, HTTP 200 |
| Licence | `LICENSE` | MIT, © 2008-present Eduardo Naufel Schettino |
| Package metadata | `pyproject.toml` | `doit`, `0.38.0.dev0` on master, `requires-python >= 3.10`, `dependencies = []` |
| Installed release used | `pip install doit` | 0.37.0 (the released version; master is a dev version) |
| Extension points are real, not invented | `doit/plugin.py` `PluginDict.add_plugins(cfg, 'COMMAND')`, `doit/dependency.py:614-651` | `uptodate` callables called before file checks; `doit.COMMAND` entry-point group loaded by `DoitMain.get_cmds()` |
| Nothing is patched | `git grep`-style search of `src/` | no import of `doit.dependency`/`doit.task` internals except `doit.action` types and `doit.cmd_base.Command` (both public API used for type sniffing and sub-command definition) |

Probes run **before** writing any code, then promoted into tests:

| Probe | Observation | Consequence in the design |
|---|---|---|
| `uptodate` returns `True`, target missing | task runs anyway | the hook must *restore*, not just assert |
| `uptodate` returns `True` after a `touch` of a source with identical content | task does not run | content hashing beats mtime; receipts may ignore mtimes |
| `.doit.db` deleted, artefacts present, `file_dep` used | everything re-runs | exactly the gap this project fills |
| Hook raising an exception | the whole `doit` process aborts | hook must never raise |

## 2. Test suite

```bash
$ python -m pytest tests/ -q
214 passed, 1 skipped in 50.84s
```

(The skip is the symlink-escape test: creating a symlink needs a privilege that
a plain Windows user account does not have. It runs on POSIX and in CI with
developer mode enabled.)

| File | Tests | Covers |
|---|---|---|
| `test_e2e.py` | 15 | real doit runner: execute / reuse / restore / early cutoff / failures / drift / mixed dodo |
| `test_example_codeaudit.py` | 18 | the shipped example driven through the CLI as a subprocess |
| `test_hook.py` | 22 | hook verdicts, degradation, restore safety, drift modes |
| `test_fingerprint.py` | 20 | key stability **across separate interpreters** |
| `test_store.py` | 21 | atomicity, integrity, gc, concurrency, crash leftovers |
| `test_receipt.py` | 20 | schema, tamper detection, history |
| `test_bundle.py` | 37 | export / verify / import, tampered and hostile bundles |
| `test_config.py` | 16 | precedence `pyproject` < `doit-port.toml` < `DOIT_CONFIG` |
| `test_command.py` | 28 | `doit port` argument surface and dispatch |
| `test_api.py` | 25 | refusals that protect portability, sub-task groups |

## 3. Mutation check — does the suite actually catch bugs?

Test count proves nothing. `tools/mutation_check.py` injects a plausible defect,
runs the full suite, and asserts it turns red.

```
$ python tools/mutation_check.py
baseline: green
[ 1/21] targets-not-restored               killed
[ 2/21] blob-integrity-unchecked           killed
[ 3/21] drift-noted-but-ignored            killed
[ 4/21] input-digest-dropped               killed
[ 5/21] dep-outputs-dropped                killed
[ 6/21] action-signature-dropped           killed
[ 7/21] receipt-written-on-failure         killed
[ 8/21] bundle-import-skips-verification   killed
[ 9/21] stale-legal-artefact-replayed      killed
[10/21] index-not-refreshed-on-hit         killed
[11/21] group-spec-not-refiled             killed
[12/21] unsafe-index-filename              killed
[13/21] bundle-path-traversal-allowed      killed
[14/21] bundle-dotdot-guard-removed        killed
[15/21] hook-raises-instead-of-degrading   killed
[16/21] drift-restore-mode-ignored         killed
[17/21] history-ignored                    killed
[18/21] gc-deletes-referenced              killed
[19/21] unstored-marked-restorable         killed
[20/21] index-not-written                  killed
[21/21] file-dep-accepted                  killed
```

### Two gaps this process exposed (and fixed)

**Gap 1 — `bundle-path-traversal-allowed` survived.** The traversal guard was
only exercised through `import_bundle()`, where the digest check usually fails
first, so removing the guard changed nothing observable. Fixed by unit-testing
`_safe_join()` directly against eleven hostile paths, plus one test that puts a
*valid* file outside the bundle and asserts it is still refused.

**Gap 2 — an early run left a mutant in place.** The first version backed up a
file right before mutating it and restored it in a `finally`. A hard kill
between those two points left the source mutated. The script now snapshots every
file once up front, restores idempotently in `finally`, on `SIGINT`/`SIGTERM`,
and via `atexit`.

## 4. Behaviour verified by hand, then encoded as tests

Command sequence in `examples/codeaudit` (verbatim output, condensed):

```
$ doit all
.  lint:src/sample_pkg/__init__.py
.  lint:src/sample_pkg/calc.py
.  lint:src/sample_pkg/legacy.py
.  summary
.  gate
-- all

$ doit all                    # nothing changed
-- lint:src/sample_pkg/__init__.py
-- lint:src/sample_pkg/calc.py
-- lint:src/sample_pkg/legacy.py
-- summary
-- gate
-- all

# append a function to legacy.py
$ doit all
-- lint:src/sample_pkg/__init__.py
-- lint:src/sample_pkg/calc.py
.  lint:src/sample_pkg/legacy.py
-- summary
-- gate
-- all

# append a comment to calc.py (bytes change, findings do not)
$ doit all
-- lint:src/sample_pkg/__init__.py
.  lint:src/sample_pkg/calc.py
-- lint:src/sample_pkg/legacy.py
-- summary
-- gate
-- all

$ rm -rf build .doit.db       # a fresh checkout
$ doit all
-- ...                        # every task restored, nothing executed
gate.txt: passed: threshold=8

# threshold lowered to 2
$ doit all
gate: FAILED -- 6 findings exceed the budget of 2      (exit != 0)
$ ls build/gate.txt           # absent -- no target, therefore no receipt
```

## 5. Known unverified areas

Stated so that nobody has to guess which claims are backed by evidence:

* **`doit --parallel` / multiprocessing** — not exercised by any test.
* **Python 3.10** — the code guards for it (`tomli` fallback) and CI-style
  checks are written, but only 3.13.14 was actually run here.
* **Large artefacts** — the streaming path is tested with a 1 MiB blob, not with
  multi-GB files, and `max_blob_mb` default of 256 was not stress-tested.
* **Cross-OS bundle exchange** — paths are posix-relative and tested for that,
  but a bundle produced on Windows was not imported on Linux during this
  session.
