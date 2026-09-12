"""프로세스 실행기 (W2). Windows 대상.

자식 프로세스를 정리된 환경에서 시작하고, stdout/stderr 를 바이너리 파일로 수집하고,
프로세스 트리 전체를 종료하고, 나중에 같은 프로세스인지 대조할 식별 정보를 남긴다.

이 모듈은 기록 구조(records.py)를 모른다. snapshot() 과 StopResult 는 평범한 dict/dataclass 이며
저장은 호출자가 한다. 확신할 수 없는 상태는 예외가 아니라 "unknown" 으로 돌려준다.
호출자가 그 시도를 unknown 으로 기록하고 자동 재실행을 멈추는 것이 목적이다.

프로세스 열거는 PowerShell 의 Win32_Process 를 한 번 호출해 전체 목록을 받고 파이썬에서
부모-자식 관계를 따라간다. 죽은 프로세스의 PID 는 재사용될 수 있으므로 자손 판정과 잔존
확인에는 항상 OS 가 보고한 시작 시각(CreationDate)을 함께 본다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- 상수 (테스트가 직접 검증한다)

DEFAULT_STRIP_ENV: tuple[str, ...] = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY")
DEFAULT_ENV_EXTRA: dict[str, str] = {"PYTHONUTF8": "1"}

ARGV_HASH_ALGORITHM = "sha256"
ARGV_HASH_SEPARATOR = "\x00"
ARGV_HASH_LENGTH = 16

KILL_SETTLE_SECONDS = 3.0          # taskkill 뒤 잔존 확인까지 기다리는 시간
IDENTITY_TOLERANCE_SECONDS = 2.0   # os_start_time 이 없을 때 started_at 과 OS 시작 시각의 허용 차이
CREATION_MATCH_TOLERANCE_SECONDS = 0.01  # 같은 프로세스로 볼 CreationDate 차이 (ms 단위 기록의 반올림 여유)

_PS_QUERY = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process{filter} | "
    "Select-Object {fields} | ConvertTo-Json -Compress"
)
_PS_FIELDS = "ProcessId,ParentProcessId,Name,CreationDate"
_PS_COMMAND_PREFIX = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
_ENUMERATION_TIMEOUT = 60.0
_TASKKILL_TIMEOUT = 30.0
_DATE_RE = re.compile(r"/Date\((-?\d+)\)/")
_FRACTION_RE = re.compile(r"(\.\d+)(?=[+-Z]|$)")


# ---------------------------------------------------------------- 자료형

class ProcessEnumerationError(RuntimeError):
    """프로세스 목록을 얻지 못했다. 호출자는 상태를 unknown 으로 다뤄야 한다."""


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    parent_pid: int
    name: str
    created_at: float | None  # epoch 초(UTC). OS 가 주지 않으면 None
    command_line: str | None = None


@dataclass
class StopResult:
    state: str                       # "stopped" | "unknown"
    exit_code: int | None
    killed_pids: list[int] = field(default_factory=list)
    survivor_pids: list[int] = field(default_factory=list)
    method: str = ""                 # 실제로 밟은 단계. 예: "already_exited", "ctrl_break", "ctrl_break+taskkill"
    checked_at: str = ""             # ISO 8601 UTC
    detail: str | None = None        # unknown 사유 등 짧은 설명. 원문 로그는 넣지 않는다

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- 유틸리티

def utc_now_iso() -> str:
    return _iso(datetime.now(timezone.utc))


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(text: str) -> datetime:
    """ISO 8601 을 읽는다. Python 3.10 의 fromisoformat 은 소수 초를 3자리나 6자리만 받으므로
    PowerShell 7 이 내는 7자리(100ns) 등은 6자리로 맞춘다."""
    text = text.strip().replace("Z", "+00:00")
    text = _FRACTION_RE.sub(lambda m: (m.group(1) + "000000")[:7], text, count=1)
    return datetime.fromisoformat(text)


def epoch_to_iso(seconds: float) -> str:
    return _iso(datetime.fromtimestamp(seconds, tz=timezone.utc))


def argv_hash(argv: list[str]) -> str:
    """argv 를 NUL 로 이어붙여 sha256 한 뒤 앞 16자. argv 자체는 어디에도 남기지 않는다."""
    joined = ARGV_HASH_SEPARATOR.join(argv).encode("utf-8", "surrogatepass")
    return hashlib.new(ARGV_HASH_ALGORITHM, joined).hexdigest()[:ARGV_HASH_LENGTH]


def _parse_creation_date(value: object) -> float | None:
    """PowerShell 5.1 은 "/Date(밀리초)/", PowerShell 7 은 ISO 문자열을 준다. 둘 다 받는다."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000.0
    if isinstance(value, str):
        match = _DATE_RE.search(value)
        if match:
            return int(match.group(1)) / 1000.0
        try:
            return _parse_iso(value).timestamp()
        except ValueError:
            return None
    return None


def _same_creation(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= CREATION_MATCH_TOLERANCE_SECONDS


# ---------------------------------------------------------------- 프로세스 열거

def _run_powershell(query: str) -> str:
    """PowerShell 을 한 번 실행해 stdout 문자열을 돌려준다. 실패는 ProcessEnumerationError."""
    try:
        completed = subprocess.run(
            _PS_COMMAND_PREFIX + [query],
            capture_output=True, timeout=_ENUMERATION_TIMEOUT, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProcessEnumerationError(f"PowerShell 실행 실패: {type(error).__name__}") from error
    if completed.returncode != 0:
        raise ProcessEnumerationError(f"PowerShell 종료 코드 {completed.returncode}")
    return completed.stdout.decode("utf-8", "replace")


def _parse_process_json(text: str, *, with_command_line: bool = False) -> list[ProcInfo]:
    text = text.strip().lstrip("﻿")
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProcessEnumerationError("프로세스 목록 JSON 을 해석할 수 없다") from error
    if isinstance(data, dict):          # 결과가 하나면 ConvertTo-Json 이 배열로 감싸지 않는다
        data = [data]
    if not isinstance(data, list):
        raise ProcessEnumerationError("프로세스 목록 형식이 예상과 다르다")
    result = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ProcessEnumerationError("프로세스 항목 형식이 예상과 다르다")
        pid, ppid = entry.get("ProcessId"), entry.get("ParentProcessId")
        if not isinstance(pid, int) or not isinstance(ppid, int):
            raise ProcessEnumerationError("프로세스 항목에 PID 가 없다")
        command_line = entry.get("CommandLine") if with_command_line else None
        result.append(ProcInfo(
            pid=pid, parent_pid=ppid, name=str(entry.get("Name") or ""),
            created_at=_parse_creation_date(entry.get("CreationDate")),
            command_line=command_line if isinstance(command_line, str) else None,
        ))
    return result


def list_processes() -> list[ProcInfo]:
    """시스템의 전체 프로세스 목록. 한 번의 PowerShell 호출."""
    return _parse_process_json(_run_powershell(_PS_QUERY.format(filter="", fields=_PS_FIELDS)))


def query_process(pid: int, *, with_command_line: bool = False) -> ProcInfo | None:
    """PID 하나만 조회한다. 없으면 None. 열거 실패는 예외."""
    fields = _PS_FIELDS + (",CommandLine" if with_command_line else "")
    query = _PS_QUERY.format(filter=f' -Filter "ProcessId = {int(pid)}"', fields=fields)
    found = _parse_process_json(_run_powershell(query), with_command_line=with_command_line)
    return found[0] if found else None


def process_tree(pid: int, processes: list[ProcInfo] | None = None) -> list[ProcInfo]:
    """pid 자신과 자손 전체. pid 가 없으면 빈 목록.

    PID 재사용 방지: 시작 시각이 부모보다 이른 프로세스는 자손으로 보지 않는다
    (그 PID 의 진짜 부모는 이미 죽었고 번호만 다시 쓰인 것이다). 시작 시각을 모르는 쪽이
    있으면 관계를 부정할 근거가 없으므로 자손으로 포함한다.
    processes 를 주면 그 목록을 쓰고(테스트용), 없으면 지금 열거한다.
    """
    if processes is None:
        processes = list_processes()
    by_pid = {p.pid: p for p in processes}
    root = by_pid.get(pid)
    if root is None:
        return []
    children: dict[int, list[ProcInfo]] = {}
    for proc in processes:
        children.setdefault(proc.parent_pid, []).append(proc)
    result, visited, queue = [], {pid}, [root]
    while queue:
        parent = queue.pop(0)
        result.append(parent)
        for child in children.get(parent.pid, []):
            if child.pid in visited:
                continue
            if (child.created_at is not None and parent.created_at is not None
                    and child.created_at < parent.created_at - CREATION_MATCH_TOLERANCE_SECONDS):
                continue  # 부모보다 먼저 태어난 "자식" 은 재사용된 PID 다
            visited.add(child.pid)
            queue.append(child)
    return result


def _taskkill_tree(pid: int) -> tuple[bool, str]:
    """taskkill /PID <pid> /T /F. (성공 여부, 짧은 설명)."""
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            capture_output=True, timeout=_TASKKILL_TIMEOUT, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"taskkill 실행 실패: {type(error).__name__}"
    if completed.returncode == 0:
        return True, "taskkill ok"
    # 128: 대상 프로세스를 찾지 못함(이미 종료). 최종 판정은 뒤의 잔존 확인이 한다.
    return False, f"taskkill 종료 코드 {completed.returncode}"


# ---------------------------------------------------------------- 시작

def build_env(env_extra: dict[str, str] | None = None,
              strip_env: list[str] | None = None) -> tuple[dict[str, str], list[str]]:
    """os.environ 복사본에서 strip_env 를 제거하고 env_extra 를 덮어쓴다.

    (환경, 실제로 제거된 변수 이름 목록) 을 돌려준다. 값은 어디에도 남기지 않는다.
    현재 프로세스의 os.environ 은 바꾸지 않는다.
    """
    env = dict(os.environ)
    names = list(DEFAULT_STRIP_ENV if strip_env is None else strip_env)
    stripped = []
    for name in names:
        present = [key for key in env if key.upper() == name.upper()]  # Windows 는 대소문자 구분 없음
        for key in present:
            del env[key]
        if present:
            stripped.append(name)
    env.update(DEFAULT_ENV_EXTRA)
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})
    return env, stripped


def start(argv: list[str], cwd: Path, stdout_path: Path, stderr_path: Path,
          stdin_path: Path | None = None, env_extra: dict[str, str] | None = None,
          strip_env: list[str] | None = None) -> "ProcessHandle":
    """자식 프로세스를 시작한다. shell 없이 argv 그대로, 새 프로세스 그룹으로."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv 는 비어 있지 않은 str 리스트여야 한다")
    env, stripped = build_env(env_extra, strip_env)
    cwd = Path(cwd)
    stdout_path, stderr_path = Path(stdout_path), Path(stderr_path)
    if stdout_path.resolve() == stderr_path.resolve():
        raise ValueError("stdout_path 와 stderr_path 는 서로 다른 파일이어야 한다")

    stdin_file = None
    out = err = None
    try:
        out = stdout_path.open("wb")
        err = stderr_path.open("wb")
        stdin_file = Path(stdin_path).open("rb") if stdin_path is not None else None
        started_at = datetime.now(timezone.utc)
        popen = subprocess.Popen(
            argv, cwd=str(cwd), env=env, shell=False,
            stdin=stdin_file if stdin_file is not None else subprocess.DEVNULL,
            stdout=out, stderr=err,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    finally:
        # 자식은 핸들을 이미 복제해 가졌다. 부모 쪽 사본은 닫아 파일을 잡고 있지 않게 한다.
        for handle in (out, err, stdin_file):
            if handle is not None:
                handle.close()

    os_start = None
    try:
        info = query_process(popen.pid)
        if info is not None and info.created_at is not None:
            os_start = epoch_to_iso(info.created_at)
    except ProcessEnumerationError:
        os_start = None  # 얻지 못하면 null. start 자체를 실패시키지 않는다.

    return ProcessHandle(popen, argv, cwd, stdout_path, stderr_path, stripped,
                         _iso(started_at), os_start)


# ---------------------------------------------------------------- 핸들

class ProcessHandle:
    def __init__(self, popen: subprocess.Popen, argv: list[str], cwd: Path,
                 stdout_path: Path, stderr_path: Path, stripped_env: list[str],
                 started_at: str, os_start_time: str | None):
        self._popen = popen
        self.pid: int = popen.pid
        self.started_at: str = started_at
        self.os_start_time: str | None = os_start_time
        self.argv_hash: str = argv_hash(argv)
        self.argv_len: int = len(argv)
        self.cwd: Path = cwd
        self.stdout_path: Path = stdout_path
        self.stderr_path: Path = stderr_path
        self.stripped_env: list[str] = list(stripped_env)
        self.state: str = "running"
        self.exit_code: int | None = None

    # -- 상태
    def poll(self) -> int | None:
        code = self._popen.poll()
        if code is not None:
            self.exit_code = code
            if self.state == "running":
                self.state = "exited"
        return code

    def wait(self, timeout: float) -> int:
        """시간 안에 끝나지 않으면 TimeoutError. 프로세스는 그대로 둔다."""
        try:
            code = self._popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"pid {self.pid} 가 {timeout}초 안에 끝나지 않았다") from None
        self.exit_code = code
        if self.state == "running":
            self.state = "exited"
        return code

    def snapshot(self) -> dict:
        """process.json 내용. argv 자체는 넣지 않는다(경로·프롬프트가 섞일 수 있다)."""
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "os_start_time": self.os_start_time,
            "argv_hash": self.argv_hash,
            "argv_len": self.argv_len,
            "cwd": str(self.cwd),
            "stripped_env": list(self.stripped_env),
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
        }

    # -- 종료
    def stop(self, grace_seconds: float = 0) -> StopResult:
        """트리 전체를 종료하고 잔존을 확인한다. 확인할 수 없으면 unknown 을 *반환* 한다."""
        steps: list[str] = []
        detail: str | None = None

        if self.poll() is not None:
            self.state = "stopped"
            return StopResult("stopped", self.exit_code, [], [], "already_exited", utc_now_iso())

        # 1. 종료 전 트리. 이것이 잔존 확인의 대상이다.
        try:
            before = process_tree(self.pid)
        except ProcessEnumerationError as error:
            before = None
            detail = f"종료 전 열거 실패: {error}"

        # 2. 정상 종료 시도 (프로세스 그룹에 CTRL_BREAK_EVENT)
        forced = False
        if grace_seconds > 0:
            try:
                self._popen.send_signal(signal.CTRL_BREAK_EVENT)
                steps.append("ctrl_break")
            except OSError as error:
                steps.append("ctrl_break_failed")
                detail = detail or f"CTRL_BREAK 전송 실패: {type(error).__name__}"
            try:
                self._popen.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                pass

        # 3. 강제 종료
        if self._popen.poll() is None:
            forced = True
            ok, note = _taskkill_tree(self.pid)
            steps.append("taskkill" if ok else "taskkill_failed")
            if not ok:
                detail = detail or note

        # 4. 잔존 확인. 정상 종료로 부모가 죽었어도 자손이 남았을 수 있으므로 항상 확인한다.
        time.sleep(KILL_SETTLE_SECONDS)
        checked_at = utc_now_iso()
        try:
            self._popen.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass
        code = self._popen.poll()
        exit_code = None if forced else code
        if code is not None:
            self.exit_code = code

        if before is None:
            self.state = "unknown"
            return StopResult("unknown", exit_code, [], [], "+".join(steps), checked_at, detail)
        try:
            after = {p.pid: p for p in list_processes()}
        except ProcessEnumerationError as error:
            self.state = "unknown"
            return StopResult("unknown", exit_code, [], [p.pid for p in before],
                              "+".join(steps), checked_at, f"종료 후 열거 실패: {error}")

        survivors, killed = [], []
        for proc in before:
            now = after.get(proc.pid)
            # 같은 PID 가 있어도 시작 시각이 다르면 재사용된 PID 이므로 잔존이 아니다.
            # 시작 시각을 모르면 잔존을 부정할 근거가 없으므로 잔존으로 본다(보수적).
            if now is not None and (_same_creation(now.created_at, proc.created_at)
                                    or now.created_at is None or proc.created_at is None):
                survivors.append(proc.pid)
            else:
                killed.append(proc.pid)

        if survivors or code is None:
            self.state = "unknown"
            return StopResult("unknown", exit_code, killed, survivors, "+".join(steps), checked_at,
                              detail or ("잔존 프로세스 있음" if survivors else "부모 종료 미확인"))
        self.state = "stopped"
        return StopResult("stopped", exit_code, killed, [], "+".join(steps), checked_at, detail)


# ---------------------------------------------------------------- 식별

def _split_command_line(command_line: str) -> list[str]:
    """Windows 규칙(CommandLineToArgvW)으로 명령줄을 argv 로 되돌린다."""
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    argc = ctypes.c_int(0)
    pointer = shell32.CommandLineToArgvW(command_line, ctypes.byref(argc))
    if not pointer:
        raise OSError(ctypes.get_last_error(), "CommandLineToArgvW 실패")
    try:
        return [pointer[i] for i in range(argc.value)]
    finally:
        kernel32.LocalFree(pointer)


def identity_matches(snapshot: dict, pid: int) -> bool:
    """지금 pid 로 도는 프로세스가 snapshot 의 그 프로세스인지 판정한다.

    PID 만 같다고 True 를 주지 않는다. 판정 순서:
      1. snapshot["pid"] == pid 가 아니면 False. pid 가 지금 없으면 False.
      2. 양쪽에 os_start_time 이 있으면 같아야 한다. 없으면 started_at 과 OS 시작 시각의 차이가
         IDENTITY_TOLERANCE_SECONDS 이내여야 한다.
      3. OS 가 명령줄을 보여 주면 argv 로 되돌려 해시해 argv_hash 와 비교한다. 다르면 False.
         명령줄을 얻지 못하면(권한 등) 이 단계는 건너뛴다.
    열거 자체가 실패하면 False 가 아니라 ProcessEnumerationError 를 올린다. False 는
    "그 프로세스가 없다" 는 뜻이라 호출자가 새 시도를 시작할 수 있는데, 모르는 상태에서
    그렇게 하면 안 되기 때문이다.
    """
    if not isinstance(snapshot, dict) or snapshot.get("pid") != pid:
        return False
    info = query_process(pid, with_command_line=True)
    if info is None:
        return False

    recorded_os = snapshot.get("os_start_time")
    if recorded_os and info.created_at is not None:
        if not _same_creation(_parse_iso(recorded_os).timestamp(), info.created_at):
            return False
    else:
        started_at = snapshot.get("started_at")
        if not started_at or info.created_at is None:
            return False
        if abs(_parse_iso(started_at).timestamp() - info.created_at) > IDENTITY_TOLERANCE_SECONDS:
            return False

    expected_hash = snapshot.get("argv_hash")
    if info.command_line:
        try:
            actual_hash = argv_hash(_split_command_line(info.command_line))
        except OSError:
            actual_hash = None
        if actual_hash is not None and expected_hash != actual_hash:
            return False
    return True
