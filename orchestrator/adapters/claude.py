"""Claude Code `claude -p` 명령 조립과 출력 정규화 (W4). 프로세스는 띄우지 않는다.

W7 이 route 설정을 ClaudeSpec 으로 풀어 build_command 의 argv 와 **같은 prompt_path** 를
W2 의 stdin_path 로 넘긴다. 프롬프트는 argv 에 넣지 않는다(`claude -p` 는 위치 인수가 없으면
stdin 을 프롬프트로 읽는다). `--bare` 는 구독 인증을 읽지 않으므로 절대 넣지 않는다.

파서는 저장소의 CLI 2.1.268 fixture(`tests/fixtures/claude/`)를 대상으로 한다. `--output-format json`
은 실행이 끝날 때 JSON 객체 하나를 한 번에 쓰므로, 강제 종료된 실행은 **빈 파일** 을 남긴다.
빈 출력은 파싱 실패가 아니라 "incomplete" 다(08 §4.2). 성공과 실패의 필드 구조가 같으므로
`result` 문자열이 아니라 `is_error` 와 `terminal_reason` 으로 판정한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .base import AttemptResult, ProcessControl

PERMISSION_MODES = frozenset({"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"})
EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
FORBIDDEN_FLAGS = frozenset({"--bare"})
USAGE_COUNTERS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")

# reason_code
REASON_NOT_LOGGED_IN = "account_not_logged_in"   # is_error + "Not logged in"
REASON_API_ERROR = "api_error"                   # is_error + terminal_reason api_error (다른 문구)
REASON_CLI_ERROR = "cli_error"                   # is_error, 그 밖
REASON_NO_OUTPUT = "no_output"                   # 빈 파일: 중지 가능성. 실패로 단정하지 않는다


@dataclass(frozen=True)
class ClaudeSpec:
    """route 설정을 풀어 놓은 실행 설정. 모델·effort 는 전체 ID·정확한 단계 문자열로."""

    model: str
    effort: str | None = None
    permission_mode: str = "acceptEdits"
    allowed_tools: tuple[str, ...] = ()
    add_dirs: tuple[Path, ...] = ()
    json_schema_path: Path | None = None       # 파일을 읽어 인라인 JSON 으로 넘긴다
    max_budget_usd: float | None = None
    max_turns: int | None = None               # 2.1.268 에는 --max-turns 가 없다. 기본 미출력
    session_persistence: bool = True           # False 면 --no-session-persistence


class ResultParseError(ValueError):
    """출력 파일을 해석할 수 없다. 메시지에 원문은 넣지 않는다."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{field} must be a nonempty string without NUL")
    if value.startswith("TO_BE_"):
        raise ValueError(f"{field} is unresolved")
    if value.startswith("-"):
        raise ValueError(f"{field} must not look like a flag")
    return value


def _schema_argument(path: Path, workspace: Path) -> str:
    path = Path(path)
    path = path if path.is_absolute() else workspace / path
    try:
        schema = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"json_schema_path is not a readable JSON file: {type(error).__name__}") from None
    if not isinstance(schema, dict):
        raise ValueError("json_schema_path must contain a JSON object")
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


class ClaudeAdapter:
    def __init__(self, claude_path: Path):
        self.claude_path = Path(claude_path).resolve()

    def capabilities(self) -> frozenset[str]:
        """지원하는 동작. 설치·로그인 여부 조회가 아니다. 실제 권한은 permission_mode 와 allowed_tools 가 정한다."""
        return frozenset({"read_files", "edit_files", "run_tests", "json_result",
                          "output_schema", "stdin_prompt", "subagent_stats"})

    def build_command(self, spec: ClaudeSpec, workspace: Path, prompt_path: Path) -> list[str]:
        """argv 만 돌려준다. W2 가 prompt_path 를 stdin 으로 열고 shell=False 로 시작한다.

        프롬프트 파일은 존재만 확인하고 읽지 않는다. 가변 인수 옵션(--add-dir, --allowedTools)은
        뒤따르는 인수를 모두 삼키므로 argv 맨 끝에 둔다.
        """
        if not Path(prompt_path).is_file():
            raise ValueError("prompt_path must be an existing file")
        model = _text(spec.model, "model")
        if spec.permission_mode not in PERMISSION_MODES:
            raise ValueError("Unsupported permission_mode")
        workspace = Path(workspace).resolve()

        argv = [str(self.claude_path), "-p", "--output-format", "json", "--model", model,
                "--permission-mode", spec.permission_mode]
        if spec.effort is not None:
            effort = _text(spec.effort, "effort")
            if effort not in EFFORT_LEVELS:
                raise ValueError("Unsupported effort")
            argv += ["--effort", effort]
        if spec.max_budget_usd is not None:
            if isinstance(spec.max_budget_usd, bool) or not isinstance(spec.max_budget_usd, (int, float)) \
                    or spec.max_budget_usd <= 0:
                raise ValueError("max_budget_usd must be a positive number")
            argv += ["--max-budget-usd", repr(float(spec.max_budget_usd))]
        if spec.max_turns is not None:
            if isinstance(spec.max_turns, bool) or not isinstance(spec.max_turns, int) or spec.max_turns <= 0:
                raise ValueError("max_turns must be a positive int")
            argv += ["--max-turns", str(spec.max_turns)]
        if spec.json_schema_path is not None:
            argv += ["--json-schema", _schema_argument(spec.json_schema_path, workspace)]
        if not spec.session_persistence:
            argv.append("--no-session-persistence")
        if spec.add_dirs:
            argv.append("--add-dir")
            for directory in spec.add_dirs:
                directory = Path(directory)
                argv.append(str((directory if directory.is_absolute() else workspace / directory).resolve()))
        if spec.allowed_tools:
            argv.append("--allowedTools")
            argv += [_text(tool, "allowed_tools item") for tool in spec.allowed_tools]

        forbidden = FORBIDDEN_FLAGS.intersection(argv)
        if forbidden:
            raise ValueError("Forbidden flag in argv")
        return argv

    def parse_events(self, path: Path) -> AttemptResult:
        return parse_result(path)

    def stop(self, process: ProcessControl, grace_seconds: float = 3.0) -> object:
        return process.stop(grace_seconds=grace_seconds)


def parse_result(path: Path) -> AttemptResult:
    """`--output-format json` 의 stdout 파일 하나를 AttemptResult 로 정규화한다.

    빈 파일 → incomplete / no_output (중지 가능성, 08 §4.2). 잘못된 JSON → ResultParseError.
    is_error → failed, reason_code 는 로그인 실패 / api_error / cli_error 로 구분.
    is_error 가 아니어도 terminal_reason 이 "completed" 가 아니면 completed 로 보지 않는다.
    """
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig", "strict") if raw else ""
    result = AttemptResult()
    if not text.strip():
        result.status = "incomplete"
        result.reason_code = REASON_NO_OUTPUT
        return result
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ResultParseError("Output is not a single JSON object") from None
    if not isinstance(data, dict) or data.get("type") != "result":
        raise ResultParseError("Output is not a claude result object")

    session_id = data.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        raise ResultParseError("Invalid session_id")
    result.thread_id = session_id

    message = data.get("result")
    if message is not None and not isinstance(message, str):
        raise ResultParseError("Invalid result text")
    result.final_message = message

    usage = data.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise ResultParseError("Invalid usage")
        for key in USAGE_COUNTERS:
            if key in usage:
                value = usage[key]
                if type(value) is not int or value < 0:
                    raise ResultParseError("Invalid usage counter")
                result.usage[key] = value

    stats = data.get("subagent_stats")
    if stats is not None:
        if not isinstance(stats, dict):
            raise ResultParseError("Invalid subagent_stats")
        spawned = stats.get("spawned")
        if spawned is not None:
            if type(spawned) is not int or spawned < 0:
                raise ResultParseError("Invalid subagent_stats.spawned")
            result.subagents_spawned = spawned

    cost = data.get("total_cost_usd")
    if cost is not None:
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
            raise ResultParseError("Invalid total_cost_usd")
        result.cost_usd_reported = float(cost)

    is_error = data.get("is_error")
    if not isinstance(is_error, bool):
        raise ResultParseError("Missing is_error")
    terminal = data.get("terminal_reason")
    if terminal is not None and not isinstance(terminal, str):
        raise ResultParseError("Invalid terminal_reason")

    if is_error:
        result.status = "failed"
        if message and "not logged in" in message.lower():
            result.reason_code = REASON_NOT_LOGGED_IN
        elif terminal == "api_error":
            result.reason_code = REASON_API_ERROR
        else:
            result.reason_code = REASON_CLI_ERROR
    elif terminal == "completed":
        result.status = "completed"
    else:
        result.status = "incomplete"
        result.reason_code = terminal or "missing_terminal_reason"
    return result
