# Design notes

This is the reasoning behind the code: what `doit` actually does, where the
portable-cache idea breaks, and which failure modes shaped each decision. Every
behavioural claim below was verified against **doit 0.37.0** rather than inferred
from documentation.

---

## 1. What `doit` gives you, and where it stops

`doit` decides whether a task is up to date in
`Dependency.get_status()` (`doit/dependency.py`). The order is:

1. every callable in `task.uptodate` is called; any `False` ⇒ **run**;
2. "no `file_dep` and no uptodate verdict" ⇒ **always run**;
3. a declared target that does not exist ⇒ **run**;
4. the file-watcher class changed ⇒ **run**;
5. `file_dep` state changed (mtime, size, md5) ⇒ **run**;
6. otherwise ⇒ **up to date**.

Everything in steps 3–5 reads `.doit.db`, a `dbm` file next to `dodo.py`. That
file is machine-local by design: it stores absolute paths and mtimes.

Three consequences, each verified by running the real CLI:

**It is machine-local.** Copy a project to another directory without
`.doit.db` and every task that has a `file_dep` re-runs, even though the
artefacts are present and correct.

**Downstream tasks cannot use byte-level reasoning.** If an upstream task runs,
doit marks its result as changed and every dependent re-runs — it has no notion
of "it ran, but produced the same bytes".

**`uptodate` runs *before* the target check.** A hook that returns `True` for a
task whose target is missing is overruled in the very next step. So a portable
cache must *materialise* the artefact inside the hook, not merely assert that it
exists somewhere.

That last point is the load-bearing one: it is what turns "cache lookup" into
"cache lookup + restore", and it is why `doit-port` can work without touching
the scheduler.

**Exception behaviour.** An exception escaping an `uptodate` callable aborts the
whole `doit` process — no task runs, no summary is printed. Every path in the
hook is therefore wrapped, and `PortHook.__call__` degrades to `False` (rebuild)
on any unexpected error, recording it in `.doit-port/errors.log`.

## 2. The key

A receipt is found by a sha256 over:

| Component | Why it is in the key | Why it is *only* this |
|---|---|---|
| `schema` | key-derivation version | lets a future change invalidate cleanly |
| `flavor` | user-visible pipeline identity | bumping it invalidates everything on purpose |
| task name | one key space per task | — |
| input paths + content sha256 | the source is different ⇒ different result | content, never mtime |
| upstream task name + its `outputs_key` | early cutoff | contents of upstream artefacts, not "did it run" |
| actions fingerprint | different build steps ⇒ different result | bytecode, not source text |
| engine fingerprint | `doit`/`doit-port`/Python minor version | behaviour of the toolchain itself |

Deliberately excluded: absolute paths, the project directory, mtimes, hostname,
user, wall-clock time, and the *source text* of actions. Two consequences worth
stating out loud:

* **Comments and blank lines do not invalidate anything.** The action's
  bytecode is unchanged, so the receipt is still true. (`test_code_fingerprint_
  ignores_comments_and_blank_lines` runs this in two separate interpreters.)
* **Reformatting a Python source file changes its bytes**, so a task whose
  `inputs=` includes that file *will* re-run. That is correct and intentional:
  the input really did change, and we cannot know the reformat was cosmetic.

`code_fingerprint()` digests `co_code`, `co_names`, `co_varnames`,
`co_freevars`, `co_cellvars` and every constant — recursing into nested code
objects, so an edit inside a closure is noticed. `co_filename` and
`co_firstlineno` are excluded because they differ per machine and shift when
someone adds a line above the function.

### Why `file_dep` is refused

`doit` resolves `file_dep` against `.doit.db`, whose whole content is exactly
what does not travel. Silently accepting it alongside `inputs=` would produce
tasks that claim to be portable and are not. `port_task()` raises
`UnsupportedTask` with the reason and the fix.

## 3. Restore is the hard part

The hook has to leave the workspace in a state where doit's own checks pass.
That means:

* **Verify before writing.** All blobs for a receipt are read and re-hashed
  first; only if every one is intact does the first byte hit the workspace. A
  corrupt store therefore cannot leave a half-restored build behind
  (`test_restore_verifies_before_writing`).
* **Write atomically.** Each output is written to a sibling temp file, fsynced,
  then `os.replace`d. A killed process leaves either the old file or the new
  one, never half of each.
* **Create parent directories.** doit does not, and a restore that fails on a
  missing directory is a silent downgrade to a rebuild.
* **Roll back on failure.** If the third of five outputs cannot be written, the
  two already written are removed, so the next run sees a clean slate.

## 4. Drift: three different situations that look the same

An artefact whose bytes differ from the receipt can mean three different things,
and conflating them produces either false rebuilds or silent corruption:

| Situation | Disk holds | Correct response |
|---|---|---|
| **Deleted** | nothing | restore from the store |
| **Stale but legal** | bytes this task produced earlier (revert / branch switch) | rebuild, not replay — see below |
| **Tampered** | bytes this task never produced (hand edit, truncated write) | `on_drift` policy: rebuild / warn / restore |

Distinguishing the last two needs memory: the per-task index keeps a short
history (≤ 16) of every digest that task has legitimately produced, so a
`git checkout` of an old artefact is recognised as a revert rather than damage.

### Why a stale-but-legal artefact is rebuilt rather than replayed

The first implementation replayed it, on the theory that the bytes were correct
for *some* receipt. That broke a real scenario: revert one source file, and the
module's artefact still held the newer build's output. The stage was satisfied
from the old receipt, doit therefore skipped the dependents, and they kept
describing the newer state — a workspace that was internally inconsistent, with
every individual check passing.

The fix is to hand the task back to doit in that case: the action re-runs, and
because it is deterministic it produces the same bytes the receipt describes, so
the cascade stops one level later. The cost is one cheap action; the alternative
is a graph that lies. (See
`test_reverting_a_module_reruns_only_what_is_stale`.)

### Why a hit refreshes the index

A hit used to only update in-memory state. After a revert, the index still
pointed at the newer receipt, so dependent tasks kept computing their upstream
fingerprint from a state that no longer existed on disk. Writing the index on
every hit/restore fixed it (`test_bundle_moves_the_cache_to_another_directory`
and the revert tests cover this).

## 5. Failure handling

`doit-port` is written so that **no failure of the caching layer can change the
outcome of a build**, only its speed:

* hook error ⇒ `False` ⇒ the task runs;
* corrupt receipt ⇒ ignored with a log line ⇒ the task runs;
* missing or corrupt blob ⇒ receipt not claimed ⇒ the task runs;
* restore failure ⇒ rolled back ⇒ the task runs;
* index write failure ⇒ logged ⇒ the build is still correct.

The single asymmetry: a task that *succeeds* but does not produce its declared
targets gets **no receipt**. It will be rebuilt next time. That is the truthful
outcome; caching it would make a lie durable.

The hook also writes nothing when it never saw the action signature — the case
with `doit --always`, which skips the `uptodate` hooks entirely. A receipt keyed
on a guess would never be found again.

## 6. Bundle format

```
bundle/
  manifest.json          schema, counts, one entry per file, root_hash
  receipts/<input_key>.json
  objects/<xx>/<sha256>
```

Export hashes every file *after* copying it, so a bundle that survived a broken
disk is caught at write time rather than at import time.

Import is two-phase: **verify everything, then write anything**. Files that are
missing, hash-mismatched, or whose manifest root hash does not match abort the
whole import before a single byte reaches the local store. Paths are rejected if
absolute, drive-qualified, containing `..`, or resolving outside the bundle —
`_safe_join` is unit-tested directly, because a test through `import_bundle` can
pass for the wrong reason (the digests usually fail first). One mutation survived
exactly that test weakness, which is how the direct tests came to exist.

## 7. Things `doit` does that shaped the API

* A task dict carries either `name` or `basename`, never both: `name` is for
  sub-tasks only. `port_task()` enforces this and explains it, instead of
  letting doit emit `Only subtasks use field name`.
* A generator (`yield`) supplies its own group task, so a `port_group()` that
  also yielded a group dict defined it twice.
* Task names are arbitrary strings and appear in index file names. `:` is
  illegal in Windows file names, so names are encoded per segment with a short
  digest appended (`lint__src__sample_pkg__calc.py.b880d5acd7.json`).
* A task with no dependencies is *always* run, which is why an aggregate target
  needs an explicit `uptodate: [True]`.

## 8. Known limits

* **A revert re-runs the chain once.** Documented in the README, covered by a
  test that asserts the *result*, not the action count.
* **`--parallel` is untested.** The hook is re-entrant and the store is atomic,
  but "should work" is not evidence.
* **No remote cache transport.** `export`/`import` are the seam; HTTP is left to
  the user.
* **Undeclared inputs are invisible.** No cache can see a file that was read but
  not declared.
* **No age-based pruning.** `gc` keeps everything any receipt references.
