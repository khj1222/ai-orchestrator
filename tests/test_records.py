"""Offline checks for W1 state ownership and JSON persistence."""

from dataclasses import asdict
from datetime import datetime, timedelta
import json

import pytest

from orchestrator import records
from orchestrator.records import (
    Attempt, Check, IllegalTransition, Run, Task, accept_attempt, load_checks,
    load_run, save_check, save_run, transition,
)


def example_run():
    first = Attempt(
        "attempt-1", "task-1", "codex", state="exited", workspace="work/attempt-1",
        process={"pid": 123, "exit_code": 0, "logs": {"stdout": "logs/out.jsonl"}},
        result_manifest="results/manifest.json",
        started_at="2026-09-12T01:00:00Z", ended_at="2026-09-12T01:01:00Z",
    )
    second = Attempt("attempt-2", "task-1", "claude", state="unknown",
                     reason_code="process_unobservable")
    task = Task("task-1", "v1", state="accepted", accepted_attempt_id="attempt-1",
                attempts=["attempt-1", "attempt-2"])
    return Run("run-1", "v1", created_at="2026-09-12T00:00:00Z",
               tasks={"task-1": task, "task-2": Task("task-2", "v1")},
               attempts={"attempt-1": first, "attempt-2": second},
               summary={"note": "검증 결과", "counts": {"accepted": 1}})


def example_check(verdict="pass", attempt_id="attempt-1"):
    return Check("contract-v1", attempt_id, "abc123", "sha256:manifest", "v1",
                 "test-contract", 0 if verdict == "pass" else None, verdict,
                 {"stdout": "logs/check.txt", "note": "검사 기록"})


def test_task_normal_path():
    task = Task("task-1", "v1")
    for state in ("ready", "running", "verifying", "accepted", "integrated"):
        assert transition(task, state) is None
        assert task.state == state


@pytest.mark.parametrize("source,target", [
    ("planned", "accepted"), ("accepted", "running"), ("failed", "ready"),
    ("cancelled", "ready"), ("integrated", "ready"), ("running", "running"),
])
def test_illegal_task_transitions_leave_record_unchanged(source, target):
    task = Task("task-1", "v1", state=source, reason_code="original")
    before = asdict(task)
    with pytest.raises(IllegalTransition) as error:
        transition(task, target)
    assert source in str(error.value) and target in str(error.value)
    assert asdict(task) == before


@pytest.mark.parametrize("source", ["running", "verifying"])
@pytest.mark.parametrize("target", ["blocked", "failed", "cancelled"])
def test_task_exception_paths(source, target):
    task = Task("task-1", "v1", state=source)
    transition(task, target, "execution_problem")
    assert (task.state, task.reason_code) == (target, "execution_problem")
    if target == "blocked":
        transition(task, "ready")
        assert task.reason_code is None


def test_attempt_normal_path_and_cancellation():
    attempt = Attempt("attempt-1", "task-1", "codex")
    for state in ("starting", "running", "exited"):
        transition(attempt, state)
        assert attempt.state == state
    attempt = Attempt("attempt-2", "task-1", "codex", state="running")
    with pytest.raises(IllegalTransition):
        transition(attempt, "cancelled", "user_stop")
    transition(attempt, "stopping")
    transition(attempt, "cancelled", "user_stop")
    assert attempt.state == "cancelled"


@pytest.mark.parametrize("source,target", [
    ("created", "failed"), ("created", "cancelled"), ("starting", "failed"),
    ("starting", "unknown"), ("starting", "stopping"), ("running", "failed"),
    ("running", "unknown"), ("stopping", "failed"), ("stopping", "unknown"),
])
def test_attempt_exception_paths(source, target):
    attempt = Attempt("attempt-1", "task-1", "codex", state=source)
    transition(attempt, target, "execution_problem")
    assert attempt.state == target


@pytest.mark.parametrize("source", ["unknown", "failed", "cancelled", "exited"])
@pytest.mark.parametrize("target", ["running", "starting"])
def test_terminal_attempts_cannot_restart(source, target):
    attempt = Attempt("attempt-1", "task-1", "codex", state=source)
    with pytest.raises(IllegalTransition, match=f"{source!r} -> {target!r}"):
        transition(attempt, target)


@pytest.mark.parametrize("kind,target", [
    (Task, "blocked"), (Task, "failed"), (Task, "cancelled"),
    (Attempt, "failed"), (Attempt, "unknown"), (Attempt, "cancelled"),
    (Run, "blocked"), (Run, "failed"), (Run, "cancelled"),
])
@pytest.mark.parametrize("reason", [None, "", "  "])
def test_exception_states_require_reason_without_mutating(kind, target, reason):
    if kind is Task:
        obj = Task("task-1", "v1", state="running")
    elif kind is Attempt:
        obj = Attempt("attempt-1", "task-1", "codex",
                      state="stopping" if target == "cancelled" else "running")
    else:
        obj = Run("run-1", "v1")
    before = asdict(obj)
    with pytest.raises(ValueError, match="reason_code"):
        transition(obj, target, reason)
    assert asdict(obj) == before


@pytest.mark.parametrize("target", ["failed", "cancelled", "completed"])
def test_run_paths_and_terminal_states(target):
    run = Run("run-1", "v1")
    transition(run, "blocked", "auth_required")
    transition(run, "running")
    assert run.reason_code is None
    transition(run, target, None if target == "completed" else "execution_problem")
    with pytest.raises(IllegalTransition):
        transition(run, "running")


def test_run_roundtrip_and_file_layout(tmp_path):
    run = example_run()
    save_run(run, tmp_path)
    restored = load_run(run.run_id, tmp_path)
    assert restored == run
    assert restored is not run
    assert isinstance(restored.tasks["task-1"], Task)
    assert isinstance(restored.attempts["attempt-1"], Attempt)
    assert restored.attempts["attempt-2"].state == "unknown"
    directory = tmp_path / "runs" / "run-1"
    assert {p.relative_to(directory).as_posix() for p in directory.rglob("*")
            if p.is_file()} == {
        "summary.json", "tasks/task-1.json", "tasks/task-2.json",
        "attempts/attempt-1/attempt.json", "attempts/attempt-2/attempt.json",
    }
    text = (directory / "summary.json").read_text(encoding="utf-8")
    assert "검증 결과" in text
    assert text == json.dumps(json.loads(text), ensure_ascii=False,
                              indent=2, sort_keys=True) + "\n"


def test_save_updates_and_load_ignores_unreferenced_records(tmp_path):
    run = example_run()
    save_run(run, tmp_path)
    del run.tasks["task-2"]
    run.tasks["task-1"].attempts.remove("attempt-2")
    del run.attempts["attempt-2"]
    run.summary["note"] = "갱신됨"
    save_run(run, tmp_path)
    assert load_run(run.run_id, tmp_path) == run


@pytest.mark.parametrize("second_id", ["attempt-1", "attempt-2"])
def test_accept_is_idempotent_and_keeps_state(second_id):
    task = Task("task-1", "v1", state="verifying", attempts=["attempt-1", "attempt-2"])
    assert accept_attempt(task, "attempt-1") is True
    before = asdict(task)
    assert accept_attempt(task, second_id) is False
    assert asdict(task) == before
    assert task.state == "verifying"


@pytest.mark.parametrize("accepted", [None, "attempt-1"])
def test_accept_rejects_attempt_outside_task(accepted):
    task = Task("task-1", "v1", accepted_attempt_id=accepted, attempts=["attempt-1"])
    before = asdict(task)
    with pytest.raises(ValueError, match="does not belong"):
        accept_attempt(task, "missing")
    assert asdict(task) == before


@pytest.mark.parametrize("verdict", ["pass", "fail", "undecidable"])
def test_check_roundtrip(tmp_path, verdict):
    check = example_check(verdict)
    save_check(check, tmp_path, "run-1")
    assert load_checks(tmp_path, "run-1") == [check]
    assert (tmp_path / "runs/run-1/checks/contract-v1.attempt-1.json").is_file()


def test_multiple_checks_and_missing_directory(tmp_path):
    assert load_checks(tmp_path, "run-1") == []
    checks = [example_check(attempt_id=f"attempt-{n}") for n in (1, 2)]
    for check in reversed(checks):
        save_check(check, tmp_path, "run-1")
    assert load_checks(tmp_path, "run-1") == checks
    save_check(checks[0], tmp_path, "run-1")
    assert load_checks(tmp_path, "run-1") == checks


def test_invalid_verdict_rejected_on_creation_save_and_load(tmp_path):
    with pytest.raises(ValueError, match="verdict"):
        example_check("approved")
    check = example_check()
    save_check(check, tmp_path, "run-1")
    check.verdict = "approved"
    with pytest.raises(ValueError, match="verdict"):
        save_check(check, tmp_path, "run-1")
    path = tmp_path / "runs/run-1/checks/contract-v1.attempt-1.json"
    path.write_text(json.dumps(asdict(check)), encoding="utf-8")
    with pytest.raises(ValueError, match="verdict"):
        load_checks(tmp_path, "run-1")


def test_failed_replace_preserves_original_and_removes_temporary(tmp_path, monkeypatch):
    check = example_check()
    save_check(check, tmp_path, "run-1")
    path = tmp_path / "runs/run-1/checks/contract-v1.attempt-1.json"
    original = path.read_bytes()

    def fail_replace(source, destination):
        assert source.parent == destination.parent
        assert json.loads(source.read_text(encoding="utf-8"))["verdict"] == "fail"
        raise OSError("simulated replace failure")

    monkeypatch.setattr(records.os, "replace", fail_replace)
    check.verdict = "fail"
    with pytest.raises(OSError, match="simulated"):
        save_check(check, tmp_path, "run-1")
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]


# W16d: a Windows sharing violation on the temporary file is retried for a bounded time.
# The bound is stated here rather than derived from SHARING_RETRY_DELAYS, so changing the
# delay list cannot silently change what the tests accept (W16i).
MAX_CALLS_PER_OPERATION = 9
MAX_WAIT_PER_OPERATION = 2.0


@pytest.fixture
def no_wait(monkeypatch):
    waits = []
    monkeypatch.setattr(records, "RETRY_SHARING_VIOLATIONS", True)
    monkeypatch.setattr(records.time, "sleep", waits.append)
    return waits


def saved_check(tmp_path):
    check = example_check()
    save_check(check, tmp_path, "run-1")
    path = tmp_path / "runs/run-1/checks/contract-v1.attempt-1.json"
    check.verdict = "fail"
    return check, path, path.read_bytes()


@pytest.mark.skipif(records.os.name != "nt", reason="Windows sharing violation")
def test_replace_succeeds_after_another_handle_releases_temporary(tmp_path, monkeypatch):
    import threading
    check, path, _ = saved_check(tmp_path)
    real_replace, calls, errors = records.os.replace, [], []

    def replace(source, destination):
        calls.append(source)
        if len(calls) == 1:  # a real second handle without delete sharing, released shortly
            handle = open(source, "rb")
            threading.Timer(0.15, handle.close).start()
        try:
            return real_replace(source, destination)
        except PermissionError as error:
            errors.append(error.winerror)
            raise

    monkeypatch.setattr(records.os, "replace", replace)
    save_check(check, tmp_path, "run-1")
    assert errors and set(errors) == {32} and len(calls) == len(errors) + 1
    assert load_checks(tmp_path, "run-1")[0].verdict == "fail"
    assert list(path.parent.iterdir()) == [path]


def test_persistent_sharing_violation_raises_replace_error_within_bound(tmp_path, monkeypatch, no_wait):
    check, path, original = saved_check(tmp_path)
    calls = []

    def replace(source, destination):
        calls.append(source)
        raise PermissionError(32, "simulated sharing violation", str(source), None, str(destination))

    monkeypatch.setattr(records.os, "replace", replace)
    with pytest.raises(PermissionError, match="simulated sharing"):
        save_check(check, tmp_path, "run-1")
    assert len(calls) == MAX_CALLS_PER_OPERATION
    assert no_wait == list(records.SHARING_RETRY_DELAYS)
    assert 1.0 <= sum(no_wait) <= MAX_WAIT_PER_OPERATION
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]


def test_cleanup_failure_does_not_hide_replace_error(tmp_path, monkeypatch, no_wait):
    check, path, original = saved_check(tmp_path)
    unlinks = []

    def replace(source, destination):
        raise PermissionError(32, "simulated replace violation", str(source), None, str(destination))

    def unlink(self, missing_ok=False):
        unlinks.append(self)
        raise PermissionError(32, "simulated unlink violation", str(self))

    monkeypatch.setattr(records.os, "replace", replace)
    monkeypatch.setattr(records.Path, "unlink", unlink)
    with pytest.raises(PermissionError, match="simulated replace") as raised:
        save_check(check, tmp_path, "run-1")
    assert raised.value.filename2 == str(path)
    assert len(unlinks) == MAX_CALLS_PER_OPERATION
    # replace and cleanup each have their own budget, so the worst case waits about 4 seconds
    assert len(no_wait) == 2 * (MAX_CALLS_PER_OPERATION - 1)
    assert sum(no_wait) <= 2 * MAX_WAIT_PER_OPERATION
    assert path.read_bytes() == original
    assert [p.suffix for p in path.parent.iterdir()].count(".tmp") == 1  # kept, not hidden


def test_cleanup_retries_a_sharing_violation(tmp_path, monkeypatch, no_wait):
    check, path, original = saved_check(tmp_path)
    real_unlink, unlinks = records.Path.unlink, []

    def replace(source, destination):
        raise OSError("simulated replace failure")

    def unlink(self, missing_ok=False):
        unlinks.append(self)
        if len(unlinks) < 3:
            raise PermissionError(32, "simulated unlink violation", str(self))
        return real_unlink(self, missing_ok)

    monkeypatch.setattr(records.os, "replace", replace)
    monkeypatch.setattr(records.Path, "unlink", unlink)
    with pytest.raises(OSError, match="simulated replace failure"):
        save_check(check, tmp_path, "run-1")
    assert len(unlinks) == 3 and list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("error,windows", [(OSError("other failure"), True),
                                           (FileNotFoundError("missing"), True),
                                           (PermissionError(13, "denied"), False)])
def test_only_windows_sharing_violations_are_retried(tmp_path, monkeypatch, no_wait, error, windows):
    check, path, original = saved_check(tmp_path)
    monkeypatch.setattr(records, "RETRY_SHARING_VIOLATIONS", windows)
    calls = []

    def replace(source, destination):
        calls.append(source)
        raise error

    monkeypatch.setattr(records.os, "replace", replace)
    with pytest.raises(type(error)):
        save_check(check, tmp_path, "run-1")
    assert len(calls) == 1 and no_wait == []
    assert path.read_bytes() == original and list(path.parent.iterdir()) == [path]


def test_non_json_value_preserves_original(tmp_path):
    check = example_check()
    save_check(check, tmp_path, "run-1")
    check.evidence["bad"] = object()
    with pytest.raises(TypeError):
        save_check(check, tmp_path, "run-1")
    assert load_checks(tmp_path, "run-1") == [example_check()]
    assert not list(tmp_path.rglob("*.tmp"))


def test_utc_timestamps_and_independent_defaults():
    run = Run("run-1", "v1")
    assert datetime.fromisoformat(run.created_at).utcoffset() == timedelta(0)
    assert Run("run-2", "v1").tasks is not run.tasks
    attempt = Attempt("attempt-1", "task-1", "codex",
                      started_at="2026-09-12T10:00:00+09:00")
    assert attempt.started_at == "2026-09-12T01:00:00+00:00"
    with pytest.raises(ValueError, match="timezone"):
        Run("run-1", "v1", created_at="2026-09-12T00:00:00")


@pytest.mark.parametrize("record_id", ["../outside", "a/b", "a\\b", "", "CON", "a.b"])
def test_invalid_ids_cannot_escape_or_collide(tmp_path, record_id):
    with pytest.raises(ValueError, match="record ID"):
        save_run(Run(record_id, "v1"), tmp_path)
    with pytest.raises(ValueError, match="record ID"):
        load_run(record_id, tmp_path)
    with pytest.raises(ValueError, match="record ID"):
        save_check(example_check(attempt_id=record_id), tmp_path, "run-1")
    assert list(tmp_path.iterdir()) == []


def test_inconsistent_run_rejected_before_writes(tmp_path):
    run = example_run()
    del run.attempts["attempt-1"]
    with pytest.raises(ValueError, match="missing or mismatched"):
        save_run(run, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_missing_referenced_record_is_not_silently_dropped(tmp_path):
    run = example_run()
    save_run(run, tmp_path)
    (tmp_path / "runs/run-1/attempts/attempt-1/attempt.json").unlink()
    with pytest.raises(FileNotFoundError):
        load_run("run-1", tmp_path)


def test_tampered_record_identity_rejected(tmp_path):
    save_run(example_run(), tmp_path)
    path = tmp_path / "runs/run-1/tasks/task-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["task_id"] = "task-other"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid task"):
        load_run("run-1", tmp_path)


@pytest.mark.parametrize("kind", ["task", "attempt"])
def test_case_collisions_rejected_before_writes(tmp_path, kind):
    run = example_run()
    if kind == "task":
        run.tasks["TASK-1"] = Task("TASK-1", "v1")
    else:
        run.attempts["ATTEMPT-1"] = Attempt("ATTEMPT-1", "task-1", "codex")
        run.tasks["task-1"].attempts.append("ATTEMPT-1")
    with pytest.raises(ValueError, match="case-insensitive"):
        save_run(run, tmp_path)
    assert list(tmp_path.iterdir()) == []
