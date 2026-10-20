"""Per-process runtime state shared by every ported task.

One process running ``doit`` gets one :class:`PortContext`. It caches what has
already been resolved in this run (so downstream tasks can reuse an upstream
``outputs_key`` without touching the disk again) and keeps counters that both
the tests and ``doit port status`` read.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import PortConfig
from .fingerprint import engine_fingerprint
from .store import ObjectStore


@dataclass(frozen=True)
class PortSpec:
    """Declarative description of one ported task."""

    name: str
    targets: tuple[str, ...]
    inputs: tuple[str, ...] = ()
    deps: tuple[str, ...] = ()
    key_salt: str = ""

    def signature(self) -> str:
        return "|".join([*self.targets, "::", *self.inputs, "::", *self.deps, "::", self.key_salt])


@dataclass
class Stats:
    hit: int = 0          # receipt matched, artefacts already correct
    restored: int = 0     # receipt matched, artefacts re-materialised from store
    rebuilt: int = 0      # no usable receipt, doit ran the actions
    drift: int = 0        # artefacts on disk disagreed with the receipt
    superseded: int = 0   # artefacts were an older legal build of the same task
    unsafe: int = 0       # receipt matched but a blob was missing/corrupt
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hit": self.hit,
            "restored": self.restored,
            "rebuilt": self.rebuilt,
            "drift": self.drift,
            "superseded": self.superseded,
            "unsafe": self.unsafe,
            "errors": list(self.errors),
        }


class PortContext:
    """Mutable runtime state for the current ``doit`` invocation."""

    _lock = threading.Lock()
    _registry: dict[str, PortSpec] = {}
    _current: "PortContext | None" = None

    def __init__(self, config: PortConfig) -> None:
        self.config = config
        self.store = ObjectStore(config.store)
        self.engine_sig = engine_fingerprint()
        self.completed: dict[str, str] = {}
        self.stats = Stats()
        self.decisions: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def current(cls, config: PortConfig | None = None) -> "PortContext":
        """Return the context for this process, creating it on first use.

        ``doit`` may load ``dodo.py`` more than once in a single process (for
        example ``doit list`` followed by ``doit run`` inside one Python
        session, or a test harness that imports the module repeatedly). The
        context therefore keys off the resolved config root, not the caller.
        """
        with cls._lock:
            if config is not None:
                if cls._current is None or cls._current.config.root != config.root:
                    cls._current = PortContext(config)
                else:
                    cls._current.config = config
                return cls._current
            if cls._current is None:
                from .config import load_config

                cls._current = PortContext(load_config("."))
            return cls._current

    @classmethod
    def reset(cls) -> None:
        """Drop all process state. Used by tests between scenarios."""
        with cls._lock:
            cls._current = None
            cls._registry.clear()

    # -------------------------------------------------------------- registry

    def register(self, spec: PortSpec) -> None:
        self._registry[spec.name] = spec

    def unregister(self, name: str) -> None:
        """Drop a spec (used when a group re-files its sub-tasks)."""
        self._registry.pop(name, None)

    def spec_for(self, name: str) -> PortSpec | None:
        return self._registry.get(name)

    def specs(self) -> dict[str, PortSpec]:
        return dict(self._registry)

    # ------------------------------------------------------------- decisions

    def record(self, task_name: str, decision: str, detail: dict[str, Any]) -> None:
        self.decisions[task_name] = {"decision": decision, **detail}

    def note_error(self, message: str) -> None:
        self.stats.errors.append(message)
        log = self.config.store / "errors.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(message.replace("\n", " ") + "\n")
        except OSError:  # pragma: no cover - logging must never break a build
            pass

    def rel(self, path: str | Path) -> str:
        return self.config.relpath(path)
