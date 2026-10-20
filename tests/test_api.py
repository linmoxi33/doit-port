"""Task factory validation: refuse the cases we cannot make portable."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from doit_port import port_task
from doit_port.context import PortContext
from doit_port.errors import UnsupportedTask


@pytest.fixture()
def cwd(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    PortContext.reset()
    yield tmp_path
    PortContext.reset()


def test_minimal_task_shape(cwd: Path) -> None:
    task = port_task(name="t", actions=[lambda: None], targets=["out.txt"])
    assert task["targets"] == ["out.txt"]
    assert callable(task["uptodate"][0])
    assert task["meta"]["port"]["key_salt"] == ""


def test_inputs_are_normalised_to_posix(cwd: Path) -> None:
    task = port_task(
        name="t", actions=[], targets=["build\\out.txt"], inputs=["src\\a.txt"]
    )
    assert task["targets"] == ["build/out.txt"]
    assert task["meta"]["port"]["inputs"] == ["src/a.txt"]


def test_file_dep_is_rejected_with_a_helpful_message(cwd: Path) -> None:
    with pytest.raises(UnsupportedTask, match="use inputs"):
        port_task(name="t", actions=[], targets=["o"], file_dep=["a.txt"])


def test_missing_targets_is_rejected(cwd: Path) -> None:
    with pytest.raises(UnsupportedTask, match="at least one target"):
        port_task(name="t", actions=[], targets=[])


def test_absolute_target_is_rejected(cwd: Path) -> None:
    with pytest.raises(UnsupportedTask, match="absolute"):
        port_task(name="t", actions=[], targets=[str(cwd / "out.txt")])


def test_windows_absolute_target_is_rejected(cwd: Path) -> None:
    with pytest.raises(UnsupportedTask, match="absolute"):
        port_task(name="t", actions=[], targets=["C:/temp/out.txt"])


def test_target_escaping_the_project_is_rejected(cwd: Path) -> None:
    with pytest.raises(UnsupportedTask, match="escapes"):
        port_task(name="t", actions=[], targets=["../outside.txt"])


def test_directory_target_is_rejected(cwd: Path) -> None:
    (cwd / "dist").mkdir()
    with pytest.raises(UnsupportedTask, match="directory"):
        port_task(name="t", actions=[], targets=["dist"])


def test_unknown_attribute_is_rejected(cwd: Path) -> None:
    with pytest.raises(UnsupportedTask, match="unsupported task attribute"):
        port_task(name="t", actions=[], targets=["o"], rebase=True)


def test_passthrough_attributes_land_on_the_dict(cwd: Path) -> None:
    task = port_task(
        name="t", actions=[], targets=["o"], doc="hello", verbosity=2, clean=True
    )
    assert task["doc"] == "hello"
    assert task["verbosity"] == 2
    assert task["clean"] is True


def test_user_uptodate_runs_before_the_receipt(cwd: Path) -> None:
    task = port_task(
        name="t", actions=[], targets=["o"], uptodate=[lambda: False]
    )
    assert len(task["uptodate"]) == 2
    assert task["uptodate"][0]() is False


def test_spec_is_registered_for_the_running_context(cwd: Path) -> None:
    port_task(
        name="registered", actions=[], targets=["o"], inputs=["i"], task_dep=["up"]
    )
    from doit_port import describe

    info = describe("registered")
    assert info is not None
    assert info["inputs"] == ["i"]
    assert info["deps"] == ["up"]


def test_describe_returns_none_for_unknown(cwd: Path) -> None:
    assert port_task  # keep the import obvious
    from doit_port import describe

    assert describe("nope") is None


def test_setup_returns_config_and_sets_context(cwd: Path) -> None:
    from doit_port import setup

    cfg = setup(cwd, flavor="from-setup")
    assert cfg.flavor == "from-setup"
    assert PortContext.current().config.flavor == "from-setup"


def test_import_has_no_side_effects(tmp_path: Path) -> None:
    """Importing doit_port must not create a store or a context on disk."""
    result = subprocess.run(
        [sys.executable, "-c", "import doit_port; print(doit_port.__version__)"],
        capture_output=True, text=True, cwd=tmp_path, check=True,
    )
    assert result.stdout.strip() == "0.1.0"
    assert list(tmp_path.iterdir()) == []


def test_package_does_not_import_doit_eagerly(tmp_path: Path) -> None:
    """Diagnostic tools must work even when doit itself is broken."""
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import sys, doit_port; "
            "print('doit' in sys.modules)",
        ],
        capture_output=True, text=True, cwd=tmp_path, check=True,
    )
    assert result.stdout.strip() == "False"


# ------------------------------------------------------------------ groups


def test_port_group_refiles_specs_under_the_display_name(cwd: Path) -> None:
    """A yielded subtask is registered as "<basename>:<name>" by doit.

    If the spec stays filed under the bare name, the hook never finds it: the
    group's own task is the only thing that keeps the bare name, so the group
    shadows every one of its children. That is not a hypothetical -- it is what
    happened before `port_group` existed, and the symptom was "no receipt ever
    written" with no error.
    """
    from doit_port import port_group
    from doit_port.context import PortContext

    children = [
        port_task(name="a.py", actions=[], targets=["out/a.json"]),
        port_task(name="b.py", actions=[], targets=["out/b.json"]),
    ]
    yielded = list(port_group("lint", children))

    assert [item["name"] for item in yielded] == ["a.py", "b.py"]
    assert "basename" not in yielded[0], "a yielded sub-task must not carry basename"

    ctx = PortContext.current()
    specs = ctx.specs()
    assert "lint:a.py" in specs
    assert "lint:b.py" in specs
    assert "a.py" not in specs, "the bare name must not survive re-filing"


def test_port_group_rejects_foreign_tasks(cwd: Path) -> None:
    from doit_port import port_group

    with pytest.raises(UnsupportedTask, match="was not created by port_task"):
        list(port_group("lint", [{"name": "made-by-hand", "actions": []}]))


def test_port_group_rejects_an_empty_list(cwd: Path) -> None:
    from doit_port import port_group

    with pytest.raises(UnsupportedTask, match="needs at least one"):
        list(port_group("lint", []))


def test_port_task_rejects_both_name_and_basename(cwd: Path) -> None:
    with pytest.raises(UnsupportedTask, match="not both"):
        port_task(name="a", basename="b", actions=[], targets=["o"])


def test_port_task_requires_an_identity(cwd: Path) -> None:
    with pytest.raises(UnsupportedTask, match="needs name="):
        port_task(actions=[], targets=["o"])
