"""검증기 실행 (W6). 등록된 검사를 별도 검증 worktree 에서 돌리고 Check 기록을 만든다.

원칙 (01 §8, 04 §3·§7):
- 검사는 작업자의 worktree 가 아니라 **대상 commit 에서 새로 만든 검증 worktree** 에서 돈다.
  작업자가 남긴 미커밋 상태·캐시는 결과에 끼지 않는다.
- 검증 worktree 를 만들 수 없으면 모든 검사가 ``undecidable`` 이다. 예외를 던지지 않는다.
- 산출물(required_artifacts)이 없으면 명령을 돌리지 않고 ``fail`` 이다.
- 시간 초과, 명령을 시작할 수 없음, 범위 검사의 inspection_error 는 ``undecidable`` 이다.
  판단할 수 없는 것을 실패나 통과로 바꾸지 않는다.
- 종료 코드 0 만 ``pass`` 다. 검사 정의는 checks/ 에 있고 작업자는 바꿀 수 없다.
- Check.subject_commit 이 다르면 이전 pass 는 쓸 수 없다(is_check_current).

이 모듈은 상태 전이를 하지 않는다. Check 목록을 돌려주고 저장·판정은 호출자(W7)가 한다.
"""

from __future__ import annotations

import json
import re
import sys
import platform
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import process as proc
from . import workspace
from .records import Check

VERIFIER_VERSION = "verify-1"
CHECK_KINDS = frozenset({"command", "scope"})
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_CACHE_PATTERNS = ("__pycache__/", ".pytest_cache/", "*.pyc")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# reason_code (Check.evidence["reason"] 와 verdict 설명에 쓴다)
REASON_MISSING_ARTIFACT = "missing_artifact"
REASON_NONZERO_EXIT = "nonzero_exit"
REASON_TIMEOUT = "timeout"
REASON_START_FAILED = "start_failed"
REASON_WORKSPACE_UNAVAILABLE = "workspace_unavailable"
REASON_INVALID_DEFINITION = "invalid_check_definition"
REASON_OUT_OF_SCOPE = "out_of_scope_changes"
REASON_INSPECTION_ERROR = "scope_inspection_error"


class CheckDefinitionError(ValueError):
    """checks/<check_id>.json 이 없거나 형식이 틀리다."""


@dataclass(frozen=True)
class CheckDefinition:
    check_id: str
    kind: str                                  # "command" | "scope"
    validator_version: str
    command_id: str
    command: tuple[str, ...] = ()              # kind=command. "{python}", "{worktree}" 자리표시자 허용
    cwd: str = "."                             # worktree 기준 상대 경로
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    required_artifacts: tuple[str, ...] = ()   # worktree 기준 상대 경로. 없으면 fail
    allowed_write_paths: tuple[str, ...] | None = None   # kind=scope. None 이면 run_checks 인수를 쓴다
    cache_patterns: tuple[str, ...] = DEFAULT_CACHE_PATTERNS  # kind=scope. 범위 밖이어도 따로 분류
    description: str = ""


@dataclass
class VerificationRun:
    """run_checks 의 결과. checks 는 save_check 로 그대로 저장할 수 있다."""

    subject_commit: str
    subject_manifest_hash: str | None
    checks: list[Check] = field(default_factory=list)
    worktree_error: str | None = None          # 검증 worktree 를 못 만든 이유. 있으면 전부 undecidable
    evidence_root: str = ""

    def all_passed(self) -> bool:
        return bool(self.checks) and all(c.verdict == "pass" for c in self.checks)


# ---------------------------------------------------------------- 검사 정의

def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise CheckDefinitionError(f"{name} must be a nonempty string")
    if value.startswith("TO_BE_"):
        raise CheckDefinitionError(f"{name} is unresolved")
    return value


def _relative(value: object, name: str) -> str:
    text = _text(value, name).replace("\\", "/")
    parts = PurePosixPath(text).parts
    if text.startswith("/") or ":" in text or ".." in parts:
        raise CheckDefinitionError(f"{name} must be a relative path inside the worktree")
    return text


def load_check_definition(checks_dir: Path, check_id: str) -> CheckDefinition:
    """checks/<check_id>.json 을 읽고 검증한다. 파일 이름과 안의 check_id 가 같아야 한다."""
    if not isinstance(check_id, str) or not _ID_RE.match(check_id):
        raise CheckDefinitionError("invalid check_id")
    path = Path(checks_dir) / f"{check_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise CheckDefinitionError(f"check definition not found: {check_id}") from None
    except (OSError, json.JSONDecodeError) as error:
        raise CheckDefinitionError(f"check definition unreadable: {type(error).__name__}") from None
    if not isinstance(data, dict) or data.get("check_id") != check_id:
        raise CheckDefinitionError("check_id in file does not match its filename")
    kind = data.get("kind", "command")
    if kind not in CHECK_KINDS:
        raise CheckDefinitionError("unsupported check kind")
    validator_version = _text(data.get("validator_version"), "validator_version")
    command_id = _text(data.get("command_id", check_id), "command_id")
    timeout = data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise CheckDefinitionError("timeout_seconds must be a positive number")
    artifacts = data.get("required_artifacts", [])
    if not isinstance(artifacts, list):
        raise CheckDefinitionError("required_artifacts must be a list")
    artifacts = tuple(_relative(a, "required_artifacts item") for a in artifacts)

    command: tuple[str, ...] = ()
    cwd = "."
    allowed = None
    cache = DEFAULT_CACHE_PATTERNS
    if kind == "command":
        raw = data.get("command")
        if not isinstance(raw, list) or not raw:
            raise CheckDefinitionError("command must be a nonempty list")
        command = tuple(_text(item, "command item") for item in raw)
        cwd = _relative(data.get("cwd", "."), "cwd") if data.get("cwd", ".") != "." else "."
    else:
        raw_allowed = data.get("allowed_write_paths")
        if raw_allowed is not None:
            if not isinstance(raw_allowed, list):
                raise CheckDefinitionError("allowed_write_paths must be a list")
            allowed = tuple(_relative(a, "allowed_write_paths item") for a in raw_allowed)
        raw_cache = data.get("cache_patterns")
        if raw_cache is not None:
            if not isinstance(raw_cache, list):
                raise CheckDefinitionError("cache_patterns must be a list")
            cache = tuple(_text(c, "cache_patterns item") for c in raw_cache)
    description = data.get("description", "")
    if not isinstance(description, str):
        raise CheckDefinitionError("description must be a string")
    return CheckDefinition(check_id, kind, validator_version, command_id, command, cwd,
                           float(timeout), artifacts, allowed, cache, description)


# ---------------------------------------------------------------- 유틸리티

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _expand(argument: str, worktree: Path) -> str:
    return argument.replace("{python}", sys.executable).replace("{worktree}", str(worktree))


def _is_cache_path(path: str, patterns: tuple[str, ...]) -> bool:
    parts = PurePosixPath(path).parts
    for pattern in patterns:
        if pattern.endswith("/"):
            if pattern[:-1] in parts[:-1] or pattern[:-1] in parts:
                return True
        elif PurePosixPath(path).match(pattern):
            return True
    return False


def is_check_current(check: Check, subject_commit: str) -> bool:
    """이 Check 를 subject_commit 의 증거로 쓸 수 있는가. commit 이 다르면 이전 pass 는 무효다."""
    return check.subject_commit == subject_commit and check.verdict == "pass"


def tree_hash(repo: Path, commit: str) -> str:
    """commit 의 tree 해시. 같은 파일 내용이면 같은 값이라 subject_manifest_hash 로 쓴다."""
    return workspace._git(Path(repo), "rev-parse", "--verify", "--end-of-options",
                          f"{commit}^{{tree}}").decode("ascii").strip()


# ---------------------------------------------------------------- 실행

def _undecidable(definition_id: str, attempt_id: str, subject_commit: str, manifest: str,
                 validator_version: str, command_id: str, reason: str, detail: str,
                 evidence_dir: Path, evidence_root: Path) -> Check:
    _write_text(evidence_dir / "reason.txt", f"{reason}\n{detail}\n")
    return Check(definition_id, attempt_id, subject_commit, manifest, validator_version, command_id,
                 None, "undecidable", {"reason": reason, "detail": detail,
                                       "reason_file": _rel(evidence_dir / "reason.txt", evidence_root)})


def run_checks(repo: Path, subject_commit: str, check_ids: list[str], checks_dir: Path,
               evidence_root: Path, attempt_id: str, *, base_commit: str | None = None,
               allowed_write_paths: list[str] | None = None,
               worktree_path: Path | None = None, keep_worktree: bool = False) -> VerificationRun:
    """subject_commit 에서 검증 worktree 를 만들고 check_ids 를 차례로 실행한다. 예외를 던지지 않는다.

    - base_commit / allowed_write_paths: kind=scope 검사가 쓴다(작업의 기준 commit 과 허용 경로).
    - worktree_path: 검증 worktree 위치. 기본은 evidence_root/verify-worktree. 끝나면 지운다(keep_worktree=False).
    - 증거 파일은 evidence_root/<check_id>/ 아래에 남고 Check.evidence 는 그 상대 경로다.
    """
    evidence_root = Path(evidence_root)
    evidence_root.mkdir(parents=True, exist_ok=True)
    result = VerificationRun(subject_commit=subject_commit, subject_manifest_hash=None,
                             evidence_root=str(evidence_root))

    # 정의를 먼저 읽는다. 정의가 틀린 검사는 worktree 와 무관하게 undecidable 이다.
    definitions: dict[str, CheckDefinition | str] = {}
    for check_id in check_ids:
        try:
            definitions[check_id] = load_check_definition(checks_dir, check_id)
        except CheckDefinitionError as error:
            definitions[check_id] = str(error)

    # 대상 commit 을 고정하고 검증 worktree 를 만든다.
    manifest = ""
    worktree: Path | None = None
    try:
        resolved = workspace._commit(workspace._repository(Path(repo)), subject_commit)
        manifest = tree_hash(repo, resolved)
        result.subject_commit = resolved
        target = Path(worktree_path) if worktree_path is not None else evidence_root / "verify-worktree"
        worktree = workspace.create_worktree(repo, resolved, target)
    except workspace.WorkspaceError as error:
        result.worktree_error = f"{type(error).__name__}: {error}"
    result.subject_manifest_hash = manifest or None

    try:
        for check_id in check_ids:
            evidence_dir = evidence_root / check_id
            definition = definitions[check_id]
            safe_id = check_id if isinstance(check_id, str) and _ID_RE.match(check_id) else "invalid-check"
            if isinstance(definition, str):
                result.checks.append(_undecidable(
                    safe_id, attempt_id, result.subject_commit, manifest, "unknown", safe_id,
                    REASON_INVALID_DEFINITION, definition, evidence_root / safe_id, evidence_root))
                continue
            if worktree is None:
                result.checks.append(_undecidable(
                    check_id, attempt_id, result.subject_commit, manifest, definition.validator_version,
                    definition.command_id, REASON_WORKSPACE_UNAVAILABLE, result.worktree_error or "",
                    evidence_dir, evidence_root))
                continue
            if definition.kind == "scope":
                result.checks.append(_run_scope_check(definition, worktree, attempt_id, result.subject_commit,
                                                      manifest, base_commit, allowed_write_paths,
                                                      evidence_dir, evidence_root))
            else:
                result.checks.append(_run_command_check(definition, worktree, attempt_id, result.subject_commit,
                                                        manifest, evidence_dir, evidence_root))
    finally:
        if worktree is not None and not keep_worktree:
            try:
                workspace.remove_worktree(repo, worktree, force=True)   # 검사가 남긴 캐시가 있어도 지운다
            except workspace.WorkspaceError as error:
                _write_text(evidence_root / "worktree-cleanup-error.txt", str(error) + "\n")
    return result


def _missing_artifacts(definition: CheckDefinition, worktree: Path) -> list[str]:
    return [a for a in definition.required_artifacts if not (worktree / a).exists()]


def _run_command_check(definition: CheckDefinition, worktree: Path, attempt_id: str, subject_commit: str,
                       manifest: str, evidence_dir: Path, evidence_root: Path) -> Check:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    common = dict(check_id=definition.check_id, attempt_id=attempt_id, subject_commit=subject_commit,
                  subject_manifest_hash=manifest, validator_version=definition.validator_version,
                  command_id=definition.command_id)

    missing = _missing_artifacts(definition, worktree)
    if missing:
        _write_json(evidence_dir / "missing-artifacts.json", {"missing": missing})
        return Check(**common, exit_code=None, verdict="fail",
                     evidence={"reason": REASON_MISSING_ARTIFACT,
                               "missing_artifacts": _rel(evidence_dir / "missing-artifacts.json", evidence_root)})

    argv = [_expand(a, worktree) for a in definition.command]
    cwd = worktree / definition.cwd
    stdout_path, stderr_path = evidence_dir / "stdout.bin", evidence_dir / "stderr.bin"
    started = _now()
    try:
        handle = proc.start(argv, cwd, stdout_path, stderr_path)
    except (OSError, ValueError) as error:
        _write_json(evidence_dir / "command.json", {"argv": argv, "cwd": str(cwd), "started_at": started,
                                                    "error": f"{type(error).__name__}: {error}"})
        return Check(**common, exit_code=None, verdict="undecidable",
                     evidence={"reason": REASON_START_FAILED, "detail": type(error).__name__,
                               "command": _rel(evidence_dir / "command.json", evidence_root)})

    stop_result = None
    try:
        exit_code: int | None = handle.wait(definition.timeout_seconds)
    except TimeoutError:
        exit_code = None
        stop_result = handle.stop().to_dict()

    ended = _now()
    _write_json(evidence_dir / "command.json", {
        "argv": argv, "cwd": str(cwd), "started_at": started, "ended_at": ended,
        "exit_code": exit_code, "timeout_seconds": definition.timeout_seconds,
        "process": handle.snapshot(), "stop_result": stop_result})
    _write_json(evidence_dir / "environment.json", {
        "verifier_version": VERIFIER_VERSION, "validator_version": definition.validator_version,
        "python": sys.version.split()[0], "platform": platform.platform(),
        "stripped_env": handle.snapshot()["stripped_env"], "worktree": str(worktree)})
    evidence = {"stdout_file": _rel(stdout_path, evidence_root), "stderr_file": _rel(stderr_path, evidence_root),
                "command": _rel(evidence_dir / "command.json", evidence_root),
                "environment_manifest": _rel(evidence_dir / "environment.json", evidence_root)}

    if exit_code is None:
        evidence["reason"] = REASON_TIMEOUT
        evidence["stop_state"] = (stop_result or {}).get("state", "")
        return Check(**common, exit_code=None, verdict="undecidable", evidence=evidence)
    if exit_code != 0:
        evidence["reason"] = REASON_NONZERO_EXIT
        return Check(**common, exit_code=exit_code, verdict="fail", evidence=evidence)
    return Check(**common, exit_code=0, verdict="pass", evidence=evidence)


def _run_scope_check(definition: CheckDefinition, worktree: Path, attempt_id: str, subject_commit: str,
                     manifest: str, base_commit: str | None, allowed_write_paths: list[str] | None,
                     evidence_dir: Path, evidence_root: Path) -> Check:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    common = dict(check_id=definition.check_id, attempt_id=attempt_id, subject_commit=subject_commit,
                  subject_manifest_hash=manifest, validator_version=definition.validator_version,
                  command_id=definition.command_id)
    allowed = list(definition.allowed_write_paths) if definition.allowed_write_paths is not None \
        else list(allowed_write_paths or [])
    if base_commit is None:
        return _undecidable(definition.check_id, attempt_id, subject_commit, manifest,
                            definition.validator_version, definition.command_id,
                            REASON_INSPECTION_ERROR, "base_commit is required for scope checks",
                            evidence_dir, evidence_root)

    changes = workspace.detect_out_of_scope_changes(worktree, base_commit, allowed)
    cache_paths = sorted(p for p in changes if _is_cache_path(p, definition.cache_patterns))
    violations = sorted(p for p in changes if p not in cache_paths)
    report = {"base_commit": base_commit, "allowed_write_paths": allowed, "out_of_scope": violations,
              "cache_paths_ignored": cache_paths, "inspection_error": changes.inspection_error}
    _write_json(evidence_dir / "scope.json", report)
    evidence = {"scope_report": _rel(evidence_dir / "scope.json", evidence_root)}

    # U1: inspection_error 를 먼저 본다. 빈 리스트라도 검사 불가면 pass 가 아니다.
    if changes.inspection_error:
        evidence["reason"] = REASON_INSPECTION_ERROR
        evidence["detail"] = changes.inspection_error
        return Check(**common, exit_code=None, verdict="undecidable", evidence=evidence)
    if violations:
        evidence["reason"] = REASON_OUT_OF_SCOPE
        evidence["out_of_scope_count"] = str(len(violations))
        return Check(**common, exit_code=None, verdict="fail", evidence=evidence)
    return Check(**common, exit_code=0, verdict="pass", evidence=evidence)
