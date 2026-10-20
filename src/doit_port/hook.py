"""The ``uptodate`` hook: the only place where doit's decisions are influenced.

``doit`` calls every entry of ``task.uptodate`` before it looks at file mtimes
or target existence. A callable there receives the ``Task`` object as its first
argument (when the parameter is named ``task``) and may return:

``True``
    the task is up to date -- doit will not run the actions;
``False``
    the task must run;
``None``
    "no opinion", doit falls back to its own checks.

This module returns ``True`` only after it has *materialised* every declared
target from the content store, because doit checks target existence right
after the uptodate hooks and would otherwise rebuild anyway.

Failure policy: this hook must never raise. An exception escaping into
``Dependency.get_status`` aborts the whole ``doit`` process (verified against
doit 0.37.0), so every path is wrapped and any surprise degrades to
"return False" -- a rebuild, never a silent lie.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .context import PortContext
from .errors import PortError, ReceiptCorrupt
from .fingerprint import (
    SCHEMA_FOR_KEY,
    actions_fingerprint,
    file_digest,
    outputs_key as compute_outputs_key,
    compute_input_key,
)
from .receipt import OutputRecord, Receipt

_TRUE = True
_FALSE = False

#: How many historical digests to remember per artefact path.
_HISTORY_LIMIT = 16


def _saver_factory(hook: "PortHook", task_name: str):
    def _saver() -> dict[str, Any]:
        hook._on_success(task_name)
        return {}

    _saver._port_saver = True  # type: ignore[attr-defined]
    return _saver


class PortHook:
    """Callable installed in ``task['uptodate']`` by :func:`doit_port.port_task`."""

    def __init__(self, ctx: PortContext) -> None:
        self.ctx = ctx
        self._last_task = ""

    def __repr__(self) -> str:  # shows up in ``doit --get-log`` reasons
        return f"<port receipt for {self._last_task!r}>"

    # ------------------------------------------------------------------ entry

    def __call__(self, task) -> bool | None:
        self._last_task = getattr(task, "name", "")
        try:
            return self._evaluate(task)
        except Exception as exc:  # noqa: BLE001 - see module docstring
            self.ctx.note_error(f"{self._last_task}: hook error: {type(exc).__name__}: {exc}")
            return _FALSE

    # ----------------------------------------------------------------- logic

    def _evaluate(self, task) -> bool | None:
        ctx = self.ctx
        spec = ctx.spec_for(task.name)
        if spec is None:
            return None  # a plain doit task sharing the dodo file: stay out of it

        self._ensure_saver(task)

        def _rebuild(reason: str, detail: dict[str, Any] | None = None) -> bool:
            """Hand the task back to doit, recording why."""
            if detail is not None:
                ctx.record(task.name, reason, detail)
            ctx.stats.rebuilt += 1
            self.mark_started(task.name)
            return _FALSE

        # 1. inputs -----------------------------------------------------------
        inputs: dict[str, str] = {}
        missing_inputs: list[str] = []
        for raw in spec.inputs:
            abspath = ctx.config.abspath(raw)
            rel = ctx.config.relpath(abspath)
            if not abspath.is_file():
                missing_inputs.append(rel)
                continue
            try:
                inputs[rel] = file_digest(abspath)
            except OSError as exc:
                missing_inputs.append(f"{rel} ({exc.strerror or exc})")

        if missing_inputs:
            return _rebuild("missing-input", {"paths": sorted(missing_inputs)})

        # 2. upstream artefacts ----------------------------------------------
        dep_outputs: dict[str, str] = {}
        for dep in spec.deps:
            key = self._outputs_key_of(dep)
            if key is None:
                return _rebuild("missing-dep", {"dep": dep})
            dep_outputs[dep] = key

        # 3. declared targets + action identity -------------------------------
        targets = tuple(sorted(spec.targets))
        actions_sig = (
            actions_fingerprint(task.actions) + "|" + "|".join(targets) + "|" + spec.key_salt
        )
        self.remember_actions(task.name, actions_sig)
        input_key = compute_input_key(
            schema=SCHEMA_FOR_KEY,
            flavor=ctx.config.flavor,
            task_name=task.name,
            inputs=inputs,
            dep_outputs=dep_outputs,
            actions_sig=actions_sig,
            engine_sig=ctx.engine_sig,
        )

        # 4. receipt lookup ---------------------------------------------------
        try:
            receipt = Receipt.load(ctx.store, input_key)
        except ReceiptCorrupt as exc:
            ctx.note_error(f"{task.name}: ignoring corrupt receipt: {exc}")
            receipt = None

        if receipt is None:
            return _rebuild("no-receipt", {"input_key": input_key})

        # 5. can every artefact actually be restored? -------------------------
        unusable = receipt.verify_blobs(ctx.store)
        if unusable:
            ctx.stats.unsafe += 1
            return _rebuild(
                "unsafe-receipt", {"input_key": input_key, "unusable": unusable}
            )

        # 6. compare the disk with the receipt --------------------------------
        drifted, absent, may_reuse, superseded = self._diff_disk(receipt)

        if may_reuse:
            # The artefact holds bytes from a *different legal* build of this
            # task -- typically an older receipt being replayed after a revert.
            # Restoring is correct, but doit would then skip the dependent
            # tasks, and they might still be describing the newer state. So the
            # task is handed back to doit: it re-runs, produces the same bytes,
            # and the early cutoff stops the cascade one level later.
            ctx.stats.superseded += 1
            return _rebuild("superseded", {"paths": sorted(may_reuse)})

        if superseded:
            # The file is byte-identical to an earlier legal build of this very
            # task: this is a revert (git checkout, branch switch), not damage.
            # Re-materialising the current version is exactly what is wanted.
            ctx.stats.superseded += 1
            ctx.record(task.name, "superseded", {"paths": sorted(superseded)})

        if drifted:
            ctx.stats.drift += 1
            mode = ctx.config.on_drift
            if mode == "warn":
                print(
                    f"[doit-port] WARNING: {task.name}: artefact(s) changed outside the "
                    f"build: {', '.join(drifted)} -- rebuilding",
                    file=sys.stderr,
                )
            if mode in ("rebuild", "warn"):
                return _rebuild(f"drift/{mode}", {"input_key": input_key, "paths": drifted})
            ctx.record(task.name, f"drift/{mode}", {"input_key": input_key, "paths": drifted})

        if drifted or absent or superseded:
            if not self._restore(receipt):
                return _rebuild("restore-failed", {"input_key": input_key})
            ctx.record(
                task.name, "restored",
                {
                    "input_key": input_key,
                    "paths": sorted(absent),
                    "drift": sorted(drifted),
                    "superseded": sorted(superseded),
                },
            )
            ctx.stats.restored += 1
            self._adopt(task.name, receipt)
            return _TRUE

        ctx.record(task.name, "hit", {"input_key": input_key})
        ctx.stats.hit += 1
        self._adopt(task.name, receipt)
        return _TRUE

    def _adopt(self, task_name: str, receipt: Receipt) -> None:
        """Register a hit/restore as the task's current state.

        Two things happen here and both matter:

        * ``completed`` lets downstream tasks in this same run compute their
          dependency fingerprint without re-hashing the disk;
        * the index is rewritten so a task that was satisfied from an *older*
          receipt (a revert) becomes the current state. Without this the index
          keeps pointing at the newer receipt, downstream tasks keep seeing the
          newer artefact digest, and they re-run for a state that no longer
          exists.

        Failing to write the index is not fatal: the build is still correct.
        """
        self.ctx.completed[task_name] = receipt.outputs_key
        try:
            receipt.save_index(self.ctx.store, self._merge_history(receipt))
        except OSError as exc:  # pragma: no cover - read-only store
            self.ctx.note_error(f"{task_name}: cannot refresh index: {exc}")

    # ------------------------------------------------------------- internals

    def _ensure_saver(self, task) -> None:
        """Install the post-success callback exactly once per Task object."""
        savers = getattr(task, "value_savers", None)
        if savers is None:
            return
        for existing in savers:
            if getattr(existing, "_port_saver", False):
                return
        savers.append(_saver_factory(self, task.name))

    def _diff_disk(
        self, receipt: Receipt
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        """Classify each expected artefact against what is on disk.

        :return: ``(drifted, absent, may_reuse, superseded)``.

        * ``may_reuse`` -- the file holds bytes this task produced at *some*
          point (per the index history) but not the bytes this receipt
          describes. Usually an older state restored by a revert; the caller
          rebuilds rather than replays, so dependents are re-evaluated.
        * ``superseded`` -- the file already equals the *current* target and at
          least one sibling differs; the caller restores the rest and is done.
        * ``drifted`` -- bytes that never came from this task. Falls to the
          ``on_drift`` policy, because no claim about them is safe.
        """
        index = Receipt.load_index(self.ctx.store, receipt.task) or {}
        history = index.get("history") or {}
        drifted: list[str] = []
        absent: list[str] = []
        may_reuse: list[str] = []
        superseded: list[str] = []
        for out in receipt.outputs:
            path = self.ctx.config.abspath(out.path)
            if not path.is_file():
                absent.append(out.path)
                continue
            try:
                actual = file_digest(path)
            except OSError:
                absent.append(out.path)
                continue
            if actual == out.sha256:
                continue
            known = history.get(out.path) or []
            if actual in known:
                may_reuse.append(out.path)
            else:
                drifted.append(out.path)
        return drifted, absent, may_reuse, superseded

    def _restore(self, receipt: Receipt) -> bool:
        """Materialise every output, or roll back and report failure.

        All blobs are verified *before* the first byte hits the workspace, so a
        corrupt store cannot leave a half-restored build behind.
        """
        store = self.ctx.store
        payloads: list[tuple[OutputRecord, bytes]] = []
        for out in receipt.outputs:
            try:
                data = store.get_bytes(out.sha256)
            except PortError as exc:
                self.ctx.note_error(f"{receipt.task}: {exc}")
                return False
            if len(data) != out.size:
                self.ctx.note_error(
                    f"{receipt.task}: blob {out.sha256[:12]}… is {len(data)} bytes, "
                    f"receipt says {out.size}"
                )
                return False
            payloads.append((out, data))

        written: list[Path] = []
        try:
            for out, data in payloads:
                dest = self.ctx.config.abspath(out.path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.parent / f".{dest.name}.port-{uuid.uuid4().hex}.tmp"
                try:
                    with open(tmp, "wb") as fh:
                        fh.write(data)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, dest)
                finally:
                    tmp.unlink(missing_ok=True)
                written.append(dest)
        except OSError as exc:
            self.ctx.note_error(f"{receipt.task}: restore failed: {exc}")
            for path in written:
                path.unlink(missing_ok=True)
            return False
        return True

    def _outputs_key_of(self, dep_name: str) -> str | None:
        """Fingerprint of an upstream task's artefacts, disk first, index last."""
        cached = self.ctx.completed.get(dep_name)
        if cached:
            return cached

        spec = self.ctx.spec_for(dep_name)
        if spec is not None and spec.targets:
            records: list[dict[str, Any]] = []
            for raw in sorted(spec.targets):
                path = self.ctx.config.abspath(raw)
                rel = self.ctx.config.relpath(path)
                if not path.is_file():
                    records = []
                    break
                try:
                    records.append(
                        {"path": rel, "sha256": file_digest(path), "size": path.stat().st_size}
                    )
                except OSError:
                    records = []
                    break
            if records:
                return compute_outputs_key(records)

        index = Receipt.load_index(self.ctx.store, dep_name)
        if index and isinstance(index.get("outputs_key"), str):
            return index["outputs_key"]
        return None

    # ------------------------------------------------------------- on success

    def _on_success(self, task_name: str) -> None:
        """Called by doit after the actions returned successfully.

        This is where a receipt is born. It is deliberately *not* called on
        failure: a half-finished artefact must never get a portable receipt.
        """
        ctx = self.ctx
        spec = ctx.spec_for(task_name)
        if spec is None or not spec.targets:
            return

        started = getattr(self, "_started", {}).get(task_name)
        elapsed_ms = int((time.time() - started) * 1000) if started else 0

        records: list[OutputRecord] = []
        missing: list[str] = []
        for raw in sorted(spec.targets):
            path = ctx.config.abspath(raw)
            rel = ctx.config.relpath(path)
            if not path.is_file():
                missing.append(rel)
                continue
            try:
                digest = file_digest(path)
                size = path.stat().st_size
            except OSError as exc:
                ctx.note_error(f"{task_name}: cannot read artefact {rel}: {exc}")
                missing.append(rel)
                continue
            stored = ctx.store.put_file(path, max_bytes=ctx.config.max_blob_bytes)
            records.append(
                OutputRecord(path=rel, sha256=digest, size=size, stored=stored is not None)
            )

        if missing:
            # Declared targets that were never produced: refuse to write a
            # receipt. The task will simply be rebuilt next time, which is the
            # truthful outcome.
            ctx.note_error(
                f"{task_name}: actions succeeded but did not produce: {', '.join(missing)}; "
                "no receipt written"
            )
            return

        inputs: dict[str, str] = {}
        for raw in spec.inputs:
            path = ctx.config.abspath(raw)
            if path.is_file():
                try:
                    inputs[ctx.config.relpath(path)] = file_digest(path)
                except OSError:
                    pass

        dep_outputs: dict[str, str] = {}
        for dep in spec.deps:
            key = self._outputs_key_of(dep)
            if key:
                dep_outputs[dep] = key

        actions_sig = self._actions_sig_for(task_name)
        if actions_sig == "unknown":
            # ``doit --always`` skips the uptodate hooks entirely, so we never
            # saw this task's action signature. A receipt keyed on a guess
            # would never be found again: write nothing.
            ctx.note_error(
                f"{task_name}: no action signature captured (was --always used?); "
                "no receipt written"
            )
            return

        input_key = compute_input_key(
            schema=SCHEMA_FOR_KEY,
            flavor=ctx.config.flavor,
            task_name=task_name,
            inputs=inputs,
            dep_outputs=dep_outputs,
            actions_sig=actions_sig,
            engine_sig=ctx.engine_sig,
        )

        receipt = Receipt(
            input_key=input_key,
            task=task_name,
            outputs=records,
            inputs=dict(inputs),
            deps=dep_outputs,
            actions_sig=actions_sig,
            flavor=ctx.config.flavor,
            engine={
                "doit": _doit_version(),
                "doit_port": _port_version(),
                "python": _python_tag(),
            },
            elapsed_ms=elapsed_ms,
        )
        try:
            receipt.save(ctx.store)
            receipt.save_index(ctx.store, self._merge_history(receipt))
        except OSError as exc:
            ctx.note_error(f"{task_name}: cannot write receipt: {exc}")
            return
        ctx.completed[task_name] = receipt.outputs_key
        ctx.record(task_name, "executed", {"input_key": input_key})

    def _merge_history(self, receipt: Receipt) -> dict[str, list[str]]:
        """Keep a short list of every digest this task has legitimately produced.

        Used by :meth:`_diff_disk` to tell a revert apart from an edit.
        """
        index = Receipt.load_index(self.ctx.store, receipt.task) or {}
        history = {k: list(v) for k, v in (index.get("history") or {}).items()}
        for out in receipt.outputs:
            bucket = history.setdefault(out.path, [])
            if out.sha256 not in bucket:
                bucket.append(out.sha256)
            if len(bucket) > _HISTORY_LIMIT:
                del bucket[:-_HISTORY_LIMIT]
        return history

    # actions signature captured at evaluation time so the receipt matches the
    # exact key we looked up (doit may hand us a different Task object later).
    def _actions_sig_for(self, task_name: str) -> str:
        sig = getattr(self, "_action_sigs", {}).get(task_name)
        return sig or "unknown"

    def remember_actions(self, task_name: str, sig: str) -> None:
        if not hasattr(self, "_action_sigs"):
            self._action_sigs: dict[str, str] = {}
        self._action_sigs[task_name] = sig

    def mark_started(self, task_name: str) -> None:
        if not hasattr(self, "_started"):
            self._started: dict[str, float] = {}
        self._started[task_name] = time.time()


def _doit_version() -> str:
    try:
        from doit import __version__

        return __version__
    except Exception:  # pragma: no cover
        return "unknown"


def _port_version() -> str:
    from . import __version__

    return __version__


def _python_tag() -> str:
    import platform

    return platform.python_version()
