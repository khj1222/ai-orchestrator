"""W5 checks using real local Git repositories, with no model CLI or network."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess

import pytest

from orchestrator import workspace
from orchestrator.workspace import (
    ScopeChanges, WorkspaceDirty, WorkspaceError, create_worktree,
    detect_out_of_scope_changes, remove_worktree,
)


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Workspace Test",
         "-c", "user.email=workspace@example.invalid", "-c", "commit.gpgsign=false",
         *args], capture_output=True, check=check, timeout=30,
    )


def commit(repo, message="fixture commit"):
    git(repo, "add", "--all")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.decode("ascii").strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "source repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    (repo / "allowed").mkdir()
    (repo / "allowed/file.txt").write_text("base allowed\n", encoding="utf-8")
    (repo / "outside.txt").write_text("base outside\n", encoding="utf-8")
    (repo / "delete.txt").write_text("base deletion\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    return repo, commit(repo)


@pytest.fixture
def checkout(repository, tmp_path):
    repo, base = repository
    path = create_worktree(repo, base, tmp_path / "작업 공간")
    return repo, base, path


def registered(repo):
    return {Path(token[len(b"worktree "):].decode("utf-8")).resolve()
            for token in git(repo, "worktree", "list", "--porcelain", "-z").stdout.split(b"\0")
            if token.startswith(b"worktree ")}


def test_create_uses_exact_base_and_detached_head(repository, tmp_path):
    repo, base = repository
    (repo / "outside.txt").write_text("later revision\n", encoding="utf-8")
    later = commit(repo, "advance main")
    before_branches = git(repo, "branch", "--list").stdout
    path = create_worktree(repo, base, tmp_path / "worker")
    assert path == (tmp_path / "worker").resolve()
    assert (path / "outside.txt").read_text(encoding="utf-8") == "base outside\n"
    assert git(path, "rev-parse", "HEAD").stdout.decode().strip() == base
    assert git(path, "symbolic-ref", "-q", "HEAD", check=False).returncode == 1
    assert git(path, "status", "--porcelain").stdout == b""
    assert path in registered(repo)
    assert git(repo, "rev-parse", "HEAD").stdout.decode().strip() == later
    assert git(repo, "branch", "--list").stdout == before_branches


@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked"])
def test_dirty_source_rejected(repository, tmp_path, kind):
    repo, base = repository
    target = repo / ("new.txt" if kind == "untracked" else "outside.txt")
    target.write_text("pending\n", encoding="utf-8")
    if kind == "staged":
        git(repo, "add", "--", "outside.txt")
    before = git(repo, "status", "--porcelain=v1", "-z").stdout
    path = tmp_path / "worker"
    with pytest.raises(WorkspaceDirty):
        create_worktree(repo, base, path)
    assert not path.exists()
    assert git(repo, "status", "--porcelain=v1", "-z").stdout == before
    assert registered(repo) == {repo.resolve()}


@pytest.mark.parametrize("revision", ["missing-revision", "--help", "HEAD:outside.txt"])
def test_invalid_base_rejected(repository, tmp_path, revision):
    repo, _ = repository
    with pytest.raises(WorkspaceError):
        create_worktree(repo, revision, tmp_path / "worker")
    assert not (tmp_path / "worker").exists()


def test_non_repository_rejected(tmp_path):
    with pytest.raises(WorkspaceError):
        create_worktree(tmp_path, "HEAD", tmp_path / "worker")


def test_existing_worktree_preserved(checkout):
    repo, base, path = checkout
    before = registered(repo)
    with pytest.raises(WorkspaceError, match="already exists"):
        create_worktree(repo, base, path)
    assert registered(repo) == before
    assert (path / "outside.txt").read_text(encoding="utf-8") == "base outside\n"


def test_existing_unregistered_directory_preserved(repository, tmp_path):
    repo, base = repository
    path = tmp_path / "existing"
    path.mkdir()
    keep = path / "keep.txt"
    keep.write_text("user data", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="already exists"):
        create_worktree(repo, base, path)
    assert keep.read_text(encoding="utf-8") == "user data"


def test_two_workers_can_be_created_concurrently(repository, tmp_path):
    repo, base = repository
    before_branches = git(repo, "branch", "--list").stdout
    paths = [tmp_path / f"worker-{n}" for n in (1, 2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create_worktree, repo, base, path) for path in paths]
        first, second = [future.result() for future in futures]
    assert registered(repo) == {repo.resolve(), first, second}
    for path in (first, second):
        assert git(path, "symbolic-ref", "-q", "HEAD", check=False).returncode == 1
    remove_worktree(repo, first)
    assert not first.exists()
    assert second in registered(repo)
    assert git(second, "status", "--porcelain").stdout == b""
    assert git(repo, "branch", "--list").stdout == before_branches


def test_remove_unregisters_and_deletes(checkout):
    repo, _, path = checkout
    assert remove_worktree(repo, path) is None
    assert not path.exists()
    assert registered(repo) == {repo.resolve()}
    with pytest.raises(WorkspaceError, match="not a registered"):
        remove_worktree(repo, path)


def test_remove_unregistered_path_never_deletes(repository, tmp_path):
    repo, _ = repository
    path = tmp_path / "unregistered"
    path.mkdir()
    keep = path / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="not a registered"):
        remove_worktree(repo, path, force=True)
    assert keep.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked"])
def test_dirty_remove_requires_force(checkout, kind):
    repo, _, path = checkout
    file = path / ("new.txt" if kind == "untracked" else "outside.txt")
    file.write_text("pending", encoding="utf-8")
    if kind == "staged":
        git(path, "add", "--", "outside.txt")
    with pytest.raises(WorkspaceDirty):
        remove_worktree(repo, path)
    assert path in registered(repo)
    assert file.read_text(encoding="utf-8") == "pending"
    remove_worktree(repo, path, force=True)
    assert not path.exists()
    assert path not in registered(repo)


def test_primary_and_nested_paths_cannot_be_removed(checkout):
    repo, _, path = checkout
    with pytest.raises(WorkspaceError, match="primary"):
        remove_worktree(repo, repo, force=True)
    with pytest.raises(WorkspaceError, match="not a registered"):
        remove_worktree(repo, path / "allowed", force=True)
    assert (repo / "outside.txt").exists()
    assert path in registered(repo)


def test_ignored_user_files_also_require_force_to_remove(checkout):
    repo, _, path = checkout
    (path / "ignored").mkdir()
    keep = path / "ignored/local.txt"
    keep.write_text("local data", encoding="utf-8")
    with pytest.raises(WorkspaceDirty):
        remove_worktree(repo, path)
    assert keep.read_text(encoding="utf-8") == "local data"
    remove_worktree(repo, path, force=True)
    assert not path.exists()


def test_foreign_repository_worktree_cannot_be_removed(checkout, tmp_path):
    _, _, path = checkout
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "--initial-branch=main")
    (other / "file.txt").write_text("other", encoding="utf-8")
    commit(other)
    with pytest.raises(WorkspaceError, match="not a registered"):
        remove_worktree(other, path, force=True)
    assert path.exists()


def test_locked_worktree_is_preserved(checkout):
    repo, _, path = checkout
    git(repo, "worktree", "lock", str(path))
    try:
        with pytest.raises(WorkspaceError):
            remove_worktree(repo, path, force=True)
        assert path.exists()
        assert path in registered(repo)
    finally:
        git(repo, "worktree", "unlock", str(path))


def test_allowed_changes_return_empty_list(checkout):
    _, base, path = checkout
    (path / "allowed/file.txt").write_text("modified", encoding="utf-8")
    (path / "allowed/new.txt").write_text("added", encoding="utf-8")
    result = detect_out_of_scope_changes(path, base, ["allowed/"])
    assert isinstance(result, list)
    assert result == []
    assert result.inspection_error is None


def test_all_change_layers_and_deletions_are_detected(checkout):
    _, base, path = checkout
    (path / "committed.txt").write_text("committed", encoding="utf-8")
    commit(path)
    (path / "staged.txt").write_text("staged", encoding="utf-8")
    git(path, "add", "--", "staged.txt")
    (path / "outside.txt").write_text("modified", encoding="utf-8")
    (path / "new 한글 file.txt").write_text("new", encoding="utf-8")
    (path / "delete.txt").unlink()
    (path / "allowed/new.txt").write_text("allowed", encoding="utf-8")
    before = git(path, "status", "--porcelain=v1", "-z").stdout
    result = detect_out_of_scope_changes(path, base, ["allowed/"])
    assert result.inspection_error is None
    assert result == ["committed.txt", "delete.txt", "new 한글 file.txt", "outside.txt", "staged.txt"]
    assert git(path, "status", "--porcelain=v1", "-z").stdout == before


@pytest.mark.parametrize("stage", ["committed", "staged", "unstaged"])
def test_deleted_file_is_detected_at_each_layer(checkout, stage):
    _, base, path = checkout
    (path / "delete.txt").unlink()
    if stage == "committed":
        commit(path)
    elif stage == "staged":
        git(path, "add", "--all")
    result = detect_out_of_scope_changes(path, base, ["allowed/"])
    assert result == ["delete.txt"]
    assert result.inspection_error is None


def test_rename_reports_both_sides(checkout):
    _, base, path = checkout
    git(path, "mv", "outside.txt", "allowed/renamed.txt")
    commit(path)
    result = detect_out_of_scope_changes(path, base, ["allowed/"])
    assert result == ["outside.txt"]
    assert result.inspection_error is None


def test_reverted_working_copy_does_not_hide_index_change(checkout):
    _, base, path = checkout
    (path / "outside.txt").write_text("staged edit", encoding="utf-8")
    git(path, "add", "--", "outside.txt")
    (path / "outside.txt").write_text("base outside\n", encoding="utf-8")
    assert detect_out_of_scope_changes(path, base, []) == ["outside.txt"]


def test_ignore_rules_do_not_hide_scope_changes(checkout):
    _, base, path = checkout
    (path / "ignored").mkdir()
    (path / "ignored/hidden.txt").write_text("hidden", encoding="utf-8")
    assert git(path, "status", "--porcelain").stdout == b""
    result = detect_out_of_scope_changes(path, base, ["allowed/"])
    assert result == ["ignored/hidden.txt"]
    assert result.inspection_error is None


def test_prefix_matching_and_directory_boundary(checkout):
    _, base, path = checkout
    (path / "allowed-extra.txt").write_text("outside directory", encoding="utf-8")
    assert detect_out_of_scope_changes(path, base, ["allowed/"]) == ["allowed-extra.txt"]
    # W5 explicitly specifies prefix matching, rather than globs or exact files.
    assert detect_out_of_scope_changes(path, base, ["allowed"]) == []
    (path / "allowed/new.txt").write_text("allowed", encoding="utf-8")
    assert detect_out_of_scope_changes(path, base, ["allowed\\"]) == ["allowed-extra.txt"]


def test_bad_repository_or_revision_returns_inspection_error(checkout, tmp_path):
    _, _, path = checkout
    for location, revision in ((path, "missing"), (tmp_path, "HEAD")):
        result = detect_out_of_scope_changes(location, revision, [])
        assert isinstance(result, list)
        assert result == []
        assert result.inspection_error


@pytest.mark.parametrize("prefixes", [[""], ["../"], [None], "allowed/"])
def test_invalid_allowed_prefixes_report_error(checkout, prefixes):
    _, base, path = checkout
    result = detect_out_of_scope_changes(path, base, prefixes)
    assert result.inspection_error


def test_missing_git_and_timeout_are_reported_without_raising(checkout, monkeypatch):
    _, base, path = checkout
    for error in (FileNotFoundError("git unavailable"),
                  subprocess.TimeoutExpired("git", 30)):
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(workspace.subprocess, "run", fail)
        result = detect_out_of_scope_changes(path, base, [])
        assert result.inspection_error


def test_partial_findings_are_preserved_with_error(checkout, monkeypatch):
    _, base, path = checkout
    (path / "outside.txt").write_text("committed edit", encoding="utf-8")
    commit(path)
    original = workspace._git

    def fail_index(repo, *args):
        if "--cached" in args:
            raise WorkspaceError("index unavailable")
        return original(repo, *args)

    monkeypatch.setattr(workspace, "_git", fail_index)
    result = detect_out_of_scope_changes(path, base, [])
    assert result == ["outside.txt"]
    assert result.inspection_error == "index unavailable"


def test_scope_changes_is_sorted_deduplicated_list():
    result = ScopeChanges(["z.txt", "a.txt", "z.txt"])
    assert result == ["a.txt", "z.txt"]
    assert result.inspection_error is None
