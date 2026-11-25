"""Executor-supplied change descriptions. Read paths only in the explicit CLI helper."""

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess

from .store import TeamError


def _path(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise TeamError("Expected a short relative file path")
    if (
        PureWindowsPath(value).drive
        or PurePosixPath(value).is_absolute()
        or any(c in value for c in "\\*?[")
        or any(ord(c) < 32 for c in value)
        or any(p in {"", ".", "..", ".git"} for p in value.split("/"))
    ):
        raise TeamError("Expected a plain project-relative file path")
    return value


def validate(changes, scopes):
    if not isinstance(changes, dict) or changes.keys() - {
        "base_ref",
        "head_ref",
        "files",
    }:
        raise TeamError("Invalid change manifest fields")
    files = changes.get("files")
    if not isinstance(files, list) or len(files) > 64:
        raise TeamError("A manifest accepts at most 64 files")
    for key in changes.keys() - {"files"}:
        if not isinstance(changes[key], str) or not 1 <= len(changes[key]) <= 256:
            raise TeamError("Invalid manifest revision reference")
    seen = set()
    for entry in files:
        if not isinstance(entry, dict) or entry.keys() - {
            "path",
            "status",
            "fingerprint",
            "summary",
            "additions",
            "deletions",
        }:
            raise TeamError("Invalid manifest entry")
        name = _path(entry.get("path"))
        if name in seen or not any(
            s == "." or name == s or name.startswith(s + "/") for s in scopes
        ):
            raise TeamError("Duplicate file or file outside the task scope")
        seen.add(name)
        if entry.get("status") not in (
            "added",
            "modified",
            "deleted",
            "renamed",
            "binary",
        ):
            raise TeamError("Invalid change status")
        digest = entry.get("fingerprint")
        if not isinstance(digest, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8,64}", digest
        ):
            raise TeamError("Expected a hex fingerprint")
        if (
            not isinstance(entry.get("summary", ""), str)
            or len(entry.get("summary", "")) > 300
        ):
            raise TeamError("Change summary exceeds 300 characters")
        for key in entry.keys() & {"additions", "deletions"}:
            if type(entry[key]) is not int or not 0 <= entry[key] <= 2147483647:
                raise TeamError("Expected a nonnegative line count")
    encoded = json.dumps(changes, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 32768:
        raise TeamError("Change manifest exceeds 32 KiB")
    return encoded


def latest(db, task_id):
    from .schema import has_task_changes

    if not has_task_changes(db):
        return None
    row = db.execute(
        """SELECT revision,manifest FROM peer_task_changes WHERE task_id=?
        AND revision=(SELECT MAX(revision) FROM peer_task_history WHERE task_id=?
        AND kind IN ('review','blocked','released','recovered'))""",
        (task_id, task_id),
    ).fetchone()
    return {"revision": row[0], **json.loads(row[1])} if row else None


def _git(*args, **options):
    try:
        return subprocess.run(*args, **options)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TeamError(f"Cannot inspect the requested Git base: {error}") from error


def build(project, paths, base=None):
    root = Path(project).resolve(strict=True)
    if not root.is_dir() or not 1 <= len(paths) <= 64:
        raise TeamError("Select a directory and 1–64 individual files")
    commit = None
    if base:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/~^\-]{0,199}", base):
            raise TeamError("Invalid Git revision")
        commit = _git(
            ["git", "-C", str(root), "rev-parse", "--verify", base + "^{commit}"],
            capture_output=True,
            timeout=10,
        )
        if commit.returncode:
            raise TeamError("Cannot resolve base commit")
    files = []
    for value in paths:
        name = _path(value)
        target = root / name
        resolved = target.resolve()
        if (
            not resolved.is_relative_to(root)
            or resolved.relative_to(root).as_posix() != name
            or target.is_symlink()
        ):
            raise TeamError("File escapes project or is a symlink")
        status = "modified"
        base_blob = None
        if commit is not None:
            object_id = _git(
                [
                    "git",
                    "-C",
                    str(root),
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    commit.stdout.decode().strip() + ":./" + name,
                ],
                capture_output=True,
                timeout=10,
            )
            if object_id.returncode not in (0, 1):
                raise TeamError("Could not inspect the selected Git tree")
            base_blob = object_id.stdout.decode().strip() or None
            if base_blob is None:
                status = "added"
        if target.is_file():
            digest = hashlib.sha256()
            with target.open("rb") as stream:
                git_hash = None
                if base_blob is not None:
                    git_hash = hashlib.new(
                        "sha256" if len(base_blob) == 64 else "sha1",
                        usedforsecurity=False,
                    )
                    git_hash.update(f"blob {target.stat().st_size}\0".encode())
                first = stream.read(8192)
                if b"\0" in first:
                    status = "binary"
                digest.update(first)
                if git_hash is not None:
                    git_hash.update(first)
                for chunk in iter(lambda: stream.read(262144), b""):
                    digest.update(chunk)
                    if git_hash is not None:
                        git_hash.update(chunk)
                if git_hash is not None and git_hash.hexdigest() == base_blob:
                    continue
        elif base_blob and not target.exists():
            digest = hashlib.sha256(base_blob.encode())
            status = "deleted"
        else:
            raise TeamError(
                "Select existing regular files or a deletion relative to --base"
            )
        files.append(dict(path=name, status=status, fingerprint=digest.hexdigest()))
    result = {"files": files}
    if base:
        result["base_ref"] = commit.stdout.decode().strip()
    validate(result, paths)
    return result
