# Packaging and release checklist

Kept as a file on purpose: the checks below caught real defects in this
repository during development, and repeating them by memory is how the same
defects come back.

## 1. Build from a clean tree

```bash
rm -rf build/ dist/ src/*.egg-info
python -m build --wheel --sdist
```

Requires `build` (`pip install build`). Never ship a wheel built from a tree
that still contains a previous `dist/` — pip will happily install the stale one.

## 2. Verify the wheel actually contains the fix

```bash
python -m zipfile -l dist/doit_port-0.1.0-py3-none-any.whl
```

Then, in a throwaway environment:

```bash
python -m venv /tmp/verify && /tmp/verify/bin/pip install dist/doit_port-*.whl
/tmp/verify/bin/python -c "import doit_port; print(doit_port.__version__)"
```

A wheel that imports from the source tree proves nothing: check
`doit_port.__file__` points inside the venv.

## 3. Entry point survives installation

```bash
python -c "import importlib.metadata as m; print([e.value for e in m.entry_points(group='doit.COMMAND')])"
```

Expected: `['doit_port.command:PortCommand']`. If this is empty, `doit port`
silently disappears — the package works but the CLI does not.

## 4. Test from the installed wheel, not the source tree

The suite imports `doit_port`; with `pip install -e .` an editable path is on
`sys.path`, which can mask a missing package-data file. Run at least the
end-to-end tests once against the installed wheel.

## 5. Determinism of the artefact

```bash
python tools/build_archive.py           # writes dist/doit-port-0.1.0.zip
python tools/build_archive.py --check   # same input => same sha256
```

Every text file is written with `\n` and a fixed mtime, so the ZIP hash is
reproducible on this machine. Cross-platform byte-identity is not promised
(zlib differs between CPython builds) and is not asserted.

## 6. Line endings

`.gitattributes` forces `text=auto eol=lf` for source, and the archive builder
normalises before writing. Without that, Windows checkouts produce CRLF files
and every `inputs=` file changes hash on a Linux machine — a cache that never
hits, with no error message anywhere.

## 7. Documentation claims

Before releasing, grep the docs for numbers and paths and confirm each one:

* the test count in `README.md` and `docs/verification.md`;
* the mutant count in the same two files;
* every `examples/…` path used in a code block.

A README that quotes yesterday's test count is a small lie that grows into a
habit.

## 8. Ship the licence of the dependency

`LICENSE` names `doit` and its copyright holder. doit is a *dependency*, not
vendored code, but a reader of the ZIP is entitled to know what they are getting.
