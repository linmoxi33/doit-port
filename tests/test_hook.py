"""The uptodate hook under adversarial conditions.

Every case here corresponds to a way the hook could lie (claim a hit it cannot
back) or crash doit. Both are treated as bugs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from doit_port.api import port_task
from doit_port.context import PortContext, PortSpec
from doit_port.hook import PortHook
from doit_port.receipt import OutputRecord, Receipt
from doit_port.store import ObjectStore


class FakeTask:
    """Minimal stand-in for doit's Task: only the attributes the hook touches."""

    def __init__(self, name: str, actions: list[Any] | None = None) -> None:
        self.name = name
        self.actions = actions or [lambda: None]
        self.value_savers: list[Any] = []


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    """A project dir, a configured context and a hook wired to it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "in.txt").write_text("source\n", encoding="utf-8")
    PortContext.reset()
    from doit_port.config import load_config

    ctx = PortContext.current(load_config(tmp_path))
    hook = PortHook(ctx)
    yield tmp_path, ctx, hook
    PortContext.reset()


def _register(ctx: PortContext, name: str = "step", **kw) -> PortSpec:
    spec = PortSpec(
        name=name,
        targets=tuple(kw.get("targets", ("out.txt",))),
        inputs=tuple(kw.get("inputs", ("src/in.txt",))),
        deps=tuple(kw.get("deps", ())),
        key_salt=kw.get("key_salt", ""),
    )
    ctx.register(spec)
    return spec


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------- basic verdicts


def test_unknown_task_returns_none(env) -> None:
    _, _, hook = env
    assert hook(FakeTask("not-ported")) is None


def test_missing_input_forces_a_rebuild(env) -> None:
    root, ctx, hook = env
    _register(ctx)
    (root / "src" / "in.txt").unlink()
    assert hook(FakeTask("step")) is False
    assert ctx.decisions["step"]["decision"] == "missing-input"


def test_first_call_without_receipt_returns_false(env) -> None:
    root, ctx, hook = env
    _register(ctx)
    assert hook(FakeTask("step")) is False
    assert ctx.decisions["step"]["decision"] == "no-receipt"


def test_missing_upstream_returns_false(env) -> None:
    root, ctx, hook = env
    _register(ctx, name="down", deps=("up",))
    assert hook(FakeTask("down")) is False
    assert ctx.decisions["down"]["decision"] == "missing-dep"


def test_second_call_hits_when_nothing_changed(env) -> None:
    root, ctx, hook = env
    _register(ctx)
    task = FakeTask("step")
    assert hook(task) is False
    _write(root / "out.txt", "produced\n")
    hook._on_success("step")

    ctx2 = PortContext.current(
        __import__("doit_port.config", fromlist=["load_config"]).load_config(root)
    )
    hook2 = PortHook(ctx2)
    assert hook2(FakeTask("step")) is True
    assert ctx2.stats.hit == 1


# ------------------------------------------------------------------- failures


def test_hook_never_raises(env, monkeypatch) -> None:
    """A broken action fingerprint must degrade to a rebuild, not crash doit."""
    root, ctx, hook = env
    _register(ctx)

    def boom(_action):
        raise RuntimeError("fingerprinting exploded")

    monkeypatch.setattr("doit_port.hook.actions_fingerprint", boom)
    assert hook(FakeTask("step")) is False
    assert any("hook error" in e for e in ctx.stats.errors)


def test_corrupt_receipt_is_ignored_not_fatal(env) -> None:
    root, ctx, hook = env
    _register(ctx)
    task = FakeTask("step")
    assert hook(task) is False
    _write(root / "out.txt", "x")
    hook._on_success("step")

    receipt_dir = root / ".doit-port" / "receipts"
    for path in receipt_dir.glob("*.json"):
        path.write_text("{broken", encoding="utf-8")

    ctx2 = PortContext.current(
        __import__("doit_port.config", fromlist=["load_config"]).load_config(root)
    )
    hook2 = PortHook(ctx2)
    assert hook2(FakeTask("step")) is False
    assert ctx2.decisions["step"]["decision"] == "no-receipt"


def test_restore_failure_returns_false(env) -> None:
    root, ctx, hook = env
    _register(ctx)
    task = FakeTask("step")
    hook(task)
    _write(root / "out.txt", "content")
    hook._on_success("step")

    (root / "out.txt").unlink()
    receipt = next(
        Receipt.load(ctx.store, p.stem) for p in (root / ".doit-port" / "receipts").glob("*.json")
    )
    assert receipt is not None
    for out in receipt.outputs:
        ctx.store.blob_path(out.sha256).unlink()

    ctx2 = PortContext.current(
        __import__("doit_port.config", fromlist=["load_config"]).load_config(root)
    )
    hook2 = PortHook(ctx2)
    assert hook2(FakeTask("step")) is False
    assert ctx2.decisions["step"]["decision"] == "unsafe-receipt"
    assert ctx2.stats.unsafe == 1


def test_no_receipt_when_a_declared_target_is_missing(env) -> None:
    """Actions that 'succeed' without producing the target must not be cached."""
    root, ctx, hook = env
    _register(ctx)
    hook(FakeTask("step"))
    hook._on_success("step")  # out.txt was never written
    assert list((root / ".doit-port" / "receipts").glob("*.json")) == []
    assert any("did not produce" in e for e in ctx.stats.errors)


def test_no_receipt_without_a_captured_action_signature(env) -> None:
    root, ctx, hook = env
    _register(ctx)
    _write(root / "out.txt", "made")
    hook._on_success("step")
    assert list((root / ".doit-port" / "receipts").glob("*.json")) == []


# --------------------------------------------------------------------- drift


def _prime(root: Path, ctx: PortContext, hook: PortHook, text: str = "v1") -> None:
    _register(ctx)
    hook(FakeTask("step"))
    _write(root / "out.txt", text)
    hook._on_success("step")


def test_drift_rebuild_is_the_default(env) -> None:
    root, ctx, hook = env
    _prime(root, ctx, hook)
    _write(root / "out.txt", "edited by hand")

    ctx2 = PortContext.current(
        __import__("doit_port.config", fromlist=["load_config"]).load_config(root)
    )
    hook2 = PortHook(ctx2)
    assert hook2(FakeTask("step")) is False
    assert ctx2.decisions["step"]["decision"] == "drift/rebuild"
    assert (root / "out.txt").read_text(encoding="utf-8") == "edited by hand"


def test_drift_restore_overwrites(env) -> None:
    root, ctx, hook = env
    _prime(root, ctx, hook)
    _write(root / "out.txt", "edited by hand")

    ctx2 = PortContext.current(
        __import__("doit_port.config", fromlist=["load_config"]).load_config(
            root, {"on_drift": "restore"}
        )
    )
    hook2 = PortHook(ctx2)
    assert hook2(FakeTask("step")) is True
    assert (root / "out.txt").read_text(encoding="utf-8") == "v1"


def test_drift_warn_rebuilds_and_prints(env, capsys) -> None:
    root, ctx, hook = env
    _prime(root, ctx, hook)
    _write(root / "out.txt", "edited")

    ctx2 = PortContext.current(
        __import__("doit_port.config", fromlist=["load_config"]).load_config(
            root, {"on_drift": "warn"}
        )
    )
    hook2 = PortHook(ctx2)
    assert hook2(FakeTask("step")) is False
    assert "changed outside the build" in capsys.readouterr().err


def test_deleted_target_is_restored_and_reported_as_hit(env) -> None:
    root, ctx, hook = env
    _prime(root, ctx, hook)
    (root / "out.txt").unlink()

    ctx2 = PortContext.current(
        __import__("doit_port.config", fromlist=["load_config"]).load_config(root)
    )
    hook2 = PortHook(ctx2)
    assert hook2(FakeTask("step")) is True
    assert (root / "out.txt").read_text(encoding="utf-8") == "v1"
    assert ctx2.stats.restored == 1


def test_previous_legal_build_is_treated_as_a_revert(env) -> None:
    """An older *legal* digest is superseded, not treated as tampering."""
    root, ctx, hook = env
    _prime(root, ctx, hook, text="v1")
    _write(root / "src" / "in.txt", "changed\n")
    ctx.register(PortSpec(name="step", targets=("out.txt",), inputs=("src/in.txt",)))
    hook(FakeTask("step"))
    _write(root / "out.txt", "v2")
    hook._on_success("step")

    _write(root / "out.txt", "v1")  # simulate git checkout of an old artefact
    _write(root / "src" / "in.txt", "source\n")

    ctx2 = PortContext.current(
        __import__("doit_port.config", fromlist=["load_config"]).load_config(root)
    )
    hook2 = PortHook(ctx2)
    assert hook2(FakeTask("step")) is True
    # the input was reverted too, so the very first receipt is valid again:
    # this is a plain hit. What matters is that no drift was reported.
    assert ctx2.stats.drift == 0
    assert (root / "out.txt").read_text(encoding="utf-8") == "v1"


def test_reverted_artefact_with_changed_input_is_superseded(env) -> None:
    """git checkout of an artefact only: inputs moved on, so restore, don't rebuild."""
    root, ctx, hook = env
    _prime(root, ctx, hook, text="v1")
    _write(root / "src" / "in.txt", "changed\n")
    ctx.register(PortSpec(name="step", targets=("out.txt",), inputs=("src/in.txt",)))
    hook(FakeTask("step"))
    _write(root / "out.txt", "v2")
    hook._on_success("step")

    _write(root / "out.txt", "v1")  # revert the artefact, keep the new input

    ctx2 = PortContext.current(
        __import__("doit_port.config", fromlist=["load_config"]).load_config(root)
    )
    ctx2.register(PortSpec(name="step", targets=("out.txt",), inputs=("src/in.txt",)))
    # a stale-but-legal artefact is rebuilt rather than replayed, so that
    # dependent tasks are re-evaluated against the current bytes
    assert PortHook(ctx2)(FakeTask("step")) is False
    assert ctx2.stats.superseded == 1
    assert ctx2.stats.drift == 0, "a previous legal build is not tampering"


def test_a_hit_rewrites_the_index_to_the_current_state(env) -> None:
    """A hit on an *older* receipt must become the current state.

    Otherwise the index keeps advertising the newer artefact digest, dependent
    tasks compute their upstream fingerprint from a state that is no longer on
    disk, and they re-run for a build that no longer exists.
    """
    root, ctx, hook = env
    _prime(root, ctx, hook, text="v1")
    first_key = next(
        Receipt.load(ctx.store, p.stem) for p in (root / ".doit-port" / "receipts").glob("*.json")
    )
    assert first_key is not None

    _write(root / "src" / "in.txt", "changed\n")
    ctx.register(PortSpec(name="step", targets=("out.txt",), inputs=("src/in.txt",)))
    hook(FakeTask("step"))
    _write(root / "out.txt", "v2")
    hook._on_success("step")
    assert Receipt.load_index(ctx.store, "step")["input_key"] != first_key.input_key

    # revert both the input and the artefact
    _write(root / "src" / "in.txt", "source\n")
    _write(root / "out.txt", "v1")

    ctx2 = PortContext.current(
        __import__("doit_port.config", fromlist=["load_config"]).load_config(root)
    )
    ctx2.register(PortSpec(name="step", targets=("out.txt",), inputs=("src/in.txt",)))
    assert PortHook(ctx2)(FakeTask("step")) is True

    index = Receipt.load_index(ctx2.store, "step")
    assert index is not None
    assert index["input_key"] == first_key.input_key, (
        "after a hit the index must describe the state that is actually on disk"
    )


# --------------------------------------------------------------- restore safety


def test_restore_verifies_before_writing(env) -> None:
    """A corrupt second blob must not leave the first output replaced."""
    root, ctx, hook = env
    spec = PortSpec(name="multi", targets=("a.txt", "b.txt"), inputs=("src/in.txt",))
    ctx.register(spec)
    hook(FakeTask("multi"))
    _write(root / "a.txt", "AAA")
    _write(root / "b.txt", "BBB")
    hook._on_success("multi")

    (root / "a.txt").unlink()
    (root / "b.txt").unlink()
    receipt = next(
        Receipt.load(ctx.store, p.stem) for p in (root / ".doit-port" / "receipts").glob("*.json")
    )
    assert receipt is not None
    corrupted = receipt.outputs[1]
    ctx.store.blob_path(corrupted.sha256).write_bytes(b"tampered")

    ctx2 = PortContext.current(
        __import__("doit_port.config", fromlist=["load_config"]).load_config(root)
    )
    hook2 = PortHook(ctx2)
    assert hook2(FakeTask("multi")) is False
    assert not (root / "a.txt").exists(), "nothing may be written before verification"


def test_restore_creates_parent_directories(env) -> None:
    root, ctx, hook = env
    ctx.register(
        PortSpec(name="deep", targets=("a/b/c/out.txt",), inputs=("src/in.txt",))
    )
    hook(FakeTask("deep"))
    _write(root / "a" / "b" / "c" / "out.txt", "deep")
    hook._on_success("deep")
    (root / "a").unlink() if (root / "a").is_symlink() else None
    import shutil

    shutil.rmtree(root / "a")

    ctx2 = PortContext.current(
        __import__("doit_port.config", fromlist=["load_config"]).load_config(root)
    )
    assert PortHook(ctx2)(FakeTask("deep")) is True
    assert (root / "a" / "b" / "c" / "out.txt").read_text(encoding="utf-8") == "deep"


# ------------------------------------------------------------ multiple tasks


def test_two_tasks_share_one_hook_instance(env) -> None:
    root, ctx, hook = env
    _register(ctx, name="one", targets=("one.txt",))
    _register(ctx, name="two", targets=("two.txt",))
    hook(FakeTask("one"))
    _write(root / "one.txt", "1")
    hook._on_success("one")
    hook(FakeTask("two"))
    _write(root / "two.txt", "2")
    hook._on_success("two")
    assert ctx.completed["one"] != ctx.completed["two"]


def test_saver_is_installed_once_per_task(env) -> None:
    root, ctx, hook = env
    _register(ctx)
    task = FakeTask("step")
    hook(task)
    hook(task)
    hook(task)
    assert len(task.value_savers) == 1


def test_stats_accumulate(env) -> None:
    root, ctx, hook = env
    _prime(root, ctx, hook)
    (root / "out.txt").unlink()
    ctx2 = PortContext.current(
        __import__("doit_port.config", fromlist=["load_config"]).load_config(root)
    )
    PortHook(ctx2)(FakeTask("step"))
    payload = ctx2.stats.as_dict()
    assert payload["restored"] == 1
    assert set(payload) >= {"hit", "restored", "rebuilt", "drift", "unsafe", "errors"}
