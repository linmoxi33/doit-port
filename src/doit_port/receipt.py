"""Receipt model: what a task produced, from what inputs, with which toolchain.

A receipt is the portable counterpart of the row ``doit`` keeps in its local
``.doit.db``. It is pure JSON, contains only relative posix paths, and is
stored under its own ``input_key`` so it is immutable and self-verifying.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ReceiptCorrupt
from .fingerprint import outputs_key as compute_outputs_key
from .store import ObjectStore

SCHEMA = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON via tmp + os.replace so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@dataclass
class OutputRecord:
    path: str          # posix, relative to project root
    sha256: str
    size: int
    stored: bool = True   # False when the blob was skipped (size cap / disabled)

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size, "stored": self.stored}


@dataclass
class Receipt:
    input_key: str
    task: str
    outputs: list[OutputRecord] = field(default_factory=list)
    inputs: dict[str, str] = field(default_factory=dict)
    deps: dict[str, str] = field(default_factory=dict)
    actions_sig: str = ""
    flavor: str = "default"
    engine: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)
    elapsed_ms: int = 0
    schema: int = SCHEMA

    # ---------------------------------------------------------------- derived

    @property
    def outputs_key(self) -> str:
        return compute_outputs_key([o.as_dict() for o in self.outputs])

    @property
    def restorable(self) -> bool:
        """Whether every output has a blob available for cross-machine replay."""
        return bool(self.outputs) and all(o.stored for o in self.outputs)

    def output_paths(self) -> list[str]:
        return [o.path for o in self.outputs]

    # ------------------------------------------------------------ (de)serialise

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "input_key": self.input_key,
            "task": self.task,
            "outputs_key": self.outputs_key,
            "outputs": [o.as_dict() for o in self.outputs],
            "inputs": dict(self.inputs),
            "deps": dict(self.deps),
            "actions_sig": self.actions_sig,
            "flavor": self.flavor,
            "engine": dict(self.engine),
            "created_at": self.created_at,
            "elapsed_ms": self.elapsed_ms,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, expected_key: str | None = None
                  ) -> "Receipt":
        if not isinstance(payload, Mapping):
            raise ReceiptCorrupt("receipt is not a JSON object")
        schema = payload.get("schema")
        if schema != SCHEMA:
            raise ReceiptCorrupt(f"unsupported receipt schema {schema!r} (expected {SCHEMA})")

        key = payload.get("input_key")
        if not isinstance(key, str) or len(key) != 64:
            raise ReceiptCorrupt("receipt has no valid input_key")
        if expected_key is not None and key != expected_key:
            raise ReceiptCorrupt(
                f"receipt key mismatch: file says {expected_key[:12]}…, body says {key[:12]}…"
            )

        raw_outputs = payload.get("outputs")
        if not isinstance(raw_outputs, list) or not raw_outputs:
            raise ReceiptCorrupt("receipt has no outputs")

        outputs: list[OutputRecord] = []
        for item in raw_outputs:
            if not isinstance(item, Mapping):
                raise ReceiptCorrupt("output entry is not an object")
            try:
                path = str(item["path"])
                sha = str(item["sha256"])
                size = int(item["size"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ReceiptCorrupt(f"malformed output entry: {exc}") from exc
            if "\\" in path or path.startswith("/") or ":" in path:
                raise ReceiptCorrupt(f"output path must be relative posix: {path!r}")
            if len(sha) != 64:
                raise ReceiptCorrupt(f"output {path!r} has an invalid sha256")
            outputs.append(OutputRecord(path, sha, size, bool(item.get("stored", True))))

        declared = payload.get("outputs_key")
        receipt = cls(
            input_key=key,
            task=str(payload.get("task", "")),
            outputs=outputs,
            inputs={str(k): str(v) for k, v in (payload.get("inputs") or {}).items()},
            deps={str(k): str(v) for k, v in (payload.get("deps") or {}).items()},
            actions_sig=str(payload.get("actions_sig", "")),
            flavor=str(payload.get("flavor", "default")),
            engine={str(k): str(v) for k, v in (payload.get("engine") or {}).items()},
            created_at=str(payload.get("created_at", "")),
            elapsed_ms=int(payload.get("elapsed_ms", 0)),
            schema=SCHEMA,
        )
        if declared and declared != receipt.outputs_key:
            raise ReceiptCorrupt(
                f"receipt {key[:12]}… was tampered with: declared outputs_key "
                f"{str(declared)[:12]}… != recomputed {receipt.outputs_key[:12]}…"
            )
        return receipt

    # ------------------------------------------------------------------- store

    def save(self, store: ObjectStore) -> Path:
        path = store.receipt_path(self.input_key)
        atomic_write_json(path, self.as_dict())
        return path

    @classmethod
    def load(cls, store: ObjectStore, input_key: str) -> "Receipt | None":
        """Load a receipt; ``None`` when absent, raise when corrupt."""
        path = store.receipt_path(input_key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReceiptCorrupt(f"cannot read receipt {path.name}: {exc}") from exc
        return cls.from_dict(payload, expected_key=input_key)

    # -------------------------------------------------------------------- index

    def save_index(self, store: ObjectStore, history: Mapping[str, Sequence[str]] | None = None
                   ) -> None:
        payload: dict[str, Any] = {
            "task": self.task,
            "input_key": self.input_key,
            "outputs_key": self.outputs_key,
            "created_at": self.created_at,
            "outputs": [o.as_dict() for o in self.outputs],
        }
        if history:
            payload["history"] = {k: list(v) for k, v in history.items()}
        atomic_write_json(store.index_path(self.task), payload)

    @staticmethod
    def load_index(store: ObjectStore, task_name: str) -> dict[str, Any] | None:
        path = store.index_path(task_name)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def verify_blobs(self, store: ObjectStore) -> list[str]:
        """Return the list of output paths whose blob is missing or corrupt."""
        problems: list[str] = []
        for out in self.outputs:
            if not out.stored:
                problems.append(out.path)
                continue
            if not store.verify(out.sha256):
                problems.append(out.path)
        return problems


def load_all(store: ObjectStore) -> list[Receipt]:
    """Load every receipt, skipping (and reporting) the corrupt ones.

    Corrupt receipts are ignored rather than fatal: a truncated file in the
    cache must never stop a build, it just means less can be reused.
    """
    if not store.receipts_dir.is_dir():
        return []
    result: list[Receipt] = []
    for path in sorted(store.receipts_dir.glob("*.json")):
        try:
            receipt = Receipt.load(store, path.stem)
        except ReceiptCorrupt:
            continue
        if receipt is not None:
            result.append(receipt)
    return result


def referenced_blobs(receipts: Sequence[Receipt]) -> set[str]:
    return {o.sha256 for r in receipts for o in r.outputs if o.stored}
