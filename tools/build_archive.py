#!/usr/bin/env python3
"""Build a reproducible source ZIP for distribution.

Why not ``git archive`` / ``zip -r``:
the archive must not depend on the machine that built it. Timestamps, file
order, line endings and ``__pycache__`` all leak environment details into the
bytes, so two people building "the same" ZIP get different hashes and nobody
can tell whether the difference matters.

What this does instead:

* walks an explicit include list (never the working tree as-is);
* skips generated state (``.doit-port``, ``build``, ``audit``, ``dist``,
  ``__pycache__``, ``*.pyc``, ``.doit.db``);
* rewrites every text file to ``\\n`` line endings;
* fixes the mtime of every entry to a constant;
* sorts entries by name.

Usage::

    python tools/build_archive.py                 # write dist/doit-port-<v>.zip
    python tools/build_archive.py --check         # write twice, compare hashes
    python tools/build_archive.py --list          # show what would be included
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

INCLUDE_DIRS = ("src", "tests", "examples", "docs", "tools", ".github")
INCLUDE_FILES = (
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "CHANGELOG.md",
    ".gitattributes",
)
SKIP_DIR_NAMES = {
    "__pycache__", ".doit-port", "build", "audit", "dist", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", ".git", ".venv",
}
SKIP_FILE_SUFFIXES = {".pyc", ".pyo", ".db", ".zip", ".whl", ".egg-info"}
SKIP_FILE_NAMES = {".doit.db", ".DS_Store", "Thumbs.db"}

TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".cfg", ".txt", ".rst", ".json", ".yml", ".yaml",
    ".ini", ".gitattributes", ".sh", ".bat", ".ps1",
}

#: Never line-ending-normalised, whatever their suffix.
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".whl", ".gz", ".db"}

#: Fixed timestamp for every entry: 1980-01-01 00:00, the ZIP epoch.
FIXED_DATE = (1980, 1, 1, 0, 0, 0)


def version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("cannot read version from pyproject.toml")
    return match.group(1)


def _is_text(path: Path) -> bool:
    if path.suffix in BINARY_SUFFIXES:
        return False
    if path.suffix in TEXT_SUFFIXES or path.name in TEXT_SUFFIXES:
        return True
    return path.suffix == ""


def collect() -> list[Path]:
    """Every file that belongs in the archive, sorted, with nothing generated."""
    files: list[Path] = []
    for name in INCLUDE_FILES:
        path = ROOT / name
        if path.is_file():
            files.append(path)
    for directory in INCLUDE_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            parts = path.relative_to(ROOT).parts
            if any(part in SKIP_DIR_NAMES for part in parts):
                continue
            if any(part.endswith(".egg-info") for part in parts):
                continue
            if path.name in SKIP_FILE_NAMES:
                continue
            if path.suffix in SKIP_FILE_SUFFIXES:
                continue
            files.append(path)
    return sorted(files, key=lambda p: p.relative_to(ROOT).as_posix())


def _payload(path: Path) -> bytes:
    data = path.read_bytes()
    if _is_text(path):
        # normalise CRLF and lone CR so the archive is identical on Windows
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data


def build(target: Path, prefix: str | None = None) -> Path:
    files = collect()
    version_tag = version()
    prefix = prefix or f"doit-port-{version_tag}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(f"{prefix}/{relative}", date_time=FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, _payload(path))
    return target


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(262144), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify(target: Path) -> int:
    """Re-open the archive and check every entry is intact and LF-normalised."""
    problems: list[str] = []
    with zipfile.ZipFile(target) as zf:
        bad = zf.testzip()
        if bad:
            problems.append(f"corrupt entry: {bad}")
        names = zf.namelist()
        if len(names) != len(set(names)):
            problems.append("duplicate entries")
        for name in names:
            if "__pycache__" in name or name.endswith(".pyc"):
                problems.append(f"generated file leaked into the archive: {name}")
            data = zf.read(name)
            if name.endswith((".py", ".md", ".toml")) and b"\r\n" in data:
                problems.append(f"CRLF in {name}")
    if problems:
        print("ARCHIVE VERIFY FAILED")
        for problem in problems:
            print(f"  ! {problem}")
        return 1
    print(f"archive ok: {len(names)} entries")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="output zip path")
    parser.add_argument("--check", action="store_true", help="build twice and compare")
    parser.add_argument("--list", action="store_true", help="list files and exit")
    args = parser.parse_args()

    if args.list:
        for path in collect():
            print(path.relative_to(ROOT).as_posix())
        return 0

    target = Path(args.out) if args.out else DIST / f"doit-port-{version()}.zip"
    build(target)
    digest = sha256(target)
    size = target.stat().st_size
    print(f"wrote {target} ({size} bytes)")
    print(f"sha256 {digest}")

    status = _verify(target)
    if status:
        return status

    if args.check:
        second = DIST / "_rebuild-check.zip"
        build(second)
        again = sha256(second)
        second.unlink()
        if again != digest:
            print(f"NOT REPRODUCIBLE: {digest} != {again}")
            return 1
        print("reproducible: a second build produced identical bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
