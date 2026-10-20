"""Bundle export / verify / import, including hostile input."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from doit_port.bundle import export_bundle, import_bundle, verify_bundle
from doit_port.errors import BundleError
from doit_port.receipt import OutputRecord, Receipt
from doit_port.store import ObjectStore


@pytest.fixture()
def populated(tmp_path: Path) -> tuple[ObjectStore, Path]:
    """A store with two tasks, their blobs and receipts."""
    store = ObjectStore(tmp_path / "store")
    store.ensure()

    out_a = store.put_bytes(b"artefact-a")
    out_b = store.put_bytes(b"artefact-b")
    assert out_a and out_b

    Receipt(
        input_key="1" * 64, task="a",
        outputs=[OutputRecord(".build/a.txt", out_a.sha256, out_a.size)],
        actions_sig="sig-a",
    ).save(store)
    Receipt(
        input_key="2" * 64, task="b",
        outputs=[OutputRecord(".build/b.txt", out_b.sha256, out_b.size)],
        actions_sig="sig-b",
    ).save(store)
    return store, tmp_path / "bundle"


# --------------------------------------------------------------------- export


def test_export_writes_manifest_receipts_and_objects(populated) -> None:
    store, target = populated
    report = export_bundle(store, target, flavor="test")
    assert report.receipts == 2
    assert report.objects == 2
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == 1
    assert manifest["flavor"] == "test"
    assert manifest["counts"] == {"receipts": 2, "objects": 2}
    assert len(manifest["files"]) == 4


def test_export_refuses_to_clobber_without_force(populated) -> None:
    store, target = populated
    target.mkdir()
    with pytest.raises(BundleError, match="already exists"):
        export_bundle(store, target)
    export_bundle(store, target, overwrite=True)


def test_export_without_objects_is_receipts_only(populated) -> None:
    store, target = populated
    report = export_bundle(store, target, include_objects=False)
    assert report.objects == 0
    assert (target / "receipts").is_dir()
    assert list((target / "objects").iterdir()) == []


def test_export_skips_receipts_whose_blob_is_gone(populated) -> None:
    store, target = populated
    victim = store.put_bytes(b"will vanish")
    assert victim
    Receipt(
        input_key="3" * 64, task="c",
        outputs=[OutputRecord(".build/c.txt", victim.sha256, victim.size)],
    ).save(store)
    store.blob_path(victim.sha256).unlink()

    report = export_bundle(store, target)
    assert report.receipts == 2
    assert any("unusable" in s for s in report.skipped)


def test_export_can_filter_by_task(populated) -> None:
    store, target = populated
    report = export_bundle(store, target, task_filter={"a"})
    assert report.receipts == 1
    assert report.objects == 1


# --------------------------------------------------------------------- verify


def test_verify_clean_bundle(populated) -> None:
    store, target = populated
    export_bundle(store, target)
    report = verify_bundle(target)
    assert report["ok"] is True
    assert report["problems"] == []
    assert report["root_hash_ok"] is True


def test_verify_detects_a_tampered_object(populated) -> None:
    store, target = populated
    export_bundle(store, target)
    blob = next(p for p in target.rglob("*") if p.is_file() and p.parent.name != "receipts"
                and p.name != "manifest.json")
    with blob.open("ab") as fh:
        fh.write(b"evil")
    report = verify_bundle(target)
    assert report["ok"] is False
    assert any("hash mismatch" in p for p in report["problems"])


def test_verify_detects_a_missing_file(populated) -> None:
    store, target = populated
    export_bundle(store, target)
    next((target / "receipts").glob("*.json")).unlink()
    report = verify_bundle(target)
    assert any("missing" in p for p in report["problems"])


def test_verify_detects_an_edited_manifest(populated) -> None:
    store, target = populated
    export_bundle(store, target)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = manifest["files"][:1]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = verify_bundle(target)
    assert report["root_hash_ok"] is False
    assert report["ok"] is False


def test_verify_rejects_unknown_schema(populated) -> None:
    store, target = populated
    export_bundle(store, target)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema"] = 7
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleError, match="schema"):
        verify_bundle(target)


def test_verify_rejects_a_directory_without_manifest(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="no manifest"):
        verify_bundle(tmp_path)


# --------------------------------------------------------------------- import


def test_import_into_an_empty_store(populated, tmp_path: Path) -> None:
    store, target = populated
    export_bundle(store, target)

    fresh = ObjectStore(tmp_path / "fresh")
    result = import_bundle(fresh, target)
    assert result["objects"] == 2
    assert result["receipts"] == 2
    assert Receipt.load(fresh, "1" * 64) is not None
    # indexes are rebuilt so dependency lookups work immediately
    assert Receipt.load_index(fresh, "a") is not None


def test_import_is_idempotent(populated, tmp_path: Path) -> None:
    store, target = populated
    export_bundle(store, target)
    fresh = ObjectStore(tmp_path / "fresh")
    import_bundle(fresh, target)
    second = import_bundle(fresh, target)
    assert second["objects"] == 0
    assert second["receipts"] == 0
    assert second["receipts_already_present"] == 2


def test_import_dry_run_writes_nothing(populated, tmp_path: Path) -> None:
    store, target = populated
    export_bundle(store, target)
    fresh = ObjectStore(tmp_path / "fresh")
    result = import_bundle(fresh, target, dry_run=True)
    assert result["dry_run"] is True
    assert result["objects"] == 2
    assert list(fresh.iter_objects()) == []


def test_import_refuses_a_tampered_bundle_and_writes_nothing(populated, tmp_path: Path) -> None:
    store, target = populated
    export_bundle(store, target)
    blob = next(p for p in (target / "objects").rglob("*") if p.is_file())
    with blob.open("ab") as fh:
        fh.write(b"evil")

    fresh = ObjectStore(tmp_path / "fresh")
    with pytest.raises(BundleError, match="failed verification"):
        import_bundle(fresh, target)
    assert list(fresh.iter_objects()) == []
    assert list(fresh.receipts_dir.glob("*.json")) == []


def test_import_rejects_path_traversal(populated, tmp_path: Path) -> None:
    store, target = populated
    export_bundle(store, target)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append({"path": "../../escape.txt", "sha256": "0" * 64, "size": 1})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    fresh = ObjectStore(tmp_path / "fresh")
    with pytest.raises(BundleError):
        import_bundle(fresh, target)


def test_import_rejects_absolute_paths(populated, tmp_path: Path) -> None:
    store, target = populated
    export_bundle(store, target)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append({"path": "C:/Windows/system32/x.dll", "sha256": "0" * 64, "size": 1})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    fresh = ObjectStore(tmp_path / "fresh")
    with pytest.raises(BundleError):
        import_bundle(fresh, target)


def test_import_rejects_malformed_manifest_entries(populated) -> None:
    store, target = populated
    export_bundle(store, target)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = [{"path": "x"}]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleError, match="malformed"):
        verify_bundle(target)


# ------------------------------------------------------------- path safety
#
# These call _safe_join directly. Going through import_bundle only proves that
# *something* rejects the path; the digests usually fail first, which would
# leave the traversal guard itself untested (a surviving mutant showed exactly
# that).


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "/etc/passwd",
        "\\windows\\system32",
        "C:/Windows/notepad.exe",
        "C:relative-with-drive",
        "../outside.txt",
        "../../outside.txt",
        "objects/../../outside.txt",
        "objects/../../../etc/shadow",
    ],
)
def test_safe_join_refuses_dangerous_paths(tmp_path: Path, bad: str) -> None:
    from doit_port.bundle import _safe_join

    with pytest.raises(BundleError):
        _safe_join(tmp_path, bad)


def test_safe_join_rejects_non_relative_paths_before_resolving(
    tmp_path: Path, monkeypatch
) -> None:
    """Absolute paths, drive letters and empty names are refused lexically.

    Belt and braces on purpose: on a POSIX root the resolved check would let
    ``/etc/passwd`` through as ``tmp_path/etc/passwd`` instead of rejecting it,
    so the explicit prefix check is the only thing that means "this manifest is
    not even well-formed". To prove that check runs, the resolve() step is
    stubbed to a harmless value -- if the prefix check is gone, the path is
    accepted.
    """
    from doit_port import bundle as bundle_module
    from doit_port.bundle import _safe_join

    class _Harmless:
        parts = ("x",)
        parents: tuple = ()

        def __eq__(self, other: object) -> bool:  # noqa: D105
            return True

        def __hash__(self) -> int:  # pragma: no cover
            return 1

    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: _Harmless())
    for bad in ("/etc/passwd", "\windows\system32", "C:/Windows/notepad.exe", "C:relative"):
        with pytest.raises(bundle_module.BundleError, match="unsafe path"):
            _safe_join(tmp_path, bad)
    monkeypatch.undo()

    with pytest.raises(bundle_module.BundleError, match="unsafe path"):
        _safe_join(tmp_path, "")


def test_safe_join_rejects_every_dotdot_form(tmp_path: Path, monkeypatch) -> None:
    """A '..' component is refused even when it resolves back inside the root.

    `_safe_join` has two guards: a lexical one (any ``..`` part) and a resolved
    one (the resolved path must be inside the root). Testing only "does it
    raise" does not tell them apart, because for the obvious hostile paths both
    fire -- a mutation run proved that the lexical guard could be deleted with
    the suite still green. Here the "candidate" is stubbed to look like a
    perfectly normal inside-root path, so only the lexical guard can refuse.
    """
    from doit_port import bundle as bundle_module
    from doit_port.bundle import _safe_join

    inside = (tmp_path / "a" / "b").resolve()

    class _FakeCandidate:
        parents = (inside,)
        parts = ("x",)

        def __eq__(self, other: object) -> bool:  # noqa: D105
            return True

        def __hash__(self) -> int:  # pragma: no cover
            return id(self)

    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: _FakeCandidate())
    with pytest.raises(bundle_module.BundleError, match="escapes"):
        _safe_join(tmp_path, "a/../b")
    monkeypatch.undo()

    # and the ordinary cases, both guards live
    for path in ("../outside.txt", "a/../../b", ".."):
        with pytest.raises(bundle_module.BundleError):
            _safe_join(tmp_path, path)


def test_safe_join_rejects_a_symlink_that_leaves_the_bundle(tmp_path: Path) -> None:
    """Lexically innocent, resolves outside: only the resolved check catches it."""
    from doit_port.bundle import _safe_join
    from doit_port.errors import BundleError

    outside = tmp_path.parent / "sibling"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - needs privileges
        pytest.skip("symlinks are not available to this user")

    with pytest.raises(BundleError, match="escapes"):
        _safe_join(tmp_path, "link/file.txt")


def test_safe_join_accepts_normal_paths(tmp_path: Path) -> None:
    from doit_port.bundle import _safe_join

    assert _safe_join(tmp_path, "objects/ab/abcdef") == tmp_path / "objects" / "ab" / "abcdef"
    assert _safe_join(tmp_path, "manifest.json") == tmp_path / "manifest.json"


def test_import_rejects_traversal_even_when_the_digest_is_fine(populated, tmp_path: Path) -> None:
    """A perfectly valid file placed outside the bundle is still refused."""
    store, target = populated
    export_bundle(store, target)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"legit bytes")

    from doit_port.fingerprint import file_digest

    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append(
        {"path": "../outside.txt", "sha256": file_digest(outside), "size": outside.stat().st_size}
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    fresh = ObjectStore(tmp_path / "fresh2")
    with pytest.raises(BundleError, match="escapes"):
        import_bundle(fresh, target)


def test_bundle_roundtrip_preserves_bytes(populated, tmp_path: Path) -> None:
    store, target = populated
    export_bundle(store, target)
    fresh = ObjectStore(tmp_path / "fresh")
    import_bundle(fresh, target)
    receipt = Receipt.load(fresh, "1" * 64)
    assert receipt is not None
    assert fresh.get_bytes(receipt.outputs[0].sha256) == b"artefact-a"


def test_verify_bundle_refuses_traversal_with_matching_digest(tmp_path: Path) -> None:
    """verify_bundle() must reject a hostile path on its own.

    Without this, the guard is only reachable through import_bundle(), where the
    digest check fires first -- so deleting the guard changes nothing that any
    test can see. That is exactly how the "bundle-path-traversal-allowed"
    mutant survived the first mutation run.
    """
    from doit_port.fingerprint import file_digest

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"valid bytes")
    bundle = tmp_path / "b"
    (bundle / "receipts").mkdir(parents=True)
    manifest = {
        "schema": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "flavor": None,
        "include_objects": False,
        "files": [
            {
                "path": "../outside.txt",
                "sha256": file_digest(outside),
                "size": outside.stat().st_size,
            }
        ],
    }
    manifest["root_hash"] = "irrelevant"
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_bundle(bundle)
    assert report["ok"] is False
    # the path guard fires before any file is opened, so the message names the
    # path as unsafe rather than "escapes"; both are refusals, but this asserts
    # the guard itself ran (a resolve()-only check is caught by the other tests)
    assert any("unsafe path" in p or "escapes" in p for p in report["problems"])


def test_export_of_empty_store_produces_a_valid_bundle(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "store")
    store.ensure()
    target = tmp_path / "empty-bundle"
    report = export_bundle(store, target)
    assert report.receipts == 0
    assert verify_bundle(target)["ok"] is True
