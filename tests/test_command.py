"""The ``doit port`` sub-command: argparse surface plus real dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from doit_port.command import PortCommand
from doit_port.receipt import OutputRecord, Receipt
from doit_port.store import ObjectStore


@pytest.fixture()
def project(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "doit-port.toml").write_text('[doit_port]\nflavor = "cmd-test"\n', encoding="utf-8")
    return tmp_path


def run(argv: list[str], **opts) -> int:
    base = {"json": False, "force": False, "dry_run": False, "no_objects": False}
    base.update(opts)
    return PortCommand().execute(base, argv)


def seed(root: Path) -> tuple[ObjectStore, str]:
    store = ObjectStore(root / ".doit-port")
    store.ensure()
    obj = store.put_bytes(b"payload")
    assert obj is not None
    receipt = Receipt(
        input_key="a" * 64, task="build",
        outputs=[OutputRecord("out/x.txt", obj.sha256, obj.size)],
        actions_sig="s",
    )
    receipt.save(store)
    receipt.save_index(store)
    return store, obj.sha256


# --------------------------------------------------------------- arg surface


def test_no_arguments_prints_help(project, capsys) -> None:
    assert run([]) == 2
    assert "sub-commands" in capsys.readouterr().err


def test_unknown_sub_command(project, capsys) -> None:
    assert run(["explode"]) == 2
    assert "unknown sub-command" in capsys.readouterr().err


def test_export_without_directory(project, capsys) -> None:
    assert run(["export"]) == 2
    assert "usage" in capsys.readouterr().err


def test_import_without_directory(project, capsys) -> None:
    assert run(["import"]) == 2


def test_forget_without_task(project, capsys) -> None:
    assert run(["forget"]) == 2


def test_command_name_and_docs() -> None:
    assert PortCommand.get_name() == "port"
    assert "receipt" in PortCommand.doc_purpose
    assert PortCommand.execute_tasks is False
    names = {opt["name"] for opt in PortCommand.cmd_options}
    assert names == {"json", "force", "dry_run", "no_objects"}


# ------------------------------------------------------------------ status


def test_status_on_an_empty_project(project, capsys) -> None:
    assert run(["status"]) == 0
    out = capsys.readouterr().out
    assert "no receipts yet" in out


def test_status_lists_tasks(project, capsys) -> None:
    seed(project)
    assert run(["status"]) == 0
    out = capsys.readouterr().out
    assert "build" in out
    assert "ok" in out
    assert "cmd-test" in out


def test_status_reports_missing_blob(project, capsys) -> None:
    store, sha = seed(project)
    store.blob_path(sha).unlink()
    assert run(["status"]) == 0
    assert "missing/corrupt" in capsys.readouterr().out


def test_status_reports_unstored_output(project, capsys) -> None:
    store = ObjectStore(project / ".doit-port")
    store.ensure()
    Receipt(
        input_key="b" * 64, task="big",
        outputs=[OutputRecord("out/big.bin", "c" * 64, 99, stored=False)],
    ).save_index(store)
    assert run(["status"]) == 0
    assert "not stored" in capsys.readouterr().out


def test_status_json(project, capsys) -> None:
    seed(project)
    assert run(["status"], json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"][0]["task"] == "build"


# ------------------------------------------------------------------ verify


def test_verify_ok(project, capsys) -> None:
    seed(project)
    assert run(["verify"]) == 0
    assert "OK" in capsys.readouterr().out


def test_verify_detects_corrupt_blob(project, capsys) -> None:
    store, sha = seed(project)
    store.blob_path(sha).write_bytes(b"nope")
    assert run(["verify"]) == 1
    assert "bad" in capsys.readouterr().out


def test_verify_flags_unstored_output(project, capsys) -> None:
    store = ObjectStore(project / ".doit-port")
    store.ensure()
    Receipt(
        input_key="c" * 64, task="skip",
        outputs=[OutputRecord("out/s.bin", "d" * 64, 1, stored=False)],
    ).save(store)
    assert run(["verify"]) == 1
    assert "never stored" in capsys.readouterr().out


def test_verify_bundle_from_disk(project, capsys) -> None:
    seed(project)
    assert run(["export", "bundle"]) == 0
    capsys.readouterr()
    assert run(["verify", "bundle"]) == 0
    assert "root hash       : OK" in capsys.readouterr().out


def test_verify_bundle_json(project, capsys) -> None:
    seed(project)
    run(["export", "bundle"])
    capsys.readouterr()
    assert run(["verify", "bundle"], json=True) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


# ---------------------------------------------------------------------- gc


def test_gc_removes_orphans(project, capsys) -> None:
    store, sha = seed(project)
    orphan = store.put_bytes(b"nobody wants me")
    assert orphan
    assert run(["gc"]) == 0
    assert not store.has(orphan.sha256)
    assert store.has(sha)
    assert "removed 1 object" in capsys.readouterr().out


def test_gc_dry_run(project, capsys) -> None:
    store, _ = seed(project)
    orphan = store.put_bytes(b"spare")
    assert orphan
    assert run(["gc"], dry_run=True) == 0
    assert store.has(orphan.sha256)
    assert "would remove" in capsys.readouterr().out


def test_gc_json(project, capsys) -> None:
    seed(project)
    store = ObjectStore(project / ".doit-port")
    store.put_bytes(b"orphan")
    assert run(["gc"], json=True) == 0
    assert json.loads(capsys.readouterr().out)["removed"] == 1


# ------------------------------------------------------------------ export


def test_export_creates_bundle(project, capsys) -> None:
    seed(project)
    assert run(["export", "b1"]) == 0
    assert (project / "b1" / "manifest.json").is_file()
    assert "exported 1 receipt" in capsys.readouterr().out


def test_export_refuses_existing_without_force(project, capsys) -> None:
    seed(project)
    (project / "b1").mkdir()
    assert run(["export", "b1"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_export_force(project) -> None:
    seed(project)
    (project / "b1").mkdir()
    (project / "b1" / "junk.txt").write_text("x", encoding="utf-8")
    assert run(["export", "b1"], force=True) == 0
    assert not (project / "b1" / "junk.txt").exists()


def test_export_without_objects(project) -> None:
    seed(project)
    assert run(["export", "b1"], no_objects=True) == 0
    assert list((project / "b1" / "objects").iterdir()) == []


# ------------------------------------------------------------------ import


def test_import_roundtrip(project, tmp_path, capsys) -> None:
    seed(project)
    run(["export", "b1"])
    capsys.readouterr()

    other = tmp_path / "other"
    other.mkdir()
    (other / ".doit-port").mkdir()
    # move the bundle, then import with cwd pointed at the empty project
    import os
    import shutil

    shutil.copytree(project / "b1", other / "b1")
    old = os.getcwd()
    os.chdir(other)
    try:
        assert run(["import", "b1"]) == 0
    finally:
        os.chdir(old)
    assert (other / ".doit-port" / "receipts" / f"{'a' * 64}.json").is_file()
    assert "imported 1 object" in capsys.readouterr().out


def test_import_dry_run(project, capsys) -> None:
    seed(project)
    run(["export", "b1"])
    capsys.readouterr()
    assert run(["import", "b1"], dry_run=True) == 0
    assert "would import" in capsys.readouterr().out


def test_import_of_a_missing_bundle_is_a_clean_error(project, capsys) -> None:
    assert run(["import", "nope"]) == 1
    assert "not a directory" in capsys.readouterr().err


# ------------------------------------------------------------------ forget


def test_forget_removes_receipts_for_one_task(project, capsys) -> None:
    store, _ = seed(project)
    out = store.put_bytes(b"other")
    assert out
    Receipt(
        input_key="e" * 64, task="other",
        outputs=[OutputRecord("out/y.txt", out.sha256, out.size)],
    ).save(store)

    assert run(["forget", "build"]) == 0
    assert Receipt.load(store, "a" * 64) is None
    assert Receipt.load(store, "e" * 64) is not None
    assert Receipt.load_index(store, "build") is None
    assert "forgot 1 receipt" in capsys.readouterr().out


def test_forget_unknown_task_is_harmless(project, capsys) -> None:
    assert run(["forget", "ghost"]) == 0
    assert "forgot 0 receipt" in capsys.readouterr().out
