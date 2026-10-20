"""Configuration loading.

Sources are merged in increasing priority:

1. built-in defaults
2. ``doit-port.toml`` in the working directory
3. ``[tool.doit_port]`` in ``pyproject.toml``
4. ``DOIT_CONFIG['doit_port']`` inside ``dodo.py`` (highest)

``dodo.py`` wins because it is the file the user is already editing when they
wire tasks together, and it is the only source that can react to runtime
conditions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

try:  # Python >= 3.11
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        _toml = None  # type: ignore[assignment]

DRIFT_MODES = ("rebuild", "warn", "restore")

DEFAULTS: dict[str, Any] = {
    "store": ".doit-port",
    "flavor": "default",
    "on_drift": "rebuild",
    "ignore": (),
    "bundle_include_objects": True,
    "max_blob_mb": 256,
}


@dataclass(frozen=True)
class PortConfig:
    """Resolved configuration for one project directory."""

    store: Path
    flavor: str = "default"
    on_drift: str = "rebuild"
    ignore: tuple[str, ...] = ()
    bundle_include_objects: bool = True
    max_blob_mb: int = 256
    root: Path = field(default_factory=Path)
    _source: str = "defaults"

    def __post_init__(self) -> None:
        if self.on_drift not in DRIFT_MODES:
            raise ConfigError(
                f"on_drift must be one of {DRIFT_MODES}, got {self.on_drift!r}"
            )
        if self.max_blob_mb < 0:
            raise ConfigError("max_blob_mb must be >= 0 (0 disables blobs)")

    @property
    def max_blob_bytes(self) -> int:
        return self.max_blob_mb * 1024 * 1024

    @property
    def source(self) -> str:
        """Human readable description of where the config came from."""
        return self._source

    def abspath(self, value: str | os.PathLike[str]) -> Path:
        """Resolve *value* against the project root (never against cwd)."""
        p = Path(value)
        return p if p.is_absolute() else (self.root / p)

    def relpath(self, value: str | os.PathLike[str]) -> str:
        """Posix-style path relative to the project root.

        Receipts must never contain absolute paths or backslashes: a receipt
        produced on Windows has to be replayable on Linux and vice versa.
        """
        p = Path(value)
        # ``Path("build\out.txt").as_posix()`` keeps the backslash on POSIX,
        # where it is a legal filename character rather than a separator. That
        # produced receipts keyed on "build\out.txt" on Linux and
        # "build/out.txt" on Windows -- the same project, two different keys.
        # Normalising explicitly is the only way to get one key everywhere.
        raw = str(value).replace("\\", "/")
        p = Path(raw)
        try:
            rel = p.resolve().relative_to(self.root.resolve())
        except ValueError:
            # Outside the project: keep it as given so callers can detect the
            # escape via a leading ``..`` instead of silently absolutising it.
            return raw
        return rel.as_posix()


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    if _toml is None:  # pragma: no cover
        raise ConfigError(
            "Reading TOML on Python 3.10 requires the 'tomli' package: pip install tomli"
        )
    try:
        with path.open("rb") as fh:
            return _toml.load(fh)
    except Exception as exc:  # noqa: BLE001 - surface any parse problem uniformly
        raise ConfigError(f"cannot parse {path}: {exc}") from exc


def _coerce(key: str, value: Any) -> Any:
    if key == "ignore":
        if isinstance(value, str):
            return (value,)
        return tuple(str(v) for v in value)
    if key in ("bundle_include_objects",):
        return bool(value)
    if key == "max_blob_mb":
        return int(value)
    if key in ("store", "flavor", "on_drift"):
        return str(value)
    raise ConfigError(f"unknown configuration key: {key!r}")


def load_config(
    root: str | os.PathLike[str] = ".",
    overrides: Mapping[str, Any] | None = None,
) -> PortConfig:
    """Load configuration for *root*.

    :param root: project directory that holds ``dodo.py``.
    :param overrides: highest-priority values, usually
        ``DOIT_CONFIG['doit_port']`` from ``dodo.py``.
    """
    root_path = Path(root).resolve()
    merged: dict[str, Any] = {}
    sources: list[str] = []

    pyproject = _read_toml(root_path / "pyproject.toml")
    tool_section = pyproject.get("tool", {}).get("doit_port")
    if isinstance(tool_section, dict) and tool_section:
        merged.update(tool_section)
        sources.append("pyproject.toml")

    standalone = _read_toml(root_path / "doit-port.toml")
    standalone_section = standalone.get("doit_port", standalone)
    if isinstance(standalone_section, dict) and standalone_section:
        merged.update(standalone_section)
        sources.append("doit-port.toml")

    if overrides:
        merged.update(dict(overrides))
        sources.append("DOIT_CONFIG")

    clean: dict[str, Any] = {}
    for key, value in merged.items():
        clean[key] = _coerce(key, value)

    unknown = set(clean) - set(DEFAULTS)
    if unknown:
        raise ConfigError(
            "unknown configuration key(s): " + ", ".join(sorted(unknown))
        )

    resolved = {**DEFAULTS, **clean}
    store = Path(resolved.pop("store"))
    return PortConfig(
        store=store if store.is_absolute() else root_path / store,
        root=root_path,
        _source="+".join(sources) if sources else "defaults",
        **{k: resolved[k] for k in DEFAULTS if k != "store"},
    )


def with_updates(config: PortConfig, **changes: Any) -> PortConfig:
    """Return a copy of *config* with *changes* applied (used by tests)."""
    return replace(config, **changes)
