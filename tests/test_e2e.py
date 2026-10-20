"""End-to-end behaviour against the real doit runner.

These are the tests that justify the project: they exercise execution, reuse,
cross-machine replay, early cutoff and failure recovery on an actual
three-stage pipeline, and they check *artefact bytes*, not just exit codes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from conftest import clear_runs, load_dodo, read_runs, run_doit
from doit_port.store import ObjectStore
from support import drop_doit_state

BUILD = "build"


def _build(project: Path, name: str) -> Path:
    return project / BUILD / name


# --------------------------------------------------------------------- basics


def test_first_run_executes_every_stage(project: Path) -> None:
    assert run_doit(project) == 0
    assert read_runs(project) == ["collect", "summarize", "report"]
    assert _build(project, "rows.txt").read_text(encoding="utf-8") == "hello world"
    data = json.loads(_build(project, "summary.json").read_text(encoding="utf-8"))
    assert data == {"chars": 11, "words": 2, "text": "hello world"}
    assert "chars: 11" in _build(project, "report.md").read_text(encoding="utf-8")


def test_second_run_touches_nothing(project: Path) -> None:
    assert run_doit(project) == 0
    clear_runs(project)
    assert run_doit(project) == 0
    assert read_runs(project) == []


def test_receipts_are_written_for_every_stage(project: Path) -> None:
    run_doit(project)
    store = project / ".doit-port"
    receipts = sorted(p.stem for p in (store / "receipts").glob("*.json"))
    indexes = sorted(
        json.loads(p.read_text(encoding="utf-8"))["task"]
        for p in (store / "index").glob("*.json")
    )
    assert len(receipts) == 3
    assert indexes == ["collect", "report", "summarize"]
    for key in receipts:
        payload = json.loads((store / "receipts" / f"{key}.json").read_text(encoding="utf-8"))
        assert payload["schema"] == 1
        assert payload["input_key"] == key
        for out in payload["outputs"]:
            assert "\\" not in out["path"], "receipts must use posix paths"
            assert not out["path"].startswith("/")


# ------------------------------------------------------------- cross-machine


def test_survives_lost_doit_db_and_wiped_build_dir(project: Path) -> None:
    """The scenario doit alone cannot handle: a clean checkout on another box.

    ``.doit.db`` is machine-local, so doit rebuilds every task with a
    ``file_dep`` when it is missing. With receipts, nothing runs and the
    artefacts come back byte-identical.
    """
    assert run_doit(project) == 0
    expected = {
        name: _build(project, name).read_bytes()
        for name in ("rows.txt", "summary.json", "report.md")
    }
    clear_runs(project)
    shutil.rmtree(project / BUILD)
    drop_doit_state(project)

    assert run_doit(project) == 0
    assert read_runs(project) == [], "no action should run when receipts are valid"
    for name, payload in expected.items():
        assert _build(project, name).read_bytes() == payload


def test_bundle_carries_the_cache_to_another_directory(project: Path, tmp_path: Path) -> None:
    assert run_doit(project) == 0
    bundle = tmp_path / "cache-bundle"
    assert run_doit(project, "port", "export", str(bundle)) == 0
    assert (bundle / "manifest.json").is_file()
    assert (bundle / "objects").is_dir()

    other = tmp_path / "other-machine"
    shutil.copytree(
        project, other,
        ignore=shutil.ignore_patterns(BUILD, ".doit.db*", ".doit-port", "runs.log"),
    )
    clear_runs(other)
    assert run_doit(other, "port", "import", str(bundle)) == 0
    assert run_doit(other) == 0
    assert read_runs(other) == []
    assert _build(other, "rows.txt").read_text(encoding="utf-8") == "hello world"


def test_import_is_idempotent(project: Path, tmp_path: Path) -> None:
    run_doit(project)
    bundle = tmp_path / "b"
    run_doit(project, "port", "export", str(bundle))
    first = run_doit(project, "port", "import", str(bundle))
    second = run_doit(project, "port", "import", str(bundle))
    assert first == 0 and second == 0


# ---------------------------------------------------------------- early cutoff


def test_early_cutoff_when_upstream_output_is_unchanged(project: Path) -> None:
    """Editing a line collect() throws away must not rebuild downstream.

    doit re-runs every downstream task as soon as an upstream task ran, because
    it only knows *that* it ran. Here the downstream key depends on the bytes
    of ``build/rows.txt``, which did not change, so summarize/report stay valid.
    """
    assert run_doit(project) == 0
    clear_runs(project)

    (project / "src" / "input.txt").write_text(
        "hello world\na completely different second line\n", encoding="utf-8"
    )
    assert run_doit(project) == 0

    assert read_runs(project) == ["collect"], (
        "only collect reads src/input.txt; its output is byte-identical"
    )


def test_real_change_propagates_through_the_whole_chain(project: Path) -> None:
    assert run_doit(project) == 0
    clear_runs(project)

    (project / "src" / "input.txt").write_text("goodbye\nwhatever\n", encoding="utf-8")
    assert run_doit(project) == 0

    assert read_runs(project) == ["collect", "summarize", "report"]
    assert "chars: 7" in _build(project, "report.md").read_text(encoding="utf-8")


def test_reverting_an_input_reproduces_the_original_build(project: Path) -> None:
    """Going back to an earlier input rebuilds that exact state.

    Downstream keys fold in the *contents* of their upstream artefacts, not a
    flag saying "upstream ran", but they are still recomputed only after the
    upstream stage has produced its bytes -- so a revert re-runs the chain once
    and then converges. The point of the assertion is the result, not the
    number of actions.
    """
    assert run_doit(project) == 0
    original = (project / "src" / "input.txt").read_text(encoding="utf-8")
    (project / "src" / "input.txt").write_text("something else\nx\n", encoding="utf-8")
    run_doit(project)

    clear_runs(project)
    (project / "src" / "input.txt").write_text(original, encoding="utf-8")
    assert run_doit(project) == 0
    assert read_runs(project) == ["collect", "summarize", "report"]
    assert (project / "build" / "rows.txt").read_text(encoding="utf-8") == "hello world"
    assert "chars: 11" in (project / "build" / "report.md").read_text(encoding="utf-8")


# ------------------------------------------------------------ failure recovery


FAILING_DODO = '''
import pathlib

from doit_port import port_task

RUNLOG = pathlib.Path("runs.log")


def _rec(name):
    with RUNLOG.open("a", encoding="utf-8") as fh:
        fh.write(name + "\\n")


def task_one():
    def act():
        _rec("one")
        pathlib.Path("build").mkdir(parents=True, exist_ok=True)
        pathlib.Path("build/one.txt").write_text("ONE", encoding="utf-8")
        return None

    return port_task(basename="one", actions=[act], targets=["build/one.txt"])


def task_two():
    def act():
        _rec("two")
        if pathlib.Path("boom.flag").exists():
            raise RuntimeError("simulated downstream failure")
        pathlib.Path("build").mkdir(parents=True, exist_ok=True)
        pathlib.Path("build/two.txt").write_text("TWO", encoding="utf-8")
        return None

    return port_task(
        basename="two", actions=[act], targets=["build/two.txt"],
        inputs=["build/one.txt"], task_dep=["one"],
    )
'''


def test_failed_task_leaves_no_receipt_and_retries_alone(bare: Path) -> None:
    (bare / "dodo.py").write_text(FAILING_DODO, encoding="utf-8")
    (bare / "boom.flag").write_text("", encoding="utf-8")

    assert run_doit(bare) != 0, "the failure must surface as a non-zero exit code"
    assert read_runs(bare) == ["one", "two"]
    assert (bare / "build" / "one.txt").is_file()
    assert not (bare / "build" / "two.txt").exists()
    from doit_port.receipt import Receipt
    from doit_port.store import ObjectStore

    store = ObjectStore(bare / ".doit-port")
    assert Receipt.load_index(store, "one") is not None
    assert Receipt.load_index(store, "two") is None, (
        "a failed task must never get a receipt"
    )

    (bare / "boom.flag").unlink()
    clear_runs(bare)
    assert run_doit(bare) == 0
    assert read_runs(bare) == ["two"], "the successful upstream stage must not be repeated"
    assert (bare / "build" / "two.txt").read_text(encoding="utf-8") == "TWO"


def test_partial_artefact_from_a_crash_is_rebuilt(project: Path) -> None:
    """A truncated target is not 'good enough': the bytes decide."""
    assert run_doit(project) == 0
    _build(project, "report.md").write_text("# half", encoding="utf-8")
    clear_runs(project)
    assert run_doit(project) == 0
    assert read_runs(project) == ["report"]
    assert "chars: 11" in _build(project, "report.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------- drift


def test_tampered_artefact_is_rebuilt_and_downstream_reused(project: Path) -> None:
    """Hand-editing an artefact is caught, rebuilt -- and stops there.

    ``summary.json`` is rewritten by summarize, but because the source row did
    not change it comes back byte-identical, so ``report`` keeps its receipt.
    """
    assert run_doit(project) == 0
    good_report = _build(project, "report.md").read_bytes()
    _build(project, "summary.json").write_text('{"chars": 999}', encoding="utf-8")
    clear_runs(project)

    assert run_doit(project) == 0
    assert read_runs(project) == ["summarize"], (
        "report must not re-run: summarize produced identical bytes again"
    )
    data = json.loads(_build(project, "summary.json").read_text(encoding="utf-8"))
    assert data["chars"] == 11
    assert _build(project, "report.md").read_bytes() == good_report


def test_deleted_artefact_is_restored_without_rerunning(project: Path) -> None:
    assert run_doit(project) == 0
    expected = _build(project, "summary.json").read_bytes()
    _build(project, "summary.json").unlink()
    clear_runs(project)

    assert run_doit(project) == 0
    assert read_runs(project) == []
    assert _build(project, "summary.json").read_bytes() == expected


def test_corrupt_blob_blocks_the_hit_and_rebuilds(project: Path) -> None:
    assert run_doit(project) == 0
    from doit_port.receipt import Receipt

    index = Receipt.load_index(ObjectStore(project / ".doit-port"), "summarize")
    assert index is not None
    key = index["outputs"][0]["sha256"]
    blob = project / ".doit-port" / "objects" / key[:2] / key
    blob.write_bytes(b"corrupted")
    shutil.rmtree(project / BUILD)
    drop_doit_state(project)
    clear_runs(project)

    assert run_doit(project) == 0
    assert "summarize" in read_runs(project), (
        "a receipt we cannot back with bytes must not be claimed"
    )
    assert _build(project, "summary.json").is_file()


# --------------------------------------------------------------- mixed dodo


def test_plain_doit_tasks_keep_working_alongside_ported_ones(bare: Path) -> None:
    (bare / "dodo.py").write_text(
        '''
import pathlib

from doit_port import port_task


def task_ported():
    def act():
        pathlib.Path("p.txt").write_text("P", encoding="utf-8")
        return None
    return port_task(basename="ported", actions=[act], targets=["p.txt"])


def task_plain():
    """A normal doit task: no targets, no port."""
    def act():
        pathlib.Path("q.txt").write_text("Q", encoding="utf-8")
        return None
    return {"actions": [act]}
''',
        encoding="utf-8",
    )
    assert run_doit(bare) == 0, "the hook must return None for unregistered tasks"
    assert (bare / "p.txt").read_text(encoding="utf-8") == "P"
    assert (bare / "q.txt").read_text(encoding="utf-8") == "Q"
