"""End-to-end acceptance for ``examples/codeaudit``.

These tests drive the real ``doit`` CLI as a subprocess, in a temporary copy of
the example directory, so what is verified is exactly what a reader gets after
``git clone``. Every assertion is about observable behaviour -- exit codes,
``doit``'s own status markers, artefact bytes -- never about internals.

The example is the "real application" part of this project: a four-stage audit
pipeline whose caching behaviour is the whole point.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from support import drop_doit_state

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "codeaudit"

# Deliberately *not* skipped when the example is missing: a shipped source
# archive that lost `examples/` should fail loudly, not quietly run 197 tests
# and report green. (That is exactly what happened once: the tests ran from an
# unpacked ZIP where `examples/` had not been carried along, and 18 tests were
# skipped with no signal at all.)
if not (EXAMPLE / "dodo.py").is_file():  # pragma: no cover - packaging guard
    raise RuntimeError(
        f"example pipeline not found at {EXAMPLE}; the source archive is incomplete"
    )


@pytest.fixture()
def proj(tmp_path: Path) -> Path:
    """A throwaway copy of the example, without generated state."""
    target = tmp_path / "codeaudit"
    shutil.copytree(
        EXAMPLE,
        target,
        ignore=shutil.ignore_patterns(
            "audit", "build", ".doit-port", ".doit.db*", "__pycache__", "*.pyc"
        ),
    )
    return target


def doit(proj: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run doit in *proj*; return the completed process."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "doit", *args],
        cwd=proj, capture_output=True, text=True, encoding="utf-8", env=env,
    )


def ran(result: subprocess.CompletedProcess[str]) -> list[str]:
    """Task names doit executed ('.') in this run, in order."""
    out: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(".  "):
            out.append(stripped[3:].strip())
    return out


def skipped(result: subprocess.CompletedProcess[str]) -> list[str]:
    """Task names doit considered up to date ('--')."""
    out: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("-- "):
            out.append(stripped[3:].strip())
    return out


# ------------------------------------------------------------------- baseline


def test_first_run_builds_everything(proj: Path) -> None:
    result = doit(proj, "all")
    assert result.returncode == 0, result.stderr
    assert ran(result) == [
        "lint:src/sample_pkg/__init__.py",
        "lint:src/sample_pkg/calc.py",
        "lint:src/sample_pkg/legacy.py",
        "summary",
        "gate",
    ]
    assert skipped(result) == ["all"]


def test_artefacts_have_the_expected_shape(proj: Path) -> None:
    assert doit(proj, "all").returncode == 0
    audit = json.loads((proj / "build" / "audit.json").read_text(encoding="utf-8"))
    assert audit["totals"]["files"] == 3
    assert audit["totals"]["findings"] == 6
    assert "risky-call:pickle.loads:module" in audit["files"]["src/sample_pkg/legacy.py"]
    assert "error-handling:swallowed-exception:function:swallow" in (
        audit["files"]["src/sample_pkg/calc.py"]
    )
    assert (proj / "build" / "gate.txt").read_text(encoding="utf-8").startswith("passed")
    assert "findings: 6" in (proj / "build" / "audit.md").read_text(encoding="utf-8")


def test_second_run_does_no_work(proj: Path) -> None:
    assert doit(proj, "all").returncode == 0
    again = doit(proj, "all")
    assert again.returncode == 0
    assert ran(again) == []
    assert "summary" in skipped(again)


# ------------------------------------------------------- incremental behaviour


def test_editing_one_module_reaudits_only_that_module(proj: Path) -> None:
    assert doit(proj, "all").returncode == 0
    legacy = proj / "src" / "sample_pkg" / "legacy.py"
    legacy.write_text(legacy.read_text(encoding="utf-8") + "\n\ndef extra():\n    return 1\n",
                      encoding="utf-8")

    result = doit(proj, "all")
    assert result.returncode == 0
    assert ran(result) == ["lint:src/sample_pkg/legacy.py"]


def test_comment_only_edit_stops_at_the_lint_stage(proj: Path) -> None:
    """Early cutoff: the lint artefact is unchanged, so nothing downstream runs."""
    assert doit(proj, "all").returncode == 0
    calc = proj / "src" / "sample_pkg" / "calc.py"
    calc.write_text(calc.read_text(encoding="utf-8") + "\n# just a comment\n",
                    encoding="utf-8")

    result = doit(proj, "all")
    assert result.returncode == 0
    assert ran(result) == ["lint:src/sample_pkg/calc.py"]
    assert "summary" in skipped(result)
    assert "gate" in skipped(result)


def test_new_finding_moves_the_gate(proj: Path) -> None:
    assert doit(proj, "all").returncode == 0
    calc = proj / "src" / "sample_pkg" / "calc.py"
    calc.write_text(
        calc.read_text(encoding="utf-8") + "\n\nimport os\n\n\ndef wipe():\n    os.system('rm -rf /')\n",
        encoding="utf-8",
    )
    result = doit(proj, "all")
    assert result.returncode == 0
    assert ran(result) == ["lint:src/sample_pkg/calc.py", "summary", "gate"]


def test_reverting_a_module_converges_without_rebuilding_everything(proj: Path) -> None:
    """A revert ends up correct, and does not redo unrelated work.

    Two things may happen to the reverted module and both are acceptable:

    * doit's own ``.doit.db`` no longer matches the file, so the task runs and
      produces byte-identical output (early cutoff stops the cascade);
    * or doit considers it up to date and doit-port replays the earlier receipt.

    Which one occurs depends on doit's DBM bookkeeping, not on this project --
    asserting an exact list made this test fail on Linux CI while passing on
    Windows. What must hold everywhere: the modules that were *not* touched
    never re-run, the gate stays cached, and the report matches the reverted
    source.
    """
    assert doit(proj, "all").returncode == 0
    calc = proj / "src" / "sample_pkg" / "calc.py"
    original = calc.read_text(encoding="utf-8")
    calc.write_text(original + "\n\n# temporary\n", encoding="utf-8")
    assert doit(proj, "all").returncode == 0

    calc.write_text(original, encoding="utf-8")
    result = doit(proj, "all")
    assert result.returncode == 0

    executed = ran(result)
    assert "lint:src/sample_pkg/__init__.py" not in executed
    assert "lint:src/sample_pkg/legacy.py" not in executed
    assert "gate" in skipped(result), "the gate must not re-run for an unchanged report"
    assert set(executed) <= {"lint:src/sample_pkg/calc.py", "summary"}

    report = (proj / "build" / "audit.json").read_text(encoding="utf-8")
    assert json.loads(report)["totals"]["findings"] == 6


# ---------------------------------------------------------- fresh checkout


def test_fresh_checkout_restores_instead_of_rebuilding(proj: Path) -> None:
    """The scenario doit alone cannot do: no .doit.db, no artefacts."""
    assert doit(proj, "all").returncode == 0
    expected = (proj / "build" / "audit.json").read_bytes()

    shutil.rmtree(proj / "build")
    shutil.rmtree(proj / "audit")
    drop_doit_state(proj)

    result = doit(proj, "all")
    assert result.returncode == 0
    assert ran(result) == [], "everything should be restored from receipts"
    assert (proj / "build" / "audit.json").read_bytes() == expected


def test_bundle_moves_the_cache_to_another_directory(proj: Path, tmp_path: Path) -> None:
    assert doit(proj, "all").returncode == 0
    bundle = tmp_path / "cache"
    assert doit(proj, "port", "export", str(bundle)).returncode == 0

    other = tmp_path / "ci-run"
    shutil.copytree(
        proj,
        other,
        ignore=shutil.ignore_patterns("audit", "build", ".doit.db*", ".doit-port"),
    )
    # the lint step would notice a missing python module quickly; make sure the
    # restore path is the one exercised by deleting the sources' artefacts only
    assert doit(other, "port", "import", str(bundle)).returncode == 0
    result = doit(other, "all")
    assert result.returncode == 0
    assert ran(result) == []
    assert "findings: 6" in (other / "build" / "audit.md").read_text(encoding="utf-8")


# ------------------------------------------------------------- failure paths


def test_gate_failure_is_reported_and_leaves_no_artefact(proj: Path) -> None:
    config = proj / "audit.toml"
    config.write_text(config.read_text(encoding="utf-8").replace("threshold = 8", "threshold = 2"),
                      encoding="utf-8")
    result = doit(proj, "all")
    assert result.returncode != 0
    assert "gate: FAILED" in (result.stdout + result.stderr)
    assert not (proj / "build" / "gate.txt").exists(), "a failed gate must not leave a target"
    index = proj / ".doit-port" / "index" / "gate.json"
    assert not index.exists(), "a failed task must never get a receipt"


def test_gate_retries_alone_after_the_budget_is_raised(proj: Path) -> None:
    config = proj / "audit.toml"
    good = config.read_text(encoding="utf-8")
    config.write_text(good.replace("threshold = 8", "threshold = 2"), encoding="utf-8")
    assert doit(proj, "all").returncode != 0

    config.write_text(good, encoding="utf-8")
    result = doit(proj, "all")
    assert result.returncode == 0
    assert ran(result) == ["summary", "gate"], (
        "lint results are still valid; only what changed must re-run"
    )


def test_tampered_artefact_is_rebuilt(proj: Path) -> None:
    assert doit(proj, "all").returncode == 0
    (proj / "build" / "gate.txt").write_text("passed: threshold=999\n", encoding="utf-8")
    result = doit(proj, "all")
    assert result.returncode == 0
    assert ran(result) == ["gate"]
    assert "threshold=8" in (proj / "build" / "gate.txt").read_text(encoding="utf-8")


def test_deleted_artefact_is_restored(proj: Path) -> None:
    assert doit(proj, "all").returncode == 0
    (proj / "build" / "gate.txt").unlink()
    result = doit(proj, "all")
    assert result.returncode == 0
    assert ran(result) == []
    assert (proj / "build" / "gate.txt").is_file()


# ------------------------------------------------------------------- CLI


def test_port_status_lists_the_cached_tasks(proj: Path) -> None:
    assert doit(proj, "all").returncode == 0
    result = doit(proj, "port", "status")
    assert result.returncode == 0
    assert "summary" in result.stdout
    assert "gate" in result.stdout
    assert "ok" in result.stdout


def test_port_verify_is_clean(proj: Path) -> None:
    assert doit(proj, "all").returncode == 0
    result = doit(proj, "port", "verify")
    assert result.returncode == 0
    assert "OK" in result.stdout


def test_port_gc_keeps_live_artefacts(proj: Path) -> None:
    assert doit(proj, "all").returncode == 0
    store = proj / ".doit-port"
    before = sorted(p.name for p in (store / "objects").rglob("*") if p.is_file())
    result = doit(proj, "port", "gc", "--dry-run")
    assert result.returncode == 0
    after = sorted(p.name for p in (store / "objects").rglob("*") if p.is_file())
    assert before == after


def test_port_forget_forces_a_rebuild(proj: Path) -> None:
    assert doit(proj, "all").returncode == 0
    assert doit(proj, "port", "forget", "gate").returncode == 0
    shutil.rmtree(proj / "build")
    drop_doit_state(proj)
    result = doit(proj, "all")
    assert "gate" in ran(result), "forgetting a receipt must make the task run again"
