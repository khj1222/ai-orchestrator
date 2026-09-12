"""W6 회귀 검사 — Codex 검토가 45a10d9 에서 재현한 결함 4건.

P1 검사가 소스를 바꾸면 후속 검사가 원래 commit 의 pass 로 기록됨 → 소스 변경 검사는 undecidable, 다음 검사는 새 worktree.
P1 시간 초과 후 종료 상태 unknown 인데 계속 실행하고 worktree 삭제 → 남은 검사 중단, worktree 보존.
P2 잘못된 정의·증거 저장 오류가 예외로 전파 → 모두 undecidable 로 기록.
P2 검사 ID 가 verify-worktree 면 정리 때 증거 삭제 → 증거는 evidence_root/checks/ 아래, 경로 겹침 거부.
"""

import json
import subprocess
from pathlib import Path

import pytest

from orchestrator import verify, workspace
from orchestrator.verify import (
    REASON_ABORTED,
    REASON_INVALID_DEFINITION,
    REASON_SUBJECT_MODIFIED,
    REASON_TIMEOUT,
    REASON_VERIFIER_ERROR,
    REASON_WORKSPACE_UNAVAILABLE,
    CheckDefinitionError,
    load_check_definition,
    run_checks,
)

PY = "{python}"


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


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "subject repo"
    (repo / "methods").mkdir(parents=True)
    git(repo, "init", "--initial-branch=main")
    (repo / "methods/a.py").write_text("A = 1\n", encoding="utf-8")
    (repo / "outside.txt").write_text("base\n", encoding="utf-8")
    return repo, commit(repo, "base")


@pytest.fixture
def checks_dir(tmp_path):
    d = tmp_path / "checks"
    d.mkdir()
    return d


@pytest.fixture
def evidence(tmp_path):
    return tmp_path / "evidence"


CONTENT_PROBE = ("import pathlib,sys; "
                 "sys.exit(0 if pathlib.Path('methods/a.py').read_text().strip()=='A = 1' else 3)")


# ---------------------------------------------------------------- P1: 소스 변경

def test_source_modifying_check_is_undecidable_and_next_check_runs_on_fresh_worktree(repo, checks_dir, evidence):
    repo_path, base = repo
    tamper = define(checks_dir, "tamper-v1", command=[
        PY, "-c", "import pathlib; pathlib.Path('methods/a.py').write_text('A = 999')"])
    content = define(checks_dir, "content-v1", command=[PY, "-c", CONTENT_PROBE])
    result = run_checks(repo_path, base, [tamper, content, tamper, content], checks_dir, evidence, "a")
    assert [(c.check_id, c.verdict) for c in result.checks] == [
        ("tamper-v1", "undecidable"), ("content-v1", "pass"), ("tamper-v1", "undecidable"), ("content-v1", "pass")]
    first = result.checks[0]
    assert first.evidence["reason"] == REASON_SUBJECT_MODIFIED
    assert first.evidence["original_verdict"] == "pass"
    assert "tracked path" in first.evidence["detail"]
    assert first.exit_code is None
    assert result.worktree_error is None and result.worktree_path is None
    assert git(repo_path, "status", "--porcelain").stdout == b""
    assert not (evidence / "verify-worktree").exists()


def test_head_moving_check_is_undecidable(repo, checks_dir, evidence):
    repo_path, base = repo
    (repo_path / "outside.txt").write_text("v2\n", encoding="utf-8")
    later = commit(repo_path, "later")
    mover = define(checks_dir, "mover-v1", command=["git", "-c", "advice.detachedHead=false", "checkout", "--quiet", later])
    ok = define(checks_dir, "ok-v1", command=[
        PY, "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('outside.txt').read_text().strip()=='base' else 3)"])
    result = run_checks(repo_path, base, [mover, ok], checks_dir, evidence, "a")
    assert [c.verdict for c in result.checks] == ["undecidable", "pass"]
    assert result.checks[0].evidence["reason"] == REASON_SUBJECT_MODIFIED
    assert "HEAD moved" in result.checks[0].evidence["detail"]


def test_untracked_leftovers_keep_verdict_but_next_check_gets_fresh_worktree(repo, checks_dir, evidence):
    repo_path, base = repo
    leaver = define(checks_dir, "leaver-v1", command=[PY, "-c", "open('leftover.txt', 'w').close()"])
    probe = define(checks_dir, "probe-v1", command=[
        PY, "-c", "import pathlib,sys; sys.exit(6 if pathlib.Path('leftover.txt').exists() else 0)"])
    result = run_checks(repo_path, base, [leaver, probe], checks_dir, evidence, "a")
    assert [c.verdict for c in result.checks] == ["pass", "pass"]
    assert "original_verdict" not in result.checks[0].evidence


# ---------------------------------------------------------------- P1: 종료 상태 unknown

def test_unknown_stop_state_aborts_remaining_checks_and_keeps_worktree(repo, checks_dir, evidence, monkeypatch):
    monkeypatch.setattr(verify.proc, "KILL_SETTLE_SECONDS", 1.0)
    repo_path, base = repo
    slow = define(checks_dir, "slow-v1", command=[PY, "-c", "import time; time.sleep(120)"], timeout_seconds=1)
    ok = define(checks_dir, "ok-v1", command=[PY, "-c", "pass"])
    scope = define(checks_dir, "scope-v1", kind="scope")
    original_stop = verify.proc.ProcessHandle.stop

    def unknown_stop(self, grace_seconds=0):
        real = original_stop(self, grace_seconds)     # 프로세스는 실제로 죽인다(테스트 정리용). 보고만 unknown 으로.
        real.state = "unknown"
        real.survivor_pids = [self.pid]
        return real

    monkeypatch.setattr(verify.proc.ProcessHandle, "stop", unknown_stop)
    result = run_checks(repo_path, base, [slow, ok, scope], checks_dir, evidence, "a", base_commit=base)
    assert [c.verdict for c in result.checks] == ["undecidable", "undecidable", "undecidable"]
    assert result.checks[0].evidence["reason"] == REASON_TIMEOUT
    assert result.checks[0].evidence["stop_state"] == "unknown"
    assert result.checks[1].evidence["reason"] == REASON_ABORTED
    assert result.checks[2].evidence["reason"] == REASON_ABORTED
    assert result.aborted_reason and "unknown" in result.aborted_reason
    assert result.worktree_path and Path(result.worktree_path).is_dir()          # 지우지 않고 남긴다
    assert not (evidence / "checks" / "ok-v1" / "stdout.bin").exists()          # 두 번째 검사는 실행되지 않았다
    workspace.remove_worktree(repo_path, Path(result.worktree_path), force=True)


# ---------------------------------------------------------------- P2: 예외 전파

@pytest.mark.parametrize("fields", [
    {"kind": []}, {"kind": {"x": 1}}, {"command": [None]}, {"command": [PY], "required_artifacts": [1]},
    {"command": [PY], "cwd": 5}, {"kind": "scope", "cache_patterns": [{}]}, {"command": [PY], "timeout_seconds": "1"},
    {"command": [PY], "validator_version": ["1"]}, {"command": [PY], "description": 3},
    {"command": [PY], "required_artifacts": "methods"},
])
def test_odd_definition_shapes_never_raise_from_run_checks(repo, checks_dir, evidence, fields):
    repo_path, base = repo
    define(checks_dir, "odd-v1", **fields)
    with pytest.raises(CheckDefinitionError):
        load_check_definition(checks_dir, "odd-v1")
    ok = define(checks_dir, "ok-v1", command=[PY, "-c", "pass"])
    result = run_checks(repo_path, base, ["odd-v1", ok, None, 42], checks_dir, evidence, "a")
    assert [c.verdict for c in result.checks] == ["undecidable", "pass", "undecidable", "undecidable"]
    assert result.checks[0].evidence["reason"] == REASON_INVALID_DEFINITION
    assert result.checks[2].check_id == result.checks[3].check_id == "invalid-check"


def test_evidence_write_failure_is_recorded_not_raised(repo, checks_dir, evidence, monkeypatch):
    repo_path, base = repo
    ok = define(checks_dir, "ok-v1", command=[PY, "-c", "pass"])
    other = define(checks_dir, "other-v1", command=[PY, "-c", "pass"])
    original = verify._write_json
    calls = {"n": 0}

    def flaky(path, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return original(path, payload)

    monkeypatch.setattr(verify, "_write_json", flaky)
    result = run_checks(repo_path, base, [ok, other], checks_dir, evidence, "a")
    assert [c.verdict for c in result.checks] == ["undecidable", "pass"]
    assert result.checks[0].evidence["reason"] == REASON_VERIFIER_ERROR
    assert "OSError" in result.checks[0].evidence["detail"]
    assert result.worktree_path is None
    assert git(repo_path, "worktree", "list", "--porcelain").stdout.count(b"worktree ") == 1


def test_reason_file_write_failure_still_returns_undecidable(repo, checks_dir, evidence, monkeypatch):
    repo_path, base = repo
    (repo_path / "outside.txt").write_text("dirty\n", encoding="utf-8")   # worktree 생성 불가
    ok = define(checks_dir, "ok-v1", command=[PY, "-c", "pass"])

    def broken_write(path, text):
        raise OSError("read-only evidence")

    monkeypatch.setattr(verify, "_write_text", broken_write)
    result = run_checks(repo_path, base, [ok], checks_dir, evidence, "a")
    assert result.checks[0].verdict == "undecidable"
    assert result.checks[0].evidence["reason"] == REASON_WORKSPACE_UNAVAILABLE
    assert "evidence_write_error" in result.checks[0].evidence
    git(repo_path, "checkout", "--", "outside.txt")


# ---------------------------------------------------------------- P2: 경로 겹침

def test_check_ids_colliding_with_worktree_dir_keep_their_evidence(repo, checks_dir, evidence):
    repo_path, base = repo
    ids = [define(checks_dir, "verify-worktree", command=[PY, "-c", "print('kept')"]),
           define(checks_dir, "checks", command=[PY, "-c", "print('kept too')"])]
    result = run_checks(repo_path, base, ids, checks_dir, evidence, "a")
    assert [c.verdict for c in result.checks] == ["pass", "pass"]
    for check in result.checks:
        assert b"kept" in (evidence / check.evidence["stdout_file"]).read_bytes()
    assert not (evidence / "verify-worktree").exists()
    assert (evidence / "checks" / "verify-worktree" / "stdout.bin").is_file()
    assert (evidence / "checks" / "checks" / "stdout.bin").is_file()


def test_worktree_path_overlapping_evidence_is_rejected(repo, checks_dir, evidence):
    repo_path, base = repo
    ok = define(checks_dir, "ok-v1", command=[PY, "-c", "pass"])
    result = run_checks(repo_path, base, [ok], checks_dir, evidence, "a",
                        worktree_path=evidence / "checks" / "ok-v1")
    assert result.worktree_error and "overlap" in result.worktree_error
    assert result.checks[0].verdict == "undecidable"
    assert result.checks[0].evidence["reason"] == REASON_WORKSPACE_UNAVAILABLE
    result = run_checks(repo_path, base, [ok], checks_dir, evidence / "deep" / "evidence", "a",
                        worktree_path=evidence / "deep")
    assert result.worktree_error and "overlap" in result.worktree_error
    assert git(repo_path, "worktree", "list", "--porcelain").stdout.count(b"worktree ") == 1
