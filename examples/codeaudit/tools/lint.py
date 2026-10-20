#!/usr/bin/env python3
"""lint.py -- a small, deterministic AST checker.

Deliberately dull: no formatting, no line/column numbers, no file paths. It
emits a list of findings in a fixed order so that two runs on the same source
produce byte-identical output. Determinism is what makes the example's
early-cutoff behaviour observable: if you add a function, the report changes;
if you reformat, it does not.

Finding format::

    <rule>:<kind>:<name>

``kind`` is ``module`` for module-level facts, ``function:<name>`` for facts
about a function, and ``class:<name>`` for classes.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

# Calls that deserve a second look. Keys are the *displayed* name, values are
# the attribute/dotted names we match on.
RISKY_CALLS = {
    "subprocess.call": {"subprocess.call", "call"},
    "subprocess.run": {"subprocess.run", "run"},
    "pickle.loads": {"pickle.loads", "loads"},
    "os.system": {"os.system", "system"},
    "eval": {"eval"},
    "exec": {"exec"},
    "yaml.load": {"yaml.load"},
}

BANNED_IMPORTS = {"telnetlib", "pickle", "marshal", "shelve"}
STDLIB_TOP_LEVEL = {
    "os", "sys", "json", "ast", "argparse", "pathlib", "subprocess", "pickle",
    "typing", "dataclasses", "collections", "itertools", "functools", "re",
    "hashlib", "logging", "datetime", "math", "shutil", "tempfile", "unittest",
    "telnetlib", "marshal", "shelve", "asyncio", "csv", "io", "textwrap",
}


def _node_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _node_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _has_swallowed_exception(func: ast.AST) -> bool:
    for handler in ast.walk(func):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        body = handler.body
        if len(body) == 1 and isinstance(body[0], ast.Pass):
            return True
        if len(body) == 1 and isinstance(body[0], ast.Continue):
            return True
    return False


def _is_true_test(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def audit_source(text: str) -> list[str]:
    """Return the sorted finding list for one source file."""
    tree = ast.parse(text)
    findings: list[tuple[str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in BANNED_IMPORTS:
                    findings.append((f"import:{top}", "module"))
                elif top and top not in STDLIB_TOP_LEVEL:
                    findings.append((f"thirdparty:{top}", "module"))

        elif isinstance(node, ast.Call):
            dotted = _node_name(node.func)
            for label, matches in RISKY_CALLS.items():
                if dotted in matches or dotted.split(".")[-1] in matches:
                    if dotted in matches:
                        findings.append((f"risky-call:{label}", "module"))
                        break

        elif isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)):
                    if isinstance(comparator, ast.Constant) and comparator.value is None:
                        findings.append(("style:compare-to-none", "module"))
                    if _is_true_test(comparator) or _is_true_test(node.left):
                        findings.append(("style:compare-to-true", "module"))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = f"function:{node.name}"
            if _has_swallowed_exception(node):
                findings.append(("error-handling:swallowed-exception", kind))
            if any(
                isinstance(child, ast.Global) if isinstance(child, ast.Global)
                else False
                for child in ast.walk(node)
            ):
                findings.append(("style:global-statement", kind))
        elif isinstance(node, ast.ClassDef):
            findings.append(("api:public-class", f"class:{node.name}"))

    return sorted({f"{rule}:{where}" for rule, where in findings})


def audit_files(paths: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for raw in paths:
        path = Path(raw)
        try:
            text = path.read_text(encoding="utf-8")
            result[path.as_posix()] = audit_source(text)
        except SyntaxError as exc:
            result[path.as_posix()] = [f"parse-error:{exc.msg}:module"]
        except OSError as exc:
            result[path.as_posix()] = [f"io-error:{exc.strerror}:module"]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="deterministic AST audit")
    parser.add_argument("--files", nargs="*", default=[], help="files to audit")
    parser.add_argument("--out", default="-", help="output path ('-' for stdout)")
    parser.add_argument(
        "--warn", nargs="*", default=[],
        help="strings that should raise a warning (proves change detection)",
    )
    args = parser.parse_args(argv)

    report = {
        "files": audit_files(args.files),
        "warnings": sorted(args.warn),
    }
    payload = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.out == "-":
        sys.stdout.write(payload)
    else:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
