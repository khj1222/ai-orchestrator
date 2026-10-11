"""W6 검증기 검사. 임시 git 저장소와 python -c 명령만 쓴다. 모델 CLI·네트워크 없음."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import verify, workspace
from orchestrator.records import Check, load_checks, save_check
from orchestrator.verify import (
    REASON_INSPECTION_ERROR,
    REASON_INVALID_DEFINITION,
    REASON_MISSING_ARTIFACT,
    REASON_NONZERO_EXIT,
    REASON_OUT_OF_SCOPE,
    REASON_START_FAILED,
    REASON_TIMEOUT,
    REASON_WORKSPACE_UNAVAILABLE,
    CheckDefinitionError,
    PublicationRejected,
    is_check_current,
    read_publication,
    load_check_definition,
    run_checks,
    tree_hash,
)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Verify Test",
                           "-c", "user.email=verify@example.invalid", "-c", "commit.gpgsign=false", *args],
                          capture_output=True, check=True, timeout=30)


def commit(repo, message="commit"):
    git(repo, "add", "--all")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.decode().strip()


def define(checks_dir, check_id, **fields):
    data = {"check_id": check_id, "validator_version": "1", **fields}
    (checks_dir / f"{check_id}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return check_id


PY = "{python}"


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "subject repo"
    (repo / "methods").mkdir(parents=True)
    git(repo, "init", "--initial-branch=main")
    (repo / "methods/a.py").write_text("A = 1\n", encoding="utf-8")
    (repo / "outside.txt").write_text("base\n", encoding="utf-8")
    base = commit(repo, "base")
    return repo, base


@pytest.fixture
def checks_dir(tmp_path):
    d = tmp_path / "checks"
    d.mkdir()
    return d


@pytest.fixture
def evidence(tmp_path):
    return tmp_path / "evidence"


# ---------------------------------------------------------------- 통과·실패

def test_pass_records_commit_tree_hash_and_evidence(repo, checks_dir, evidence):
    repo_path, base = repo
    check_id = define(checks_dir, "content-v1", command=[
        PY, "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('methods/a.py').read_text().strip()=='A = 1' else 3)"],
        required_artifacts=["methods/a.py"], command_id="content-check")
    result = run_checks(repo_path, base, [check_id], checks_dir, evidence, "attempt-1")
    assert result.worktree_error is None
    assert result.subject_commit == base
    assert result.subject_manifest_hash == tree_hash(repo_path, base)
    assert len(result.subject_manifest_hash) == 40
    [check] = result.checks
    assert check.verdict == "pass" and check.exit_code == 0
    assert (check.check_id, check.attempt_id, check.command_id, check.validator_version) == \
        (check_id, "attempt-1", "content-check", "1")
    assert check.subject_commit == base and check.subject_manifest_hash == result.subject_manifest_hash
    for key in ("stdout_file", "stderr_file", "command", "environment_manifest"):
        assert (evidence / check.evidence[key]).is_file()
    command = json.loads((evidence / check.evidence["command"]).read_text(encoding="utf-8"))
    assert command["exit_code"] == 0 and command["argv"][0] == sys.executable
    assert command["process"]["argv_hash"] and "argv" not in command["process"]
    assert result.all_passed()


def test_nonzero_exit_is_fail_with_streams_separated(repo, checks_dir, evidence):
    repo_path, base = repo
    check_id = define(checks_dir, "failing-v1", command=[
        PY, "-c", "import sys; print('to stdout'); print('to stderr', file=sys.stderr); sys.exit(7)"])
    [check] = run_checks(repo_path, base, [check_id], checks_dir, evidence, "a").checks
    assert check.verdict == "fail" and check.exit_code == 7
    assert check.evidence["reason"] == REASON_NONZERO_EXIT
    assert b"to stdout" in (evidence / check.evidence["stdout_file"]).read_bytes()
    assert b"to stderr" in (evidence / check.evidence["stderr_file"]).read_bytes()
    assert b"to stderr" not in (evidence / check.evidence["stdout_file"]).read_bytes()


def test_missing_artifact_fails_without_running_command(repo, checks_dir, evidence):
    repo_path, base = repo
    marker = evidence.parent / "ran.marker"
    check_id = define(checks_dir, "artifact-v1", command=[PY, "-c", f"open({str(marker)!r}, 'w').close()"],
                      required_artifacts=["methods/a.py", "methods/b.py"])
    [check] = run_checks(repo_path, base, [check_id], checks_dir, evidence, "a").checks
    assert check.verdict == "fail" and check.exit_code is None
    assert check.evidence["reason"] == REASON_MISSING_ARTIFACT
    missing = json.loads((evidence / check.evidence["missing_artifacts"]).read_text(encoding="utf-8"))
    assert missing == {"missing": ["methods/b.py"]}
    assert not marker.exists()
    assert not (evidence / "checks" / check_id / "stdout.bin").exists()


# ---------------------------------------------------------------- undecidable

def test_dirty_repo_and_unknown_commit_make_every_check_undecidable(repo, checks_dir, evidence):
    repo_path, base = repo
    ids = [define(checks_dir, "ok-v1", command=[PY, "-c", "pass"]),
           define(checks_dir, "scope-v1", kind="scope")]
    (repo_path / "outside.txt").write_text("uncommitted\n", encoding="utf-8")
    result = run_checks(repo_path, base, ids, checks_dir, evidence, "a", base_commit=base)
    assert result.worktree_error and "WorkspaceDirty" in result.worktree_error
    assert [c.verdict for c in result.checks] == ["undecidable", "undecidable"]
    assert all(c.evidence["reason"] == REASON_WORKSPACE_UNAVAILABLE for c in result.checks)
    assert all((evidence / c.evidence["reason_file"]).is_file() for c in result.checks)
    assert not result.all_passed()
    git(repo_path, "checkout", "--", "outside.txt")

    result = run_checks(repo_path, "0" * 40, ids[:1], checks_dir, evidence / "2", "a")
    assert result.worktree_error and result.subject_manifest_hash is None
    assert result.checks[0].verdict == "undecidable"
    assert result.checks[0].subject_commit == "0" * 40


def test_timeout_is_undecidable_and_process_is_stopped(repo, checks_dir, evidence, monkeypatch):
    monkeypatch.setattr(verify.proc, "KILL_SETTLE_SECONDS", 1.0)
    repo_path, base = repo
    check_id = define(checks_dir, "slow-v1", command=[PY, "-c", "import time; time.sleep(120)"], timeout_seconds=1)
    [check] = run_checks(repo_path, base, [check_id], checks_dir, evidence, "a").checks
    assert check.verdict == "undecidable" and check.exit_code is None
    assert check.evidence["reason"] == REASON_TIMEOUT
    command = json.loads((evidence / check.evidence["command"]).read_text(encoding="utf-8"))
    assert command["stop_result"]["state"] == "stopped"
    assert check.evidence["stop_state"] == "stopped"


def test_unstartable_command_is_undecidable(repo, checks_dir, evidence):
    repo_path, base = repo
    check_id = define(checks_dir, "nocmd-v1", command=["definitely-not-a-program-xyz", "--version"])
    [check] = run_checks(repo_path, base, [check_id], checks_dir, evidence, "a").checks
    assert check.verdict == "undecidable"
    assert check.evidence["reason"] == REASON_START_FAILED


def test_invalid_definitions_are_undecidable_but_others_still_run(repo, checks_dir, evidence):
    repo_path, base = repo
    define(checks_dir, "unresolved-v1", command=[PY, "-c", "pass"], validator_version="TO_BE_SET")
    (checks_dir / "mismatch-v1.json").write_text(json.dumps({"check_id": "other", "validator_version": "1",
                                                              "command": ["x"]}), encoding="utf-8")
    good = define(checks_dir, "good-v1", command=[PY, "-c", "pass"])
    ids = ["unresolved-v1", "mismatch-v1", "absent-v1", "bad id!", good]
    result = run_checks(repo_path, base, ids, checks_dir, evidence, "a")
    verdicts = {c.check_id: c.verdict for c in result.checks}
    assert verdicts == {"unresolved-v1": "undecidable", "mismatch-v1": "undecidable",
                        "absent-v1": "undecidable", "invalid-check": "undecidable", good: "pass"}
    for check in result.checks[:4]:
        assert check.evidence["reason"] == REASON_INVALID_DEFINITION
        assert check.subject_commit == base


@pytest.mark.parametrize("fields,message", [
    ({"command": "not a list"}, "command"),
    ({"command": []}, "command"),
    ({"command": [PY], "timeout_seconds": 0}, "timeout"),
    ({"command": [PY], "required_artifacts": ["../escape"]}, "relative"),
    ({"command": [PY], "cwd": "C:/abs"}, "relative"),
    ({"kind": "shell"}, "kind"),
    ({"kind": "scope", "allowed_write_paths": "src/"}, "allowed_write_paths"),
    ({"command": [PY], "validator_version": ""}, "validator_version"),
])
def test_load_check_definition_rejects_bad_shapes(checks_dir, fields, message):
    define(checks_dir, "bad-v1", **fields)
    with pytest.raises(CheckDefinitionError, match=message):
        load_check_definition(checks_dir, "bad-v1")
    with pytest.raises(CheckDefinitionError):
        load_check_definition(checks_dir, "missing-v1")
    with pytest.raises(CheckDefinitionError):
        load_check_definition(checks_dir, "../etc")


# ---------------------------------------------------------------- 범위 검사 (kind=scope)

def test_scope_check_pass_fail_cache_and_inspection_error(repo, checks_dir, evidence, monkeypatch):
    repo_path, base = repo
    scope = define(checks_dir, "allowed-changes-v1", kind="scope", command_id="workspace-scope")

    # 작업자가 허용 경로 안만 바꾸고 캐시를 남긴 commit
    worker = workspace.create_worktree(repo_path, base, evidence.parent / "worker")
    (worker / "methods/a.py").write_text("A = 2\n", encoding="utf-8")
    (worker / "methods/__pycache__").mkdir()
    (worker / "methods/__pycache__/a.cpython-310.pyc").write_bytes(b"\0")
    (worker / ".pytest_cache").mkdir()
    (worker / ".pytest_cache/v").write_text("x", encoding="utf-8")
    git(worker, "add", "-f", "--all")
    subject = commit(worker, "allowed edit with caches")
    result = run_checks(repo_path, subject, [scope], checks_dir, evidence / "1", "a",
                        base_commit=base, allowed_write_paths=["methods/"])
    [check] = result.checks
    assert check.verdict == "pass", check.evidence
    report = json.loads(((evidence / "1") / check.evidence["scope_report"]).read_text(encoding="utf-8"))
    assert report["out_of_scope"] == []
    # 허용 경로(methods/) 안의 캐시는 범위 검사 결과에 나오지 않는다. 허용 밖 캐시만 따로 분류된다.
    assert report["cache_paths_ignored"] == [".pytest_cache/v"]

    # 허용 밖 파일을 고친 commit
    (worker / "outside.txt").write_text("changed\n", encoding="utf-8")
    subject2 = commit(worker, "out of scope edit")
    [check] = run_checks(repo_path, subject2, [scope], checks_dir, evidence / "2", "a",
                         base_commit=base, allowed_write_paths=["methods/"]).checks
    assert check.verdict == "fail" and check.evidence["reason"] == REASON_OUT_OF_SCOPE
    report = json.loads(((evidence / "2") / check.evidence["scope_report"]).read_text(encoding="utf-8"))
    assert report["out_of_scope"] == ["outside.txt"]

    # base_commit 없음 → undecidable
    [check] = run_checks(repo_path, subject, [scope], checks_dir, evidence / "3", "a").checks
    assert check.verdict == "undecidable" and check.evidence["reason"] == REASON_INSPECTION_ERROR

    # inspection_error 가 있으면 빈 결과라도 pass 가 아니다 (W5 검토 U1)
    def broken(worktree_path, base_commit, allowed):
        return workspace.ScopeChanges([], inspection_error="simulated git failure")
    monkeypatch.setattr(workspace, "detect_out_of_scope_changes", broken)
    [check] = run_checks(repo_path, subject, [scope], checks_dir, evidence / "4", "a",
                         base_commit=base, allowed_write_paths=["methods/"]).checks
    assert check.verdict == "undecidable" and check.evidence["reason"] == REASON_INSPECTION_ERROR
    assert check.evidence["detail"] == "simulated git failure"
    workspace.remove_worktree(repo_path, worker, force=True)


def test_scope_definition_allowed_paths_override_argument(repo, checks_dir, evidence):
    repo_path, base = repo
    scope = define(checks_dir, "fixed-scope-v1", kind="scope", allowed_write_paths=["docs/"])
    worker = workspace.create_worktree(repo_path, base, evidence.parent / "worker")
    (worker / "methods/a.py").write_text("A = 3\n", encoding="utf-8")
    subject = commit(worker, "edit methods")
    [check] = run_checks(repo_path, subject, [scope], checks_dir, evidence, "a",
                         base_commit=base, allowed_write_paths=["methods/"]).checks
    assert check.verdict == "fail"   # 정의의 docs/ 가 인수의 methods/ 보다 우선한다
    workspace.remove_worktree(repo_path, worker, force=True)


@pytest.mark.parametrize("path,expected", [
    ("methods/__pycache__/a.pyc", True), ("__pycache__/x", True), (".pytest_cache/v/cache", True),
    ("methods/a.pyc", True), ("methods/a.py", False), ("pycache/x", False), ("docs/.pytest_cache.md", False),
])
def test_cache_pattern_matcher(path, expected):
    assert verify._is_cache_path(path, verify.DEFAULT_CACHE_PATTERNS) is expected


# ---------------------------------------------------------------- 격리·정리·호환

def test_runs_in_fresh_worktree_and_cleans_up(repo, checks_dir, evidence):
    repo_path, base = repo
    check_id = define(checks_dir, "touch-v1", command=[
        PY, "-c", "import pathlib; pathlib.Path('created-by-check.txt').write_text('x'); "
                  "assert pathlib.Path('.git').exists()"])
    result = run_checks(repo_path, base, [check_id], checks_dir, evidence, "a")
    assert result.checks[0].verdict == "pass"
    assert not (evidence / "verify-worktree").exists()            # 캐시·산출물이 있어도 지운다
    assert not (repo_path / "created-by-check.txt").exists()       # 원본 저장소는 건드리지 않는다
    assert git(repo_path, "status", "--porcelain").stdout == b""
    listed = git(repo_path, "worktree", "list", "--porcelain").stdout.decode()
    assert "verify-worktree" not in listed

    kept = run_checks(repo_path, base, [check_id], checks_dir, evidence / "kept", "a",
                      worktree_path=evidence / "custom-wt", keep_worktree=True)
    assert kept.checks[0].verdict == "pass"
    assert (evidence / "custom-wt" / "created-by-check.txt").is_file()
    workspace.remove_worktree(repo_path, evidence / "custom-wt", force=True)


def test_subject_ref_is_resolved_to_commit_hash(repo, checks_dir, evidence):
    repo_path, base = repo
    check_id = define(checks_dir, "ok-v1", command=[PY, "-c", "pass"])
    result = run_checks(repo_path, "main", [check_id], checks_dir, evidence, "a")
    assert result.subject_commit == base == result.checks[0].subject_commit


def test_check_command_env_is_stripped_and_placeholders_expanded(repo, checks_dir, evidence, monkeypatch):
    repo_path, base = repo
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value-123")
    check_id = define(checks_dir, "env-v1", command=[
        PY, "-c", "import os,sys,pathlib; wt=pathlib.Path(sys.argv[1]); "
                  "sys.exit(0 if 'ANTHROPIC_API_KEY' not in os.environ and os.environ.get('PYTHONUTF8')=='1' "
                  "and (wt/'methods/a.py').is_file() and pathlib.Path.cwd().resolve()==wt.resolve() else 5)",
        "{worktree}"])
    [check] = run_checks(repo_path, base, [check_id], checks_dir, evidence, "a").checks
    assert check.verdict == "pass", (evidence / check.evidence["stderr_file"]).read_bytes()
    env_manifest = json.loads((evidence / check.evidence["environment_manifest"]).read_text(encoding="utf-8"))
    assert "ANTHROPIC_API_KEY" in env_manifest["stripped_env"]
    everything = "".join(p.read_text(encoding="utf-8", errors="ignore")
                         for p in evidence.rglob("*.json"))
    assert "sk-secret-value-123" not in everything
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-secret-value-123"


SUMMARY = {"schema": "eval-summary-1", "k": 3, "queries": 20, "protocol_hash": "a" * 64,
           "a": {"hit_at_3": 0.3, "hits": 6, "no_result_queries": 0},
           "b": {"hit_at_3": 0.35, "hits": 7, "no_result_queries": 1}}
PUBLISHER = ("import json,pathlib,sys; out=pathlib.Path(sys.argv[1]); assert out.is_dir(); "
             "wt=pathlib.Path(sys.argv[2]).resolve(); assert wt not in out.resolve().parents; "
             "files=json.loads(sys.argv[4]); "
             "[(out/name).write_text(text, encoding='utf-8') for name, text in files.items()]; sys.exit(int(sys.argv[3]))")


def publish_check(checks_dir, files, exit_code=0):
    return define(checks_dir, "pub-v1", command=[PY, "-c", PUBLISHER, "{publish}", "{worktree}", str(exit_code),
                                                 json.dumps(files)])


@pytest.mark.parametrize("exit_code,published", [(0, True), (3, False)])
def test_publish_placeholder_is_outside_worktree_and_counts_only_on_pass(repo, checks_dir, evidence,
                                                                         exit_code, published):  # W13b
    repo_path, base = repo
    check_id = publish_check(checks_dir, {"eval-summary.json": json.dumps(SUMMARY)}, exit_code)
    [check] = run_checks(repo_path, base, [check_id], checks_dir, evidence, "a").checks
    assert check.verdict == ("pass" if exit_code == 0 else "fail")
    assert ("published" in check.evidence) is published
    if published:
        assert read_publication(evidence, check.evidence) == SUMMARY


# W13b Codex review F1: the content, not the pass verdict, decides what is published.
@pytest.mark.parametrize("files,reason", [
    ({"eval-summary.json": json.dumps(SUMMARY), "query-details.json": '{"text": "secret query"}'}, "unexpected_files"),
    ({"eval-summary.json": json.dumps({**SUMMARY, "query_rows": [{"query_id": "q11"}]})}, "invalid_schema"),
    ({"eval-summary.json": json.dumps({**SUMMARY, "a": {**SUMMARY["a"], "hit_at_3": {"text": "secret query"}}})},
     "invalid_schema"),
    ({"eval-summary.json": json.dumps({**SUMMARY, "a": {**SUMMARY["a"], "hit_at_3": 0.9}})}, "invalid_schema"),
    ({"eval-summary.json": json.dumps({**SUMMARY, "protocol_hash": "secret query text"})}, "invalid_schema"),
    ({"eval-summary.json": json.dumps({**SUMMARY, "a": {**SUMMARY["a"], "hits": True}})}, "invalid_schema"),
    ({"eval-summary.json": "not json"}, "invalid_json"),
    ({"eval-summary.json": json.dumps(SUMMARY) + " " * 5000}, "too_large"),
])
def test_publication_must_be_exact_aggregate(repo, checks_dir, evidence, files, reason):
    repo_path, base = repo
    [check] = run_checks(repo_path, base, [publish_check(checks_dir, files)], checks_dir, evidence, "a").checks
    assert check.verdict == "pass"  # the verdict rule is unchanged
    assert "published" not in check.evidence and check.evidence["publish_rejected"] == reason
    assert "secret" not in json.dumps(check.evidence)


def test_sealed_copy_ignores_later_writes_and_detects_tampering(repo, checks_dir, evidence):
    repo_path, base = repo
    check_id = publish_check(checks_dir, {"eval-summary.json": json.dumps(SUMMARY)})
    [check] = run_checks(repo_path, base, [check_id], checks_dir, evidence, "a").checks
    publish = evidence / "checks" / "pub-v1" / "publish"
    (publish / "eval-summary.json").write_text(json.dumps({**SUMMARY, "text": "late secret"}), encoding="utf-8")
    (publish / "query-details.json").write_text("late secret", encoding="utf-8")
    assert read_publication(evidence, check.evidence) == SUMMARY  # sealed before, unaffected
    sealed = evidence / check.evidence["published"]
    sealed.write_text(sealed.read_text(encoding="utf-8").replace("0.3", "0.30"), encoding="utf-8")
    with pytest.raises(PublicationRejected, match="sealed_copy_changed"):
        read_publication(evidence, check.evidence)


def test_no_publish_folder_without_placeholder(repo, checks_dir, evidence):  # W13b
    repo_path, base = repo
    check_id = define(checks_dir, "plain-v1", command=[PY, "-c", "pass"])
    [check] = run_checks(repo_path, base, [check_id], checks_dir, evidence, "a").checks
    assert check.verdict == "pass" and "published" not in check.evidence
    assert not (evidence / "checks" / "plain-v1" / "publish").exists()


def test_cwd_field_is_relative_to_worktree(repo, checks_dir, evidence):
    repo_path, base = repo
    check_id = define(checks_dir, "cwd-v1", cwd="methods",
                      command=[PY, "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('a.py').is_file() else 4)"])
    [check] = run_checks(repo_path, base, [check_id], checks_dir, evidence, "a").checks
    assert check.verdict == "pass"


def test_is_check_current_requires_same_commit_and_pass():
    check = Check("c", "a", "abc", "tree", "1", "cmd", 0, "pass")
    assert is_check_current(check, "abc") is True
    assert is_check_current(check, "def") is False
    assert is_check_current(Check("c", "a", "abc", "tree", "1", "cmd", 1, "fail"), "abc") is False
    assert is_check_current(Check("c", "a", "abc", "tree", "1", "cmd", None, "undecidable"), "abc") is False


def test_checks_round_trip_through_records(repo, checks_dir, evidence, tmp_path):
    repo_path, base = repo
    ids = [define(checks_dir, "ok-v1", command=[PY, "-c", "pass"]),
           define(checks_dir, "bad-v1", command=[PY, "-c", "raise SystemExit(2)"])]
    result = run_checks(repo_path, base, ids, checks_dir, evidence, "attempt-01")
    root = tmp_path / "records-root"
    for check in result.checks:
        save_check(check, root, "run-01")
    loaded = load_checks(root, "run-01")
    assert sorted(c.check_id for c in loaded) == ["bad-v1", "ok-v1"]
    assert {c.check_id: c.verdict for c in loaded} == {"ok-v1": "pass", "bad-v1": "fail"}
    assert all(isinstance(v, str) for c in loaded for v in c.evidence.values())


def test_repository_checks_directory_definitions_are_valid():
    checks_dir = Path(__file__).resolve().parent.parent / "checks"
    for path in sorted(checks_dir.glob("*.json")):
        definition = load_check_definition(checks_dir, path.stem)
        assert definition.check_id == path.stem
        for item in definition.command:
            assert not Path(item).is_absolute(), "checks/ must not contain absolute paths"
