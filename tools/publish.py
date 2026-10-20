#!/usr/bin/env python3
"""Publish the project to GitHub through the Git Data API.

Why not ``git push``: outbound HTTPS to github.com is frequently blocked on this
machine (``Failed to connect github.com:443``), while ``gh api`` works. The Git
Data API also lets us build the whole tree in one commit instead of pushing a
history, which is what a first publication needs.

Steps: blobs -> tree (with ``base_tree`` when the repo already has content) ->
commit -> update ref. Every blob is verified by sha256 against the local file
right before it is sent, so a file that changed under us fails loudly.

Usage::

    python tools/publish.py --owner linmoxi33 --repo doit-port --dry-run
    python tools/publish.py --owner linmoxi33 --repo doit-port
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INCLUDE_DIRS = ("src", "tests", "examples", "docs", "tools", ".github")
INCLUDE_FILES = ("README.md", "LICENSE", "CHANGELOG.md", "pyproject.toml", ".gitattributes", ".gitignore")
SKIP_DIR_NAMES = {"__pycache__", ".doit-port", "build", "audit", "dist", ".pytest_cache", ".git", ".venv"}
SKIP_NAMES = {".doit.db", ".DS_Store", "Thumbs.db"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".db", ".zip", ".whl"}

#: How many times one API call is attempted before the run is aborted.
_RETRIES = 4

TEXT_SUFFIXES = {".py", ".md", ".toml", ".cfg", ".txt", ".rst", ".json", ".yml", ".yaml", ".ini", ".gitattributes", ".gitignore", ".sh"}
BINARY_SUFFIXES = {".png", ".jpg", ".gif", ".ico"}


def local_files() -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE_FILES:
        if (ROOT / name).is_file():
            files.append(ROOT / name)
    for directory in INCLUDE_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            parts = path.relative_to(ROOT).parts
            if any(p in SKIP_DIR_NAMES for p in parts):
                continue
            if any(p.endswith(".egg-info") for p in parts):
                continue
            if path.name in SKIP_NAMES or path.suffix in SKIP_SUFFIXES:
                continue
            files.append(path)
    return sorted(files, key=lambda p: p.relative_to(ROOT).as_posix())


def payload(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix in BINARY_SUFFIXES:
        return data
    # GitHub stores exactly what we send; LF keeps the repo byte-identical to
    # what the cache hashes, so a checkout on either OS produces the same keys.
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def gh(args: list[str], *, input_json: object | None = None) -> object:
    """Call ``gh api`` and return parsed JSON (or raw text for -q-less calls)."""
    cmd = ["gh", "api", *args]
    stdin = None
    tmp_path = None
    if input_json is not None:
        # A file avoids the quoting and length limits of argv on Windows.
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(input_json, handle)
        handle.close()
        tmp_path = handle.name
        cmd += ["--input", tmp_path]
    # A transient 404 was seen mid-run once (around blob 32 of 50) and aborted
    # the whole push, leaving a half-built tree. Retry with a growing pause;
    # anything still failing after that is a real error.
    result = None
    for attempt in range(1, _RETRIES + 1):
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", input=stdin)
        if result.returncode == 0:
            break
        retriable = any(tag in result.stderr for tag in ("Not Found", "Bad Gateway", "timeout", "502", "503"))
        if attempt == _RETRIES or not retriable:
            break
        time.sleep(1.5 * attempt)
    if tmp_path:
        Path(tmp_path).unlink(missing_ok=True)
    assert result is not None
    if result.returncode != 0:
        raise SystemExit(
            f"gh api failed ({result.returncode}) after {_RETRIES} attempt(s): "
            f"{' '.join(args)}\n{result.stderr.strip()}"
        )
    out = result.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except ValueError:
        return out


def get_repo(owner: str, repo: str) -> dict | None:
    probe = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if probe.returncode != 0:
        return None
    return json.loads(probe.stdout)


def create_repo(owner: str, repo: str, description: str) -> None:
    print(f"creating {owner}/{repo}")
    # POST /repos returns 404 for a personal token even with full scopes; the
    # documented create endpoint is POST /user/repos.
    gh([
        "user/repos", "--method", "POST",
        "-f", f"name={repo}",
        "-f", f"description={description}",
        "-f", "homepage=https://github.com/pydoit/doit",
        "-F", "private=false",
        "-F", "has_issues=true",
        "-F", "has_wiki=false",
        "-F", "has_projects=false",
    ])


def ensure_seed_commit(owner: str, repo: str) -> None:
    """Create an initial commit so the Git Data API has a tree to extend."""
    exists = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/git/ref/heads/main"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if exists.returncode == 0:
        print("repo already has refs/heads/main")
        return
    print("seeding an initial commit")
    gh([
        f"repos/{owner}/{repo}/contents/README.md", "--method", "PUT",
        "-f", "message=chore: seed repository",
        "-f", "content=" + __import__("base64").b64encode(b"doit-port\n").decode(),
        "-f", "branch=main",
    ])


def head_commit(owner: str, repo: str) -> str | None:
    result = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/git/ref/heads/main"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)["object"]["sha"]


def upload_blobs(owner: str, repo: str, files: list[Path]) -> dict[str, str]:
    """Upload every file as a blob; return posix path -> blob sha."""
    shas: dict[str, str] = {}
    for index, path in enumerate(files, 1):
        relative = path.relative_to(ROOT).as_posix()
        data = payload(path)
        binary = path.suffix in BINARY_SUFFIXES
        body = {
            "encoding": "base64" if binary else "utf-8",
            # base64 in the JSON body, never on the command line: a 33 KB PNG
            # encodes to ~45 KB and Windows rejects argv beyond ~32 KB with
            # "filename or extension is too long".
            "content": base64.b64encode(data).decode() if binary else data.decode("utf-8"),
        }
        blob = gh([f"repos/{owner}/{repo}/git/blobs", "--method", "POST"], input_json=body)
        if not isinstance(blob, dict) or "sha" not in blob:
            raise SystemExit(f"unexpected blob response for {relative}: {blob}")
        shas[relative] = blob["sha"]
        print(f"  [{index:2}/{len(files)}] {relative}")
    return shas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default="linmoxi33")
    parser.add_argument("--repo", default="doit-port")
    parser.add_argument("--message", default="feat: doit-port 0.1.0 — portable build receipts for doit")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    who = gh(["user"])
    login = who.get("login") if isinstance(who, dict) else None
    if login != args.owner:
        raise SystemExit(f"active gh account is {login!r}, expected {args.owner!r}; run `gh auth switch`")

    files = local_files()
    total = sum(path.stat().st_size for path in files)
    print(f"{len(files)} files, {total} bytes")

    if args.dry_run:
        for path in files:
            print("  " + path.relative_to(ROOT).as_posix())
        return 0

    description = ("Portable, content-addressed build receipts for doit: carry your incremental "
                   "build cache across machines and CI runs.")
    if get_repo(args.owner, args.repo) is None:
        create_repo(args.owner, args.repo, description)
        ensure_seed_commit(args.owner, args.repo)

    parent = head_commit(args.owner, args.repo)
    print("uploading blobs")
    shas = upload_blobs(args.owner, args.repo, files)

    entries = [{"path": path, "mode": "100644", "type": "blob", "sha": sha}
               for path, sha in shas.items()]
    tree_payload: dict = {"tree": entries}
    if parent:
        tree_payload["base_tree"] = gh([f"repos/{args.owner}/{args.repo}/git/commits/{parent}"])["tree"]["sha"]
    tree = gh([f"repos/{args.owner}/{args.repo}/git/trees", "--method", "POST"], input_json=tree_payload)
    commit = gh(
        [f"repos/{args.owner}/{args.repo}/git/commits", "--method", "POST"],
        input_json={"message": args.message, "tree": tree["sha"], "parents": [parent] if parent else []},
    )
    if parent:
        gh([f"repos/{args.owner}/{args.repo}/git/refs/heads/main", "--method", "PATCH",
            "-f", f"sha={commit['sha']}", "-F", "force=false"])
    else:  # pragma: no cover - seed commit guarantees a parent
        gh([f"repos/{args.owner}/{args.repo}/git/refs", "--method", "POST",
            "-f", "ref=refs/heads/main", "-f", f"sha={commit['sha']}"])

    print()
    print(f"commit {commit['sha']}")
    print(f"https://github.com/{args.owner}/{args.repo}")
    print(f"tree   {tree['sha']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
