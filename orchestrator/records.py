"""Run records and explicit state transitions (single-writer storage).

``root`` is the directory containing ``runs/``. A Run owns Attempt objects;
Task.attempts contains their IDs. Checks are persisted separately. Each JSON
file is replaced atomically, but saving a whole run is not a transaction.
Callers serialize writes and acceptance events; this module provides no lock.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Literal


TASK_TRANSITIONS = {
    "planned": {"ready"},
    "ready": {"running"},
    "running": {"verifying", "blocked", "failed", "cancelled"},
    "verifying": {"accepted", "blocked", "failed", "cancelled"},
    "accepted": {"integrated"},
    "integrated": set(),
    "blocked": {"ready"},
    "failed": set(),
    "cancelled": set(),
}

ATTEMPT_TRANSITIONS = {
    "created": {"starting", "failed", "cancelled"},
    "starting": {"running", "stopping", "failed", "unknown"},
    "running": {"exited", "stopping", "failed", "unknown"},
    "stopping": {"cancelled", "failed", "unknown"},
    "exited": set(),
    "cancelled": set(),
    "failed": set(),
    "unknown": set(),
}

RUN_TRANSITIONS = {
    "running": {"blocked", "failed", "cancelled", "completed"},
    "blocked": {"running", "failed", "cancelled"},
    "failed": set(),
    "cancelled": set(),
    "completed": set(),
}

REASON_REQUIRED = {"blocked", "failed", "cancelled", "unknown"}
VERDICTS = {"pass", "fail", "undecidable"}


class IllegalTransition(ValueError):
    """The requested state is not reachable from the current state."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass
class Task:
    task_id: str
    plan_version: str
    state: str = "planned"
    accepted_attempt_id: str | None = None
    attempts: list[str] = field(default_factory=list)
    reason_code: str | None = None


@dataclass
class Attempt:
    attempt_id: str
    task_id: str
    route_id: str
    state: str = "created"
    reason_code: str | None = None
    workspace: str | None = None
    process: dict | None = None
    result_manifest: str | None = None
    started_at: str | None = None
    ended_at: str | None = None

    def __post_init__(self) -> None:
        self.started_at = _utc(self.started_at)
        self.ended_at = _utc(self.ended_at)


@dataclass
class Run:
    run_id: str
    plan_version: str
    state: str = "running"
    created_at: str = field(default_factory=_utc_now)
    tasks: dict[str, Task] = field(default_factory=dict)
    attempts: dict[str, Attempt] = field(default_factory=dict)
    reason_code: str | None = None
    summary: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.created_at is None:
            raise ValueError("created_at is required")
        self.created_at = _utc(self.created_at)


@dataclass
class Check:
    check_id: str
    attempt_id: str
    subject_commit: str
    subject_manifest_hash: str
    validator_version: str
    command_id: str
    exit_code: int | None
    verdict: Literal["pass", "fail", "undecidable"]
    evidence: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"invalid verdict: {self.verdict!r}")


def transition(obj: Run | Task | Attempt, new_state: str,
               reason_code: str | None = None) -> None:
    """Change state and replace the current reason; timestamps are caller-owned.

    Structural transition checks do not evaluate checks or aggregate child states.
    The scheduler must establish those conditions before calling this function.
    """
    tables = {Run: RUN_TRANSITIONS, Task: TASK_TRANSITIONS,
              Attempt: ATTEMPT_TRANSITIONS}
    try:
        table = tables[type(obj)]
    except KeyError:
        raise TypeError("transition requires a Run, Task or Attempt") from None
    if new_state not in table.get(obj.state, set()):
        raise IllegalTransition(f"illegal transition: {obj.state!r} -> {new_state!r}")
    if new_state in REASON_REQUIRED and (
        not isinstance(reason_code, str) or not reason_code.strip()
    ):
        raise ValueError(f"reason_code is required for {new_state!r}")
    obj.state = new_state
    obj.reason_code = reason_code


def accept_attempt(task: Task, attempt_id: str) -> bool:
    """Claim acceptance once, without changing task state or evaluating checks."""
    if attempt_id not in task.attempts:
        raise ValueError(f"attempt {attempt_id!r} does not belong to task {task.task_id!r}")
    if task.accepted_attempt_id is not None:
        return False
    task.accepted_attempt_id = attempt_id
    return True


def _identifier(value: str) -> str:
    # IDs are filenames on Windows too. Dots are reserved for the check separator.
    if (not isinstance(value, str) or not value or
            any(c in value for c in '.<>:"/\\|?*') or
            any(ord(c) < 32 for c in value) or value.endswith(" ") or
            value.upper() in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} or
            value.upper() in {f"{prefix}{n}" for prefix in ("COM", "LPT")
                              for n in "123456789¹²³"}):
        raise ValueError(f"invalid record ID: {value!r}")
    return value


def _run_dir(root: Path, run_id: str) -> Path:
    return Path(root) / "runs" / _identifier(run_id)


# W16d: on Windows another program (a virus scanner or search indexer, for example) can
# briefly open a file just written here. Replacing or removing it then fails with a sharing
# violation (PermissionError), so those two operations wait and try again for about 2 seconds.
RETRY_SHARING_VIOLATIONS = os.name == "nt"
SHARING_RETRY_DELAYS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.4, 0.6, 0.6)


def _retry_sharing_violation(operation, *args):
    """Call operation; on Windows retry a PermissionError after each delay, then let it raise."""
    for delay in SHARING_RETRY_DELAYS if RETRY_SHARING_VIOLATIONS else ():
        try:
            return operation(*args)
        except PermissionError:
            time.sleep(delay)
    return operation(*args)


def _write_json(path: Path, payload: dict) -> None:
    # Serialize before creating a temporary file, so encoding errors leave no file.
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True,
                      allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _retry_sharing_violation(os.replace, temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                _retry_sharing_violation(temporary.unlink, True)
            except OSError:
                pass  # keep the temporary file rather than hide the error being raised


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _validate_run(run: Run) -> None:
    _identifier(run.run_id)
    if run.created_at is None:
        raise ValueError("created_at is required")
    if run.state not in RUN_TRANSITIONS:
        raise ValueError(f"invalid run state: {run.state!r}")
    for label, mapping in (("task", run.tasks), ("attempt", run.attempts)):
        names = [_identifier(key).casefold() for key in mapping]
        if len(names) != len(set(names)):
            raise ValueError(f"{label} IDs collide on a case-insensitive filesystem")
    for task_id, task in run.tasks.items():
        if _identifier(task_id) != task.task_id or task.state not in TASK_TRANSITIONS:
            raise ValueError(f"invalid task record: {task_id!r}")
        if len(task.attempts) != len(set(task.attempts)):
            raise ValueError(f"duplicate attempt IDs in task {task_id!r}")
        if (task.accepted_attempt_id is not None and
                task.accepted_attempt_id not in task.attempts):
            raise ValueError(f"accepted attempt is missing from task {task_id!r}")
        for attempt_id in task.attempts:
            _identifier(attempt_id)
            if attempt_id not in run.attempts or run.attempts[attempt_id].task_id != task_id:
                raise ValueError(f"missing or mismatched attempt: {attempt_id!r}")
    for attempt_id, attempt in run.attempts.items():
        if (_identifier(attempt_id) != attempt.attempt_id or
                attempt.state not in ATTEMPT_TRANSITIONS):
            raise ValueError(f"invalid attempt record: {attempt_id!r}")
        if (attempt.task_id not in run.tasks or
                attempt_id not in run.tasks[attempt.task_id].attempts):
            raise ValueError(f"attempt has no owning task: {attempt_id!r}")


def save_run(run: Run, root: Path) -> None:
    """Save all owned records, then the summary containing their ordered ID lists.

    IDs must be Windows-safe filename components without dots. Old unreferenced
    files are retained but ignored on load. No process is resumed on loading.
    """
    _validate_run(run)
    directory = _run_dir(root, run.run_id)
    payload = asdict(run)
    payload["created_at"] = _utc(run.created_at)
    task_payloads = payload.pop("tasks")
    attempt_payloads = payload.pop("attempts")
    for data in attempt_payloads.values():
        data["started_at"] = _utc(data["started_at"])
        data["ended_at"] = _utc(data["ended_at"])
    payload["tasks"] = list(run.tasks)
    payload["attempts"] = list(run.attempts)
    for task_id, data in task_payloads.items():
        _write_json(directory / "tasks" / f"{task_id}.json", data)
    for attempt_id, data in attempt_payloads.items():
        _write_json(directory / "attempts" / attempt_id / "attempt.json", data)
    _write_json(directory / "summary.json", payload)


def load_run(run_id: str, root: Path) -> Run:
    directory = _run_dir(root, run_id)
    payload = _read_json(directory / "summary.json")
    if payload["run_id"] != run_id:
        raise ValueError("summary run_id does not match requested run")
    task_ids = payload.pop("tasks")
    attempt_ids = payload.pop("attempts")
    if len(task_ids) != len(set(task_ids)) or len(attempt_ids) != len(set(attempt_ids)):
        raise ValueError("duplicate IDs in summary")
    tasks = {key: Task(**_read_json(directory / "tasks" / f"{_identifier(key)}.json"))
             for key in task_ids}
    attempts = {key: Attempt(**_read_json(directory / "attempts" / _identifier(key)
                                         / "attempt.json")) for key in attempt_ids}
    run = Run(**payload, tasks=tasks, attempts=attempts)
    _validate_run(run)
    return run


def save_check(check: Check, root: Path, run_id: str) -> None:
    if check.verdict not in VERDICTS:
        raise ValueError(f"invalid verdict: {check.verdict!r}")
    name = f"{_identifier(check.check_id)}.{_identifier(check.attempt_id)}.json"
    _write_json(_run_dir(root, run_id) / "checks" / name, asdict(check))


def load_checks(root: Path, run_id: str) -> list[Check]:
    checks = []
    for path in sorted((_run_dir(root, run_id) / "checks").glob("*.json")):
        check = Check(**_read_json(path))
        expected = f"{_identifier(check.check_id)}.{_identifier(check.attempt_id)}.json"
        if path.name != expected:
            raise ValueError(f"check filename does not match its IDs: {path.name!r}")
        checks.append(check)
    return checks
