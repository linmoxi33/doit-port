#!/usr/bin/env python3
"""gate.py -- decide whether the audit passes, and fail loudly if it doesn't.

The gate exists so that the example has a *real* failure to recover from:
set ``threshold`` in ``audit.toml`` below the current finding count and the
task exits non-zero. doit-port must then leave no receipt behind, and the next
run must retry only this step.
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fail when the audit budget is exceeded")
    parser.add_argument("--audit", required=True, help="summary JSON")
    parser.add_argument("--threshold", type=int, required=True)
    args = parser.parse_args(argv)

    try:
        with open(args.audit, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"gate: cannot read {args.audit}: {exc}", file=sys.stderr)
        return 2

    findings = int(payload.get("totals", {}).get("findings", 0))
    if findings > args.threshold:
        print(
            f"gate: FAILED -- {findings} findings exceed the budget of {args.threshold}",
            flush=True,
        )
        return 1
    print(f"gate: OK -- {findings} findings within the budget of {args.threshold}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
