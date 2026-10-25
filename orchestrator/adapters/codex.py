"""Codex exec command construction and offline log parsing (W3).

W7 resolves route settings into CodexSpec, then passes build_command's argv and
the SAME prompt_path as stdin_path to W2. No shell quoting or prompt text belongs
in argv. Relative workspace/prompt paths use the caller's cwd; relative output
and schema paths use workspace. This module never launches a process.

The parser targets the checked-in CLI 0.153.4 fixtures. Session rate_limits are
a separate, version-dependent rollout format, not part of exec --json output.
Ephemeral runs do not create those rollouts; a supplied older snapshot is not a
measurement of the current attempt or a guarantee that a new run is affordable.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .base import AttemptResult, CommandExecution, ProcessControl


@dataclass(frozen=True)
class CodexSpec:
    """Resolved execution settings; model/time budgeting remains the caller's job."""

    model: str
    last_message_path: Path
    output_schema_path: Path | None = None
    sandbox: str = "read-only"
    effort: str | None = None
    ephemeral: bool = True
    skip_git_repo_check: bool = False
    approve_for_me: bool = False
    # W18b: one `--disable <name>` each. CLI 0.163 enables `multi_agent` (subagents) by default.
    disable_features: tuple[str, ...] = ()


FEATURE_NAME = re.compile(r"[a-z0-9_.]+")
# exec --json thread item types (Codex exec docs) plus collab_tool_call; only the first two
# and collab_tool_call are read. Any other type is listed in unknown_item_types.
KNOWN_ITEM_TYPES = frozenset({"agent_message", "reasoning", "command_execution", "file_change",
                              "mcp_tool_call", "web_search", "todo_list", "error", "collab_tool_call"})


class EventParseError(ValueError):
    """Invalid evidence. Messages intentionally omit raw log content."""


def _events(path: Path, *, allow_partial_tail: bool = False) -> Iterator[dict]:
    with Path(path).open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # A live session writer may not have finished its last record.
                if allow_partial_tail and not line.endswith("\n"):
                    return
                raise EventParseError(f"Invalid JSON at line {number}") from None
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise EventParseError(f"Invalid event at line {number}")
            yield event


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{field} must be a nonempty string without NUL")
    if value.startswith("TO_BE_"):
        raise ValueError(f"{field} is unresolved")
    return value


def _workspace_path(path: Path, workspace: Path) -> str:
    path = Path(path)
    return str((path if path.is_absolute() else workspace / path).resolve())


class CodexAdapter:
    def __init__(self, codex_path: Path):
        # Resolve once, before the process changes cwd to its worktree.
        self.codex_path = Path(codex_path).resolve()

    def capabilities(self) -> frozenset[str]:
        """Supported operations, not a live installation/authentication probe.

        Actual file/edit/command access still depends on the selected sandbox policy.
        """
        return frozenset({"read_files", "edit_files", "run_tests", "json_events",
                          "output_schema", "stdin_prompt"})

    def build_command(
        self, spec: CodexSpec, workspace: Path, prompt_path: Path
    ) -> list[str]:
        """Return argv only; W2 must open prompt_path as stdin, with shell=False.

The file is checked here but never read, copied, or interpolated into argv.
The process layer must still handle a file disappearing before process start.
"""
        if not Path(prompt_path).is_file():
            raise ValueError("prompt_path must be an existing file")
        model = _text(spec.model, "model")
        if spec.sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("Unsupported sandbox")
        for option in (spec.ephemeral, spec.skip_git_repo_check, spec.approve_for_me):
            if not isinstance(option, bool):
                raise ValueError("Execution switches must be boolean")
        if spec.approve_for_me and spec.sandbox != "workspace-write":
            raise ValueError("approve_for_me requires workspace-write sandbox")
        features = spec.disable_features
        if (not isinstance(features, tuple) or len(set(features)) != len(features)
                or not all(isinstance(f, str) and FEATURE_NAME.fullmatch(f) for f in features)):
            raise ValueError("disable_features must be distinct lowercase feature names")
        workspace = Path(workspace).resolve()
        argv = [str(self.codex_path), "exec", "--json", "-o",
                _workspace_path(spec.last_message_path, workspace)]
        if spec.output_schema_path is not None:
            argv += ["--output-schema", _workspace_path(spec.output_schema_path, workspace)]
        # The convenience flag selects workspace-write itself; CLI rejects -s with it.
        if spec.approve_for_me:
            argv.append("--approve-for-me")
        else:
            argv += ["-s", spec.sandbox]
        for feature in features:
            argv += ["--disable", feature]
        argv += ["-C", str(workspace), "--model", model]
        if spec.effort is not None:
            effort = _text(spec.effort, "effort")
            # JSON string syntax is valid TOML for these simple effort values.
            argv += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
        if spec.ephemeral:
            argv.append("--ephemeral")
        if spec.skip_git_repo_check:
            argv.append("--skip-git-repo-check")
        return argv + ["-"]

    def parse_events(self, path: Path) -> AttemptResult:
        return parse_events(path)

    def stop(self, process: ProcessControl, grace_seconds: float = 3.0) -> object:
        return process.stop(grace_seconds=grace_seconds)


def parse_events(path: Path) -> AttemptResult:
    """Normalize one exec stream; malformed evidence raises EventParseError.

Started commands are retained when an attempt is killed. Updates replace the
same item, rather than duplicating it. Unknown event types are ignored for
forward compatibility. Missing terminal events never imply successful exit.

W18b: collab_tool_call items (captured from CLI 0.163: id, tool, sender_thread_id,
receiver_thread_ids, agents_states, status) list their tool names in collab_tools.
subagents_spawned is the larger of the distinct spawn_agent items and the distinct
receiver threads other than the sender (0 when none were read). It is only what the
stream shows: in CLI 0.163 a real spawn appeared in the session records, not in the
exec stream (see exec-spawn-unseen-disabled.jsonl), so 0 is not proof that no
subagent ran. Other item types are listed by name only in unknown_item_types.
"""
    result = AttemptResult()
    commands: dict[str, CommandExecution] = {}
    spawned: set[str] = set()
    receivers: set[str] = set()
    tools: set[str] = set()
    unknown: set[str] = set()
    failed = False
    for event in _events(path):
        kind = event["type"]
        if kind == "thread.started":
            thread_id = event.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                raise EventParseError("Invalid thread_id")
            if result.thread_id is not None and result.thread_id != thread_id:
                raise EventParseError("Multiple threads in one attempt log")
            result.thread_id = thread_id
        elif kind == "turn.started":
            result.status = "incomplete"
            result.final_message = None
        elif kind == "turn.completed":
            usage = event.get("usage")
            if not isinstance(usage, dict):
                raise EventParseError("Invalid turn usage")
            counters = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                        "output_tokens", "reasoning_output_tokens")
            result.usage = {}
            for key in counters:
                if key in usage:
                    value = usage[key]
                    if type(value) is not int or value < 0:
                        raise EventParseError("Invalid usage counter")
                    result.usage[key] = value
            result.status = "completed"
        elif kind in {"turn.failed", "error"}:
            failed = True
            result.reason_code = "cli_error" if kind == "error" else "turn_failed"
        elif kind in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, dict):
                raise EventParseError("Invalid item")
            if item.get("type") == "agent_message" and kind == "item.completed":
                message = item.get("text")
                if not isinstance(message, str):
                    raise EventParseError("Invalid agent message")
                result.final_message = message
            elif item.get("type") == "command_execution":
                item_id, command = item.get("id"), item.get("command")
                code = item.get("exit_code")
                status = item.get("status", "in_progress")
                if (not isinstance(item_id, str) or not isinstance(command, str)
                        or (code is not None and type(code) is not int)
                        or not isinstance(status, str)):
                    raise EventParseError("Invalid command execution")
                commands[item_id] = CommandExecution(item_id, command, code, status)
            elif item.get("type") == "collab_tool_call":
                item_id = item.get("id")
                threads = item.get("receiver_thread_ids", [])
                if (not isinstance(item_id, str) or not item_id or not isinstance(item.get("tool"), str)
                        or not isinstance(threads, list) or not all(isinstance(t, str) for t in threads)):
                    raise EventParseError("Invalid collab tool call")
                tools.add(item["tool"])
                if item["tool"] == "spawn_agent":
                    spawned.add(item_id)
                receivers.update(t for t in threads if t and t != item.get("sender_thread_id"))
            elif isinstance(item.get("type"), str) and item["type"] not in KNOWN_ITEM_TYPES:
                unknown.add(item["type"])
    result.command_executions = list(commands.values())
    result.subagents_spawned = max(len(spawned), len(receivers))
    result.collab_tools = sorted(tools)
    result.unknown_item_types = sorted(unknown)
    if failed:
        result.status = "failed"
    elif result.status == "incomplete":
        result.reason_code = "missing_terminal_event"
    elif result.thread_id is None:
        raise EventParseError("Completed turn without thread_id")
    return result


def read_rate_limits(session_log_path: Path) -> dict | None:
    """Read the last non-null token_count rate_limits snapshot in file order.

No home-directory discovery or credential lookup. Missing files/data return
None; permission errors and malformed complete records propagate. A trailing
partial JSON record is ignored because rollout files may still be growing.
Only plan_type and the two windows are returned, never arbitrary payload data.
"""
    latest = None
    try:
        for event in _events(session_log_path, allow_partial_tail=True):
            payload = event.get("payload")
            if (event["type"] != "event_msg" or not isinstance(payload, dict)
                    or payload.get("type") != "token_count"):
                continue
            limits = payload.get("rate_limits")
            if limits is None:
                continue
            if not isinstance(limits, dict):
                raise EventParseError("Invalid rate_limits")
            latest = {"plan_type": limits.get("plan_type"),
                      "primary": _window(limits.get("primary")),
                      "secondary": _window(limits.get("secondary"))}
            if latest["plan_type"] is not None and not isinstance(latest["plan_type"], str):
                raise EventParseError("Invalid plan_type")
    except FileNotFoundError:
        return None
    return latest


def _window(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise EventParseError("Invalid rate limit window")
    fields = ("used_percent", "window_minutes", "resets_at")
    result = {key: value[key] for key in fields if key in value}
    if any(v is not None and type(v) not in (int, float) for v in result.values()):
        raise EventParseError("Invalid rate limit window field")
    return result
