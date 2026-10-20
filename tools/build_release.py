#!/usr/bin/env python3
"""Reproducible wheel / sdist / source-zip builder.

``python -m build`` is not reproducible out of the box: setuptools stamps every
archive member with the source file's mtime, so building the same tree twice
gives two different hashes and nobody can tell a rebuild from a change. This
project's whole claim is that identical bytes mean identical content, so its own
artefacts had better mean the same.

Fix: build normally into a scratch directory, then rewrite every archive member
with a fixed timestamp (1980-01-01, the ZIP epoch) and an explicit ordering,
preserving compression and payload bytes exactly.

Usage::

    python tools/build_release.py            # all three artefacts into dist/
    python tools/build_release.py --check    # build twice, compare hashes
    python tools/build_release.py --no-sdist # wheel + zip only
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
FIXED_TAR_MTIME = 315532800  # 1980-01-01T00:00:00Z


def version() -> str:
    import re

    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("cannot read version from pyproject.toml")
    return match.group(1)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(262144), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalise_zip(path: Path, out: Path) -> None:
    """Rewrite a ZIP with fixed timestamps and sorted entries."""
    with zipfile.ZipFile(path) as src:
        names = sorted(src.namelist())
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as dst:
            for name in names:
                info = src.getinfo(name)
                data = src.read(name)
                fixed = zipfile.ZipInfo(name, date_time=FIXED_ZIP_DATE)
                fixed.compress_type = info.compress_type
                fixed.external_attr = info.external_attr
                fixed.create_system = 3          # unix, so the mode below sticks
                fixed.external_attr = (0o644 & 0xFFFF) << 16
                dst.writestr(fixed, data)


def _normalise_tar(path: Path, out: Path) -> None:
    """Rewrite an sdist tarball with fixed mtimes, uid/gid and ordering.

    The gzip wrapper needs handling too: ``tarfile.open(..., "w:gz")`` writes the
    current time into the gzip header, so two builds of the same tree still
    differed by four bytes even after every tar header was fixed. ``mtime=0``
    plus a fixed filename field removes the last source of variation.
    """
    with tarfile.open(path, "r:gz") as src:
        members = sorted(src.getmembers(), key=lambda m: m.name)
        with open(out, "wb") as raw, gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9
        ) as gz, tarfile.open(fileobj=gz, mode="w") as dst:
            for original in members:
                # Rebuild every header from scratch. Mutating the TarInfo that
                # came out of the source archive leaves fields we did not think
                # about (and tarfile's own addfile() can overwrite mtime for
                # directories), which made the sdist differ between two builds
                # of the same tree.
                member = tarfile.TarInfo(original.name)
                member.type = original.type
                member.mode = 0o755 if original.isdir() else 0o644
                member.mtime = FIXED_TAR_MTIME
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                if original.isfile():
                    # read into memory: the sdist is small, and this makes the
                    # archive independent of the source file's own mtime
                    handle = src.extractfile(original)
                    assert handle is not None
                    member.size = len(handle.read())
                    handle.seek(0)
                    dst.addfile(member, handle)
                else:
                    dst.addfile(member)


def build_once(target_dir: Path) -> dict[str, Path]:
    """Run ``python -m build`` into *target_dir*, return the artefacts."""
    target_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(target_dir)],
        cwd=ROOT, check=True, capture_output=True,
    )
    tag = version()
    wheel = next(target_dir.glob(f"*{tag}*.whl"))
    sdist = next(target_dir.glob(f"*{tag}*.tar.gz"))

    fixed_dir = target_dir / "_fixed"
    fixed_dir.mkdir(exist_ok=True)
    wheel_out = fixed_dir / wheel.name
    sdist_out = fixed_dir / sdist.name
    _normalise_zip(wheel, wheel_out)
    _normalise_tar(sdist, sdist_out)
    return {"wheel": wheel_out, "sdist": sdist_out}


def build_zip(target_dir: Path) -> Path:
    """Build the source archive via tools/build_archive.py (already reproducible)."""
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "build_archive.py"), "--out",
         str(target_dir / f"doit-port-{version()}.zip")],
        cwd=ROOT, check=True, capture_output=True,
    )
    return target_dir / f"doit-port-{version()}.zip"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="build twice, compare hashes")
    parser.add_argument("--no-sdist", action="store_true")
    args = parser.parse_args()

    DIST.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        first = build_once(Path(tmp) / "a")
        artefacts = {"wheel": first["wheel"], "sdist": first["sdist"]}
        artefacts["zip"] = build_zip(Path(tmp) / "a")

        for name, path in artefacts.items():
            if name == "sdist" and args.no_sdist:
                continue
            shutil.copyfile(path, DIST / path.name)
            print(f"{sha256(DIST / path.name)}  {DIST.joinpath(path.name).stat().st_size:>8}  "
                  f"{path.name}")

        if not args.check:
            return 0

        second = build_once(Path(tmp) / "b")
        second["zip"] = build_zip(Path(tmp) / "b")
        bad = []
        for name, path in artefacts.items():
            if name == "sdist" and args.no_sdist:
                continue
            if sha256(path) != sha256(second[name]):
                bad.append(path.name)
        if bad:
            print(f"NOT REPRODUCIBLE: {', '.join(bad)}")
            return 1
        print("reproducible: a second build produced identical bytes for every artefact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
