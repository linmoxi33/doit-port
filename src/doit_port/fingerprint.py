"""Content fingerprints.

Everything here is designed so that the same logical build produces the same
key on a different machine, in a different directory, on a different OS. That
rules out absolute paths, mtimes, file names of source files, and Python object
addresses. What is left is bytes and semantics: file contents, the bytecode of
python actions, the command string of shell actions, the declared dependencies
and the engine version.

A consequence worth stating explicitly: changing a comment inside an action
does **not** invalidate receipts, because the bytecode is unchanged. Changing
any executable line does.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import re
import types
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

_ADDR = re.compile(r"0x[0-9a-fA-F]+")
_READ_CHUNK = 1024 * 256

#: Bumped whenever the key derivation itself changes, invalidating old receipts.
SCHEMA_FOR_KEY = 1


def file_digest(path: str | os.PathLike[str]) -> str:
    """Return the sha256 of a file's bytes, streamed (safe for large files)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_READ_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def path_is_ignored(relpath: str, patterns: Sequence[str]) -> bool:
    """Very small glob matcher used for ``ignore`` in the config.

    Only ``*`` and ``?`` are supported plus bare suffix matching, which covers
    the realistic cases (``*.pyc``, ``__pycache__``, ``.DS_Store``).
    """
    import fnmatch

    for pattern in patterns:
        if fnmatch.fnmatch(relpath, pattern):
            return True
        if pattern.startswith("*") and relpath.endswith(pattern[1:]):
            return True
    return False


def _strip_addresses(text: str) -> str:
    return _ADDR.sub("<addr>", text)


def _stable_repr(value: Any, depth: int = 0) -> str:
    """A repr() that is stable across processes and machines."""
    if depth > 6:  # pragma: no cover - defensive
        return "<deep>"
    if isinstance(value, types.CodeType):
        return f"<code:{code_fingerprint(value)}>"
    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return repr(value)
    if isinstance(value, (list, tuple)):
        inner = ",".join(_stable_repr(v, depth + 1) for v in value)
        return f"[{inner}]"
    if isinstance(value, dict):
        items = sorted(
            f"{_stable_repr(k, depth + 1)}:{_stable_repr(v, depth + 1)}"
            for k, v in value.items()
        )
        return "{" + ",".join(items) + "}"
    if isinstance(value, (set, frozenset)):
        return "{" + ",".join(sorted(_stable_repr(v, depth + 1) for v in value)) + "}"
    if isinstance(value, Path):
        return f"<path:{value.as_posix()}>"
    return f"<{type(value).__name__}:{_strip_addresses(repr(value))}>"


def code_fingerprint(code: types.CodeType) -> str:
    """Stable digest of a code object.

    ``co_filename`` and ``co_firstlineno`` are deliberately excluded: they
    differ between machines and shift whenever someone adds a blank line above
    the function, neither of which changes behaviour.
    """
    h = hashlib.sha256()
    h.update(code.co_code)
    h.update(code.co_name.encode("utf-8", "surrogatepass"))
    h.update(repr(code.co_names).encode())
    h.update(repr(code.co_varnames).encode())
    h.update(repr(code.co_freevars).encode())
    h.update(repr(code.co_cellvars).encode())
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            h.update(b"|code|")
            h.update(code_fingerprint(const).encode())
        else:
            h.update(b"|const|")
            h.update(_stable_repr(const).encode("utf-8", "surrogatepass"))
    return h.hexdigest()[:32]


def callable_fingerprint(func: Any) -> str:
    """Digest a callable by its behaviour, not its identity."""
    if isinstance(func, functools.partial):
        base = callable_fingerprint(func.func)
        return f"partial:{base}:{_stable_repr(func.args)}:{_stable_repr(func.keywords)}"
    if inspect.ismethod(func):
        return f"method:{callable_fingerprint(func.__func__)}"
    code = getattr(func, "__code__", None)
    if isinstance(code, types.CodeType):
        return f"code:{code_fingerprint(code)}"
    call = getattr(type(func), "__call__", None)
    call_code = getattr(call, "__code__", None)
    if isinstance(call_code, types.CodeType) and not inspect.isroutine(func):
        return f"objcall:{type(func).__name__}:{code_fingerprint(call_code)}"
    return f"repr:{_strip_addresses(repr(func))}"


def action_fingerprint(action: Any) -> str:
    """Digest a single ``doit`` action."""
    # Imported lazily so that importing doit_port does not require doit
    # unless the user actually wires tasks.
    from doit.action import CmdAction, PythonAction

    if isinstance(action, CmdAction):
        raw = getattr(action, "_action", None)
        if callable(raw):
            return "cmd:callable:" + callable_fingerprint(raw)
        return "cmd:" + _stable_repr(raw)
    if isinstance(action, PythonAction):
        return "py:" + callable_fingerprint(action.py_callable) + ":" + _stable_repr(
            list(getattr(action, "args", ()) or ())
        ) + ":" + _stable_repr(dict(getattr(action, "kwargs", {}) or {}))
    return "obj:" + type(action).__name__ + ":" + callable_fingerprint(action)


def actions_fingerprint(actions: Iterable[Any]) -> str:
    """Digest an ordered list of actions."""
    h = hashlib.sha256()
    for action in actions or ():
        h.update(action_fingerprint(action).encode("utf-8", "surrogatepass"))
        h.update(b"\n")
    return h.hexdigest()


def engine_fingerprint() -> str:
    """Digest the toolchain that produced the artefacts.

    Only ``major.minor`` of the interpreter is used so that a patch release
    does not throw away every receipt, while a minor release (which can change
    bytecode semantics) does.
    """
    import sys

    from doit import __version__ as doit_version

    from . import __version__ as port_version

    payload = "|".join(
        [
            f"doit={doit_version}",
            f"doit-port={port_version}",
            f"python={sys.version_info.major}.{sys.version_info.minor}",
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def outputs_key(outputs: Sequence[Mapping[str, Any]]) -> str:
    """Combined digest of a task's outputs.

    Used by downstream tasks instead of the upstream *input* key: if the
    upstream re-ran but produced byte-identical artefacts, downstream work is
    still valid. This is the early-cutoff property.
    """
    h = hashlib.sha256()
    for entry in sorted(outputs, key=lambda e: e["path"]):
        h.update(entry["path"].encode("utf-8", "surrogatepass"))
        h.update(b"\0")
        h.update(entry["sha256"].encode())
        h.update(b"\0")
        h.update(str(entry["size"]).encode())
        h.update(b"\n")
    return h.hexdigest()


def compute_input_key(
    *,
    schema: int,
    flavor: str,
    task_name: str,
    inputs: Mapping[str, str],
    dep_outputs: Mapping[str, str],
    actions_sig: str,
    engine_sig: str,
) -> str:
    """The receipt lookup key for a task invocation.

    :param inputs: posix relative path -> sha256 of the file content.
    :param dep_outputs: upstream task name -> that task's ``outputs_key``.
    """
    h = hashlib.sha256()
    h.update(f"schema={schema}\n".encode())
    h.update(f"flavor={flavor}\n".encode("utf-8", "surrogatepass"))
    h.update(f"task={task_name}\n".encode("utf-8", "surrogatepass"))
    for path in sorted(inputs):
        h.update(f"in\t{path}\t{inputs[path]}\n".encode("utf-8", "surrogatepass"))
    for name in sorted(dep_outputs):
        h.update(f"dep\t{name}\t{dep_outputs[name]}\n".encode("utf-8", "surrogatepass"))
    h.update(f"act\t{actions_sig}\n".encode())
    h.update(f"eng\t{engine_sig}\n".encode())
    return h.hexdigest()


def json_digest(payload: Any) -> str:
    """Digest a JSON-serialisable structure deterministically."""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
