"""User-facing assembly helpers.

``port_task`` is deliberately a *task factory*, not a new task type: it returns
a plain ``dict`` that ``doit`` already understands. There is no subclass of
``doit.task.Task``, no monkey-patch, and no new scheduler. The only thing added
to the dict is one entry in ``uptodate`` (a documented hook) and one key in
``meta`` (a documented plugin field).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .config import PortConfig, load_config
from .context import PortContext, PortSpec
from .errors import UnsupportedTask
from .hook import PortHook

#: Basenames of dodo functions that yield subtasks. The receiving generator is
#: itself loaded by doit as a group task with no actions and no artefacts, so a
#: receipt hook there is meaningless -- it would also steal the "lint" spec and
#: shadow the real "lint:<module>" tasks.
_GENERATOR_BASENAMES: set[str] = set()


def _is_generator_parent(name: str) -> bool:
    """True when *name* is a bare generator basename with no subtask suffix."""
    return name in _GENERATOR_BASENAMES

_ALLOWED_PASSTHROUGH = {
    "doc",
    "verbosity",
    "clean",
    "teardown",
    "params",
    "pos_arg",
    "getargs",
    "title",
    "io",
    "watch",
    "setup",
    "calc_dep",
}


def setup(root: str | os.PathLike[str] = ".", **overrides: Any) -> PortConfig:
    """Initialise doit-port for this dodo file and return the config.

    Call it once at the top of ``dodo.py`` when you need non-default settings::

        from doit_port import setup, port_task
        setup(flavor="my-pipeline-v3", on_drift="restore")

    Calling it is optional; without it the defaults plus ``doit-port.toml`` are
    used. ``root`` defaults to the current working directory, which is what
    ``doit`` uses when it loads ``dodo.py``.
    """
    config = load_config(root, overrides=overrides or None)
    PortContext.current(config)
    return config


def _hook(ctx: PortContext) -> PortHook:
    hook = getattr(ctx, "_hook", None)
    if hook is None:
        hook = PortHook(ctx)
        setattr(ctx, "_hook", hook)
    return hook


def _validate_paths(
    values: Sequence[str], config: PortConfig, *, what: str
) -> tuple[str, ...]:
    """Normalise to posix relative paths and reject anything we cannot replay."""
    out: list[str] = []
    for raw in values:
        text = str(raw)
        path = Path(text)
        if path.is_absolute() or (len(text) > 1 and text[1] == ":"):
            raise UnsupportedTask(
                f"{what} entry {text!r} is absolute. doit-port receipts must be replayable "
                "on another machine, so only paths inside the project are allowed."
            )
        normalised = config.relpath(config.abspath(path))
        if normalised.startswith("..") or Path(normalised).is_absolute():
            raise UnsupportedTask(
                f"{what} entry {text!r} escapes the project directory ({normalised!r})."
            )
        out.append(normalised)
    return tuple(out)


def port_task(
    *,
    name: str = "",
    actions: Sequence[Any],
    targets: Iterable[str],
    basename: str | None = None,
    inputs: Iterable[str] = (),
    task_dep: Iterable[str] = (),
    key_salt: str = "",
    uptodate: Iterable[Any] = (),
    config: PortConfig | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a ``doit`` task dict whose completion is backed by a portable receipt.

    :param name: task name as doit will see it (``lint:legacy.py`` style names
        are fine). Inside a ``yield`` generator doit prefixes the generator's
        basename, so a yielded task passes ``name`` and leaves ``basename`` out.
    :param actions: normal ``doit`` actions.
    :param targets: files the actions produce. Required: a task with no
        artefact has nothing to restore and is refused rather than faked.
    :param basename: use this instead of ``name`` when returning a *list* of
        dicts from one generator; doit then appends the name to the basename.
    :param inputs: source files the actions read. Replaces ``file_dep``.
    :param task_dep: upstream tasks. Their *artefact contents* -- not their
        exit status -- decide whether this task must re-run.
    :param key_salt: extra string mixed into the cache key. Bump it when the
        pipeline semantics change in a way the bytecode cannot see (for example
        a value read from the environment).
    :param uptodate: extra user checks evaluated before the receipt hook. Any
        of them returning ``False`` forces a rebuild.
    :param config: pass an explicit config; by default the process context is used.
    :param extra: any other documented ``doit`` task attribute (``doc``,
        ``verbosity``, ``clean``, ``teardown``, ``params`` ...).

    ``file_dep`` is rejected on purpose: doit resolves it against its local
    ``.doit.db``, which is exactly the thing that does not travel.
    """
    if "file_dep" in extra:
        raise UnsupportedTask(
            "port_task() does not accept file_dep -- use inputs=... instead. "
            "doit's file_dep state lives in the machine-local .doit.db, which is "
            "precisely what doit-port exists to replace."
        )
    if basename is not None and name:
        raise UnsupportedTask(
            "port_task() takes either name= or basename=, not both "
            "(doit rejects a task dict carrying two identity fields)"
        )
    if not (name or basename):
        raise UnsupportedTask("port_task() needs name= or basename=")

    unknown = set(extra) - _ALLOWED_PASSTHROUGH
    if unknown:
        raise UnsupportedTask(
            f"unsupported task attribute(s): {', '.join(sorted(unknown))}"
        )

    ctx = PortContext.current(config)
    cfg = config or ctx.config

    normalised_targets = _validate_paths(list(targets), cfg, what="targets")
    if not normalised_targets:
        raise UnsupportedTask(
            f"task {name!r}: at least one target is required. Without an artefact there "
            "is nothing to restore, and claiming a cache hit would be a lie."
        )
    for target in normalised_targets:
        existing = cfg.abspath(target)
        if existing.is_dir():
            raise UnsupportedTask(
                f"task {name!r}: target {target!r} is a directory. Supporting directory "
                "targets would require recursive tree hashing; declare the files instead."
            )

    normalised_inputs = _validate_paths(list(inputs), cfg, what="inputs")
    deps = tuple(str(d) for d in task_dep)

    # The receipt is keyed on the identity doit will register: a yielded
    # sub-task ("lint:calc.py" under generator "lint") gets its own key, while a
    # basename-style dict still resolves to "<basename><name>".
    # A dict identified by basename gets its name appended by doit, and may be
    # registered later than the loader that consumes it, so the spec is filed
    # under the basename -- the identity doit actually shows and depends on.
    identity = basename if basename else name
    spec = PortSpec(
        name=identity,
        targets=normalised_targets,
        inputs=normalised_inputs,
        deps=deps,
        key_salt=key_salt,
    )
    ctx.register(spec)

    task: dict[str, Any] = {
        "actions": list(actions),
        "targets": list(normalised_targets),
        "task_dep": list(deps),
        # user checks first: any of them returning False forces a rebuild even
        # if a receipt would have matched.
        "uptodate": list(uptodate) + [_hook(ctx)],
        "meta": {
            "port": {
                "targets": list(normalised_targets),
                "inputs": list(normalised_inputs),
                "deps": list(deps),
                "key_salt": key_salt,
            }
        },
    }
    if basename:
        task["basename"] = basename
    elif _is_generator_parent(name):
        # Returning a dict from task_<parent>() means "the parent itself"; doit
        # rejects an explicit `name` there because only yielded subtasks carry
        # one. `name` stays the receipt identity.
        return task
    else:
        task["name"] = name
    task.update(extra)
    return task


def port_group(
    basename: str,
    subtasks: Iterable[dict[str, Any]],
    *,
    key_salt: str = "",
    doc: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Turn already-built ported tasks into a ``doit`` subtask group.

    ``doit`` groups subtasks when a ``task_<name>()`` function *yields* them.
    When the list comes from somewhere else (a config file, a glob), this
    helper yields the same structure without the usual pitfalls:

    * it yields the subtasks only -- doit supplies the group; yielding a group
      dict as well would define the group twice;
    * every spec is re-filed under the identity doit will display
      (``lint:<name>``), which is what the hook looks up at run time;
    * ``name`` on each subtask becomes relative to the basename.

    Example::

        def task_lint():
            return port_group(
                "lint",
                [port_task(name=m, actions=[...], targets=[...]) for m in MODULES],
            )
    """
    ctx = PortContext.current()
    count = 0
    for child in subtasks:
        relative = str(child.get("name", ""))
        if not relative:
            raise UnsupportedTask(
                "port_group() expects sub-tasks built with port_task(name=...)"
            )
        if relative.startswith(f"{basename}:"):
            relative = relative[len(basename) + 1:]
        spec = ctx.spec_for(relative)
        if spec is None:
            raise UnsupportedTask(
                f"port_group({basename!r}): {relative!r} was not created by port_task()"
            )
        ctx.unregister(spec.name)
        ctx.register(
            PortSpec(
                name=f"{basename}:{relative}",
                targets=spec.targets,
                inputs=spec.inputs,
                deps=spec.deps,
                key_salt=spec.key_salt or key_salt,
            )
        )
        entry = dict(child)
        entry["name"] = relative
        count += 1
        yield entry

    if count == 0:
        raise UnsupportedTask(f"port_group({basename!r}) needs at least one sub-task")
    _GENERATOR_BASENAMES.add(basename)


def describe(task_name: str, config: PortConfig | None = None) -> dict[str, Any] | None:
    """Return the registered spec for *task_name*, if any."""
    ctx = PortContext.current(config)
    spec = ctx.spec_for(task_name)
    if spec is None:
        return None
    return {
        "name": spec.name,
        "targets": list(spec.targets),
        "inputs": list(spec.inputs),
        "deps": list(spec.deps),
        "key_salt": spec.key_salt,
        "signature": spec.signature(),
    }
