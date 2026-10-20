"""Small helpers shared by the tests.

Kept in its own module (and imported explicitly) rather than in ``conftest``:
conftest is imported by pytest implicitly and is not importable as a module from
tests, which made the helpers awkward to reuse.
"""

from __future__ import annotations

from pathlib import Path


def drop_doit_state(project: Path) -> list[str]:
    """Remove everything doit uses to remember the previous run.

    ``doit`` writes a DBM file, and *which* files appear depends on the dbm
    backend of that interpreter: ``.doit.db`` on Windows, ``.doit.db.db`` or
    ``.doit.db.dat/.dir`` with ndbm/dumbdbm elsewhere, or nothing at all when
    the run never had to persist state. Assuming ``.doit.db`` exists made four
    tests fail on CI for a reason that had nothing to do with the code under
    test -- the very failure mode this project is supposed to remove.

    :return: the names that were actually removed (useful in assertions/logs).
    """
    removed: list[str] = []
    for path in sorted(project.glob(".doit.db*")):
        if path.is_file():
            path.unlink()
            removed.append(path.name)
    return removed


def build_dir(project: Path) -> Path:
    return project / "build"
