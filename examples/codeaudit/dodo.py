"""Example: a portable audit pipeline built on doit + doit-port.

Stages (each one a plain doit task produced by ``port_task``):

    lint     -> audit/<module>.json     one received sub-task per module
    summary  -> build/audit.json + build/audit.md
    gate     -> build/gate.txt          fails when the budget is exceeded
    all      -> aggregate (doit's default target)

What the receipt layer buys you here:

* ``doit`` alone re-runs every downstream task whenever an upstream task ran,
  and re-runs everything when ``.doit.db`` is missing (a fresh CI checkout).
  With receipts an unchanged module is never re-audited, and a fresh clone
  restores the previous results instead of recomputing them.
* Per-module keys depend only on that module's bytes plus the action bytecode,
  so touching ``legacy.py`` re-audits ``legacy.py`` and nothing else.
* ``gate`` really fails when the budget is exceeded. A failed step writes no
  receipt, so the next run retries exactly that step.

Run it::

    pip install -e ../..             # doit + doit-port
    doit                             # first pass: everything executes
    doit                             # second pass: everything is "--"
    doit port status                 # what is cached, and is it intact
    doit port export ../audit-cache  # carry the cache to CI or another machine
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib

from doit_port import port_group, port_task

PY = sys.executable
TOOLS = "tools"
GENERATED = ("audit", "build")


def _config() -> dict:
    """Read audit.toml once; it is also declared as an input of every stage."""
    with open("audit.toml", "rb") as fh:
        return tomllib.load(fh)["audit"]


CONFIG = _config()
MODULES: list[str] = list(CONFIG["modules"])
THRESHOLD = int(CONFIG["threshold"])


def _artifact(module: str) -> str:
    return "audit/" + module.replace("/", "__").removesuffix(".py") + ".json"


# --------------------------------------------------------------------- tasks
#
# Order matters: doit's default target is the *first* task defined, so the
# aggregate comes first and `wipe` -- which deletes the artefacts -- comes last.


def task_all():
    """Default target: audit everything and enforce the budget."""
    return {
        "actions": None,
        "task_dep": ["gate"],
        "uptodate": [True],   # an aggregate: it never "runs" anything itself
    }


def task_lint():
    """One sub-task per configured module: audit <module> -> audit/<mod>.json.

    The module list comes from audit.toml, so these are built with a plain
    ``return`` plus ``port_group`` instead of ``yield`` -- port_group files each
    spec under the name doit will actually register ("lint:<module>").
    """
    return port_group(
        "lint",
        [
            port_task(
                name=module,
                actions=[[PY, f"{TOOLS}/lint.py", "--files", module,
                          "--out", _artifact(module)]],
                targets=[_artifact(module)],
                inputs=[module, f"{TOOLS}/lint.py"],
                doc=f"AST audit of {module}",
            )
            for module in MODULES
        ],
    )


def task_summary():
    """Merge the per-module JSON into build/audit.{json,md}."""
    # A dict returned from task_summary() describes the task itself, so it
    # carries `basename`; doit rejects `name` for anything that is not a subtask.
    return port_task(
        basename="summary",
        actions=[
            [PY, f"{TOOLS}/summary.py",
             "--glob", "audit/*.json",
             "--out", "build/audit.json",
             "--md", "build/audit.md"]
        ],
        targets=["build/audit.json", "build/audit.md"],
        # Inputs are the lint results *plus* the sources. The lint artefacts
        # give the early cutoff (an upstream re-run that produced identical
        # bytes does not cascade); the sources give correctness even when an
        # artefact is restored rather than produced. Never list an output of
        # this task itself -- that would make the key depend on a file the task
        # rewrites, so it could never be reused.
        inputs=[f"{TOOLS}/summary.py", "audit.toml"]
        + [_artifact(m) for m in MODULES],
        task_dep=[f"lint:{m}" for m in MODULES],
        doc="merge per-module results",
    )


def task_gate():
    """Fail the build when the finding budget is exceeded."""
    def act():
        # subprocess with an explicit argv: quoting a command string breaks the
        # moment sys.executable contains a space (it does on this machine).
        os.makedirs("build", exist_ok=True)
        target = "build/gate.txt"
        # A stale "passed" file from an earlier run must not survive a failure:
        # the receipt layer would then see an artefact that no longer reflects
        # reality. Delete it first, write it only on success.
        if os.path.exists(target):
            os.remove(target)
        proc = subprocess.run(
            [PY, f"{TOOLS}/gate.py", "--audit", "build/audit.json",
             "--threshold", str(THRESHOLD)],
            capture_output=True, text=True, encoding="utf-8",
        )
        sys.stdout.write(proc.stdout)
        if proc.returncode == 0:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(f"passed: threshold={THRESHOLD}\n")
            return True
        sys.stderr.write(proc.stderr or proc.stdout)
        return False

    return port_task(
        basename="gate",
        actions=[act],
        targets=["build/gate.txt"],
        inputs=[f"{TOOLS}/gate.py", "audit.toml"],  # build/audit.json comes via task_dep
        task_dep=["summary"],
        doc=f"enforce the finding budget ({THRESHOLD})",
    )


def task_wipe():
    """Remove generated artefacts and the content store (doit owns `clean`)."""
    def act():
        # `.doit.db` is held open by the running doit process on Windows, so it
        # is left alone; delete it manually when doit is not running.
        for path in GENERATED + (".doit-port",):
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
            print(f"removed {path}")

    # No task_dep and no uptodate: `doit wipe` only runs this, and a plain
    # `doit` (whose default target is the first task, `all`) never touches it.
    return {"actions": [act], "doc": "wipe artefacts and the content store"}
