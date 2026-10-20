"""Mutation check: do the tests actually detect broken behaviour?

The important question is not "how many tests pass" but "which bugs would slip
through". This script applies a handful of deliberate, plausible defects to the
source tree -- one at a time -- and asserts that the suite turns red for each
one. A mutant that survives is a gap in the tests, not a curiosity.

Usage::

    python tools/mutation_check.py            # run every mutant
    python tools/mutation_check.py --list     # show them without running
    python tools/mutation_check.py --keep     # stop after the first survivor

Each mutant is a (file, old, new) triple. The file is restored from a backup
after every run, including on Ctrl-C, so a crashed run never leaves the tree
modified.
"""

from __future__ import annotations

import argparse
import atexit
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "doit_port"


@dataclass(frozen=True)
class Mutant:
    name: str
    file: str
    old: str
    new: str
    why: str


MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        "targets-not-restored",
        "hook.py",
        "            if not self._restore(receipt):",
        "            pass  # if not self._restore(receipt):",
        "claims a hit without materialising a missing artefact",
    ),
    Mutant(
        "blob-integrity-unchecked",
        "store.py",
        "            if actual != sha256:",
        "            if False:",
        "accepts a corrupted blob as valid",
    ),
    Mutant(
        "drift-noted-but-ignored",
        "hook.py",
        "        if drifted or absent or superseded:",
        "        if absent or superseded:",
        "never notices an artefact that was edited by hand",
    ),
    Mutant(
        "input-digest-dropped",
        "fingerprint.py",
        '        h.update(f"in\\t{path}\\t{inputs[path]}\\n".encode("utf-8", "surrogatepass"))\n',
        '        h.update(f"in\\t{path}\\t-.\\n".encode("utf-8", "surrogatepass"))\n',
        "receipt keys stop reacting to source changes",
    ),
    Mutant(
        "dep-outputs-dropped",
        "fingerprint.py",
        '        h.update(f"dep\\t{name}\\t{dep_outputs[name]}\\n".encode("utf-8", "surrogatepass"))\n',
        '        h.update(f"dep\\t{name}\\t-.\\n".encode("utf-8", "surrogatepass"))\n',
        "downstream tasks ignore what upstream produced",
    ),
    Mutant(
        "action-signature-dropped",
        "fingerprint.py",
        '    h.update(f"act\\t{actions_sig}\\n".encode())\n',
        '    h.update(b"act\\t-.\\n")\n',
        "editing the build steps silently reuses stale receipts",
    ),
    Mutant(
        "receipt-written-on-failure",
        "hook.py",
        "        if missing:\n",
        "        if False:\n",
        "caches a build whose declared target was never produced",
    ),
    Mutant(
        "bundle-import-skips-verification",
        "bundle.py",
        "    if failures:\n",
        "    if False:\n",
        "imports a tampered bundle",
    ),
    Mutant(
        "stale-legal-artefact-replayed",
        "hook.py",
        "        if may_reuse:",
        "        if False:",
        "a revert silently replays a stale artefact and desynchronises dependents",
    ),
    Mutant(
        "index-not-refreshed-on-hit",
        "hook.py",
        "            receipt.save_index(self.ctx.store, self._merge_history(receipt))",
        "            pass",
        "a revert leaves the index pointing at the newer state",
    ),
    Mutant(
        "group-spec-not-refiled",
        "api.py",
        "        ctx.unregister(spec.name)",
        "        pass",
        "a task group shadows its own sub-task specs",
    ),
    Mutant(
        "unsafe-index-filename",
        "store.py",
        "        encoded = _encode_task_name(task_name)",
        "        encoded = task_name",
        "a task name with a colon breaks the index on Windows",
    ),
    Mutant(
        "bundle-path-traversal-allowed",
        "bundle.py",
        '    if not relpath or relpath.startswith(("/", "\\\\")) or ":" in relpath:',
        "    if not relpath:",
        "a bundle can name files outside itself",
    ),
    Mutant(
        "bundle-dotdot-guard-removed",
        "bundle.py",
        '    if ".." in Path(relpath).parts:',
        "    if False:",
        "a '..' path that resolves back inside is accepted",
    ),
    Mutant(
        "hook-raises-instead-of-degrading",
        "hook.py",
        "        except Exception as exc:  # noqa: BLE001 - see module docstring",
        "        except ZeroDivisionError as exc:  # noqa: BLE001",
        "an unexpected error aborts the whole doit run",
    ),
    Mutant(
        "drift-restore-mode-ignored",
        "hook.py",
        "            if mode in (\"rebuild\", \"warn\"):",
        "            if True:",
        "config on_drift='restore' stops working",
    ),
    Mutant(
        "history-ignored",
        "hook.py",
        "            if actual in known:",
        "            if False:",
        "a git revert looks like tampering and forces a full rebuild",
    ),
    Mutant(
        "gc-deletes-referenced",
        "store.py",
        "            if sha in keep:",
        "            if False:",
        "garbage collection destroys live artefacts",
    ),
    Mutant(
        "unstored-marked-restorable",
        "receipt.py",
        "        return bool(self.outputs) and all(o.stored for o in self.outputs)",
        "        return bool(self.outputs)",
        "promises a restore for a blob that was never stored",
    ),
    Mutant(
        "index-not-written",
        "hook.py",
        "            receipt.save_index(ctx.store, self._merge_history(receipt))",
        "            pass",
        "dependency lookups and status lose their index",
    ),
    Mutant(
        "file-dep-accepted",
        "api.py",
        "    if \"file_dep\" in extra:",
        "    if False:",
        "the one thing that breaks portability is accepted silently",
    ),
)


def run_suite() -> tuple[bool, str]:
    """Return ``(passed, tail_of_output)``."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-x", "-q", "-p", "no:warnings"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-6:])
    return result.returncode == 0, tail


def apply_mutant(mutant: Mutant, backup_dir: Path) -> Path:
    target = SRC / mutant.file
    backup = backup_dir / mutant.file
    if not backup.exists():
        shutil.copyfile(target, backup)
    text = target.read_text(encoding="utf-8")
    if mutant.old not in text:
        raise SystemExit(
            f"mutant {mutant.name!r}: pattern not found in {mutant.file}. "
            "The source moved; update the mutant instead of ignoring it."
        )
    target.write_text(text.replace(mutant.old, mutant.new, 1), encoding="utf-8")
    return target


def restore_all(backups: dict[str, Path]) -> None:
    for name, backup in backups.items():
        if backup.exists():
            shutil.copyfile(backup, SRC / name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list mutants and exit")
    parser.add_argument("--keep", action="store_true", help="stop at the first survivor")
    args = parser.parse_args()

    if args.list:
        for mutant in MUTANTS:
            print(f"{mutant.name:34} {mutant.file:16} {mutant.why}")
        return 0

    # A crash or Ctrl-C must never leave a mutated file behind: keeping one
    # backup per file for the whole run makes restoration idempotent and also
    # survives the process being killed between mutants.
    backups: dict[str, Path] = {}
    workdir = Path(tempfile.mkdtemp(prefix="doit-port-mut-"))
    try:
        for name in sorted({m.file for m in MUTANTS}):
            backup = workdir / name
            shutil.copyfile(SRC / name, backup)
            backups[name] = backup

        def _restore(*_exc: object) -> None:
            restore_all(backups)

        atexit.register(_restore)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: (_restore(), sys.exit(130)))
            except (ValueError, OSError):  # pragma: no cover - platform quirks
                pass

        print("baseline: ", end="", flush=True)
        ok, tail = run_suite()
        if not ok:
            print("FAILED -- fix the suite before mutating")
            print(tail)
            return 2
        print("green")

        mutants = MUTANTS
        survivors: list[Mutant] = []
        for index, mutant in enumerate(mutants, 1):
            print(f"[{index:2}/{len(mutants)}] {mutant.name:34} ", end="", flush=True)
            target = apply_mutant(mutant, workdir)
            try:
                passed, tail = run_suite()
            finally:
                restore_all(backups)
            if passed:
                survivors.append(mutant)
                print("SURVIVED  <-- test gap")
                if args.keep:
                    break
            else:
                print("killed")
    finally:
        restore_all(backups)

    print()
    if survivors:
        print(f"{len(survivors)} of {len(MUTANTS)} mutants survived:")
        for mutant in survivors:
            print(f"  - {mutant.name}: {mutant.why}")
        shutil.rmtree(workdir, ignore_errors=True)
        return 1
    print(f"all {len(MUTANTS)} mutants killed")
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
