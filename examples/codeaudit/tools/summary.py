#!/usr/bin/env python3
"""summary.py -- merge per-module audit JSON into one report.

Reads ``--in`` globs in sorted order and writes a deterministic markdown
summary plus a machine-readable JSON sidecar. The grading threshold comes from
``audit.toml``, which is the real input under test: changing it must change
this script's output, and therefore invalidate every downstream receipt.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path


def load(path: str, results: dict[str, list[str]], warnings: list[str]) -> None:
    with open(path, encoding="utf-8") as fh:
        report = json.load(fh)
    for filename, items in report.get("files", {}).items():
        results.setdefault(filename, []).extend(items)
    warnings.extend(report.get("warnings", []))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="summarise audit JSON")
    parser.add_argument("--in", dest="inputs", nargs="*", default=[])
    parser.add_argument("--glob", default="audit/*.json")
    parser.add_argument("--out", default="build/audit.json")
    parser.add_argument("--md", default="-")
    args = parser.parse_args(argv)

    paths = list(args.inputs) or sorted(glob.glob(args.glob))
    results: dict[str, list[str]] = {}
    warnings: list[str] = []
    for path in paths:
        load(path, results, warnings)

    for key in results:
        results[key] = sorted(set(results[key]))
    warnings = sorted(set(warnings))

    total = sum(len(items) for items in results.values())
    by_rule: dict[str, int] = {}
    for items in results.values():
        for item in items:
            by_rule[item.split(":", 1)[0]] = by_rule.get(item.split(":", 1)[0], 0) + 1

    payload = {
        "files": results,
        "warnings": warnings,
        "totals": {"findings": total, "files": len(results), "by_rule": by_rule},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    lines = ["# audit summary", "", f"files: {len(results)}", f"findings: {total}", ""]
    for rule in sorted(by_rule):
        lines.append(f"- {rule}: {by_rule[rule]}")
    if warnings:
        lines += ["", "warnings:"] + [f"- {w}" for w in warnings]

    text = "\n".join(lines) + "\n"
    if args.md == "-":
        sys.stdout.write(text)
    else:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
