# Example: codeaudit

A four-stage audit pipeline over a small, deliberately imperfect Python package.
It exists to show the receipt layer doing something useful on a real workflow,
not to demo the API in isolation.

```
audit.toml
src/sample_pkg/{__init__,calc,legacy}.py     the code being audited
tools/lint.py        deterministic AST audit  -> audit/<module>.json
tools/summary.py     merge + totals           -> build/audit.json, build/audit.md
tools/gate.py        enforce a finding budget -> exit 1 when over
dodo.py              the four doit tasks
doit-port.toml       store location, flavour, drift policy
```

## Run it

```bash
pip install -e "../..[toml310]"   # doit + doit-port (+ tomli on Python 3.10)
doit all                    # everything executes the first time
doit all                    # every task is up to date ("--")
doit port status            # what is cached, sizes, integrity
doit port verify            # re-hash every artefact
```

## What to try

| Command | What you should see |
|---|---|
| `doit all` twice | the second run prints only `--` |
| append a function to `src/sample_pkg/legacy.py`, `doit all` | only `lint:src/sample_pkg/legacy.py` runs; `summary`/`gate` keep their receipts |
| append a *comment* to `src/sample_pkg/calc.py`, `doit all` | the lint task re-runs (its input bytes changed) but the report is identical, so nothing downstream runs |
| `rm -rf build audit .doit.db`, `doit all` | everything is restored; nothing executes (`gate.txt` reappears) |
| set `threshold = 2` in `audit.toml`, `doit all` | `gate` fails with exit 1 and **deletes** `build/gate.txt`; no receipt is written for it |
| set the threshold back, `doit all` | only `lint:*` and `gate` run — `summary` was already valid |
| `echo hacked > build/gate.txt`, `doit all` | the tampered artefact is detected and `gate` re-runs |
| `rm build/gate.txt`, `doit all` | restored from the store, no action runs |
| `doit port export ../ci-cache`, then import it in a fresh copy | the fresh copy reports `--` for everything |
| `doit port gc --dry-run` | nothing to remove: every blob is referenced |

## Why the pipeline is shaped this way

**One sub-task per module.** The unit of caching is a module, so editing one
file does not re-audit the others. The module list comes from `audit.toml`, so
the subtasks are built with `port_group()` rather than `yield`.

**`inputs=` never contains a task's own output.** `summary` reads
`build/audit.json` while writing it; listing that file as an input would make
the key depend on a file the task rewrites, and the receipt could never be
reused. The per-module results reach it through `task_dep=` (their contents are
part of the key as dependency outputs) and are *also* listed as inputs, because
they really are inputs.

**`gate` deletes its artefact before running.** If it left a stale
`passed: threshold=8` file behind after a failure, the receipt layer would see
an artefact that no longer reflects reality. The target only exists when the
gate actually passed.

**The lint tool has no timestamps, paths or line numbers in its output.** That is
what makes the early cutoff observable: a comment-only edit produces an
identical report. A tool that printed `generated at 12:04` would invalidate the
whole pipeline on every run, correctly and uselessly.

## Deliberate non-goals

* No network access, no third-party audit rules, no plugin system.
* `wipe` deletes `.doit-port` as well — it is the "start from nothing" button.
* The sample package contains real findings on purpose (a swallowed exception,
  `pickle.loads`, `subprocess.call`, `== None`); the gate budget of 8 sits above
  the current 6 so the pipeline passes out of the box.

## Troubleshooting

**Every run rebuilds.** Something in `inputs=` changes on every run, or an
action writes a timestamp. Check with `doit port status`: the key should be
stable between two runs.

**`no action signature captured` in `.doit-port/errors.log`.** You ran
`doit --always`, which skips the up-to-date hooks; no receipt is written. Run
plain `doit` once.

**A task never gets a receipt.** It declared a target it does not produce. The
error log names the task and the missing path.
