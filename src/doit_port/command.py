"""The ``doit port`` sub-command.

Registered through the documented ``doit.COMMAND`` entry-point group, so this
class is the *only* thing doit has to know about: no patching of
``DoitMain.DOIT_CMDS`` and no vendored CLI.

Every sub-command reads the content store, never ``dodo.py``. That is a
deliberate choice: if a pipeline is misconfigured or a dodo file is broken, the
cache diagnosis tools must still work.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from doit.cmd_base import Command

from .bundle import export_bundle, import_bundle, verify_bundle
from .config import load_config
from .errors import BundleError, PortError
from .receipt import Receipt, load_all, referenced_blobs
from .store import ObjectStore

opt_json = {
    "name": "json",
    "short": "",
    "long": "json",
    "type": bool,
    "default": False,
    "help": "machine-readable output",
}

opt_force = {
    "name": "force",
    "short": "f",
    "long": "force",
    "type": bool,
    "default": False,
    "help": "overwrite the destination directory",
}

opt_dry_run = {
    "name": "dry_run",
    "short": "n",
    "long": "dry-run",
    "type": bool,
    "default": False,
    "help": "report what would happen without writing anything",
}

opt_no_objects = {
    "name": "no_objects",
    "short": "",
    "long": "no-objects",
    "type": bool,
    "default": False,
    "help": "export receipts only (receiver must already hold the blobs)",
}

HELP = """sub-commands:
  status              show what the content store holds
  verify              re-hash every artefact referenced by a receipt
  gc                  delete artefacts no receipt refers to
  export <dir>        write a portable bundle
  import <dir>        verify and merge a bundle into this store
  forget <task>       drop every receipt belonging to <task>

The store location comes from doit-port.toml, [tool.doit_port] in
pyproject.toml, or DOIT_CONFIG['doit_port'] in dodo.py (in that order, later
wins)."""


class PortCommand(Command):
    """inspect and move the doit-port content store"""

    name = "port"
    doc_purpose = "inspect and move the portable build-receipt store"
    doc_usage = "<sub-command> [args]"
    doc_description = HELP
    cmd_options = (opt_json, opt_force, opt_dry_run, opt_no_objects)
    execute_tasks = False

    def execute(self, opt_values: dict[str, Any], pos_args: list[str]) -> int:
        if not pos_args:
            print(HELP, file=sys.stderr)
            return 2

        sub, rest = pos_args[0], pos_args[1:]
        config = load_config(".")
        store = ObjectStore(config.store)

        handler = {
            "status": self._status,
            "verify": self._verify,
            "gc": self._gc,
            "export": self._export,
            "import": self._import,
            "forget": self._forget,
        }.get(sub)
        if handler is None:
            print(f"unknown sub-command {sub!r}\n\n{HELP}", file=sys.stderr)
            return 2

        try:
            return handler(store, config, opt_values, rest)
        except (PortError, BundleError) as exc:
            print(f"doit port: {exc}", file=sys.stderr)
            return 1

    # ------------------------------------------------------------- status

    def _status(self, store: ObjectStore, config, opt: dict, args: list[str]) -> int:
        rows: list[dict[str, Any]] = []
        if store.index_dir.is_dir():
            for index_file in sorted(store.index_dir.glob("*.json")):
                try:
                    index = json.loads(index_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(index, dict):
                    continue
                outputs = index.get("outputs", [])
                broken = []
                for out in outputs:
                    if not out.get("stored", True):
                        broken.append(f"{out['path']} (not stored)")
                    elif not store.verify(out["sha256"]):
                        broken.append(f"{out['path']} (missing/corrupt blob)")
                rows.append(
                    {
                        "task": index.get("task", index_file.name),
                        "input_key": index.get("input_key", "")[:12],
                        "outputs": len(outputs),
                        "bytes": sum(int(o.get("size", 0)) for o in outputs),
                        "state": "ok" if not broken else "BROKEN",
                        "problems": broken,
                    }
                )

        if opt.get("json"):
            print(json.dumps({"store": str(store.root), "tasks": rows}, ensure_ascii=False, indent=2))
            return 0

        print(f"store: {store.root}")
        print(f"config: {config.source}   flavor: {config.flavor}")
        if not rows:
            print("  (no receipts yet -- run `doit` first)")
            return 0
        width = max(4, max(len(r["task"]) for r in rows))
        print(f"  {'task'.ljust(width)}  {'key':12}  {'out':>3}  {'bytes':>9}  state")
        for row in rows:
            print(
                f"  {row['task'].ljust(width)}  {row['input_key']:12}  "
                f"{row['outputs']:>3}  {row['bytes']:>9}  {row['state']}"
            )
            for problem in row["problems"]:
                print(f"    ! {problem}")
        total = store.total_bytes()
        print(f"  objects on disk: {total} bytes")
        return 0

    # ------------------------------------------------------------- verify

    def _verify(self, store: ObjectStore, config, opt: dict, args: list[str]) -> int:
        if args:
            # verify a bundle on disk before importing it
            report = verify_bundle(args[0])
            if opt.get("json"):
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(f"bundle          : {report['path']}")
                print(f"files checked   : {report['files_checked']}")
                print(f"total bytes     : {report['total_bytes']}")
                print(f"root hash       : {'OK' if report['root_hash_ok'] else 'MISMATCH'}")
                for problem in report["problems"]:
                    print(f"  ! {problem}")
                print("OK" if report["ok"] else f"FAILED ({len(report['problems'])} problem(s))")
            return 0 if report["ok"] else 1

        receipts = load_all(store)
        problems: list[str] = []
        checked = 0
        for receipt in receipts:
            for out in receipt.outputs:
                if not out.stored:
                    problems.append(f"{receipt.task}: {out.path} was never stored")
                    continue
                checked += 1
                if not store.verify(out.sha256):
                    problems.append(f"{receipt.task}: {out.path} -> blob {out.sha256[:12]}… bad")

        payload = {
            "store": str(store.root),
            "receipts": len(receipts),
            "artefacts_checked": checked,
            "problems": problems,
            "ok": not problems,
        }
        if opt.get("json"):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"receipts        : {len(receipts)}")
            print(f"artefacts checked: {checked}")
            for problem in problems:
                print(f"  ! {problem}")
            print("OK" if not problems else f"FAILED ({len(problems)} problem(s))")
        return 0 if not problems else 1

    # ----------------------------------------------------------------- gc

    def _gc(self, store: ObjectStore, config, opt: dict, args: list[str]) -> int:
        receipts = load_all(store)
        keep = referenced_blobs(receipts)
        dry = bool(opt.get("dry_run"))
        result = store.gc(keep, dry_run=dry)
        if not dry:
            store.prune_empty_dirs()

        payload = {"store": str(store.root), "dry_run": dry, **result,
                   "bytes": store.total_bytes()}
        if opt.get("json"):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            verb = "would remove" if dry else "removed"
            print(f"{verb} {result['removed']} object(s), "
                  f"freeing {result['freed_bytes']} bytes; kept {result['kept']}")
            print(f"store now holds {payload['bytes']} bytes")
        return 0

    # ------------------------------------------------------------- export

    def _export(self, store: ObjectStore, config, opt: dict, args: list[str]) -> int:
        if not args:
            print("usage: doit port export <directory>", file=sys.stderr)
            return 2
        report = export_bundle(
            store,
            args[0],
            include_objects=not opt.get("no_objects"),
            flavor=config.flavor,
            overwrite=bool(opt.get("force")),
        )
        if opt.get("json"):
            print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"exported {report.receipts} receipt(s), {report.objects} object(s), "
                  f"{report.total_bytes} bytes -> {report.path}")
            for skipped in report.skipped:
                print(f"  skipped {skipped}")
        return 0

    # ------------------------------------------------------------- import

    def _import(self, store: ObjectStore, config, opt: dict, args: list[str]) -> int:
        if not args:
            print("usage: doit port import <directory>", file=sys.stderr)
            return 2
        result = import_bundle(store, args[0], dry_run=bool(opt.get("dry_run")))
        if opt.get("json"):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            prefix = "would import" if result["dry_run"] else "imported"
            print(f"{prefix} {result['objects']} object(s), {result['receipts']} receipt(s), "
                  f"{result['bytes']} bytes")
            if result["receipts_already_present"]:
                print(f"  {result['receipts_already_present']} receipt(s) already present")
        return 0

    # ------------------------------------------------------------- forget

    def _forget(self, store: ObjectStore, config, opt: dict, args: list[str]) -> int:
        if not args:
            print("usage: doit port forget <task>", file=sys.stderr)
            return 2
        target = args[0]
        removed = 0
        for receipt in load_all(store):
            if receipt.task == target:
                store.receipt_path(receipt.input_key).unlink(missing_ok=True)
                removed += 1
        index_file = store.index_path(target)
        if index_file.is_file():
            index_file.unlink(missing_ok=True)
        print(f"forgot {removed} receipt(s) for {target!r} (artefacts kept; run `doit port gc`)")
        return 0
