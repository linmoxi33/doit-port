"""Content-addressed object store.

Layout::

    .doit-port/
        objects/<xx>/<sha256>     # immutable blobs, key == sha256 of content
        tmp/                      # staging area for atomic writes

Every write goes through ``tmp/`` and an atomic ``os.replace`` so a killed
process can never leave a half-written blob under its final name. Because the
name *is* the content hash, a collision between concurrent writers is
harmless: both are writing identical bytes.

Reads always re-hash. That is a few milliseconds per artefact and it is the
only way to notice silent disk corruption before we spread it into a restored
build.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .errors import StoreCorrupt

_READ_CHUNK = 1024 * 256

# Reserved device names on Windows: "CON", "NUL", "COM1" ... cannot be used as a
# file name anywhere in a path, not even with an extension.
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _encode_task_name(task_name: str) -> str:
    """Filesystem-safe, still recognisable encoding of an arbitrary task name."""
    parts = [p for p in re.split(r"[:/\\\s]+", task_name) if p]
    cleaned = [_UNSAFE.sub("_", part) for part in parts] or ["task"]
    cleaned = [
        ("_" + part) if part.upper() in _WINDOWS_RESERVED else part for part in cleaned
    ]
    encoded = "__".join(cleaned)
    return encoded or "task"


@dataclass(frozen=True)
class StoredObject:
    sha256: str
    size: int


class ObjectStore:
    """Immutable, content-addressed blob storage rooted at ``root``."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.tmp_dir = self.root / "tmp"
        self.receipts_dir = self.root / "receipts"
        self.index_dir = self.root / "index"

    # ------------------------------------------------------------------ setup

    def ensure(self) -> None:
        for directory in (
            self.root,
            self.objects_dir,
            self.tmp_dir,
            self.receipts_dir,
            self.index_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------- paths

    def blob_path(self, sha256: str) -> Path:
        return self.objects_dir / sha256[:2] / sha256

    def receipt_path(self, input_key: str) -> Path:
        return self.receipts_dir / f"{input_key}.json"

    def index_path(self, task_name: str) -> Path:
        """One file per task, named after a filesystem-safe encoding of it.

        Task names are arbitrary user strings: they can contain ``:`` (illegal on
        Windows), ``/``, spaces, or be long enough to blow ``MAX_PATH``. A plain
        substitution is not enough -- ``"lint:src/a.py"`` still contains a colon
        -- so the name is split on the separators, each segment is escaped, and a
        readable prefix is kept for humans while a digest keeps it unique.
        """
        encoded = _encode_task_name(task_name)
        readable = encoded[:64].rstrip("._-") or "task"
        digest = hashlib.sha256(task_name.encode("utf-8", "surrogatepass")).hexdigest()[:10]
        return self.index_dir / f"{readable}.{digest}.json"

    def index_task_names(self) -> dict[str, str]:
        """Map ``index file stem -> original task name`` for diagnostics."""
        mapping: dict[str, str] = {}
        if not self.index_dir.is_dir():
            return mapping
        for path in self.index_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("task"), str):
                mapping[path.name] = payload["task"]
        return mapping

    # ------------------------------------------------------------------- write

    def _stage(self, source: Path | None = None, data: bytes | None = None
               ) -> tuple[Path, str, int]:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.tmp_dir / f"blob-{uuid.uuid4().hex}"
        h = hashlib.sha256()
        size = 0
        with open(tmp, "wb") as out:
            if data is not None:
                out.write(data)
                h.update(data)
                size = len(data)
            else:
                assert source is not None
                with open(source, "rb") as fh:
                    while True:
                        chunk = fh.read(_READ_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        h.update(chunk)
                        size += len(chunk)
        return tmp, h.hexdigest(), size

    def put_file(self, path: str | os.PathLike[str], max_bytes: int | None = None
                 ) -> StoredObject | None:
        """Store the contents of *path*.

        :return: the stored object, or ``None`` when the file exceeds
            *max_bytes* (the caller then records the digest but cannot promise
            cross-machine restoration for it).
        """
        source = Path(path)
        size = source.stat().st_size
        if max_bytes is not None and size > max_bytes:
            return None
        tmp, sha, written = self._stage(source=source)
        try:
            return self._commit(tmp, sha, written)
        finally:
            tmp.unlink(missing_ok=True)

    def put_bytes(self, data: bytes, max_bytes: int | None = None
                  ) -> StoredObject | None:
        if max_bytes is not None and len(data) > max_bytes:
            return None
        tmp, sha, written = self._stage(data=data)
        try:
            return self._commit(tmp, sha, written)
        finally:
            tmp.unlink(missing_ok=True)

    def _commit(self, tmp: Path, sha: str, size: int) -> StoredObject:
        """Move the staged blob into place.

        Two writers of identical content race benignly: whoever gets there
        first wins and the loser adopts the existing file. Windows raises
        ``PermissionError`` when ``os.replace`` targets a file another thread
        is replacing at the same instant, so a lost race is retried once --
        and if the blob is there afterwards, we are done either way.
        """
        target = self.blob_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # Same content, same name: keep the existing file, drop the copy.
            return StoredObject(sha256=sha, size=size)
        try:
            os.replace(tmp, target)
        except OSError:
            if target.is_file():
                return StoredObject(sha256=sha, size=size)
            raise
        return StoredObject(sha256=sha, size=size)

    # -------------------------------------------------------------------- read

    def has(self, sha256: str) -> bool:
        return self.blob_path(sha256).is_file()

    def get_bytes(self, sha256: str, verify: bool = True) -> bytes:
        path = self.blob_path(sha256)
        if not path.is_file():
            raise StoreCorrupt(f"object {sha256[:12]}… is missing from the store")
        data = path.read_bytes()
        if verify:
            actual = hashlib.sha256(data).hexdigest()
            if actual != sha256:
                raise StoreCorrupt(
                    f"object {sha256[:12]}… is corrupt: stored bytes hash to {actual[:12]}…"
                )
        return data

    def verify(self, sha256: str) -> bool:
        """``True`` when the blob exists and matches its key."""
        try:
            self.get_bytes(sha256)
        except StoreCorrupt:
            return False
        return True

    def size_of(self, sha256: str) -> int:
        path = self.blob_path(sha256)
        if not path.is_file():
            raise StoreCorrupt(f"object {sha256[:12]}… is missing from the store")
        return path.stat().st_size

    # ------------------------------------------------------------------ upkeep

    def iter_objects(self) -> Iterator[tuple[str, int]]:
        if not self.objects_dir.is_dir():
            return
        for shard in sorted(self.objects_dir.iterdir()):
            if not shard.is_dir():
                continue
            for blob in sorted(shard.iterdir()):
                if blob.is_file() and not blob.name.startswith("."):
                    yield blob.name, blob.stat().st_size

    def total_bytes(self) -> int:
        return sum(size for _, size in self.iter_objects())

    def gc(self, referenced: Iterable[str], dry_run: bool = False) -> dict[str, int]:
        """Delete blobs that no receipt refers to.

        :param referenced: set of sha256 strings still in use.
        :return: ``{"removed": n, "freed_bytes": n, "kept": n}``
        """
        keep = set(referenced)
        removed = 0
        freed = 0
        kept = 0
        for sha, size in list(self.iter_objects()):
            if sha in keep:
                kept += 1
                continue
            removed += 1
            freed += size
            if not dry_run:
                self.blob_path(sha).unlink(missing_ok=True)
        return {"removed": removed, "freed_bytes": freed, "kept": kept}

    def prune_empty_dirs(self) -> None:
        if not self.objects_dir.is_dir():
            return
        for shard in list(self.objects_dir.iterdir()):
            if shard.is_dir() and not any(shard.iterdir()):
                shutil.rmtree(shard, ignore_errors=True)

    def clear_tmp(self) -> int:
        """Remove leftovers from interrupted writers."""
        if not self.tmp_dir.is_dir():
            return 0
        count = 0
        for item in self.tmp_dir.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)
                count += 1
            elif item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
                count += 1
        return count
