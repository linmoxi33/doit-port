"""Configuration precedence and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from doit_port.config import DEFAULTS, load_config
from doit_port.errors import ConfigError


def test_defaults_when_nothing_is_configured(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg.root == tmp_path.resolve()
    assert cfg.store == tmp_path.resolve() / ".doit-port"
    assert cfg.flavor == "default"
    assert cfg.on_drift == "rebuild"
    assert cfg.source == "defaults"


def test_standalone_toml_is_read(tmp_path: Path) -> None:
    (tmp_path / "doit-port.toml").write_text(
        '[doit_port]\nflavor = "nightly"\non_drift = "restore"\n', encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert cfg.flavor == "nightly"
    assert cfg.on_drift == "restore"
    assert "doit-port.toml" in cfg.source


def test_standalone_toml_without_section_header(tmp_path: Path) -> None:
    (tmp_path / "doit-port.toml").write_text('flavor = "flat"\n', encoding="utf-8")
    assert load_config(tmp_path).flavor == "flat"


def test_pyproject_section_is_read(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.doit_port]\nstore = "cache/port"\nflavor = "from-pyproject"\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.store == tmp_path.resolve() / "cache" / "port"
    assert cfg.flavor == "from-pyproject"


def test_standalone_overrides_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.doit_port]\nflavor = "pyproject"\n', encoding="utf-8"
    )
    (tmp_path / "doit-port.toml").write_text('[doit_port]\nflavor = "standalone"\n', encoding="utf-8")
    assert load_config(tmp_path).flavor == "standalone"


def test_doit_config_overrides_everything(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.doit_port]\nflavor = "pyproject"\n', encoding="utf-8"
    )
    (tmp_path / "doit-port.toml").write_text('[doit_port]\nflavor = "standalone"\n', encoding="utf-8")
    cfg = load_config(tmp_path, overrides={"flavor": "from-dodo"})
    assert cfg.flavor == "from-dodo"
    assert "DOIT_CONFIG" in cfg.source


def test_absolute_store_path_is_kept(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, overrides={"store": str(tmp_path / "elsewhere" / "s")})
    assert cfg.store == tmp_path / "elsewhere" / "s"


def test_ignore_accepts_string_or_list(tmp_path: Path) -> None:
    (tmp_path / "doit-port.toml").write_text('[doit_port]\nignore = "*.pyc"\n', encoding="utf-8")
    assert load_config(tmp_path).ignore == ("*.pyc",)
    (tmp_path / "doit-port.toml").write_text(
        '[doit_port]\nignore = ["*.pyc", ".DS_Store"]\n', encoding="utf-8"
    )
    assert load_config(tmp_path).ignore == ("*.pyc", ".DS_Store")


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "doit-port.toml").write_text('[doit_port]\nnonsense = 1\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown configuration key"):
        load_config(tmp_path)


def test_invalid_drift_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="on_drift"):
        load_config(tmp_path, overrides={"on_drift": "explode"})


def test_negative_blob_cap_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="max_blob_mb"):
        load_config(tmp_path, overrides={"max_blob_mb": -1})


def test_broken_toml_is_a_config_error(tmp_path: Path) -> None:
    (tmp_path / "doit-port.toml").write_text("this is not toml = = =", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot parse"):
        load_config(tmp_path)


def test_relpath_is_posix_and_relative(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    nested = tmp_path / "a" / "b" / "c.txt"
    assert cfg.relpath(nested) == "a/b/c.txt"
    assert "\\" not in cfg.relpath(nested)


def test_abspath_resolves_against_root_not_cwd(tmp_path: Path, monkeypatch) -> None:
    cfg = load_config(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    assert cfg.abspath("x/y.txt") == tmp_path / "x" / "y.txt"


def test_max_blob_bytes_conversion(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, overrides={"max_blob_mb": 3})
    assert cfg.max_blob_bytes == 3 * 1024 * 1024
    assert load_config(tmp_path, overrides={"max_blob_mb": 0}).max_blob_bytes == 0


def test_defaults_constant_matches_the_dataclass(tmp_path: Path) -> None:
    """Cheap guard against the two drift apart."""
    cfg = load_config(tmp_path)
    for key, value in DEFAULTS.items():
        if key == "store":
            continue
        assert getattr(cfg, key) == value or tuple(getattr(cfg, key)) == tuple(value)


def test_relpath_normalises_backslashes_on_every_platform(tmp_path: Path) -> None:
    r"""A Windows-style path must produce the same key on Linux.

    On POSIX a backslash is a legal filename character, so
    ``Path(r"build\out.txt").as_posix()`` keeps it. That gave the same project
    two different receipt keys -- ``build\out.txt`` on Linux, ``build/out.txt``
    on Windows -- so a cache built on one platform never hit on the other. Found
    by CI, not by the Windows-only local run.
    """
    cfg = load_config(tmp_path)
    assert cfg.relpath(cfg.abspath(r"build\out.txt")) == "build/out.txt"
    assert cfg.relpath(cfg.abspath(r"src\pkg\mod.py")) == "src/pkg/mod.py"
    # already-posix input is untouched
    assert cfg.relpath(cfg.abspath("a/b/c.txt")) == "a/b/c.txt"
