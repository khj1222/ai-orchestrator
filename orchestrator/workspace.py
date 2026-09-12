"""Independent Git workspaces. Git is invoked through PATH, without a shell.

Relative filesystem arguments resolve against the caller's current directory.
Workspace operations raise WorkspaceError; dirty workspaces raise WorkspaceDirty.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Iterable


class WorkspaceError(RuntimeError):
    """A Git workspace operation could not be completed."""


class WorkspaceDirty(WorkspaceError):
    """Uncommitted changes prevent workspace creation or removal."""


def _git(repo: Path, *args: str) -> bytes:
    # Inherited repository overrides must not redirect operations away from repo.
    environment = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                 "GIT_PREFIX"):
        environment.pop(name, None)
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
             "-c", f"core.hooksPath={os.devnull}", "-C", str(repo), *args],
            capture_output=True, check=True, timeout=30, env=environment,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        raise WorkspaceError(f"git {args[0]} failed: {detail}") from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceError(f"git {args[0]} could not finish: {error}") from error
    return result.stdout


def _repository(repo: Path) -> Path:
    repo = Path(repo).resolve()
    if _git(repo, "rev-parse", "--is-inside-work-tree").strip() != b"true":
        raise WorkspaceError("repo must be a non-bare Git working tree")
    top = _git(repo, "rev-parse", "--show-toplevel").decode("utf-8", "surrogateescape")
    return Path(top.rstrip("\r\n")).resolve()


def _commit(repo: Path, revision: str) -> str:
    # Resolve refs once so a moving branch cannot change the requested base later.
    return _git(repo, "rev-parse", "--verify", "--end-of-options",
                f"{revision}^{{commit}}").decode("ascii").strip()


def _dirty(repo: Path) -> bool:
    return bool(_git(repo, "status", "--porcelain=v1", "-z",
                     "--untracked-files=all", "--ignore-submodules=none"))


def _registered(repo: Path) -> list[Path]:
    # NUL records preserve whitespace and non-ASCII filenames. The first entry
    # is Git's primary worktree, which this module must never remove.
    data = _git(repo, "worktree", "list", "--porcelain", "-z")
    return [Path(token[len(b"worktree "):].decode("utf-8", "surrogateescape")).resolve()
            for token in data.split(b"\0") if token.startswith(b"worktree ")]


def create_worktree(repo: Path, base_commit: str, worktree_path: Path) -> Path:
    """Create a registered detached worktree at an existing commit.

    Staged, unstaged and untracked changes reported by Git status are refused;
    ignored build/cache files do not make the source repository dirty. Existing
    destinations (including broken symlinks) are never reused or removed.
    """
    repo = _repository(repo)
    requested = Path(worktree_path)
    if requested.exists() or requested.is_symlink():
        raise WorkspaceError("worktree destination already exists")
    destination = requested.resolve()
    if _dirty(repo):
        raise WorkspaceDirty("source repository has uncommitted changes")
    commit = _commit(repo, base_commit)
    _git(repo, "worktree", "add", "--detach", "--", str(destination), commit)
    return destination


def remove_worktree(repo: Path, worktree_path: Path, force: bool = False) -> None:
    """Remove only an exact registered secondary worktree, via Git itself.

    Ignored files also require force=True, to avoid deleting uncommitted user data.
    No recursive filesystem fallback or metadata pruning is performed on failure.
    A locked worktree remains protected even with force=True (one --force only).
    """
    repo = _repository(repo)
    destination = Path(worktree_path).resolve()
    registered = _registered(repo)
    if destination not in registered:
        raise WorkspaceError("path is not a registered worktree of this repository")
    if destination == registered[0] or destination == repo:
        raise WorkspaceError("cannot remove the primary or invoking worktree")
    if not force and (_dirty(destination) or
                      _git(destination, "ls-files", "--others", "-z", "--")):
        raise WorkspaceDirty("worktree has uncommitted changes; use force=True to remove")
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    _git(repo, *args, "--", str(destination))
    if destination.exists() or destination in _registered(repo):
        raise WorkspaceError("worktree removal could not be confirmed")


class ScopeChanges(list[str]):
    """Sorted out-of-scope paths, plus an optional inspection_error.

    Callers MUST check inspection_error before interpreting an empty list as
    success. A non-None error means the inspection is incomplete/undecidable.
    On failure, any returned paths are real findings collected before the error.
    No synthetic filename is inserted and normal list semantics are preserved.
    """

    def __init__(self, paths: Iterable[str] = (),
                 inspection_error: str | None = None) -> None:
        super().__init__(sorted(set(paths)))
        self.inspection_error = inspection_error


def _paths(data: bytes) -> set[str]:
    return {path.decode("utf-8", "surrogateescape") for path in data.split(b"\0") if path}


def detect_out_of_scope_changes(worktree_path: Path, base_commit: str,
                                allowed_write_paths: list[str]) -> ScopeChanges:
    """Inspect committed, staged, unstaged and untracked changes without raising.

    Compare base to HEAD, HEAD to index, and index to working tree separately so
    reverting a committed/staged edit in the next layer cannot conceal that edit.
    Renames count as deletion and addition. Ignored untracked files are included:
    .gitignore is not a write-scope boundary. This is an after-the-fact snapshot,
    not a sandbox or an audit of files created and then removed between checks.

    Allowed entries are nonempty repo-relative string prefixes, as specified by
    W5. Use a trailing slash for directory boundaries ("src/", not "src").
    Backslashes in allowed entries are normalized to forward slashes. An empty
    allowed list permits no writes. Inspect inspection_error BEFORE using paths.
    """
    changed: set[str] = set()
    prefixes: list[str] = []
    try:
        if not isinstance(allowed_write_paths, list):
            raise ValueError("allowed_write_paths must be a list of relative prefixes")
        for entry in allowed_write_paths:
            if not isinstance(entry, str):
                raise ValueError("allowed prefixes must be strings")
            prefix = entry.replace("\\", "/")
            if (not prefix or prefix.startswith("/") or ":" in prefix or
                    "\0" in prefix or ".." in prefix.split("/")):
                raise ValueError("allowed prefixes must be nonempty and repo-relative")
            prefixes.append(prefix)
        repo = _repository(worktree_path)
        commit = _commit(repo, base_commit)
        options = ("--name-only", "-z", "--no-renames", "--no-ext-diff",
                   "--no-textconv", "--no-relative", "--ignore-submodules=none")
        for comparison in ((commit, "HEAD"), ("--cached", "HEAD"), ()):
            changed.update(_paths(_git(repo, "diff", *options, *comparison, "--")))
        changed.update(_paths(_git(repo, "ls-files", "--others", "-z", "--")))
    except (WorkspaceError, OSError, ValueError, TypeError) as error:
        return ScopeChanges((path for path in changed if not path.startswith(tuple(prefixes))),
                            inspection_error=str(error))
    return ScopeChanges(path for path in changed if not path.startswith(tuple(prefixes)))
