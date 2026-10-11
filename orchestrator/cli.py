"""W7 single-task CLI. Run from the target repository or supply --repo.

Exit codes: 0 success/already accepted, 1 failed/invalid, 2 blocked/busy, 3 cancelled.
Only registered checks accept a task. Adapter messages are evidence, not verdicts.
One OS lease owns run-task for a run; a short OS lock serializes records with stop.
Both locks are released by the OS on process exit. Existing terminal tasks never
restart. A start without a persisted identity or an interrupted verification
requires manual inspection; automatically starting another worker is unsafe.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
import uuid

from . import process, records, verify, workspace
from .adapters.codex import CodexAdapter, CodexSpec
from .adapters.claude import ClaudeAdapter, ClaudeSpec, EFFORT_LEVELS, PERMISSION_MODES


class CLIError(RuntimeError):
    """An actionable configuration or execution problem."""


class Busy(CLIError):
    """Another CLI owns this run or its records lock."""


def _id(value):
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
    return (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value)
            and value.upper() not in reserved)


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def _relative(value):
    if not _text(value):
        return False
    value = value.replace("\\", "/")
    return not (value.startswith("/") or ":" in value or ".." in PurePosixPath(value).parts)


def _unresolved(value):
    if isinstance(value, str):
        return value.startswith("TO_BE_")
    if isinstance(value, dict):
        return any(_unresolved(v) for v in value.values())
    if isinstance(value, list):
        return any(_unresolved(v) for v in value)
    return False


def _read(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                               allow_nan=False) + "\n", encoding="utf-8")


def validate_task(spec: dict, config: dict, checks_dir: Path, *,
                  plan_task_ids: frozenset | None = None) -> list[str]:
    """Read-only JSON/definition validation. Does not execute Git or any process.

    run-task passes no plan_task_ids, so depends_on must stay empty (W7 scope).
    validate-plan passes the plan's task IDs and checks the references itself.
    """
    errors = []
    if not isinstance(spec, dict) or not isinstance(config, dict):
        return ["task specification and config must be JSON objects"]
    if _unresolved(spec) or _unresolved(config):
        errors.append("unresolved TO_BE_ value")
    if type(spec.get("schema_version")) is not int or spec["schema_version"] != 1:
        errors.append("schema_version must be 1")
    for key in ("run_id", "task_id"):
        if not _id(spec.get(key)):
            errors.append(f"{key} must be a portable record ID (1-64 characters)")
    for key in ("goal", "base_commit", "route_id"):
        if not _text(spec.get(key)):
            errors.append(f"{key} must be a nonempty string")
    if plan_task_ids is None:
        if spec.get("depends_on") != []:
            errors.append("depends_on must be an empty array in W7")
    elif not isinstance(spec.get("depends_on"), list) or not all(
            isinstance(v, str) for v in spec["depends_on"]):
        errors.append("depends_on must be a list of task IDs")
    for key in ("allowed_write_paths", "deliverables", "checks"):
        values = spec.get(key)
        predicate = _id if key == "checks" else _relative
        if (not isinstance(values, list) or not values or
                not all(predicate(v) for v in values)):
            errors.append(f"{key} must be a nonempty list of valid paths/IDs")
        elif key == "checks":
            if len({v.casefold() for v in values}) != len(values):
                errors.append("checks must not contain duplicate IDs")
            for check_id in values:
                try:
                    verify.load_check_definition(checks_dir, check_id)
                except verify.CheckDefinitionError as error:
                    errors.append(f"check {check_id}: {error}")
    routes = config.get("routes", {})
    route_id = spec.get("route_id")
    route = routes.get(route_id) if isinstance(routes, dict) and isinstance(route_id, str) else None
    if not isinstance(route, dict):
        errors.append("route_id is not a configured route")
    else:
        tool = route.get("tool")
        if tool not in ("codex", "claude"):
            errors.append("route.tool must be codex or claude")
        else:
            spec_type = CodexSpec if tool == "codex" else ClaudeSpec
            unknown = set(route) - {f.name for f in fields(spec_type)} - {"tool", "model_reasoning_effort"}
            if unknown:
                errors.append(f"unsupported route fields: {', '.join(sorted(unknown))}")
            if not _text(route.get("model")):
                errors.append("route.model must be a nonempty string")
            paths = config.get("paths", {})
            if not isinstance(paths, dict) or not _text(paths.get(tool)):
                errors.append(f"config.paths.{tool} must name the CLI executable")
            for key in ("ephemeral", "skip_git_repo_check", "session_persistence", "approve_for_me"):
                if key in route and type(route[key]) is not bool:
                    errors.append(f"route.{key} must be boolean")
            if tool == "codex" and route.get("sandbox", "read-only") not in (
                    "read-only", "workspace-write", "danger-full-access"):
                errors.append("unsupported Codex sandbox")
            if tool == "codex" and route.get("approve_for_me") is True and route.get("sandbox", "read-only") != "workspace-write":
                errors.append("approve_for_me requires workspace-write sandbox")
            if tool == "claude" and route.get("permission_mode", "acceptEdits") not in tuple(PERMISSION_MODES):
                errors.append("unsupported Claude permission_mode")
            effort = route.get("effort", route.get("model_reasoning_effort"))
            if effort is not None and (not _text(effort) or (tool == "claude" and effort not in EFFORT_LEVELS)):
                errors.append("unsupported route effort")
            for key in ("allowed_tools", "add_dirs"):
                if key in route and (not isinstance(route[key], list) or not all(_text(v) for v in route[key])):
                    errors.append(f"route.{key} must be a string list")
            for key in ("max_turns", "max_budget_usd"):
                if key in route:
                    value = route[key]
                    if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0 or
                            (key == "max_turns" and type(value) is not int)):
                        errors.append(f"route.{key} must be positive")
    defaults, overrides = config.get("limits", {}), spec.get("limits", {})
    if not isinstance(defaults, dict) or not isinstance(overrides, dict):
        errors.append("limits must be an object")
    else:
        for key in ("timeout_seconds", "max_attempts_total"):
            value = overrides.get(key, defaults.get(key))
            if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0 or
                    (key == "max_attempts_total" and type(value) is not int)):
                errors.append(f"limits.{key} must be a positive {'integer' if key == 'max_attempts_total' else 'number'}")
    environment = config.get("environment", {})
    if not isinstance(environment, dict):
        errors.append("environment must be an object")
    else:
        stripped = environment.get("strip_env", list(process.DEFAULT_STRIP_ENV))
        extra = environment.get("extra_env", {})
        if not isinstance(stripped, list) or not all(_text(v) and "=" not in v for v in stripped):
            errors.append("environment.strip_env must contain environment variable names")
        if (not isinstance(extra, dict) or not all(_text(k) and "=" not in k and isinstance(v, str)
                                                 and "\0" not in v for k, v in extra.items())):
            errors.append("environment.extra_env must map names to strings")
        elif any(k.upper() in {v.upper() for v in process.DEFAULT_STRIP_ENV} for k in extra):
            errors.append("environment.extra_env must not restore credential variables")
    return errors


def adapter_for(route, config, evidence: Path, config_dir: Path):
    """Resolve one installed adapter, keeping all auxiliary outputs outside worktree.

    Codex's config model_reasoning_effort is mapped to CodexSpec.effort. Schema
    inputs are copied into evidence; their original paths resolve against config.
    """
    values = dict(route)
    tool = values.pop("tool")
    alias = values.pop("model_reasoning_effort", None)
    if alias is not None:
        values.setdefault("effort", alias)
    executable = Path(config["paths"][tool])
    if not executable.is_absolute():
        executable = config_dir / executable
    schema_key = "output_schema_path" if tool == "codex" else "json_schema_path"
    schema = values.pop(schema_key, None)
    if schema is not None:
        source = Path(schema)
        data = _read(source if source.is_absolute() else config_dir / source)
        if not isinstance(data, dict):
            raise CLIError("output schema must be a JSON object")
        target = evidence / "output-schema.json"
        _write(target, data)
        values[schema_key] = target.resolve()
    if tool == "codex":
        values["last_message_path"] = (evidence / "last-message.txt").resolve()
        return CodexAdapter(executable), CodexSpec(**values)
    if "allowed_tools" in values:
        values["allowed_tools"] = tuple(values["allowed_tools"])
    if "add_dirs" in values:
        values["add_dirs"] = tuple((config_dir / p).resolve() for p in values["add_dirs"])
    return ClaudeAdapter(executable), ClaudeSpec(**values)


@contextmanager
def _lock(path: Path, *, timeout=60.0, create=True):
    """An OS-owned byte lock; never delete lock files (which would split owners)."""
    if not create and not path.exists():
        yield
        return
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b" if create else "r+b")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Busy("run is busy; another CLI owns its execution or records") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def _git(repo, *args):
    # CLI needs HEAD and a durable ref; workspace's private helpers are not API.
    env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX"):
        env.pop(name, None)
    result = subprocess.run(["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(repo), *args],
                            capture_output=True, timeout=30, check=True, env=env)
    return result.stdout.decode("utf-8", "surrogateescape").strip()


def _now():
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self, root, run_id):
        if not _id(run_id):
            raise CLIError("invalid run_id")
        self.root, self.run_id = Path(root).resolve(), run_id
        self.directory = self.root / "runs" / run_id

    def lock(self, **kwargs):
        return _lock(self.directory / ".state.lock", **kwargs)

    def load(self):
        return records.load_run(self.run_id, self.root)

    def save(self, run):
        records.save_run(run, self.root)

    def move(self, run, obj, state, reason=None):
        records.transition(obj, state, reason)
        self.save(run)

    def finish(self, run, task, state, reason=None):
        if task.state != state:
            self.move(run, task, state, reason)
        else:
            task.reason_code = reason
            self.save(run)
        run_state = "completed" if state == "accepted" else state
        if run.state != run_state:
            self.move(run, run, run_state, reason)
        return {"accepted": 0, "failed": 1, "blocked": 2, "cancelled": 3}[state]


def _identity(attempt):
    snap = attempt.process
    if (not isinstance(snap, dict) or type(snap.get("pid")) is not int or snap["pid"] <= 0
            or not snap.get("started_at") or not snap.get("argv_hash")):
        raise CLIError("attempt has no usable persisted process identity")
    return process.identity_matches(snap, snap["pid"])


def _mark_unknown(store, run, attempt, reason):
    # created is persisted before launch intent; it cannot transition to unknown.
    if attempt.state == "created":
        store.move(run, attempt, "failed", "launch_not_started")
    elif attempt.state not in {"unknown", "exited", "cancelled", "failed"}:
        store.move(run, attempt, "unknown", reason)


def _recover(store, run, task):
    if (task.state == "verifying" or run.summary.get("verification_pending")
            or run.summary.get("verification_aborted")):
        run.summary.setdefault("verification_pending", task.attempts[-1] if task.attempts else "unknown")
        return store.finish(run, task, "blocked", "verification_requires_inspection")
    for aid in task.attempts:
        attempt = run.attempts[aid]
        if attempt.state in {"exited", "failed", "cancelled"}:
            continue
        if attempt.state == "created":
            _mark_unknown(store, run, attempt, "launch_not_started")
            continue
        if attempt.state == "unknown" and attempt.reason_code == "restart_unverifiable":
            continue
        try:
            alive = _identity(attempt)
        except (process.ProcessEnumerationError, CLIError, ValueError, TypeError):
            _mark_unknown(store, run, attempt, "restart_identity_unavailable")
            return store.finish(run, task, "blocked", "restart_identity_unavailable")
        if alive:
            raise Busy("an existing attempt is still running; no new attempt started")
        if attempt.state == "unknown" or attempt.state == "stopping":
            return store.finish(run, task, "blocked", "process_tree_requires_inspection")
        _mark_unknown(store, run, attempt, "restart_unverifiable")
    return None


def _ready(store, run, task):
    if run.state == "blocked":
        store.move(run, run, "running")
    if task.state in {"running", "verifying"}:
        store.move(run, task, "blocked", "retry_preparation")
    if task.state == "blocked":
        store.move(run, task, "ready")
    if task.state == "planned":
        store.move(run, task, "ready")
    if task.state == "ready":
        store.move(run, task, "running")


def _cleanup(store, run, attempt, repo):
    """Only the exact workspace assigned to this attempt may be removed."""
    expected = store.directory / "attempts" / attempt.attempt_id / "workspace"
    if (not expected.resolve().is_relative_to(store.directory.resolve()) or not attempt.workspace
            or Path(attempt.workspace).resolve() != expected.resolve()):
        raise CLIError("workspace path does not match this attempt's owned directory")
    if not expected.exists():
        return
    try:
        workspace.remove_worktree(repo, expected, force=True)
    except workspace.WorkspaceError as error:
        run.summary.setdefault("cleanup_errors", {})[attempt.attempt_id] = str(error)
        store.save(run)


def run_task(spec, config, repo: Path, root: Path, checks_dir: Path, config_dir: Path,
             *, adapter_factory=adapter_for) -> int:
    """Execute one task with a per-run lease and short state transactions.

    Verification fail is terminal as required by W7. Timeout/start failures may
    consume retries. A timeout with unknown termination never retries. Artifact
    commits are retained by refs/orchestrator/<attempt-id> before worktree removal.
    """
    store = RunStore(root, spec["run_id"])
    with _lock(store.directory / ".runner.lock", timeout=0):
        return _run_owned(store, spec, config, repo.resolve(), checks_dir.resolve(),
                          config_dir.resolve(), adapter_factory)


def _run_owned(store, spec, config, repo, checks_dir, config_dir, factory):
    limits = {**config.get("limits", {}), **spec.get("limits", {})}
    budget, timeout = limits["max_attempts_total"], limits["timeout_seconds"]
    fingerprint = hashlib.sha256(json.dumps({"task": spec, "repo": str(repo), "limits": limits,
                                             "route": config["routes"][spec["route_id"]]},
                                            sort_keys=True).encode()).hexdigest()
    with store.lock():
        if (store.directory / "summary.json").exists():
            run = store.load()
            if set(run.tasks) != {spec["task_id"]}:
                raise CLIError("W7 requires exactly the existing task for this run_id")
            previous = run.summary.get("spec_fingerprint")
            if previous is not None and previous != fingerprint:
                raise CLIError("task specification/route changed; use a new run_id")
        else:
            task = records.Task(spec["task_id"], str(spec["schema_version"]))
            run = records.Run(spec["run_id"], task.plan_version, tasks={task.task_id: task})
        task = run.tasks[spec["task_id"]]
        if task.state in {"accepted", "integrated"}:
            if task.accepted_attempt_id is None:
                raise CLIError("accepted task has no accepted attempt record")
            if run.state == "blocked":
                store.move(run, run, "running")
            if run.state == "running":
                store.move(run, run, "completed")
            return 0
        if task.state in {"failed", "cancelled"}:
            if run.state in {"running", "blocked"}:
                store.move(run, run, task.state, task.reason_code or "terminal_task_recovered")
            return 3 if task.state == "cancelled" else 1
        recovered = _recover(store, run, task)
        if recovered is not None:
            return recovered
        run.summary.update(spec_fingerprint=fingerprint, repo=str(repo))
        _ready(store, run, task)
        store.save(run)

    while True:
        handle = None
        with store.lock():
            run = store.load()
            task = run.tasks[spec["task_id"]]
            if task.state == "cancelled":
                return 3
            if len(task.attempts) >= budget:
                return store.finish(run, task, "failed", "attempt_budget_exhausted")
            aid = "attempt-" + uuid.uuid4().hex
            attempt_dir = store.directory / "attempts" / aid
            evidence = attempt_dir / "evidence"
            evidence.mkdir(parents=True)
            attempt = records.Attempt(aid, task.task_id, spec["route_id"],
                                      workspace=str(attempt_dir / "workspace"))
            run.attempts[aid] = attempt
            task.attempts.append(aid)
            store.save(run)
            try:
                # A ref is pinned after the first creation; retries cannot follow a moving branch.
                wt = workspace.create_worktree(repo, run.summary.get("base_commit", spec["base_commit"]),
                                               Path(attempt.workspace))
                run.summary.setdefault("base_commit", _git(wt, "rev-parse", "HEAD"))
                store.save(run)
            except (workspace.WorkspaceError, subprocess.SubprocessError, OSError) as error:
                store.move(run, attempt, "failed", "workspace_unavailable")
                run.summary["detail"] = str(error)
                return store.finish(run, task, "blocked", "workspace_unavailable")
            try:
                prompt = evidence / "prompt.txt"
                prompt.write_text(
                    spec["goal"] + "\n\nTask constraints:\n" + json.dumps({
                        "allowed_write_paths": spec["allowed_write_paths"],
                        "deliverables": spec["deliverables"], "checks": spec["checks"]},
                        ensure_ascii=False, indent=2) +
                    "\nCommit your deliverables in this workspace. Do not change files outside the allowed paths.\n",
                    encoding="utf-8")
                adapter, resolved_spec = factory(config["routes"][spec["route_id"]], config, evidence, config_dir)
                argv = adapter.build_command(resolved_spec, wt, prompt)
                store.move(run, attempt, "starting")
                env = config.get("environment", {})
                stripped = list(dict.fromkeys([*process.DEFAULT_STRIP_ENV, *env.get("strip_env", [])]))
                handle = process.start(argv, cwd=wt, stdout_path=evidence / "stdout.log",
                                       stderr_path=evidence / "stderr.log", stdin_path=prompt,
                                       strip_env=stripped, env_extra=env.get("extra_env", {}))
                attempt.process = handle.snapshot()
                attempt.started_at = handle.started_at
                store.move(run, attempt, "running")
            except (CLIError, OSError, ValueError, TypeError) as error:
                if handle is not None:
                    stopped = handle.stop().to_dict()
                    _write(evidence / "start-cleanup.json", stopped)
                    if stopped["state"] != "stopped":
                        _mark_unknown(store, run, attempt, "start_cleanup_unknown")
                        return store.finish(run, task, "blocked", "start_cleanup_unknown")
                _write(evidence / "start-error.json", {"error_type": type(error).__name__})
                store.move(run, attempt, "failed", "start_failed")
                _cleanup(store, run, attempt, repo)
                continue

        timed_out = False
        try:
            exit_code = handle.wait(timeout)
        except TimeoutError:
            timed_out = True
            exit_code = None
        except KeyboardInterrupt:
            stop_task(store.root, store.run_id, spec["task_id"])
            return 3

        with store.lock():
            # stop may have committed cancellation while wait was outside the lock.
            run = store.load()
            task = run.tasks[spec["task_id"]]
            attempt = run.attempts[aid]
            if task.state == "cancelled":
                _cleanup(store, run, attempt, repo)
                return 3
            if task.state == "blocked" or attempt.state == "unknown":
                return 2
            attempt.ended_at = _now()
            attempt.process["exit_code"] = exit_code
            if timed_out:
                store.move(run, attempt, "stopping")
                stopped = handle.stop().to_dict()
                _write(evidence / "timeout.json", stopped)
                attempt.ended_at = _now() if stopped["state"] == "stopped" else None
                if stopped["state"] != "stopped":
                    store.move(run, attempt, "unknown", "timeout_stop_unknown")
                    return store.finish(run, task, "blocked", "timeout_stop_unknown")
                store.move(run, attempt, "failed", "timeout")
                _cleanup(store, run, attempt, repo)
                continue
            store.move(run, attempt, "exited")
            store.move(run, task, "verifying")
            try:
                parsed = adapter.parse_events(evidence / "stdout.log")
                _write(evidence / "adapter-result.json", asdict(parsed))
            except (OSError, ValueError, TypeError):
                _write(evidence / "adapter-result.json", {"status": "unreadable"})
            subject = _git(wt, "rev-parse", "HEAD")
            ref = f"refs/orchestrator/{aid}"
            _git(repo, "update-ref", ref, subject, "0" * len(subject))
            manifest = evidence / "result-manifest.json"
            _write(manifest, {"subject_commit": subject, "base_commit": run.summary["base_commit"], "ref": ref})
            attempt.result_manifest = str(manifest)
            run.summary["verification_pending"] = aid
            store.save(run)

        verdict = verify.run_checks(repo=repo, subject_commit=subject, check_ids=spec["checks"],
                                    checks_dir=checks_dir, evidence_root=evidence, attempt_id=aid,
                                    base_commit=run.summary["base_commit"],
                                    allowed_write_paths=spec["allowed_write_paths"])

        with store.lock():
            run = store.load()
            task = run.tasks[spec["task_id"]]
            attempt = run.attempts[aid]
            for check in verdict.checks:
                records.save_check(check, store.root, store.run_id)
            run.summary.pop("verification_pending", None)
            run.summary["verification"] = {"aborted_reason": verdict.aborted_reason,
                                            "worktree_path": verdict.worktree_path,
                                            "worktree_error": verdict.worktree_error}
            if verdict.aborted_reason:
                run.summary["verification_aborted"] = verdict.aborted_reason
            store.save(run)
            if verdict.aborted_reason or verdict.worktree_error or any(c.verdict == "undecidable" for c in verdict.checks):
                code = store.finish(run, task, "blocked", "verification_undecidable")
            elif any(c.verdict == "fail" for c in verdict.checks):
                code = store.finish(run, task, "failed", "check_failed")
            elif (verdict.all_passed() and [c.check_id for c in verdict.checks] == spec["checks"]
                  and all(verify.is_check_current(c, subject) and c.attempt_id == aid for c in verdict.checks)):
                if not records.accept_attempt(task, aid):
                    raise CLIError("attempt already accepted; refusing a second acceptance")
                code = store.finish(run, task, "accepted")
            else:
                code = store.finish(run, task, "blocked", "incomplete_check_results")
            _cleanup(store, run, attempt, repo)
            return code


def stop_task(root: Path, run_id: str, task_id: str) -> int:
    """Stop only a matching worker identity, then verify the captured tree is gone.

    taskkill's exit code alone is not proof of cancellation. During verification
    W6 owns its subprocess handles; this command reports that phase as busy.
    """
    store = RunStore(root, run_id)
    if not (store.directory / "summary.json").is_file():
        raise CLIError("run not found")
    with store.lock(timeout=60):
        run = store.load()
        if task_id not in run.tasks:
            raise CLIError("task not found")
        task = run.tasks[task_id]
        pending = [run.attempts[a] for a in task.attempts
                   if run.attempts[a].state in {"starting", "running", "stopping"}]
        if not pending:
            print("No active worker to stop." if task.state != "verifying" else "Verification is active; W6 owns its processes.")
            return 2 if task.state == "verifying" else 0
        attempt = pending[-1]
        try:
            if not _identity(attempt):
                print("The recorded worker is no longer present; no process was killed.")
                return 2
            before = process.process_tree(attempt.process["pid"])
            if not before or not _identity(attempt):
                raise CLIError("worker identity changed before stop")
        except (process.ProcessEnumerationError, CLIError, ValueError, TypeError):
            print("Cannot confirm worker identity; no process was killed.")
            return 2
        if attempt.state != "stopping":
            store.move(run, attempt, "stopping")
        pid = attempt.process["pid"]
        try:
            killed = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                    capture_output=True, timeout=30, check=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            remaining = before
            deadline = time.monotonic() + 5
            while remaining and time.monotonic() < deadline:
                after = {p.pid: p for p in process.list_processes()}
                remaining = [p for p in before if p.pid in after and (
                    p.created_at is None or after[p.pid].created_at is None or
                    abs(p.created_at - after[p.pid].created_at) <= process.CREATION_MATCH_TOLERANCE_SECONDS)]
                if remaining:
                    time.sleep(0.1)
            detail = {"taskkill_exit_code": killed.returncode, "survivor_pids": [p.pid for p in remaining]}
        except (OSError, subprocess.SubprocessError, process.ProcessEnumerationError) as error:
            remaining = before
            detail = {"error_type": type(error).__name__}
        _write(store.directory / "attempts" / attempt.attempt_id / "evidence/stop.json", detail)
        attempt.ended_at = _now() if not remaining else None
        if remaining:
            store.move(run, attempt, "unknown", "stop_unverified")
            return store.finish(run, task, "blocked", "stop_unverified")
        store.move(run, attempt, "cancelled", "user_stop")
        store.finish(run, task, "cancelled", "user_stop")
        return 0


def main(argv=None, *, adapter_factory=adapter_for) -> int:
    """Console entry point; injectable adapter factory is for offline W7 tests."""
    parser = argparse.ArgumentParser(prog="orch")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "run-task", "status", "stop"):
        child = sub.add_parser(command)
        child.add_argument("--repo", type=Path, default=Path.cwd(), help="target repository root (default: cwd)")
        child.add_argument("--run-root", type=Path)
        if command in {"validate", "run-task"}:
            child.add_argument("task_spec", type=Path)
            child.add_argument("--config", type=Path)
            child.add_argument("--checks-dir", type=Path)
        else:
            child.add_argument("run_id")
            child.add_argument("task_id", nargs="?" if command == "status" else None)
    child = sub.add_parser("validate-plan")
    child.add_argument("--repo", type=Path, default=Path.cwd(), help="target repository root (default: cwd)")
    child.add_argument("plan", type=Path)
    child.add_argument("--config", type=Path)
    child.add_argument("--checks-dir", type=Path)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    root = (getattr(args, "run_root", None) or repo).resolve()
    try:
        if args.command == "validate-plan":
            from .plan import validate_plan  # plan imports this module
            config = _read((args.config or repo / "config.json").resolve())
            errors, order = validate_plan(args.plan, config, (args.checks_dir or repo / "checks").resolve())
            if errors:
                print("\n".join(errors))
                return 1
            executable = sum("status" not in t for t in _read(args.plan)["tasks"])
            print(f"Valid plan: {len(order)} tasks ({executable} executable)")
            print("order: " + " -> ".join(order))
            return 0
        if args.command == "status":
            store = RunStore(root, args.run_id)
            with store.lock(create=False):
                run = store.load()
                value = asdict(run.tasks[args.task_id]) if args.task_id else asdict(run)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        if args.command == "stop":
            return stop_task(root, args.run_id, args.task_id)
        config_path = (args.config or repo / "config.json").resolve()
        checks_dir = (args.checks_dir or repo / "checks").resolve()
        spec, config = _read(args.task_spec), _read(config_path)
        errors = validate_task(spec, config, checks_dir)
        if errors:
            print("\n".join(errors))
            return 1
        if args.command == "validate":
            print("Valid task specification.")
            return 0
        code = run_task(spec, config, repo, root, checks_dir, config_path.parent,
                        adapter_factory=adapter_factory)
        run = records.load_run(spec["run_id"], root)
        task = run.tasks[spec["task_id"]]
        print(f"{run.run_id}/{task.task_id}: {task.state} ({task.reason_code or 'ok'})")
        if run.summary.get("detail"):
            print(run.summary["detail"])
        return code
    except Busy as error:
        print(str(error))
        return 2
    except (CLIError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(f"{type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
