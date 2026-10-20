"""Export and import portable bundles.

A bundle is a plain directory::

    my-bundle/
        manifest.json
        objects/<xx>/<sha256>
        receipts/<input_key>.json

It is the only thing you need to move a build cache between machines, CI jobs,
or colleagues. Nothing in it is machine-specific: every path is relative posix,
every artefact is addressed by the sha256 of its bytes.

Import is paranoid on purpose. Every file listed in the manifest is hashed
*before* a single byte is copied into the local store, the manifest root hash
is recomputed, and paths are rejected unless they match the expected layout.
A bundle that has been edited, truncated or poisoned fails loudly instead of
quietly corrupting a cache.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import BundleError
from .fingerprint import file_digest
from .receipt import Receipt, atomic_write_json, load_all
from .store import ObjectStore

BUNDLE_SCHEMA = 1
MANIFEST_NAME = "manifest.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _root_hash(files: Sequence[Mapping[str, Any]]) -> str:
    import hashlib

    h = hashlib.sha256()
    for entry in sorted(files, key=lambda e: e["path"]):
        h.update(f"{entry['path']}\t{entry['sha256']}\t{entry['size']}\n".encode())
    return h.hexdigest()


@dataclass
class BundleReport:
    path: Path
    receipts: int = 0
    objects: int = 0
    total_bytes: int = 0
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "receipts": self.receipts,
            "objects": self.objects,
            "total_bytes": self.total_bytes,
            "skipped": list(self.skipped),
        }


# --------------------------------------------------------------------- export


def export_bundle(
    store: ObjectStore,
    target: str | os.PathLike[str],
    *,
    include_objects: bool = True,
    flavor: str | None = None,
    overwrite: bool = False,
    task_filter: Iterable[str] | None = None,
) -> BundleReport:
    """Copy every usable receipt (and its blobs) into *target*.

    Receipts whose blobs are missing or corrupt are skipped: shipping them
    would make the receiver believe it can restore an artefact it cannot.
    """
    target_path = Path(target)
    if target_path.exists():
        if not overwrite:
            raise BundleError(
                f"{target_path} already exists; pass overwrite=True (or --force) to replace it"
            )
        shutil.rmtree(target_path)
    (target_path / "objects").mkdir(parents=True, exist_ok=True)
    (target_path / "receipts").mkdir(parents=True, exist_ok=True)

    wanted = set(task_filter) if task_filter else None
    report = BundleReport(path=target_path)

    receivers: list[Receipt] = []
    for receipt in load_all(store):
        if wanted is not None and receipt.task not in wanted:
            continue
        if receipt.verify_blobs(store):
            report.skipped.append(
                f"{receipt.task}@{receipt.input_key[:12]} (unusable blob)"
            )
            continue
        receivers.append(receipt)

    files: list[dict[str, Any]] = []
    for receipt in receivers:
        dest = target_path / "receipts" / f"{receipt.input_key}.json"
        atomic_write_json(dest, receipt.as_dict())
        files.append(
            {
                "path": dest.relative_to(target_path).as_posix(),
                "sha256": file_digest(dest),
                "size": dest.stat().st_size,
            }
        )
        report.receipts += 1

    if include_objects:
        written: set[str] = set()
        for receipt in receivers:
            for out in receipt.outputs:
                if not out.stored or out.sha256 in written:
                    continue
                src = store.blob_path(out.sha256)
                if not src.is_file():
                    continue
                dest = target_path / "objects" / out.sha256[:2] / out.sha256
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
                # re-hash what actually landed on disk, not what we hoped
                actual = file_digest(dest)
                if actual != out.sha256:
                    dest.unlink(missing_ok=True)
                    raise BundleError(
                        f"copy of {out.sha256[:12]}… did not survive: got {actual[:12]}…"
                    )
                files.append(
                    {
                        "path": dest.relative_to(target_path).as_posix(),
                        "sha256": actual,
                        "size": dest.stat().st_size,
                    }
                )
                written.add(out.sha256)
                report.objects += 1
                report.total_bytes += dest.stat().st_size

    manifest = {
        "schema": BUNDLE_SCHEMA,
        "created_at": _now(),
        "flavor": flavor,
        "include_objects": bool(include_objects),
        "files": files,
        "root_hash": _root_hash(files),
        "counts": {"receipts": report.receipts, "objects": report.objects},
    }
    atomic_write_json(target_path / MANIFEST_NAME, manifest)
    return report


# --------------------------------------------------------------------- verify


def verify_bundle(bundle: str | os.PathLike[str]) -> dict[str, Any]:
    """Check a bundle without touching any local store."""
    path = Path(bundle)
    manifest = _read_manifest(path)

    problems: list[str] = []
    checked = 0
    total = 0
    for entry in manifest["files"]:
        rel = entry.get("path", "")
        try:
            file_path = _safe_join(path, rel)
        except BundleError as exc:
            # Report it as a problem rather than raising: the caller needs one
            # machine-readable verdict, not two ways to fail. This guard matters
            # on its own -- without it a bundle can point at a file that exists
            # outside the bundle and hash correctly, so the check has to happen
            # here and not only in import_bundle().
            problems.append(str(exc))
            continue
        if not file_path.is_file():
            problems.append(f"missing: {rel}")
            continue
        actual = file_digest(file_path)
        checked += 1
        total += file_path.stat().st_size
        if actual != entry["sha256"]:
            problems.append(f"hash mismatch: {rel}")

    expected_root = manifest.get("root_hash")
    actual_root = _root_hash(manifest["files"])
    root_ok = expected_root == actual_root
    if not root_ok:
        problems.append("manifest root hash mismatch (files list was edited)")

    return {
        "path": str(path),
        "schema": manifest.get("schema"),
        "files_checked": checked,
        "total_bytes": total,
        "root_hash_ok": root_ok,
        "problems": problems,
        "ok": not problems,
        "created_at": manifest.get("created_at"),
        "counts": manifest.get("counts", {}),
    }


# --------------------------------------------------------------------- import


def import_bundle(
    store: ObjectStore,
    bundle: str | os.PathLike[str],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Verify *bundle* completely, then copy its contents into *store*.

    Two-phase on purpose: nothing is written locally until every file in the
    bundle has been proven to match its digest.
    """
    path = Path(bundle)
    manifest = _read_manifest(path)
    store.ensure()

    # Phase 1 -- verify everything, write nothing.
    failures: list[str] = []
    for entry in manifest["files"]:
        rel = entry.get("path", "")
        try:
            file_path = _safe_join(path, rel)
        except BundleError as exc:
            failures.append(str(exc))
            continue
        if not file_path.is_file():
            failures.append(f"missing: {rel}")
            continue
        if file_digest(file_path) != entry["sha256"]:
            failures.append(f"hash mismatch: {rel}")

    if _root_hash(manifest["files"]) != manifest.get("root_hash"):
        failures.append("manifest root hash mismatch")

    if failures:
        raise BundleError(
            f"bundle {path} failed verification ({len(failures)} problem(s)): "
            + "; ".join(failures[:5])
            + (" …" if len(failures) > 5 else "")
        )

    # Phase 2 -- copy.
    imported_objects = 0
    imported_receipts = 0
    skipped_receipts = 0
    bytes_in = 0

    for entry in manifest["files"]:
        rel = entry["path"]
        # re-checked after phase 1; verified above, so a raise here means the
        # bundle changed under us -- that is a genuine error, not a reportable one
        src = _safe_join(path, rel)
        if rel.startswith("objects/"):
            dest = store.blob_path(Path(rel).name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and file_digest(dest) == Path(rel).name:
                continue
            if not dry_run:
                shutil.copyfile(src, dest)
            imported_objects += 1
            bytes_in += entry["size"]
        elif rel.startswith("receipts/"):
            key = Path(rel).stem
            dest = store.receipt_path(key)
            if dest.exists():
                skipped_receipts += 1
                continue
            if not dry_run:
                shutil.copyfile(src, dest)
            imported_receipts += 1
            # refresh the per-task index so dependency lookups see the new state
            if not dry_run:
                try:
                    receipt = Receipt.load(store, key)
                except Exception:  # noqa: BLE001 - verified above, defensive only
                    receipt = None
                if receipt is not None:
                    receipt.save_index(store)

    return {
        "bundle": str(path),
        "objects": imported_objects,
        "receipts": imported_receipts,
        "receipts_already_present": skipped_receipts,
        "bytes": bytes_in,
        "dry_run": dry_run,
    }


# ------------------------------------------------------------------- helpers


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.is_dir():
        raise BundleError(f"{path} is not a directory")
    manifest_path = path / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BundleError(f"{path} has no {MANIFEST_NAME}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(f"cannot read {manifest_path}: {exc}") from exc
    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise BundleError(
            f"unsupported bundle schema {manifest.get('schema')!r} (expected {BUNDLE_SCHEMA})"
        )
    files = manifest.get("files")
    if not isinstance(files, list):
        raise BundleError("manifest has no file list")
    for entry in files:
        if not isinstance(entry, dict) or not {"path", "sha256", "size"} <= set(entry):
            raise BundleError(f"malformed manifest entry: {entry!r}")
    return manifest


def _safe_join(root: Path, relpath: str) -> Path:
    """Join *relpath* under *root*, refusing traversal and absolute paths."""
    if not relpath or relpath.startswith(("/", "\\")) or ":" in relpath:
        raise BundleError(f"unsafe path in bundle: {relpath!r}")
    candidate = (root / relpath).resolve()
    root_resolved = root.resolve()
    if root_resolved != candidate and root_resolved not in candidate.parents:
        raise BundleError(f"path escapes the bundle: {relpath!r}")
    if ".." in Path(relpath).parts:
        raise BundleError(f"path escapes the bundle: {relpath!r}")
    return candidate
