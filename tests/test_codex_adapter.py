"""Offline W3 checks. Real G0 fixtures are kept unchanged."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from orchestrator.adapters.codex import (
    CodexAdapter,
    CodexSpec,
    EventParseError,
    parse_events,
    read_rate_limits,
)


FIXTURES = Path(__file__).parent / "fixtures" / "codex"


def write_events(tmp_path, events, tail=""):
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
                    + tail, encoding="utf-8")
    return path


def thread():
    return {"type": "thread.started", "thread_id": "test-thread"}


def completed():
    return {"type": "turn.completed", "usage": {"output_tokens": 5}}


def test_real_success_fixture():
    result = CodexAdapter(Path("codex.exe")).parse_events(FIXTURES / "exec-ok.jsonl")
    assert result.thread_id == "01a090c6-cf63-7182-9bcc-0f785079a402"
    assert result.usage == {"input_tokens": 16493, "cached_input_tokens": 12544,
                            "cache_write_input_tokens": 0, "output_tokens": 5,
                            "reasoning_output_tokens": 0}
    assert result.final_message == "OK"
    assert result.status == "completed"
    assert result.reason_code is None
    assert result.command_executions == []


def test_real_stopped_fixture_keeps_started_command_without_claiming_completion():
    result = parse_events(FIXTURES / "exec-shell-stopped.jsonl")
    assert result.thread_id == "01a090c9-5e7d-7542-828e-4fb161cd9bcc"
    assert result.status == "incomplete"
    assert result.reason_code == "missing_terminal_event"
    assert result.usage == {}
    assert len(result.command_executions) == 1
    command = result.command_executions[0]
    assert command.item_id == "item_1"
    assert "time.sleep(120)" in command.command
    assert command.exit_code is None
    assert command.status == "in_progress"


def test_command_updates_and_last_message(tmp_path):
    events = [thread()]
    for kind, code, status in [("started", None, "in_progress"),
                                ("updated", None, "in_progress"),
                                ("completed", 7, "failed")]:
        events.append({"type": f"item.{kind}", "item": {
            "id": "command-1", "type": "command_execution", "command": "test-command",
            "exit_code": code, "status": status}})
    for message in ("작업 중", "최종 결과"):
        events.append({"type": "item.completed", "item": {
            "id": message, "type": "agent_message", "text": message}})
    events += [{"type": "future.event", "metadata": "ignored"}, completed()]
    result = parse_events(write_events(tmp_path, events))
    assert result.final_message == "최종 결과"
    assert len(result.command_executions) == 1
    assert result.command_executions[0].exit_code == 7
    # A tool failure and a completed model turn are independent observations.
    assert result.command_executions[0].status == "failed"
    assert result.status == "completed"


@pytest.mark.parametrize("event,reason", [
    ({"type": "turn.failed", "error": {"message": "failure"}}, "turn_failed"),
    ({"type": "error", "message": "failure"}, "cli_error"),
])
def test_explicit_errors_never_become_success(tmp_path, event, reason):
    result = parse_events(write_events(tmp_path, [thread(), event, completed()]))
    assert result.status == "failed"
    assert result.reason_code == reason


@pytest.mark.parametrize("events", [[], [thread()], [thread(), completed(),
                                                       {"type": "turn.started"}]])
def test_absent_terminal_event_is_incomplete(tmp_path, events):
    result = parse_events(write_events(tmp_path, events))
    assert result.status == "incomplete"
    assert result.final_message is None


@pytest.mark.parametrize("events,tail", [
    ([thread(), completed()], '{"partial":'),
    ([[]], ""),
    ([{"type": "item.completed", "item": None}], ""),
    ([{"type": "turn.completed", "usage": []}], ""),
    ([completed()], ""),
    ([thread(), {"type": "thread.started", "thread_id": "different"}], ""),
])
def test_corrupt_evidence_raises_without_returning_success(tmp_path, events, tail):
    with pytest.raises(EventParseError):
        parse_events(write_events(tmp_path, events, tail))


def test_parse_error_does_not_echo_raw_content(tmp_path):
    path = write_events(tmp_path, [], "private-input-not-json\n")
    with pytest.raises(EventParseError) as error:
        parse_events(path)
    assert "private-input" not in str(error.value)


@pytest.mark.parametrize("value", [-1, True, "5", None])
def test_invalid_usage_counter_is_not_silently_dropped(tmp_path, value):
    event = {"type": "turn.completed", "usage": {"output_tokens": value}}
    with pytest.raises(EventParseError, match="usage counter"):
        parse_events(write_events(tmp_path, [thread(), event]))


def test_usage_does_not_copy_arbitrary_metadata(tmp_path):
    event = {"type": "turn.completed", "usage": {"output_tokens": 5, "private": 42}}
    result = parse_events(write_events(tmp_path, [thread(), event]))
    assert result.usage == {"output_tokens": 5}


@pytest.fixture
def command_inputs(tmp_path):
    workspace = tmp_path / "작업 공간"
    workspace.mkdir()
    prompt = tmp_path / "prompt with spaces.txt"
    prompt.write_text('프롬프트 "quoted"; $(not-a-command)\n두 번째 줄', encoding="utf-8")
    spec = CodexSpec(model="test-model", last_message_path=Path("last message.txt"))
    adapter = CodexAdapter(tmp_path / "CLI path" / "codex.exe")
    return adapter, spec, workspace, prompt


def test_build_command_all_options_and_stdin_contract(command_inputs):
    adapter, spec, workspace, prompt = command_inputs
    spec = replace(spec, output_schema_path=Path("schema file.json"),
                   sandbox="workspace-write", effort="high", skip_git_repo_check=True)
    argv = adapter.build_command(spec, workspace, prompt)
    assert argv == [str(adapter.codex_path), "exec", "--json", "-o",
                    str(workspace / "last message.txt"), "--output-schema",
                    str(workspace / "schema file.json"), "-s", "workspace-write",
                    "-C", str(workspace), "--model", "test-model", "-c",
                    'model_reasoning_effort="high"', "--ephemeral",
                    "--skip-git-repo-check", "-"]
    assert prompt.read_text(encoding="utf-8") not in argv
    assert str(prompt) not in argv
    assert not (workspace / "last message.txt").exists()


def test_optional_flags_and_absolute_output_path(command_inputs):
    adapter, spec, workspace, prompt = command_inputs
    destination = workspace.parent / "output.json"
    argv = adapter.build_command(replace(spec, ephemeral=False,
                                         last_message_path=destination), workspace, prompt)
    assert argv[argv.index("-o") + 1] == str(destination)
    for flag in ("--output-schema", "--skip-git-repo-check", "--ephemeral", "-c"):
        assert flag not in argv
    assert argv[argv.index("-s") + 1] == "read-only"


@pytest.mark.parametrize("changes", [{"model": "TO_BE_SELECTED"}, {"model": ""},
                                    {"sandbox": "invalid"}, {"ephemeral": "false"},
                                    {"effort": "TO_BE_SELECTED"}])
def test_invalid_execution_settings_rejected(command_inputs, changes):
    adapter, spec, workspace, prompt = command_inputs
    with pytest.raises(ValueError):
        adapter.build_command(replace(spec, **changes), workspace, prompt)


def test_missing_prompt_rejected(command_inputs):
    adapter, spec, workspace, prompt = command_inputs
    with pytest.raises(ValueError, match="prompt_path"):
        adapter.build_command(spec, workspace, prompt.with_suffix(".missing"))


def test_stop_delegates_to_process_layer(command_inputs):
    adapter, _, _, _ = command_inputs

    class FakeProcess:
        received = None

        def stop(self, grace_seconds):
            self.received = grace_seconds
            return {"state": "unknown", "remaining_pids": [123]}

    process = FakeProcess()
    assert adapter.stop(process, 1.25) == {"state": "unknown", "remaining_pids": [123]}
    assert process.received == 1.25


def rate_event(limits):
    return {"type": "event_msg", "payload": {"type": "token_count",
                                              "rate_limits": limits}}


def test_latest_rate_snapshot_and_null_preservation(tmp_path):
    first = {"plan_type": "pro", "primary": {"used_percent": 20}}
    latest = {"plan_type": "pro", "primary": {
        "used_percent": 70.0, "window_minutes": 10080, "resets_at": 123456,
        "unrelated": "must not propagate"}, "secondary": None,
        "credentials": "must not propagate"}
    path = write_events(tmp_path, [rate_event(first), rate_event(latest),
                                   rate_event(None), {"type": "other.event"}])
    assert read_rate_limits(path) == {"plan_type": "pro", "primary": {
        "used_percent": 70.0, "window_minutes": 10080, "resets_at": 123456},
        "secondary": None}


def test_secondary_snapshot_and_partial_tail(tmp_path):
    snapshot = {"plan_type": None, "primary": None,
                "secondary": {"used_percent": 10, "window_minutes": 300,
                              "resets_at": None}}
    path = write_events(tmp_path, [rate_event(snapshot)], '{"type":"event_msg"')
    assert read_rate_limits(path) == snapshot


def test_absent_rate_limits(tmp_path):
    assert read_rate_limits(FIXTURES / "exec-ok.jsonl") is None
    assert read_rate_limits(tmp_path / "missing.jsonl") is None
    assert read_rate_limits(write_events(tmp_path, [rate_event(None)])) is None


@pytest.mark.parametrize("tail", ['invalid\n', '{"type": "event_msg",\n'])
def test_rate_reader_rejects_malformed_complete_records(tmp_path, tail):
    with pytest.raises(EventParseError):
        read_rate_limits(write_events(tmp_path, [rate_event({})], tail))


@pytest.mark.parametrize("limits", [[], {"primary": []}, {"plan_type": {}},
                                     {"secondary": {"used_percent": "unknown"}}])
def test_rate_reader_rejects_bad_snapshot_shapes(tmp_path, limits):
    with pytest.raises(EventParseError):
        read_rate_limits(write_events(tmp_path, [rate_event(limits)]))


def test_auto_review_is_opt_in_and_preserves_sandbox(command_inputs):
    adapter, spec, workspace, prompt = command_inputs
    assert "--approve-for-me" not in adapter.build_command(spec, workspace, prompt)
    argv = adapter.build_command(replace(spec, sandbox="workspace-write", approve_for_me=True), workspace, prompt)
    assert argv.count("--approve-for-me") == 1
    assert "-s" not in argv and "--sandbox" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


@pytest.mark.parametrize("changes", [
    {"approve_for_me": "true", "sandbox": "workspace-write"},
    {"approve_for_me": True, "sandbox": "read-only"},
    {"approve_for_me": True, "sandbox": "danger-full-access"},
])
def test_auto_review_rejects_ambiguous_or_conflicting_settings(command_inputs, changes):
    adapter, spec, workspace, prompt = command_inputs
    with pytest.raises(ValueError):
        adapter.build_command(replace(spec, **changes), workspace, prompt)


# W18b: feature switches and subagent evidence

def test_disable_features_come_before_workspace_and_stdin_marker(command_inputs):
    adapter, spec, workspace, prompt = command_inputs
    spec = replace(spec, sandbox="workspace-write", approve_for_me=True,
                   disable_features=("multi_agent", "multi_agent_v2"))
    argv = adapter.build_command(spec, workspace, prompt)
    start = argv.index("--approve-for-me") + 1
    assert argv[start:start + 5] == ["--disable", "multi_agent", "--disable", "multi_agent_v2", "-C"]
    assert argv[-1] == "-" and argv.count("--disable") == 2


def test_no_disable_flag_by_default(command_inputs):
    adapter, spec, workspace, prompt = command_inputs
    assert "--disable" not in adapter.build_command(spec, workspace, prompt)


@pytest.mark.parametrize("features", [["multi_agent"], ("multi_agent", "multi_agent"), ("Multi_Agent",),
                                      ("--enable",), ("",), ("multi agent",), (3,), "multi_agent"])
def test_invalid_disable_features_rejected(command_inputs, features):
    adapter, spec, workspace, prompt = command_inputs
    with pytest.raises(ValueError, match="disable_features"):
        adapter.build_command(replace(spec, disable_features=features), workspace, prompt)


def test_synthetic_collab_stream_counts_distinct_spawn_items():
    # Hand-made in the captured item shape; a real spawn_agent item has not been captured (fixtures README).
    result = parse_events(FIXTURES / "exec-collab-synthetic.jsonl")
    assert result.status == "completed" and result.final_message == "OK"
    assert result.subagents_spawned == 2 and result.collab_tools == ["spawn_agent", "wait"]
    assert result.unknown_item_types == ["future_item_kind"]


@pytest.mark.parametrize("name", ["exec-collab-wait-enabled.jsonl", "exec-collab-wait-disabled.jsonl"])
def test_real_collab_wait_streams(name):
    # CLI 0.163.0-alpha.11.2, 2026-10-25 (W18b E4): both runs, multi_agent on and off, called "wait"
    # with no receiver threads and showed no spawn_agent item.
    result = parse_events(FIXTURES / name)
    assert result.status == "completed" and result.final_message == "READY"
    assert result.collab_tools == ["wait"] and result.subagents_spawned == 0
    assert result.unknown_item_types == []


def test_spawn_items_count_without_receiver_threads(tmp_path):
    # A spawn whose child thread is not reported (or that failed) still counts, once per item id.
    items = [{"type": f"item.{kind}", "item": {"id": item_id, "type": "collab_tool_call", "tool": tool,
                                               "sender_thread_id": "test-thread", "receiver_thread_ids": []}}
             for kind, item_id, tool in [("started", "s1", "spawn_agent"), ("completed", "s1", "spawn_agent"),
                                         ("completed", "s2", "spawn_agent"), ("completed", "w1", "wait"),
                                         ("completed", "x1", "close_agent")]]
    result = parse_events(write_events(tmp_path, [thread(), *items, completed()]))
    assert result.subagents_spawned == 2 and result.collab_tools == ["close_agent", "spawn_agent", "wait"]


def test_receiver_threads_count_without_a_spawn_item(tmp_path):
    items = [{"type": "item.completed", "item": {"id": f"w{n}", "type": "collab_tool_call", "tool": "wait",
                                                 "sender_thread_id": "test-thread", "receiver_thread_ids": ids}}
             for n, ids in enumerate([["c1", "test-thread"], ["c1", "c2"], []])]
    result = parse_events(write_events(tmp_path, [thread(), *items, completed()]))
    assert result.subagents_spawned == 2 and result.collab_tools == ["wait"]


def test_real_fixtures_report_no_subagents_and_no_unknown_items():
    for name in ("exec-ok.jsonl", "exec-shell-stopped.jsonl"):
        result = parse_events(FIXTURES / name)
        assert result.subagents_spawned == 0 and result.unknown_item_types == [] and result.collab_tools == []


def test_unknown_item_types_keep_names_only(tmp_path):
    events = [thread()] + [{"type": "item.completed", "item": {"id": f"i{n}", "type": kind, "secret": "x"}}
                           for n, kind in enumerate(["zeta_kind", "alpha_kind", "zeta_kind", "file_change", "mu_kind",
                                                                     "beta_kind", "omega_kind", "kappa_kind"])]
    result = parse_events(write_events(tmp_path, events + [completed()]))
    # Six names: an unsorted set order matches sorted order by chance about once in 720 runs.
    assert result.unknown_item_types == ["alpha_kind", "beta_kind", "kappa_kind", "mu_kind", "omega_kind", "zeta_kind"]
    assert "secret" not in json.dumps(result.unknown_item_types)


@pytest.mark.parametrize("changes", [{"id": None}, {"id": ""}, {"id": 7}, {"tool": None}, {"tool": 3},
                                     {"receiver_thread_ids": "c1"}, {"receiver_thread_ids": [1]}])
def test_malformed_collab_item_is_invalid_evidence(tmp_path, changes):
    item = {"id": "c", "type": "collab_tool_call", "tool": "spawn_agent", "receiver_thread_ids": [], **changes}
    event = {"type": "item.completed", "item": item}
    with pytest.raises(EventParseError, match="collab"):
        parse_events(write_events(tmp_path, [thread(), event, completed()]))
