"""Receipt schema, tamper detection and index history."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from doit_port.errors import ReceiptCorrupt
from doit_port.receipt import (
    OutputRecord,
    Receipt,
    atomic_write_json,
    load_all,
    referenced_blobs,
)
from doit_port.store import ObjectStore


@pytest.fixture()
def store(tmp_path: Path) -> ObjectStore:
    s = ObjectStore(tmp_path / "store")
    s.ensure()
    return s


def make_receipt(**overrides) -> Receipt:
    base = dict(
        input_key="a" * 64,
        task="build",
        outputs=[OutputRecord(path="out/thing.txt", sha256="b" * 64, size=42)],
        inputs={"src/a.txt": "c" * 64},
        deps={"up": "d" * 64},
        actions_sig="sig",
        engine={"python": "3.13.14"},
    )
    base.update(overrides)
    return Receipt(**base)


def test_roundtrip_through_disk(store: ObjectStore) -> None:
    receipt = make_receipt()
    receipt.save(store)
    loaded = Receipt.load(store, receipt.input_key)
    assert loaded is not None
    assert loaded.task == "build"
    assert loaded.outputs_key == receipt.outputs_key
    assert loaded.inputs == receipt.inputs


def test_missing_receipt_returns_none(store: ObjectStore) -> None:
    assert Receipt.load(store, "f" * 64) is None


def test_schema_mismatch_is_rejected(store: ObjectStore) -> None:
    payload = make_receipt().as_dict()
    payload["schema"] = 99
    with pytest.raises(ReceiptCorrupt, match="schema"):
        Receipt.from_dict(payload)


def test_key_mismatch_is_rejected() -> None:
    payload = make_receipt().as_dict()
    with pytest.raises(ReceiptCorrupt, match="key mismatch"):
        Receipt.from_dict(payload, expected_key="e" * 64)


def test_edited_output_digest_is_detected() -> None:
    payload = make_receipt().as_dict()
    payload["outputs"][0]["sha256"] = "f" * 64
    with pytest.raises(ReceiptCorrupt, match="tampered"):
        Receipt.from_dict(payload)


def test_absolute_output_path_is_rejected() -> None:
    payload = make_receipt().as_dict()
    payload["outputs"][0]["path"] = "C:/tmp/x.txt"
    with pytest.raises(ReceiptCorrupt, match="relative posix"):
        Receipt.from_dict(payload)


def test_backslash_path_is_rejected() -> None:
    payload = make_receipt().as_dict()
    payload["outputs"][0]["path"] = "out\\thing.txt"
    with pytest.raises(ReceiptCorrupt, match="relative posix"):
        Receipt.from_dict(payload)


def test_empty_outputs_are_rejected() -> None:
    payload = make_receipt().as_dict()
    payload["outputs"] = []
    with pytest.raises(ReceiptCorrupt, match="no outputs"):
        Receipt.from_dict(payload)


def test_short_digest_is_rejected() -> None:
    payload = make_receipt().as_dict()
    payload["outputs"][0]["sha256"] = "abc"
    with pytest.raises(ReceiptCorrupt, match="invalid sha256"):
        Receipt.from_dict(payload)


def test_non_object_payload_is_rejected() -> None:
    with pytest.raises(ReceiptCorrupt, match="not a JSON object"):
        Receipt.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]


def test_truncated_json_file_is_a_clean_error(store: ObjectStore) -> None:
    path = store.receipt_path("a" * 64)
    path.write_text('{"schema": 1, "input_key"', encoding="utf-8")
    with pytest.raises(ReceiptCorrupt, match="cannot read"):
        Receipt.load(store, "a" * 64)


def test_load_all_skips_corrupt_receipts(store: ObjectStore) -> None:
    make_receipt(input_key="1" * 64).save(store)
    make_receipt(input_key="2" * 64).save(store)
    bad = store.receipt_path("3" * 64)
    bad.write_text("{not json", encoding="utf-8")
    assert len(load_all(store)) == 2


def test_restorable_requires_every_blob_to_be_stored() -> None:
    assert make_receipt().restorable is True
    partial = make_receipt(
        outputs=[OutputRecord(path="a", sha256="b" * 64, size=1, stored=False)]
    )
    assert partial.restorable is False


def test_verify_blobs_reports_unstored_and_missing(store: ObjectStore) -> None:
    receipt = make_receipt(
        outputs=[
            OutputRecord(path="have", sha256="b" * 64, size=1),
            OutputRecord(path="never", sha256="c" * 64, size=1, stored=False),
        ]
    )
    assert set(receipt.verify_blobs(store)) == {"have", "never"}


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "x.json"
    atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert [p.name for p in target.parent.iterdir()] == ["x.json"]


def test_atomic_write_replaces_previous_content(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    atomic_write_json(target, {"generation": 1})
    atomic_write_json(target, {"generation": 2})
    assert json.loads(target.read_text(encoding="utf-8"))["generation"] == 2


def test_index_records_history(store: ObjectStore) -> None:
    receipt = make_receipt()
    receipt.save_index(store, {"out/thing.txt": ["b" * 64, "e" * 64]})
    index = Receipt.load_index(store, "build")
    assert index is not None
    assert index["history"]["out/thing.txt"] == ["b" * 64, "e" * 64]


def test_index_for_unknown_task_is_none(store: ObjectStore) -> None:
    assert Receipt.load_index(store, "nope") is None


def test_index_with_bad_json_is_none(store: ObjectStore) -> None:
    (store.index_dir / "broken.json").write_text("{", encoding="utf-8")
    assert Receipt.load_index(store, "broken") is None


def test_referenced_blobs_is_the_union(store: ObjectStore) -> None:
    one = make_receipt(input_key="1" * 64)
    two = make_receipt(
        input_key="2" * 64,
        outputs=[OutputRecord(path="other", sha256="9" * 64, size=1)],
    )
    assert referenced_blobs([one, two]) == {"b" * 64, "9" * 64}
