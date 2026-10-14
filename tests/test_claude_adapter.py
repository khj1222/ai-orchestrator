"""W4 오프라인 검사. 실제 CLI 는 호출하지 않고 tests/fixtures/claude/ 의 G0 출력으로만 검증한다."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from orchestrator.adapters.base import AttemptResult
from orchestrator.adapters.claude import (
    REASON_API_ERROR,
    REASON_CLI_ERROR,
    REASON_NO_OUTPUT,
    REASON_NOT_LOGGED_IN,
    ClaudeAdapter,
    ClaudeSpec,
    ResultParseError,
    parse_result,
)

FIXTURES = Path(__file__).parent / "fixtures" / "claude"


def write_result(tmp_path, data, name="out.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data,
                    encoding="utf-8")
    return path


def base_result(**changes):
    data = json.loads((FIXTURES / "print-ok.json").read_text(encoding="utf-8"))
    data.update(changes)
    return data


# ---------------------------------------------------------------- fixture 파싱

def test_real_success_fixture():
    result = ClaudeAdapter(Path("claude.exe")).parse_events(FIXTURES / "print-ok.json")
    assert result.thread_id == "03799c1f-0334-48e9-b16b-242775a876f2"
    assert result.status == "completed" and result.reason_code is None
    assert result.final_message == "OK"
    assert result.usage == {"input_tokens": 2, "cache_creation_input_tokens": 7259,
                            "cache_read_input_tokens": 15445, "output_tokens": 4}
    assert result.subagents_spawned == 0
    assert result.cost_usd_reported == pytest.approx(0.0804225)
    assert result.command_executions == []


def test_real_cached_fixture_same_shape_lower_cost():
    result = parse_result(FIXTURES / "print-ok-cached.json")
    assert result.status == "completed"
    assert result.usage["cache_creation_input_tokens"] == 0
    assert result.usage["cache_read_input_tokens"] == 22704
    assert result.cost_usd_reported < parse_result(FIXTURES / "print-ok.json").cost_usd_reported


def test_real_not_logged_in_fixture_is_account_error():
    result = parse_result(FIXTURES / "print-not-logged-in.json")
    assert result.status == "failed"
    assert result.reason_code == REASON_NOT_LOGGED_IN
    assert result.thread_id == "9f975147-7787-40be-8582-6a6a89015183"
    assert "Not logged in" in result.final_message
    assert result.usage == {"input_tokens": 0, "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0, "output_tokens": 0}
    assert result.subagents_spawned == 0


def test_success_and_failure_are_not_distinguished_by_result_text(tmp_path):
    # 같은 필드 구조. result 에 "OK" 가 있어도 is_error 면 실패다.
    result = parse_result(write_result(tmp_path, base_result(is_error=True, terminal_reason="api_error",
                                                             result="OK")))
    assert result.status == "failed" and result.reason_code == REASON_API_ERROR
    # is_error 가 아니어도 terminal_reason 이 completed 가 아니면 완료로 보지 않는다.
    result = parse_result(write_result(tmp_path, base_result(terminal_reason="max_budget")))
    assert result.status == "incomplete" and result.reason_code == "max_budget"


@pytest.mark.parametrize("changes,reason", [
    ({"is_error": True, "terminal_reason": "api_error", "result": "Not logged in · Please run /login"},
     REASON_NOT_LOGGED_IN),
    ({"is_error": True, "terminal_reason": "api_error", "result": "not LOGGED in"}, REASON_NOT_LOGGED_IN),
    ({"is_error": True, "terminal_reason": "api_error", "result": "Rate limited"}, REASON_API_ERROR),
    ({"is_error": True, "terminal_reason": "completed", "result": "something"}, REASON_CLI_ERROR),
    ({"is_error": True, "terminal_reason": None, "result": None}, REASON_CLI_ERROR),
])
def test_error_classification(tmp_path, changes, reason):
    result = parse_result(write_result(tmp_path, base_result(**changes)))
    assert result.status == "failed" and result.reason_code == reason


def test_empty_output_is_incomplete_not_failure(tmp_path):
    # 강제 종료된 claude -p 는 stdout 을 전혀 남기지 않는다(08 §4.2).
    path = tmp_path / "empty.json"
    path.write_bytes(b"")
    result = parse_result(path)
    assert result == AttemptResult(status="incomplete", reason_code=REASON_NO_OUTPUT)
    path.write_bytes(b"  \n")
    assert parse_result(path).reason_code == REASON_NO_OUTPUT


@pytest.mark.parametrize("payload", [
    "not json", "[]", '{"type":"assistant"}', '{"type":"result"}',                 # is_error 없음
    '{"type":"result","is_error":"false"}', '{"type":"result","is_error":false,"usage":[]}',
    '{"type":"result","is_error":false,"usage":{"output_tokens":-1}}',
    '{"type":"result","is_error":false,"usage":{"output_tokens":true}}',
    '{"type":"result","is_error":false,"session_id":""}',
    '{"type":"result","is_error":false,"subagent_stats":{"spawned":"0"}}',
    '{"type":"result","is_error":false,"total_cost_usd":"0"}',
    '{"type":"result","is_error":false}{"type":"result","is_error":false}',
])
def test_corrupt_output_raises(tmp_path, payload):
    with pytest.raises(ResultParseError):
        parse_result(write_result(tmp_path, payload))


def test_parse_error_does_not_echo_raw_content(tmp_path):
    with pytest.raises(ResultParseError) as error:
        parse_result(write_result(tmp_path, "private-token-abc not json"))
    assert "private-token" not in str(error.value)


def test_usage_copies_only_known_counters(tmp_path):
    data = base_result()
    data["usage"]["private"] = 42
    result = parse_result(write_result(tmp_path, data))
    assert "private" not in result.usage
    assert set(result.usage) == {"input_tokens", "cache_creation_input_tokens",
                                 "cache_read_input_tokens", "output_tokens"}


def test_bom_and_missing_optional_fields(tmp_path):
    path = tmp_path / "bom.json"
    path.write_bytes("﻿".encode("utf-8") + b'{"type":"result","is_error":false,"terminal_reason":"completed"}')
    result = parse_result(path)
    assert result.status == "completed"
    assert result.thread_id is None and result.final_message is None
    assert result.usage == {} and result.subagents_spawned is None and result.cost_usd_reported is None


# ---------------------------------------------------------------- 명령 조립

@pytest.fixture
def command_inputs(tmp_path):
    workspace = tmp_path / "작업 공간"
    workspace.mkdir()
    prompt = tmp_path / "prompt with spaces.txt"
    prompt.write_text('프롬프트 "quoted"; --bare $(x)\n둘째 줄', encoding="utf-8")
    spec = ClaudeSpec(model="claude-opus-5", effort="high")
    adapter = ClaudeAdapter(tmp_path / "CLI path" / "claude.exe")
    return adapter, spec, workspace, prompt


def test_build_command_minimal_and_stdin_contract(command_inputs):
    adapter, spec, workspace, prompt = command_inputs
    argv = adapter.build_command(spec, workspace, prompt)
    assert argv == [str(adapter.claude_path), "-p", "--output-format", "json", "--model", "claude-opus-5",
                    "--permission-mode", "acceptEdits", "--effort", "high", "--safe-mode"]
    assert prompt.read_text(encoding="utf-8") not in argv
    assert str(prompt) not in argv
    assert "--bare" not in argv
    assert not any(arg.startswith("--max-turns") for arg in argv)   # 2.1.268 에는 없는 옵션. 기본 미출력


def test_build_command_all_options_variadic_last(command_inputs, tmp_path):
    adapter, spec, workspace, prompt = command_inputs
    schema = workspace / "schema file.json"
    schema.write_text('{\n  "type": "object",\n  "properties": {"name": {"type": "string"}}\n}', encoding="utf-8")
    other = tmp_path / "other dir"
    other.mkdir()
    spec = replace(spec, permission_mode="plan", effort="xhigh", allowed_tools=("Bash", "Read"),
                   add_dirs=(Path("sub"), other), json_schema_path=Path("schema file.json"),
                   max_budget_usd=2.5, session_persistence=False)
    argv = adapter.build_command(spec, workspace, prompt)
    assert argv == [str(adapter.claude_path), "-p", "--output-format", "json", "--model", "claude-opus-5",
                    "--permission-mode", "plan", "--effort", "xhigh", "--max-budget-usd", "2.5",
                    "--json-schema", '{"type":"object","properties":{"name":{"type":"string"}}}',
                    "--no-session-persistence", "--safe-mode",
                    "--add-dir", str(workspace / "sub"), str(other),
                    "--allowedTools", "Bash", "Read"]
    # 가변 인수 옵션 뒤에는 다른 옵션이 오지 않는다.
    tail = argv[argv.index("--add-dir"):]
    assert [a for a in tail if a.startswith("-")] == ["--add-dir", "--allowedTools"]


def test_safe_mode_is_default_and_opt_out(command_inputs):
    # W16c: 작업 공간의 훅·.mcp.json 과 사용자 설정을 끈다. 끄는 것은 명시적으로만.
    adapter, spec, workspace, prompt = command_inputs
    assert ClaudeSpec(model="m").safe_mode is True
    assert adapter.build_command(spec, workspace, prompt).count("--safe-mode") == 1
    argv = adapter.build_command(replace(spec, safe_mode=False), workspace, prompt)
    assert "--safe-mode" not in argv and "--bare" not in argv


def test_max_turns_is_opt_in(command_inputs):
    adapter, spec, workspace, prompt = command_inputs
    argv = adapter.build_command(replace(spec, max_turns=3), workspace, prompt)
    assert argv[argv.index("--max-turns") + 1] == "3"


@pytest.mark.parametrize("changes", [
    {"model": "TO_BE_SELECTED"}, {"model": ""}, {"model": "--bare"}, {"effort": "extreme"},
    {"effort": "TO_BE_SELECTED"}, {"permission_mode": "yolo"}, {"max_budget_usd": 0},
    {"max_budget_usd": True}, {"max_turns": 0}, {"allowed_tools": ("--bare",)},
    {"json_schema_path": Path("missing.json")}, {"safe_mode": "false"}, {"safe_mode": 0}, {"safe_mode": None},
])
def test_invalid_settings_rejected(command_inputs, changes):
    adapter, spec, workspace, prompt = command_inputs
    with pytest.raises(ValueError):
        adapter.build_command(replace(spec, **changes), workspace, prompt)


def test_schema_must_be_json_object(command_inputs):
    adapter, spec, workspace, prompt = command_inputs
    (workspace / "list.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        adapter.build_command(replace(spec, json_schema_path=Path("list.json")), workspace, prompt)


def test_missing_prompt_rejected(command_inputs):
    adapter, spec, workspace, prompt = command_inputs
    with pytest.raises(ValueError, match="prompt_path"):
        adapter.build_command(spec, workspace, prompt.with_suffix(".missing"))


def test_capabilities_and_stop_delegation(command_inputs):
    adapter, _, _, _ = command_inputs
    assert {"stdin_prompt", "json_result", "subagent_stats"} <= adapter.capabilities()

    class FakeProcess:
        received = None

        def stop(self, grace_seconds):
            self.received = grace_seconds
            return {"state": "stopped"}

    process = FakeProcess()
    assert adapter.stop(process, 2.0) == {"state": "stopped"}
    assert process.received == 2.0
