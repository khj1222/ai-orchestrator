"""W2 프로세스 실행기 검사. 실제 자식 프로세스를 띄우고 죽인다. Windows 전용.

실행: python -m pytest tests/test_process.py -v
더미 스크립트는 tests/fixtures/process/ 에 있고 sys.executable 로 실행한다.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator import process as proc
from orchestrator.process import (
    ARGV_HASH_ALGORITHM,
    ARGV_HASH_LENGTH,
    ARGV_HASH_SEPARATOR,
    DEFAULT_STRIP_ENV,
    ProcInfo,
    ProcessEnumerationError,
    argv_hash,
    build_env,
    identity_matches,
    list_processes,
    process_tree,
    start,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows 전용 모듈")

FIXTURES = Path(__file__).parent / "fixtures" / "process"
TREE = FIXTURES / "tree_sleeper.py"
ECHO = FIXTURES / "echo_env.py"
SLEEPER = FIXTURES / "sleeper.py"


# ---------------------------------------------------------------- 도우미

def _kill_tree_hard(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)


@pytest.fixture
def launcher(tmp_path):
    """start() 를 감싸 시작한 프로세스를 테스트 뒤 반드시 정리한다(실패·중단 포함)."""
    handles = []
    extra_pids = []

    def _start(argv, **kwargs):
        kwargs.setdefault("cwd", tmp_path)
        n = len(handles)
        kwargs.setdefault("stdout_path", tmp_path / f"out{n}.bin")
        kwargs.setdefault("stderr_path", tmp_path / f"err{n}.bin")
        handle = start(argv, **kwargs)
        handles.append(handle)
        return handle

    _start.extra_pids = extra_pids
    yield _start
    for handle in handles:
        if handle.poll() is None:
            _kill_tree_hard(handle.pid)
    for pid in extra_pids:          # 부모가 먼저 죽어 고아가 된 손자까지
        _kill_tree_hard(pid)


def _wait_for_lines(path: Path, count: int, timeout: float = 30.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = path.read_bytes().decode("utf-8", "replace").splitlines()
        if len(lines) >= count:
            return lines
        time.sleep(0.1)
    raise AssertionError(f"{path} 에 {count}줄이 {timeout}초 안에 나오지 않았다: {lines}")


def _alive(pid: int) -> ProcInfo | None:
    return next((p for p in list_processes() if p.pid == pid), None)


def _python(script: Path, *args: str) -> list[str]:
    return [sys.executable, str(script), *args]


# ---------------------------------------------------------------- 10. argv_hash

def test_argv_hash_follows_documented_scheme():
    argv = [sys.executable, "-c", "print(1)", "한글 인수"]
    expected = hashlib.new(ARGV_HASH_ALGORITHM,
                           ARGV_HASH_SEPARATOR.join(argv).encode("utf-8")).hexdigest()[:ARGV_HASH_LENGTH]
    assert argv_hash(argv) == expected
    assert len(argv_hash(argv)) == ARGV_HASH_LENGTH == 16
    assert ARGV_HASH_SEPARATOR == "\x00"


def test_argv_hash_depends_on_content_and_is_stable():
    a = ["python", "x.py", "--flag"]
    assert argv_hash(a) == argv_hash(list(a))
    assert argv_hash(a) != argv_hash(["python", "x.py", "--other"])
    assert argv_hash(a) != argv_hash(["python", "x.py --flag"])   # 인수 경계가 다르면 다른 해시
    assert argv_hash(a) != argv_hash(["python", "--flag", "x.py"])


# ---------------------------------------------------------------- 1·2. 환경 정리

def test_strip_env_removes_secrets_and_records_names_only(launcher, monkeypatch, tmp_path):
    secret = "sk-test-SECRET-VALUE-9f1c2d3e"
    for name in DEFAULT_STRIP_ENV:
        monkeypatch.setenv(name, secret + name)
    before = dict(os.environ)

    handle = launcher(_python(ECHO, *DEFAULT_STRIP_ENV, "PATH"))
    assert handle.wait(30) == 0
    out = handle.stdout_path.read_bytes().decode()
    for name in DEFAULT_STRIP_ENV:
        assert f"ENV {name} unset" in out
    assert "ENV PATH set" in out                        # 나머지 환경은 그대로 상속

    snap = handle.snapshot()
    assert sorted(snap["stripped_env"]) == sorted(DEFAULT_STRIP_ENV)
    serialized = json.dumps(snap, ensure_ascii=False)
    assert secret not in serialized
    assert secret not in out and secret not in handle.stderr_path.read_bytes().decode()
    assert dict(os.environ) == before                   # 현재 프로세스 환경은 변하지 않았다
    for name in DEFAULT_STRIP_ENV:
        assert os.environ[name] == secret + name


def test_strip_env_only_records_variables_that_existed(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    env, stripped = build_env(strip_env=["OPENAI_API_KEY", "ANTHROPIC_API_KEY"])
    assert stripped == ["ANTHROPIC_API_KEY"]
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert env["PYTHONUTF8"] == "1"
    assert "ANTHROPIC_API_KEY" in os.environ


def test_pythonutf8_default_and_env_extra_override(launcher):
    handle = launcher(_python(ECHO))
    assert handle.wait(30) == 0
    assert "PYTHONUTF8 1" in handle.stdout_path.read_bytes().decode()

    handle = launcher(_python(ECHO, "CUSTOM_FLAG"), env_extra={"PYTHONUTF8": "0", "CUSTOM_FLAG": "yes"})
    assert handle.wait(30) == 0
    out = handle.stdout_path.read_bytes().decode()
    assert "PYTHONUTF8 0" in out
    assert "ENV CUSTOM_FLAG set" in out


# ---------------------------------------------------------------- 3·4·5. 출력과 stdin

def test_stdout_and_stderr_are_separate_binary_files(launcher):
    handle = launcher(_python(ECHO))
    assert handle.wait(30) == 0
    out, err = handle.stdout_path.read_bytes(), handle.stderr_path.read_bytes()
    assert b"ERR line 1\nERR line 2\n" == err
    assert b"ERR" not in out
    assert out.startswith(b"PYTHONUTF8 ")
    assert b"STDIN" in out and b"STDIN" not in err
    assert b"\r\n" not in out                            # 바이너리 그대로: 줄바꿈 변환 없음


def test_start_rejects_same_file_for_both_streams(tmp_path):
    with pytest.raises(ValueError):
        start(_python(ECHO), tmp_path, tmp_path / "same.bin", tmp_path / "same.bin")


def test_closed_stdin_gives_immediate_eof(launcher):
    started = time.monotonic()
    handle = launcher(_python(ECHO))
    assert handle.wait(30) == 0
    assert time.monotonic() - started < 25
    assert b"STDIN 0 \n" in handle.stdout_path.read_bytes()


def test_stdin_path_content_is_delivered(launcher, tmp_path):
    prompt = tmp_path / "prompt.txt"
    payload = "프롬프트 첫 줄\nsecond line\n".encode("utf-8")
    prompt.write_bytes(payload)
    handle = launcher(_python(ECHO), stdin_path=prompt)
    assert handle.wait(30) == 0
    assert b"STDIN " + str(len(payload)).encode() + b" " + payload in handle.stdout_path.read_bytes()


# ---------------------------------------------------------------- 6. wait timeout

def test_wait_timeout_raises_and_leaves_process_alive(launcher):
    handle = launcher(_python(SLEEPER, "300"))
    with pytest.raises(TimeoutError):
        handle.wait(1.0)
    assert handle.poll() is None
    assert handle.state == "running"
    assert _alive(handle.pid) is not None


# ---------------------------------------------------------------- 7. 트리 종료

def test_stop_kills_three_generations(launcher):
    handle = launcher(_python(TREE))
    lines = _wait_for_lines(handle.stdout_path, 3)
    generations = {line.split()[0]: int(line.split()[1]) for line in lines}
    assert set(generations) == {"gen0", "gen1", "gen2"}
    launcher.extra_pids.extend(pid for pid in generations.values() if pid != handle.pid)

    # 3대가 모두 살아 있고 트리 열거가 셋을 다 본다.
    tree = process_tree(handle.pid)
    tree_pids = {p.pid for p in tree}
    # venv 의 python.exe 는 실제 인터프리터를 자식으로 띄우는 실행기다(W15a). gen0 은 시작한
    # 프로세스 자신이거나 그 직계 자식이어야 한다.
    parent_of = {p.pid: p.parent_pid for p in tree}
    assert generations["gen0"] == handle.pid or parent_of.get(generations["gen0"]) == handle.pid
    assert {generations["gen0"], generations["gen1"], generations["gen2"]} <= tree_pids
    grandchild_before = _alive(generations["gen2"])
    assert grandchild_before is not None

    result = handle.stop()
    assert result.state == "stopped", result
    assert result.survivor_pids == []
    assert generations["gen2"] in result.killed_pids
    assert "taskkill" in result.method
    assert handle.state == "stopped"
    assert handle.poll() is not None

    # 손자가 실제로 사라졌다 (같은 PID 가 다시 쓰였다면 시작 시각이 다르다).
    grandchild_after = _alive(generations["gen2"])
    assert grandchild_after is None or not proc._same_creation(
        grandchild_after.created_at, grandchild_before.created_at)


def test_stop_with_grace_terminates_and_verifies(launcher):
    handle = launcher(_python(SLEEPER, "300"))
    time.sleep(0.5)
    result = handle.stop(grace_seconds=3.0)
    assert result.state == "stopped", result
    assert result.survivor_pids == []
    assert result.method in {"ctrl_break", "ctrl_break+taskkill", "ctrl_break_failed+taskkill"}
    assert handle.poll() is not None
    if result.method == "ctrl_break":
        assert result.exit_code is not None          # 정상 종료 경로에서는 종료 코드를 남긴다
    else:
        assert result.exit_code is None              # 강제 종료의 종료 코드는 의미가 없다


# ---------------------------------------------------------------- 8. 이미 끝난 프로세스

def test_stop_on_exited_process_is_stopped_without_error(launcher):
    handle = launcher(_python(ECHO))
    code = handle.wait(30)
    result = handle.stop()
    assert result.state == "stopped"
    assert result.method == "already_exited"
    assert result.exit_code == code == 0
    assert result.survivor_pids == [] and result.killed_pids == []


# ---------------------------------------------------------------- 9. 식별

def test_identity_matches_true_for_live_process_and_false_for_tampered_copies(launcher):
    handle = launcher(_python(SLEEPER, "300"))
    snap = handle.snapshot()
    assert snap["os_start_time"] is not None
    assert set(snap) == {"pid", "started_at", "os_start_time", "argv_hash", "argv_len", "cwd",
                         "stripped_env", "stdout_path", "stderr_path"}
    assert snap["argv_len"] == 3 and "sleeper.py" not in json.dumps(snap)   # argv 는 저장하지 않는다

    assert identity_matches(snap, handle.pid) is True
    assert identity_matches(dict(snap, argv_hash="0" * 16), handle.pid) is False
    assert identity_matches(dict(snap, os_start_time="2000-01-01T00:00:00.000Z"), handle.pid) is False
    assert identity_matches(dict(snap, pid=handle.pid + 1), handle.pid) is False
    # os_start_time 이 없으면 started_at 으로 판정한다.
    assert identity_matches(dict(snap, os_start_time=None), handle.pid) is True
    assert identity_matches(dict(snap, os_start_time=None, started_at="2000-01-01T00:00:00.000Z"),
                            handle.pid) is False


def test_identity_matches_false_for_missing_pid(launcher):
    handle = launcher(_python(ECHO))
    handle.wait(30)
    snap = handle.snapshot()
    time.sleep(0.2)
    # 방금 죽은 PID. 재사용됐더라도 시작 시각이 다르므로 False 여야 한다.
    assert identity_matches(snap, handle.pid) is False
    assert identity_matches(snap, 4_000_000_000) is False   # 있을 수 없는 PID


def test_identity_matches_propagates_enumeration_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise ProcessEnumerationError("test")
    monkeypatch.setattr(proc, "_run_powershell", boom)
    with pytest.raises(ProcessEnumerationError):
        identity_matches({"pid": 1234, "started_at": "2026-01-01T00:00:00.000Z",
                          "os_start_time": None, "argv_hash": "x"}, 1234)


# ---------------------------------------------------------------- 트리 열거와 PID 재사용 (오프라인)

def test_process_tree_filters_reused_pids_and_cycles():
    t0 = 1_000_000.0
    procs = [
        ProcInfo(100, 1, "root.exe", t0),
        ProcInfo(200, 100, "child.exe", t0 + 1),
        ProcInfo(300, 200, "grandchild.exe", t0 + 2),
        ProcInfo(400, 100, "stale.exe", t0 - 50),          # 부모보다 먼저 태어남 = 재사용된 PID
        ProcInfo(500, 400, "stale-child.exe", t0 + 3),     # 재사용 PID 의 자손도 따라가지 않는다
        ProcInfo(600, 100, "unknown-time.exe", None),      # 시각 모름 = 포함
        ProcInfo(100, 300, "cycle.exe", t0),               # 순환: 무시
        ProcInfo(700, 999, "unrelated.exe", t0),
    ]
    pids = [p.pid for p in process_tree(100, procs)]
    assert pids[0] == 100
    assert set(pids) == {100, 200, 300, 600}
    assert process_tree(12345, procs) == []


def test_parse_process_json_handles_powershell_shapes():
    text = '﻿[{"ProcessId":1,"ParentProcessId":0,"Name":"a","CreationDate":"\\/Date(1789163980182)\\/"},' \
           '{"ProcessId":2,"ParentProcessId":1,"Name":"b","CreationDate":"2026-09-12T10:00:00.5+09:00"},' \
           '{"ProcessId":3,"ParentProcessId":1,"Name":null,"CreationDate":null}]'
    parsed = proc._parse_process_json(text)
    assert parsed[0].created_at == 1789163980.182
    expected = datetime(2026, 9, 12, 1, 0, 0, 500000, tzinfo=timezone.utc).timestamp()
    assert abs(parsed[1].created_at - expected) < 1e-6
    seven_digits = proc._parse_creation_date("2026-09-12T10:00:00.1234567+09:00")
    assert abs(seven_digits - (expected - 0.5 + 0.123456)) < 1e-5
    assert parsed[2].created_at is None and parsed[2].name == ""
    single = proc._parse_process_json('{"ProcessId":7,"ParentProcessId":1,"Name":"x","CreationDate":null}')
    assert [p.pid for p in single] == [7]
    assert proc._parse_process_json("") == []
    with pytest.raises(ProcessEnumerationError):
        proc._parse_process_json("not json")
    with pytest.raises(ProcessEnumerationError):
        proc._parse_process_json('[{"Name":"no pid"}]')


def test_list_processes_includes_this_process():
    me = next((p for p in list_processes() if p.pid == os.getpid()), None)
    assert me is not None
    assert me.created_at is not None
    assert me.parent_pid != 0


def test_enumeration_failure_is_not_swallowed(monkeypatch):
    def boom(*args, **kwargs):
        raise ProcessEnumerationError("test")
    monkeypatch.setattr(proc, "_run_powershell", boom)
    with pytest.raises(ProcessEnumerationError):
        list_processes()
    with pytest.raises(ProcessEnumerationError):
        process_tree(os.getpid())


def test_stop_returns_unknown_when_verification_impossible(launcher, monkeypatch):
    handle = launcher(_python(SLEEPER, "300"))
    real_list = proc.list_processes
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] >= 2:                     # 종료 전 열거는 성공, 종료 후 열거는 실패
            raise ProcessEnumerationError("simulated")
        return real_list()

    monkeypatch.setattr(proc, "list_processes", flaky)
    monkeypatch.setattr(proc, "KILL_SETTLE_SECONDS", 0.5)
    result = handle.stop()                      # 예외가 아니라 unknown 을 돌려준다
    assert result.state == "unknown"
    assert handle.pid in result.survivor_pids   # 확인 못 한 PID 는 잔존 후보로 남긴다
    assert "열거 실패" in (result.detail or "")
    assert handle.state == "unknown"
    assert handle.poll() is not None            # 실제로는 죽었지만 확인하지 못했다고 보고한다
