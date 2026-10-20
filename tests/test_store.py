"""Object store: atomicity, integrity, garbage collection, crash recovery."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from doit_port.errors import StoreCorrupt
from doit_port.store import ObjectStore


@pytest.fixture()
def store(tmp_path: Path) -> ObjectStore:
    s = ObjectStore(tmp_path / "store")
    s.ensure()
    return s


def test_layout_is_created(store: ObjectStore) -> None:
    for name in ("objects", "tmp", "receipts", "index"):
        assert (store.root / name).is_dir()


def test_put_and_get_roundtrip(store: ObjectStore) -> None:
    obj = store.put_bytes(b"hello")
    assert obj is not None
    assert obj.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert store.get_bytes(obj.sha256) == b"hello"


def test_blob_name_is_sharded_by_prefix(store: ObjectStore) -> None:
    obj = store.put_bytes(b"shard-me")
    assert obj is not None
    path = store.blob_path(obj.sha256)
    assert path.parent.name == obj.sha256[:2]
    assert path.name == obj.sha256


def test_identical_content_is_deduplicated(store: ObjectStore) -> None:
    first = store.put_bytes(b"same")
    second = store.put_bytes(b"same")
    assert first is not None and second is not None
    assert first.sha256 == second.sha256
    blobs = list(store.iter_objects())
    assert len(blobs) == 1


def test_put_file_matches_put_bytes(store: ObjectStore, tmp_path: Path) -> None:
    src = tmp_path / "f.bin"
    src.write_bytes(b"file-content")
    from_file = store.put_file(src)
    from_bytes = store.put_bytes(b"file-content")
    assert from_file is not None and from_bytes is not None
    assert from_file.sha256 == from_bytes.sha256


def test_size_cap_skips_the_blob(store: ObjectStore, tmp_path: Path) -> None:
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 100)
    assert store.put_file(src, max_bytes=10) is None
    assert store.put_file(src, max_bytes=1000) is not None


def test_zero_cap_disables_storage(store: ObjectStore) -> None:
    assert store.put_bytes(b"anything", max_bytes=0) is None


def test_missing_blob_raises(store: ObjectStore) -> None:
    with pytest.raises(StoreCorrupt):
        store.get_bytes("0" * 64)


def test_corrupt_blob_raises_and_is_reported(store: ObjectStore) -> None:
    obj = store.put_bytes(b"trust me")
    assert obj is not None
    store.blob_path(obj.sha256).write_bytes(b"trust nobody")
    assert store.verify(obj.sha256) is False
    with pytest.raises(StoreCorrupt, match="corrupt"):
        store.get_bytes(obj.sha256)


def test_verify_without_rehash_still_reads(store: ObjectStore) -> None:
    obj = store.put_bytes(b"payload")
    assert obj is not None
    assert store.get_bytes(obj.sha256, verify=False) == b"payload"


def test_tmp_is_empty_after_successful_writes(store: ObjectStore) -> None:
    for i in range(5):
        store.put_bytes(f"blob-{i}".encode())
    assert list(store.tmp_dir.iterdir()) == []


def test_clear_tmp_removes_leftovers(store: ObjectStore) -> None:
    (store.tmp_dir / "blob-deadbeef").write_bytes(b"partial")
    (store.tmp_dir / "dir-leftover").mkdir()
    assert store.clear_tmp() == 2
    assert list(store.tmp_dir.iterdir()) == []


def test_gc_keeps_referenced_and_removes_orphans(store: ObjectStore) -> None:
    keep = store.put_bytes(b"keep")
    drop = store.put_bytes(b"drop")
    assert keep and drop
    result = store.gc({keep.sha256})
    assert result["removed"] == 1
    assert result["kept"] == 1
    assert store.has(keep.sha256)
    assert not store.has(drop.sha256)


def test_gc_dry_run_changes_nothing(store: ObjectStore) -> None:
    obj = store.put_bytes(b"orphan")
    assert obj is not None
    before = store.total_bytes()
    result = store.gc(set(), dry_run=True)
    assert result["removed"] == 1
    assert store.total_bytes() == before
    assert store.has(obj.sha256)


def test_gc_on_empty_store_is_a_noop(store: ObjectStore) -> None:
    assert store.gc(set()) == {"removed": 0, "freed_bytes": 0, "kept": 0}


def test_prune_empty_dirs_removes_shards(store: ObjectStore) -> None:
    obj = store.put_bytes(b"prunable")
    assert obj is not None
    store.gc(set())
    store.prune_empty_dirs()
    assert list(store.objects_dir.iterdir()) == []


def test_iter_objects_skips_hidden_files(store: ObjectStore) -> None:
    store.put_bytes(b"real")
    shard = next(store.objects_dir.iterdir())
    (shard / ".DS_Store").write_bytes(b"junk")
    assert len(list(store.iter_objects())) == 1


def test_total_bytes_adds_up(store: ObjectStore) -> None:
    store.put_bytes(b"12345")
    store.put_bytes(b"678")
    assert store.total_bytes() == 8


def test_store_survives_a_torn_write(store: ObjectStore) -> None:
    """A crash during a write leaves junk in tmp/, never a bad final blob."""
    obj = store.put_bytes(b"good")
    assert obj is not None
    (store.tmp_dir / "blob-dangling").write_bytes(b"half a file")
    store.clear_tmp()
    assert store.get_bytes(obj.sha256) == b"good"


def test_concurrent_writers_of_the_same_content_agree(store: ObjectStore) -> None:
    import threading

    results: list[str] = []

    def worker() -> None:
        obj = store.put_bytes(b"race")
        assert obj is not None
        results.append(obj.sha256)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1
    assert store.get_bytes(results[0]) == b"race"


def test_large_blob_is_streamed(store: ObjectStore) -> None:
    payload = os.urandom(1024 * 1024 + 7)
    obj = store.put_bytes(payload)
    assert obj is not None
    assert obj.size == len(payload)
    assert store.get_bytes(obj.sha256) == payload


# ------------------------------------------------------------ index file names
#
# Task names are arbitrary user strings and become file names. The old
# implementation just replaced "/" and "\\", which left ":" in place -- legal on
# Linux, illegal on Windows, so the bug only appeared on one platform. Asserting
# "a file was created" is not enough: on Linux that passes even with the colon.
# These tests assert the *name* contains only portable characters, which is
# checkable everywhere.


@pytest.mark.parametrize(
    "task_name",
    [
        "lint:src/sample_pkg/calc.py",
        "summary",
        "build/a:b",
        "CON",
        "with space",
        "a" * 300,
        "dots...",
        "..",
        "tab	name",
    ],
)
def test_index_file_name_is_portable(store: ObjectStore, task_name: str) -> None:
    """Index names must be valid on Windows *and* POSIX, and stay short."""
    path = store.index_path(task_name)
    name = path.name

    for bad in (":", "*", "?", '"', "<", ">", "|", "\\"):
        assert bad not in name, f"{bad!r} is illegal in a Windows file name: {name}"
    assert len(name) <= 80, "index names must not approach MAX_PATH limits"
    stem = name.split(".")[0]
    assert stem.strip() != "", "the readable part must not be empty"
    assert stem[-1] not in "._-", "trailing dots/spaces are illegal on Windows"

    # distinct tasks must never collide, even after truncation
    other = store.index_path(task_name + "x")
    assert other.name != name


def test_index_names_are_unique_after_encoding(store: ObjectStore) -> None:
    a = store.index_path("lint:src/a.py")
    b = store.index_path("lint/src/a.py")
    c = store.index_path("lint__src__a.py")
    assert len({a.name, b.name, c.name}) == 3, (
        "encoding may collapse separators, but the digest suffix must keep them apart"
    )


def test_many_threads_writing_distinct_blobs(store: ObjectStore) -> None:
    """Concurrency must not surface as exceptions, including on Windows.

    An earlier version retried ``os.replace`` on ``OSError``; a losing thread
    could still observe a transient error (pytest reported it as an unhandled
    thread exception), so the race is now serialised per key instead of being
    caught after the fact. This test runs far more writers than the
    same-content test, with distinct content, which is the harder case.
    """
    import threading

    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for _ in range(20):
                obj = store.put_bytes(f"payload-{index}".encode() * 50)
                assert obj is not None
                assert store.get_bytes(obj.sha256) == f"payload-{index}".encode() * 50
        except BaseException as exc:  # noqa: BLE001 - the test wants to see anything
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [], f"concurrent writers raised: {errors[:3]}"
    assert len(list(store.iter_objects())) == 16
    assert list(store.tmp_dir.iterdir()) == []
