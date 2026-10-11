"""검증기 실행 (W6). 등록된 검사를 별도 검증 worktree 에서 돌리고 Check 기록을 만든다.

원칙 (01 §8, 04 §3·§7):
- 검사는 작업자의 worktree 가 아니라 **대상 commit 에서 새로 만든 검증 worktree** 에서 돈다.
  작업자가 남긴 미커밋 상태·캐시는 결과에 끼지 않는다.
- 검증 worktree 를 만들 수 없으면 모든 검사가 ``undecidable`` 이다. 예외를 던지지 않는다.
- 산출물(required_artifacts)이 없으면 명령을 돌리지 않고 ``fail`` 이다.
- 시간 초과, 명령을 시작할 수 없음, 범위 검사의 inspection_error, 검증기 자체의 오류는 ``undecidable`` 이다.
  판단할 수 없는 것을 실패나 통과로 바꾸지 않는다.
- 종료 코드 0 만 ``pass`` 다. 검사 정의는 checks/ 에 있고 작업자는 바꿀 수 없다.
- 검사가 검증 worktree 의 소스를 바꾸면 그 검사는 ``undecidable`` 이고, 다음 검사는 새 worktree 에서 돈다.
  Check 는 항상 subject_commit 그대로의 내용에 대한 판정이어야 한다.
- 시간 초과 후 프로세스 트리 종료를 확인할 수 없으면(unknown) 남은 검사를 중단하고 worktree 를 남긴다.
- Check.subject_commit 이 다르면 이전 pass 는 쓸 수 없다(is_check_current).

이 모듈은 상태 전이를 하지 않는다. Check 목록을 돌려주고 저장·판정은 호출자(W7)가 한다.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import process as proc
from . import workspace
from .records import Check

VERIFIER_VERSION = "verify-2"
CHECK_KINDS = frozenset({"command", "scope"})
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_CACHE_PATTERNS = ("__pycache__/", ".pytest_cache/", "*.pyc")
EVIDENCE_CHECKS_DIR = "checks"           # evidence_root/checks/<check_id>/
DEFAULT_WORKTREE_DIR = "verify-worktree"  # evidence_root/verify-worktree (증거 폴더와 겹치지 않는다)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Check.evidence["reason"]
REASON_MISSING_ARTIFACT = "missing_artifact"
REASON_NONZERO_EXIT = "nonzero_exit"
REASON_TIMEOUT = "timeout"
REASON_START_FAILED = "start_failed"
REASON_WORKSPACE_UNAVAILABLE = "workspace_unavailable"
REASON_INVALID_DEFINITION = "invalid_check_definition"
REASON_OUT_OF_SCOPE = "out_of_scope_changes"
REASON_INSPECTION_ERROR = "scope_inspection_error"
REASON_SUBJECT_MODIFIED = "subject_modified"          # 검사가 검증 worktree 의 소스를 바꿨다
REASON_ABORTED = "verification_aborted"               # 앞선 검사의 프로세스 상태가 unknown
REASON_VERIFIER_ERROR = "verifier_error"              # 검증기 자체의 예외


class CheckDefinitionError(ValueError):
    """checks/<check_id>.json 이 없거나 형식이 틀리다."""


@dataclass(frozen=True)
class CheckDefinition:
    check_id: str
    kind: str                                  # "command" | "scope"
    validator_version: str
    command_id: str
    command: tuple[str, ...] = ()              # kind=command. "{python}", "{worktree}", "{publish}" 자리표시자 허용
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
    worktree_error: str | None = None      # 검증 worktree 를 못 만든(또는 다시 못 만든) 이유
    aborted_reason: str | None = None      # 남은 검사를 중단한 이유(프로세스 상태 unknown 등)
    worktree_path: str | None = None       # 남겨 둔 검증 worktree. None 이면 지웠다
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


def _string_list(value: object, name: str, convert) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CheckDefinitionError(f"{name} must be a list")
    return tuple(convert(item, f"{name} item") for item in value)


def load_check_definition(checks_dir: Path, check_id: str) -> CheckDefinition:
    """checks/<check_id>.json 을 읽고 검증한다. 어떤 형식 오류도 CheckDefinitionError 로만 나온다."""
    try:
        return _load_check_definition(checks_dir, check_id)
    except CheckDefinitionError:
        raise
    except Exception as error:  # noqa: BLE001 - 정의 파일의 어떤 이상도 예외로 새지 않게 한다
        raise CheckDefinitionError(f"check definition invalid: {type(error).__name__}") from None


def _load_check_definition(checks_dir: Path, check_id: str) -> CheckDefinition:
    if not isinstance(check_id, str) or not _ID_RE.match(check_id):
        raise CheckDefinitionError("invalid check_id")
    path = Path(checks_dir) / f"{check_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise CheckDefinitionError(f"check definition not found: {check_id}") from None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CheckDefinitionError(f"check definition unreadable: {type(error).__name__}") from None
    if not isinstance(data, dict) or data.get("check_id") != check_id:
        raise CheckDefinitionError("check_id in file does not match its filename")
    kind = data.get("kind", "command")
    if not isinstance(kind, str) or kind not in CHECK_KINDS:
        raise CheckDefinitionError("unsupported check kind")
    validator_version = _text(data.get("validator_version"), "validator_version")
    command_id = _text(data.get("command_id", check_id), "command_id")
    timeout = data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise CheckDefinitionError("timeout_seconds must be a positive number")
    artifacts = _string_list(data.get("required_artifacts", []), "required_artifacts", _relative)

    command: tuple[str, ...] = ()
    cwd = "."
    allowed = None
    cache = DEFAULT_CACHE_PATTERNS
    if kind == "command":
        command = _string_list(data.get("command"), "command", _text)
        if not command:
            raise CheckDefinitionError("command must be a nonempty list")
        raw_cwd = data.get("cwd", ".")
        cwd = "." if raw_cwd == "." else _relative(raw_cwd, "cwd")
    else:
        if data.get("allowed_write_paths") is not None:
            allowed = _string_list(data["allowed_write_paths"], "allowed_write_paths", _relative)
        if data.get("cache_patterns") is not None:
            cache = _string_list(data["cache_patterns"], "cache_patterns", _text)
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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
                    encoding="utf-8", newline="\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _try_write_text(path: Path, text: str) -> str | None:
    """증거 기록 실패는 검증을 멈추지 않는다. 실패하면 None 을 돌려주고 호출자가 그 사실을 남긴다."""
    try:
        _write_text(path, text)
        return str(path)
    except OSError:
        return None


def _rel(path: Path, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


PUBLISH_DIR = "publish"   # evidence_dir/publish: 검사가 공개해도 되는 집계 파일만 쓰는 폴더(작업자에게 보이지 않음)
PUBLISHED_FILE = "published.json"   # evidence_dir/published.json: 관리자가 검증해 다시 쓴 봉인 사본
PUBLISH_NAME, PUBLISH_MAX_BYTES = "eval-summary.json", 4096
_HEX64 = re.compile(r"[0-9a-f]{64}")


class PublicationRejected(ValueError):
    """공개 파일이 허용된 집계 형식이 아니다. 메시지는 사유 코드뿐이며 내용을 담지 않는다."""


def _count(value, upper):
    if type(value) is not int or not 0 <= value <= upper:
        raise PublicationRejected("invalid_schema")
    return value


def validate_eval_summary(data) -> dict:
    """eval-summary-1 의 정확한 형식만 받아, 검증한 값으로 새 dict 를 만든다.

    검증 대상 코드는 검증기 프로세스 안에서 실행되므로 공개 폴더를 늦게 고칠 수 있다
    (W13b Codex 검토 F1). 그래서 pass 판정이 아니라 이 함수가 공개 내용을 확정한다.
    키 집합·자료형·범위·hit_at_3 == hits/queries 를 모두 요구하고 문자열은 schema 와
    protocol_hash(64자리 hex)뿐이다.
    """
    if (type(data) is not dict or set(data) != {"schema", "k", "queries", "protocol_hash", "a", "b"}
            or data["schema"] != "eval-summary-1"
            or not (isinstance(data["protocol_hash"], str) and _HEX64.fullmatch(data["protocol_hash"]))):
        raise PublicationRejected("invalid_schema")
    k, queries = _count(data["k"], 100), _count(data["queries"], 100000)
    if k < 1 or queries < 1:
        raise PublicationRejected("invalid_schema")
    result = {"schema": "eval-summary-1", "k": k, "queries": queries, "protocol_hash": data["protocol_hash"]}
    for method in ("a", "b"):
        row = data[method]
        if type(row) is not dict or set(row) != {"hit_at_3", "hits", "no_result_queries"}:
            raise PublicationRejected("invalid_schema")
        hits, empty = _count(row["hits"], queries), _count(row["no_result_queries"], queries)
        if type(row["hit_at_3"]) not in (int, float) or row["hit_at_3"] != hits / queries:
            raise PublicationRejected("invalid_schema")
        result[method] = {"hit_at_3": hits / queries, "hits": hits, "no_result_queries": empty}
    return result


def _seal_publication(publish: Path, evidence_dir: Path, evidence_root: Path, evidence: dict) -> None:
    """검사 프로세스가 끝난 뒤 공개 폴더를 한 번 읽어 검증하고 봉인 사본을 쓴다."""
    try:
        entries = list(publish.iterdir())
        if [p.name for p in entries] != [PUBLISH_NAME] or not entries[0].is_file() or entries[0].is_symlink():
            raise PublicationRejected("unexpected_files")
        raw = entries[0].read_bytes()
        if len(raw) > PUBLISH_MAX_BYTES:
            raise PublicationRejected("too_large")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PublicationRejected("invalid_json") from None
        sealed = json.dumps(validate_eval_summary(data), ensure_ascii=False, sort_keys=True) + "\n"
    except PublicationRejected as error:
        evidence["publish_rejected"] = str(error)
        return
    except OSError:
        evidence["publish_rejected"] = "unreadable"
        return
    target = evidence_dir / PUBLISHED_FILE
    target.write_text(sealed, encoding="utf-8", newline="\n")
    evidence["published"] = _rel(target, evidence_root)
    evidence["published_sha256"] = hashlib.sha256(sealed.encode("utf-8")).hexdigest()


def read_publication(evidence_root: Path, evidence: dict) -> dict:
    """봉인 사본을 다시 읽어 지문과 형식을 확인한다. 맞지 않으면 PublicationRejected."""
    if "published" not in evidence or "published_sha256" not in evidence:
        raise PublicationRejected("not_published")
    try:
        raw = (Path(evidence_root) / evidence["published"]).read_bytes()
    except OSError:
        raise PublicationRejected("unreadable") from None
    if hashlib.sha256(raw).hexdigest() != evidence["published_sha256"]:
        raise PublicationRejected("sealed_copy_changed")
    try:
        return validate_eval_summary(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PublicationRejected("invalid_json") from None


def _expand(argument: str, worktree: Path, publish: Path | None = None) -> str:
    argument = argument.replace("{python}", sys.executable).replace("{worktree}", str(worktree))
    return argument.replace("{publish}", str(publish)) if publish is not None else argument


def _is_cache_path(path: str, patterns: tuple[str, ...]) -> bool:
    parts = PurePosixPath(path).parts
    for pattern in patterns:
        if pattern.endswith("/"):
            if pattern[:-1] in parts:
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


def _worktree_state(worktree: Path, commit: str) -> tuple[bool, bool, str]:
    """(소스가 바뀌었나, 무엇이든 남았나, 설명).

    소스 변경 = HEAD 가 commit 에서 움직였거나 추적 파일이 수정·삭제·스테이지됐다. 이 경우 그 검사의
    결과는 subject_commit 에 대한 판정이 아니다. '무엇이든 남았나' 는 untracked·ignored 까지 포함하며,
    참이면 다음 검사 전에 worktree 를 새로 만든다.
    """
    head = workspace._git(worktree, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    status = workspace._git(worktree, "status", "--porcelain=v1", "-z", "-uall", "--ignored=matching",
                            "--ignore-submodules=none")
    entries = [e for e in status.split(b"\0") if e]
    tracked_changed = [e for e in entries if not e.startswith(b"??") and not e.startswith(b"!!")]
    modified = head != commit or bool(tracked_changed)
    detail = []
    if head != commit:
        detail.append(f"HEAD moved to {head}")
    if tracked_changed:
        detail.append(f"{len(tracked_changed)} tracked path(s) changed")
    if entries and not tracked_changed:
        detail.append(f"{len(entries)} untracked/ignored path(s) left")
    return modified, bool(entries) or head != commit, "; ".join(detail)


# ---------------------------------------------------------------- 실행

def _base_check(definition: CheckDefinition, attempt_id: str, subject_commit: str, manifest: str,
                verdict: str, exit_code: int | None, evidence: dict[str, str]) -> Check:
    return Check(definition.check_id, attempt_id, subject_commit, manifest, definition.validator_version,
                 definition.command_id, exit_code, verdict, evidence)


def _undecidable(definition: CheckDefinition, attempt_id: str, subject_commit: str, manifest: str,
                 reason: str, detail: str, evidence_dir: Path, evidence_root: Path) -> Check:
    evidence = {"reason": reason, "detail": detail}
    written = _try_write_text(evidence_dir / "reason.txt", f"{reason}\n{detail}\n")
    if written is not None:
        evidence["reason_file"] = _rel(evidence_dir / "reason.txt", evidence_root)
    else:
        evidence["evidence_write_error"] = "reason.txt could not be written"
    return _base_check(definition, attempt_id, subject_commit, manifest, "undecidable", None, evidence)


def _placeholder_definition(check_id: object) -> CheckDefinition:
    safe = check_id if isinstance(check_id, str) and _ID_RE.match(check_id) else "invalid-check"
    return CheckDefinition(safe, "command", "unknown", safe)


def run_checks(repo: Path, subject_commit: str, check_ids: list[str], checks_dir: Path,
               evidence_root: Path, attempt_id: str, *, base_commit: str | None = None,
               allowed_write_paths: list[str] | None = None,
               worktree_path: Path | None = None, keep_worktree: bool = False) -> VerificationRun:
    """subject_commit 에서 검증 worktree 를 만들고 check_ids 를 차례로 실행한다. 예외를 던지지 않는다.

    - base_commit / allowed_write_paths: kind=scope 검사가 쓴다(작업의 기준 commit 과 허용 경로).
    - worktree_path: 검증 worktree 위치. 기본 evidence_root/verify-worktree. 증거 폴더(evidence_root/checks)
      안은 거부한다. 끝나면 지운다(keep_worktree=False). 프로세스 상태가 unknown 이면 지우지 않고 경로를 남긴다.
    - 증거는 evidence_root/checks/<check_id>/ 아래에 남고 Check.evidence 는 evidence_root 기준 상대 경로다.
    """
    evidence_root = Path(evidence_root)
    checks_root = evidence_root / EVIDENCE_CHECKS_DIR
    result = VerificationRun(subject_commit=subject_commit, subject_manifest_hash=None,
                             evidence_root=str(evidence_root))
    try:
        checks_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        result.worktree_error = f"evidence_root unusable: {type(error).__name__}"

    definitions: dict[object, CheckDefinition | str] = {}
    for check_id in check_ids:
        try:
            definitions[check_id] = load_check_definition(checks_dir, check_id)
        except CheckDefinitionError as error:
            definitions[check_id] = str(error)

    # 대상 commit 고정, 검증 worktree 위치 결정
    manifest = ""
    resolved = subject_commit
    target = Path(worktree_path) if worktree_path is not None else evidence_root / DEFAULT_WORKTREE_DIR
    if result.worktree_error is None:
        try:
            resolved = workspace._commit(workspace._repository(Path(repo)), subject_commit)
            manifest = tree_hash(repo, resolved)
            result.subject_commit = resolved
            if _is_within(target, checks_root) or _is_within(evidence_root, target):
                raise workspace.WorkspaceError("worktree_path must not overlap the evidence directory")
        except workspace.WorkspaceError as error:
            result.worktree_error = f"{type(error).__name__}: {error}"
    result.subject_manifest_hash = manifest or None

    worktree: Path | None = None

    def make_worktree() -> Path | None:
        try:
            return workspace.create_worktree(repo, resolved, target)
        except workspace.WorkspaceError as error:
            result.worktree_error = f"{type(error).__name__}: {error}"
            return None

    def drop_worktree(path: Path) -> bool:
        try:
            workspace.remove_worktree(repo, path, force=True)   # 검사가 남긴 캐시가 있어도 지운다
            return True
        except workspace.WorkspaceError as error:
            _try_write_text(evidence_root / "worktree-cleanup-error.txt", str(error) + "\n")
            return False

    if result.worktree_error is None:
        worktree = make_worktree()

    keep = keep_worktree
    stale = False   # 앞선 검사가 무엇이든 남겼다. 다음 검사가 실제로 필요할 때 worktree 를 새로 만든다.
    try:
        for check_id in check_ids:
            definition = definitions[check_id]
            if isinstance(definition, str):
                placeholder = _placeholder_definition(check_id)
                result.checks.append(_undecidable(placeholder, attempt_id, result.subject_commit, manifest,
                                                  REASON_INVALID_DEFINITION, definition,
                                                  checks_root / placeholder.check_id, evidence_root))
                continue
            evidence_dir = checks_root / definition.check_id
            if result.aborted_reason is not None:
                result.checks.append(_undecidable(definition, attempt_id, result.subject_commit, manifest,
                                                  REASON_ABORTED, result.aborted_reason, evidence_dir, evidence_root))
                continue
            if worktree is not None and stale:
                # 앞선 검사의 흔적 위에서 돌리지 않는다. 못 만들면 남은 검사는 workspace_unavailable.
                drop_worktree(worktree)
                worktree = make_worktree()
                stale = False
            if worktree is None:
                result.checks.append(_undecidable(definition, attempt_id, result.subject_commit, manifest,
                                                  REASON_WORKSPACE_UNAVAILABLE, result.worktree_error or "",
                                                  evidence_dir, evidence_root))
                continue
            try:
                if definition.kind == "scope":
                    check = _run_scope_check(definition, worktree, attempt_id, result.subject_commit, manifest,
                                             base_commit, allowed_write_paths, evidence_dir, evidence_root)
                else:
                    check = _run_command_check(definition, worktree, attempt_id, result.subject_commit, manifest,
                                               evidence_dir, evidence_root)

                # P1: 프로세스 트리 종료를 확인하지 못했으면 worktree 를 믿을 수 없다. 남은 검사를 중단하고 남긴다.
                if check.evidence.get("stop_state") not in (None, "", "stopped"):
                    result.aborted_reason = (f"check {definition.check_id}: process tree state "
                                             f"{check.evidence['stop_state']} after timeout")
                    keep = True
                    result.checks.append(check)
                    continue

                # P1: 검사가 소스를 바꿨으면 그 결과는 subject_commit 에 대한 판정이 아니다.
                modified, dirty, detail = _worktree_state(worktree, result.subject_commit)
                if modified:
                    check = _base_check(definition, attempt_id, result.subject_commit, manifest, "undecidable", None,
                                        dict(check.evidence, reason=REASON_SUBJECT_MODIFIED, detail=detail,
                                             original_verdict=check.verdict,
                                             original_exit_code=str(check.exit_code)))
                result.checks.append(check)
                stale = stale or dirty
            except Exception as error:  # noqa: BLE001 - 검증기 자체의 오류도 undecidable 로 기록한다
                stale = True   # 어디까지 진행됐는지 모르므로 다음 검사 전에 worktree 를 새로 만든다
                result.checks.append(_undecidable(definition, attempt_id, result.subject_commit, manifest,
                                                  REASON_VERIFIER_ERROR, f"{type(error).__name__}: {error}",
                                                  evidence_dir, evidence_root))
    finally:
        if worktree is not None:
            if keep:
                result.worktree_path = str(worktree)
            elif not drop_worktree(worktree):
                result.worktree_path = str(worktree)
    return result


def _missing_artifacts(definition: CheckDefinition, worktree: Path) -> list[str]:
    return [a for a in definition.required_artifacts if not (worktree / a).exists()]


def _run_command_check(definition: CheckDefinition, worktree: Path, attempt_id: str, subject_commit: str,
                       manifest: str, evidence_dir: Path, evidence_root: Path) -> Check:
    evidence_dir.mkdir(parents=True, exist_ok=True)

    missing = _missing_artifacts(definition, worktree)
    if missing:
        _write_json(evidence_dir / "missing-artifacts.json", {"missing": missing})
        return _base_check(definition, attempt_id, subject_commit, manifest, "fail", None,
                           {"reason": REASON_MISSING_ARTIFACT,
                            "missing_artifacts": _rel(evidence_dir / "missing-artifacts.json", evidence_root)})

    publish = evidence_dir / PUBLISH_DIR
    if any("{publish}" in a for a in definition.command):
        publish.mkdir(exist_ok=True)
    argv = [_expand(a, worktree, publish) for a in definition.command]
    cwd = worktree / definition.cwd
    stdout_path, stderr_path = evidence_dir / "stdout.bin", evidence_dir / "stderr.bin"
    started = _now()
    try:
        handle = proc.start(argv, cwd, stdout_path, stderr_path)
    except (OSError, ValueError) as error:
        _write_json(evidence_dir / "command.json", {"argv": argv, "cwd": str(cwd), "started_at": started,
                                                    "error": f"{type(error).__name__}: {error}"})
        return _base_check(definition, attempt_id, subject_commit, manifest, "undecidable", None,
                           {"reason": REASON_START_FAILED, "detail": type(error).__name__,
                            "command": _rel(evidence_dir / "command.json", evidence_root)})

    stop_result = None
    try:
        exit_code: int | None = handle.wait(definition.timeout_seconds)
    except TimeoutError:
        exit_code = None
        stop_result = handle.stop().to_dict()

    ended = _now()
    snapshot = handle.snapshot()
    _write_json(evidence_dir / "command.json", {
        "argv": argv, "cwd": str(cwd), "started_at": started, "ended_at": ended,
        "exit_code": exit_code, "timeout_seconds": definition.timeout_seconds,
        "process": snapshot, "stop_result": stop_result})
    _write_json(evidence_dir / "environment.json", {
        "verifier_version": VERIFIER_VERSION, "validator_version": definition.validator_version,
        "python": sys.version.split()[0], "platform": platform.platform(),
        "stripped_env": snapshot["stripped_env"], "worktree": str(worktree)})
    evidence = {"stdout_file": _rel(stdout_path, evidence_root), "stderr_file": _rel(stderr_path, evidence_root),
                "command": _rel(evidence_dir / "command.json", evidence_root),
                "environment_manifest": _rel(evidence_dir / "environment.json", evidence_root)}

    if exit_code is None:
        evidence["reason"] = REASON_TIMEOUT
        evidence["stop_state"] = str((stop_result or {}).get("state") or "unknown")
        return _base_check(definition, attempt_id, subject_commit, manifest, "undecidable", None, evidence)
    if exit_code != 0:
        evidence["reason"] = REASON_NONZERO_EXIT
        return _base_check(definition, attempt_id, subject_commit, manifest, "fail", exit_code, evidence)
    # Published files count only for a passing check, and only as a validated, re-serialized
    # copy sealed after the process exited; a failed check's output is never referenced.
    if publish.is_dir() and any(publish.iterdir()):
        _seal_publication(publish, evidence_dir, evidence_root, evidence)
    return _base_check(definition, attempt_id, subject_commit, manifest, "pass", 0, evidence)


def _run_scope_check(definition: CheckDefinition, worktree: Path, attempt_id: str, subject_commit: str,
                     manifest: str, base_commit: str | None, allowed_write_paths: list[str] | None,
                     evidence_dir: Path, evidence_root: Path) -> Check:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    allowed = list(definition.allowed_write_paths) if definition.allowed_write_paths is not None \
        else list(allowed_write_paths or [])
    if base_commit is None:
        return _undecidable(definition, attempt_id, subject_commit, manifest, REASON_INSPECTION_ERROR,
                            "base_commit is required for scope checks", evidence_dir, evidence_root)

    changes = workspace.detect_out_of_scope_changes(worktree, base_commit, allowed)
    cache_paths = sorted(p for p in changes if _is_cache_path(p, definition.cache_patterns))
    violations = sorted(p for p in changes if p not in cache_paths)
    report = {"base_commit": base_commit, "allowed_write_paths": allowed, "out_of_scope": violations,
              "cache_paths_ignored": cache_paths, "inspection_error": changes.inspection_error}
    _write_json(evidence_dir / "scope.json", report)
    evidence = {"scope_report": _rel(evidence_dir / "scope.json", evidence_root)}

    # W5 검토 U1: inspection_error 를 먼저 본다. 빈 리스트라도 검사 불가면 pass 가 아니다.
    if changes.inspection_error:
        evidence["reason"] = REASON_INSPECTION_ERROR
        evidence["detail"] = changes.inspection_error
        return _base_check(definition, attempt_id, subject_commit, manifest, "undecidable", None, evidence)
    if violations:
        evidence["reason"] = REASON_OUT_OF_SCOPE
        evidence["out_of_scope_count"] = str(len(violations))
        return _base_check(definition, attempt_id, subject_commit, manifest, "fail", None, evidence)
    return _base_check(definition, attempt_id, subject_commit, manifest, "pass", 0, evidence)
