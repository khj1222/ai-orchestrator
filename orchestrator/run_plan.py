"""W10c plan execution: run a validated plan in dependency order and integrate.

Each executable task runs through the same attempt loop as run-task (W7). A task
starts from the integration line once all of its prerequisites are integrated,
and that commit is also its scope-check base. Accepted results are merged one at
a time, in execution order, with --no-ff into refs/orchestrator/runs/<run_id>/integration inside a
worktree under the run folder; the user's checkout and main are never touched.

Completed plan tasks are not turned into Task records; they are listed in
run.summary["completed_tasks"]. Per-task metadata lives in
run.summary["tasks"][task_id]. A failed check retries with feedback limited to
check IDs, verdicts and reason codes while the attempt budget lasts.
Ctrl+C and stop are covered only as far as W7 covers them for one attempt.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import json
from pathlib import Path
import subprocess

from . import records, workspace
from .cli import (EXIT_CODES, CLIError, RunStore, _attempt_loop, _git, _lock, _read, _ready,
                  _recover, adapter_for, stop_task)
from .plan import validate_plan

MAX_PARALLEL = 2
MERGE_IDENTITY = ("-c", "user.name=orchestrator", "-c", "user.email=orchestrator@localhost.invalid",
                  "-c", "commit.gpgsign=false")
DONE = {"integrated"}


def integration_ref(run_id: str) -> str:
    return f"refs/orchestrator/runs/{run_id}/integration"


def _plan_files(plan_path: Path, plan: dict) -> dict[str, str]:
    """Relative path -> sha256 for the plan and every spec and instruction file.
    One hash per file, so moving bytes across a file boundary changes the record."""
    files = {plan_path.name: hashlib.sha256(plan_path.read_bytes()).hexdigest()}
    for entry in plan["tasks"]:
        for key in ("spec", "instructions"):
            if key in entry:
                files[entry[key].replace("\\", "/")] = hashlib.sha256(
                    (plan_path.parent / entry[key]).read_bytes()).hexdigest()
    return files


def _prompt(instructions: str, spec: dict):
    def build(run, scope):
        text = (instructions.rstrip() + "\n\nTask constraints:\n" + json.dumps({
            "allowed_write_paths": spec["allowed_write_paths"],
            "deliverables": spec["deliverables"], "checks": spec["checks"]}, ensure_ascii=False, indent=2) +
            "\nCommit your deliverables in this workspace. Do not change files outside the allowed paths.\n")
        feedback = scope.get("feedback")
        if feedback:
            lines = [f"- {f['check_id']}: {f['verdict']}" + (f" ({f['reason']})" if f.get("reason") else "")
                     for f in feedback]
            text += ("\nPrevious attempt's registered checks (IDs, verdicts and reason codes only):\n"
                     + "\n".join(lines) + "\n")
        return text
    return build


class _Integrator:
    """Serial --no-ff merges of accepted results into the run's integration line."""

    def __init__(self, store: RunStore, repo: Path, base_commit: str):
        self.store, self.repo = store, repo
        self.ref = integration_ref(store.run_id)
        self.path = store.directory / "integration"
        self.base_commit = base_commit

    def head(self) -> str:
        try:
            return _git(self.repo, "rev-parse", "--verify", self.ref)
        except subprocess.CalledProcessError:
            head = _git(self.repo, "rev-parse", "--verify", self.base_commit + "^{commit}")
            _git(self.repo, "update-ref", self.ref, head, "0" * len(head))
            return head

    def merge(self, task_id: str, attempt_id: str, subject: str) -> str:
        head = self.head()
        if not self.path.exists():
            workspace.create_worktree(self.repo, head, self.path)
        _git(self.path, "checkout", "--detach", "--quiet", head)
        try:
            _git(self.path, *MERGE_IDENTITY, "merge", "--no-ff", "--no-edit",
                 "-m", f"integrate {task_id} ({attempt_id})", subject)
        except subprocess.CalledProcessError:
            try:
                _git(self.path, "merge", "--abort")
            except subprocess.CalledProcessError:
                pass
            raise
        merged = _git(self.path, "rev-parse", "HEAD")
        _git(self.repo, "update-ref", self.ref, merged, head)  # fails if the line moved meanwhile
        return merged


def run_plan(plan_path: Path, config: dict, repo: Path, root: Path, checks_dir: Path, config_dir: Path,
             *, parallel: int = 1, adapter_factory=adapter_for, environ=None) -> int:
    if not 1 <= parallel <= MAX_PARALLEL:
        raise CLIError(f"--parallel must be between 1 and {MAX_PARALLEL}")
    plan_path, repo = Path(plan_path).resolve(), Path(repo).resolve()
    errors, order = validate_plan(plan_path, config, checks_dir, environ=environ)
    if errors:
        print("\n".join(errors))
        return 1
    plan = _read(plan_path)
    entries = {e["task_id"]: e for e in plan["tasks"]}
    specs = {t: _read(plan_path.parent / e["spec"]) for t, e in entries.items() if "spec" in e}
    texts = {t: (plan_path.parent / e["instructions"]).read_text(encoding="utf-8-sig")
             for t, e in entries.items() if "instructions" in e}
    executable = [t for t in order if t in specs]
    store = RunStore(root, plan["run_id"])
    plan_files = _plan_files(plan_path, plan)
    integrator = _Integrator(store, repo, plan["base_commit"])

    with _lock(store.directory / ".runner.lock", timeout=0):
        with store.lock():
            if (store.directory / "summary.json").exists():
                run = store.load()
                if run.summary.get("plan_files") != plan_files or set(run.tasks) != set(executable):
                    raise CLIError("plan, specifications or instructions changed; use a new run_id")
            else:
                run = records.Run(plan["run_id"], str(plan["plan_version"]),
                                  tasks={t: records.Task(t, str(plan["plan_version"])) for t in executable})
                run.summary.update(
                    plan_files=plan_files, plan=str(plan_path), repo=str(repo),
                    plan_base_commit=plan["base_commit"], integration_ref=integrator.ref,
                    completed_tasks={t: e["completed_commit"] for t, e in entries.items() if "spec" not in e},
                    tasks={t: {} for t in executable}, merges=[])
            if run.state in {"completed", "failed", "cancelled"}:
                return {"completed": 0, "failed": 1, "cancelled": 3}[run.state]
            if run.state == "blocked":
                store.move(run, run, "running")
            for task_id in executable:  # W7 restart rules for interrupted attempts
                task = run.tasks[task_id]
                if task.attempts and task.state not in DONE | {"accepted", "failed", "cancelled"}:
                    code = _recover(store, run, task, run.summary["tasks"][task_id], store.finish_task)
                    if code is not None:
                        return _close(store, run, code)
            store.save(run)
            integrator.head()

        def execute(task_id: str, base: str) -> int:
            spec = specs[task_id]
            with store.lock():
                run = store.load()
                _ready(store, run, run.tasks[task_id])
            return _attempt_loop(store, spec, config, repo, checks_dir, config_dir, adapter_factory,
                                 scope=lambda r: r.summary["tasks"][task_id], base_commit=base,
                                 prompt=_prompt(texts[task_id], spec), finish=store.finish_task,
                                 retry_on_fail=True)

        running: dict = {}
        started_here: set[str] = set()
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            try:
                while True:
                    with store.lock():
                        run = store.load()
                        if run.state == "running":
                            _integrate_accepted(store, run, integrator, executable)
                        states = {t: run.tasks[t].state for t in executable}
                        # A task blocked before this invocation may restart (W7 rerun rule).
                        # One started here halts new starts as soon as its record says
                        # blocked, even before its thread returns. The retry path passes
                        # through blocked only inside one locked section, so it is never seen.
                        halted = (run.state != "running" or any(
                            states[t] in {"failed", "cancelled"} or (states[t] == "blocked" and t in started_here)
                            for t in executable))
                        if all(s in DONE for s in states.values()):
                            break
                        if not halted:
                            for task_id in executable:
                                if len(running) >= parallel:
                                    break
                                prerequisites = specs[task_id]["depends_on"]
                                if (task_id not in running.values() and states[task_id] in {"planned", "ready", "blocked"}
                                        and all(states.get(d) in DONE or d in run.summary["completed_tasks"]
                                                for d in prerequisites)):
                                    base = integrator.head()
                                    started_here.add(task_id)
                                    running[pool.submit(execute, task_id, base)] = task_id
                    if not running:
                        break
                    finished, _ = wait(running, return_when=FIRST_COMPLETED)
                    for future in finished:
                        running.pop(future)
                        future.result()  # surface programming errors
            except KeyboardInterrupt:
                for task_id in list(running.values()):
                    stop_task(store.root, store.run_id, task_id)
                raise

        with store.lock():
            run = store.load()
            states = [run.tasks[t].state for t in executable]
            if all(s in DONE for s in states):
                run.summary["integration_head"] = integrator.head()
                if run.state != "completed":
                    store.move(run, run, "completed")
                print(f"{run.run_id}: completed; integration {integrator.ref} = {run.summary['integration_head']}")
                return 0
            for state in ("cancelled", "failed", "blocked"):
                stuck = [t for t in executable if run.tasks[t].state == state]
                if stuck:
                    reason = run.tasks[stuck[0]].reason_code or state
                    print(f"{run.run_id}: {state} at {', '.join(stuck)} ({reason})")
                    return _close(store, run, EXIT_CODES[state], reason)
            if run.state == "blocked":
                waiting = [t for t in executable if run.tasks[t].state == "accepted"]
                print(f"{run.run_id}: blocked ({run.reason_code}) with {', '.join(waiting)} accepted but not integrated")
                return 2
            print(f"{run.run_id}: no task can start; check dependencies")
            return _close(store, run, 2, "no_startable_task")


def _integrate_accepted(store: RunStore, run, integrator: _Integrator, executable: list[str]) -> None:
    for task_id in executable:
        # Merges follow the validated execution order. A later result accepted first
        # waits (stays accepted) until every earlier task is integrated; if an earlier
        # task fails, it is never merged in this run.
        task = run.tasks[task_id]
        if task.state == "integrated":
            continue
        if task.state != "accepted":
            return
        attempt = run.attempts[task.accepted_attempt_id]
        subject = json.loads(Path(attempt.result_manifest).read_text(encoding="utf-8"))["subject_commit"]
        try:
            merged = integrator.merge(task_id, attempt.attempt_id, subject)
        except (subprocess.CalledProcessError, workspace.WorkspaceError, OSError) as error:
            run.summary["tasks"][task_id]["integration_error"] = type(error).__name__
            store.save(run)
            if run.state == "running":
                store.move(run, run, "blocked", "integration_conflict")
            return
        run.summary["merges"].append({"task_id": task_id, "attempt_id": attempt.attempt_id,
                                      "subject_commit": subject, "merge_commit": merged})
        store.move(run, task, "integrated")


def _close(store: RunStore, run, code: int, reason: str = "task_stopped") -> int:
    target = {1: "failed", 2: "blocked", 3: "cancelled"}.get(code)
    if target and run.state != target and target in records.RUN_TRANSITIONS.get(run.state, set()):
        store.move(run, run, target, reason)
    return code
