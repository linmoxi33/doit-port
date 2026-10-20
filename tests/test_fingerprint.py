"""Fingerprint stability.

The point of these tests is that a receipt computed in one process, in one
directory, must be reproducible in another. Several checks therefore shell out
to a *separate* interpreter: an in-process comparison can pass simply because
Python reused the same code object.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from doit_port import fingerprint as fp


def run_py(source: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True, text=True, cwd=cwd, check=True,
    )
    return result.stdout.strip()


# ------------------------------------------------------------------ file bytes


def test_file_digest_is_content_addressed(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "sub" / "b.bin"
    b.parent.mkdir()
    a.write_bytes(b"payload")
    b.write_bytes(b"payload")
    assert fp.file_digest(a) == fp.file_digest(b)


def test_file_digest_changes_with_one_byte(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    a.write_bytes(b"payload")
    first = fp.file_digest(a)
    a.write_bytes(b"payloae")
    assert fp.file_digest(a) != first


# ------------------------------------------------------------------ code


def test_code_fingerprint_stable_across_processes(tmp_path: Path) -> None:
    script = """
        from doit_port.fingerprint import callable_fingerprint

        def build():
            return {"a": 1, "b": [1, 2, 3]}

        print(callable_fingerprint(build))
    """
    first = run_py(script, cwd=tmp_path)
    second = run_py(script, cwd=tmp_path)
    assert first == second
    assert first.startswith("code:")


def test_code_fingerprint_ignores_comments_and_blank_lines(tmp_path: Path) -> None:
    base = """
        from doit_port.fingerprint import callable_fingerprint

        def build():
            {}

            return 1

        print(callable_fingerprint(build))
    """
    plain = run_py(base.format(""), cwd=tmp_path)
    commented = run_py(base.format("# a comment\n            # another one\n"), cwd=tmp_path)
    assert plain == commented, "bytecode is unchanged, so the receipt must stay valid"


def test_code_fingerprint_changes_when_behaviour_changes(tmp_path: Path) -> None:
    one = run_py("""
        from doit_port.fingerprint import callable_fingerprint

        def build():
            return 1

        print(callable_fingerprint(build))
    """, cwd=tmp_path)
    two = run_py("""
        from doit_port.fingerprint import callable_fingerprint

        def build():
            return 2

        print(callable_fingerprint(build))
    """, cwd=tmp_path)
    assert one != two


def test_nested_functions_are_included(tmp_path: Path) -> None:
    one = run_py("""
        from doit_port.fingerprint import callable_fingerprint

        def build():
            def inner():
                return "a"
            return inner

        print(callable_fingerprint(build))
    """, cwd=tmp_path)
    two = run_py("""
        from doit_port.fingerprint import callable_fingerprint

        def build():
            def inner():
                return "b"
            return inner

        print(callable_fingerprint(build))
    """, cwd=tmp_path)
    assert one != two, "a nested closure that changed must change the parent digest"


def test_stable_repr_strips_object_addresses() -> None:
    class Thing:
        pass

    obj = Thing()
    text = fp._stable_repr(obj)
    assert "0x" not in text
    assert "Thing" in text


def test_stable_repr_is_order_insensitive_for_dicts() -> None:
    assert fp._stable_repr({"a": 1, "b": 2}) == fp._stable_repr({"b": 2, "a": 1})


# ------------------------------------------------------------------ actions


def test_action_fingerprint_for_cmd_actions() -> None:
    from doit.action import CmdAction

    one = fp.action_fingerprint(CmdAction("echo hello", shell=True))
    two = fp.action_fingerprint(CmdAction("echo hello", shell=True))
    three = fp.action_fingerprint(CmdAction("echo goodbye", shell=True))
    assert one == two
    assert one != three


def test_action_fingerprint_for_python_actions() -> None:
    from doit.action import PythonAction

    def build() -> None:
        return None

    def other() -> None:
        return None

    assert fp.action_fingerprint(PythonAction(build)) == fp.action_fingerprint(PythonAction(build))
    assert fp.action_fingerprint(PythonAction(build)) != fp.action_fingerprint(PythonAction(other))


# ------------------------------------------------------------------ input key


def _key(**overrides):
    base = dict(
        schema=1, flavor="f", task_name="t",
        inputs={"a.txt": "deadbeef"}, dep_outputs={"up": "cafe"},
        actions_sig="act", engine_sig="eng",
    )
    base.update(overrides)
    return fp.compute_input_key(**base)


def test_input_key_is_deterministic() -> None:
    assert _key() == _key()


@pytest.mark.parametrize(
    "change",
    [
        {"flavor": "g"},
        {"task_name": "u"},
        {"inputs": {"a.txt": "beefdead"}},
        {"dep_outputs": {"up": "beef"}},
        {"actions_sig": "other"},
        {"engine_sig": "other"},
    ],
)
def test_input_key_reacts_to_every_component(change) -> None:
    assert _key(**change) != _key()


def test_input_key_ignores_path_insertion_order() -> None:
    left = _key(inputs={"a.txt": "1", "b.txt": "2"})
    right = _key(inputs={"b.txt": "2", "a.txt": "1"})
    assert left == right


def test_outputs_key_tracks_bytes_not_order() -> None:
    a = {"path": "x", "sha256": "aa" * 32, "size": 1}
    b = {"path": "y", "sha256": "bb" * 32, "size": 2}
    assert fp.outputs_key([a, b]) == fp.outputs_key([b, a])
    assert fp.outputs_key([a]) != fp.outputs_key([a, b])


def test_engine_fingerprint_includes_versions() -> None:
    sig = fp.engine_fingerprint()
    assert len(sig) == 16
    assert sig == fp.engine_fingerprint()
