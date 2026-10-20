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
from dataclasses import MISSING, fields
import hashlib
import json
import os
from pathlib import Path
import subprocess

from . import records, workspace
from .adapters.claude import ClaudeSpec
from .adapters.codex import CodexSpec
from .cli import (EXIT_CODES, CLIError, RunStore, _attempt_loop, _git, _lock, _read, _ready,
                  _recover, adapter_for, require_tool_paths, stop_task, tool_executable,
                  unfinished_tasks)
from .plan import PublicationSourceError, read_publication_source, validate_plan

MAX_PARALLEL = 2
MERGE_IDENTITY = ("-c", "user.name=orchestrator", "-c", "user.email=orchestrator@localhost.invalid",
                  "-c", "commit.gpgsign=false")
DONE = {"integrated"}


def _resolved_route(route: dict) -> dict:
    """The execution settings the adapter actually uses: configured values, else the
    CodexSpec/ClaudeSpec defaults (W13b Codex review F3). An effort left to the CLI's own
    default is recorded as "cli-default" because its value is not known here."""
    tool = route["tool"]
    spec_type = CodexSpec if tool == "codex" else ClaudeSpec
    defaults = {f.name: f.default for f in fields(spec_type) if f.default is not MISSING}
    keys = ("sandbox", "approve_for_me") if tool == "codex" else ("permission_mode", "safe_mode")
    effort = route.get("effort", route.get("model_reasoning_effort"))
    resolved = {"tool": tool, "model": route.get("model"),
                "effort": effort if effort is not None else "cli-default"}
    applied = []
    for key in keys:
        if key in route:
            resolved[key] = route[key]
        else:
            resolved[key] = defaults[key]
            applied.append(key)
    resolved["defaults_applied"] = applied
    return resolved


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
            "\nCommit your deliverables in this workspace. Do not change files outside the allowed paths.\n" +
            # W13 concern 4: the plan is committed after its base commit, so a spec file the
            # instructions mention may be missing from the workspace. Its content is here.
            f"\nTask specification (the instructions may refer to tasks/{spec['task_id']}.json, which may "
            "not exist in this workspace; this JSON is its full content):\n" +
            json.dumps(spec, ensure_ascii=False, indent=2) + "\n")
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
    environ = os.environ if environ is None else environ
    finished = _report_finished_run(plan_path, repo, root, environ)
    if finished is not None:
        return finished
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
    if adapter_factory is adapter_for:  # W16f: an injected factory chooses its own executable
        require_tool_paths(config, [specs[t]["route_id"] for t in unfinished_tasks(store, executable)],
                           config_dir)
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
                if plan.get("publication"):
                    # Pinned before any worker runs: the manager reads the private
                    # expected summary itself; candidate code never produces these numbers.
                    summary, digest, path = read_publication_source(plan["publication"], environ)
                    run.summary["publication"] = {**plan["publication"], "path": path, "sha256": digest,
                                                  "summary": summary, "protocol_hash": plan["protocol_hash"]}
            if run.state in {"completed", "failed", "cancelled"}:
                # A finished run is never resumed, but its report is rebuilt from the
                # records (W13b review U1); nothing missing is filled from the present.
                _write_report(store, run, executable, integrator, specs, environ)
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
            route = config["routes"][spec["route_id"]]
            version = _cli_version(config, route["tool"], config_dir)
            with store.lock():
                run = store.load()
                # W13 concern 5: what actually ran, not only the route ID.
                scope = run.summary["tasks"][task_id]
                scope["route"] = _resolved_route(route)
                scope["cli_version"] = version
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
            code = _finish(store, executable, integrator)
            _write_report(store, store.load(), executable, integrator, specs, environ)
            return code


def _report_finished_run(plan_path: Path, repo: Path, root: Path, environ) -> int | None:
    """A completed, failed or cancelled run is never resumed, but its report is rebuilt
    from the records even when the plan would no longer validate, for example because
    the publication source is gone (W13b re-review 2 F4); the report then withholds the
    numbers. Returns None when this is not such a run, and the normal path validates."""
    try:
        plan = _read(plan_path)
        entries = [e for e in plan["tasks"]]
        specs = {e["task_id"]: _read(plan_path.parent / e["spec"]) for e in entries if "spec" in e}
        store = RunStore(root, plan["run_id"])
        plan_files = _plan_files(plan_path, plan)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, CLIError):
        return None
    if not (store.directory / "summary.json").is_file():
        return None
    integrator = _Integrator(store, repo, plan["base_commit"])
    with _lock(store.directory / ".runner.lock", timeout=0), store.lock():
        run = store.load()
        if run.state not in {"completed", "failed", "cancelled"}:
            return None
        if run.summary.get("plan_files") != plan_files or set(run.tasks) != set(specs):
            raise CLIError("plan, specifications or instructions changed; use a new run_id")
        executable = [e["task_id"] for e in entries if e["task_id"] in specs]
        _write_report(store, run, executable, integrator, specs, environ)
        print(f"{run.run_id}: {run.state} (finished earlier; report rebuilt from the records)")
        return {"completed": 0, "failed": 1, "cancelled": 3}[run.state]


def _finish(store: RunStore, executable: list[str], integrator: _Integrator) -> int:
    run = store.load()
    states = [run.tasks[t].state for t in executable]
    if all(s in DONE for s in states):
        run.summary["integration_head"] = integrator.head()
        if run.state != "completed":
            store.move(run, run, "completed")
        else:
            store.save(run)
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


def _cli_version(config: dict, tool: str, config_dir: Path) -> str:
    """First line of `<cli> --version`, or "unavailable"; never fails the run."""
    try:
        executable = tool_executable(config, tool, config_dir)
        result = subprocess.run([str(executable), "--version"], capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = result.stdout.decode("utf-8", "replace").strip().splitlines()
        return line[0] if result.returncode == 0 and line else "unavailable"
    except (OSError, KeyError, subprocess.SubprocessError):
        return "unavailable"


def _write_report(store: RunStore, run, executable: list[str], integrator: _Integrator, specs: dict,
                  environ) -> None:
    """W13 concern 7: verdicts, attempts, merges, routes and the published evaluation
    aggregate in one place next to the run records (run-report.json / run-report.md)."""
    checks = records.load_checks(store.root, store.run_id)
    merges = {m["task_id"]: m for m in run.summary.get("merges", [])}
    pinned = run.summary.get("publication")
    rows, gate = [], None
    for task_id in executable:
        task, scope = run.tasks[task_id], run.summary["tasks"][task_id]
        shown = task.accepted_attempt_id or (task.attempts[-1] if task.attempts else None)
        order = {check_id: i for i, check_id in enumerate(specs[task_id]["checks"])}
        verdicts = sorted((c for c in checks if c.attempt_id == shown), key=lambda c: order.get(c.check_id, len(order)))
        rows.append({
            "task_id": task_id, "state": task.state, "reason_code": task.reason_code,
            "attempts": len(task.attempts), "accepted_attempt_id": task.accepted_attempt_id,
            "base_commit": scope.get("base_commit"), "merge_commit": merges.get(task_id, {}).get("merge_commit"),
            "route": scope.get("route"), "cli_version": scope.get("cli_version"),
            "deliverables_missing": scope.get("deliverables_missing"),
            "checks": [{"check_id": c.check_id, "verdict": c.verdict, "reason": c.evidence.get("reason")}
                       for c in verdicts]})
        for c in verdicts:  # the gate: the declared check passed on an accepted result
            if pinned and task.accepted_attempt_id and c.check_id == pinned["check"] and c.verdict == "pass":
                gate = {"task_id": task_id, "check_id": c.check_id, "attempt_id": c.attempt_id,
                        "subject_commit": c.subject_commit}
    report = {"run_id": run.run_id, "state": run.state, "reason_code": run.reason_code,
              "integration_ref": integrator.ref, "integration_head": integrator.head(),  # review F2
              "plan_base_commit": run.summary.get("plan_base_commit"), "tasks": rows,
              "evaluation": _evaluation(pinned, gate, environ)}
    (store.directory / "run-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    (store.directory / "run-report.md").write_text(_render(report), encoding="utf-8", newline="\n")


def _evaluation(pinned: dict | None, gate: dict | None, environ) -> dict | None:
    """The published aggregate is the expected summary the manager pinned before the
    run (W13b re-review R1). A passing gate check means the candidate's results equal
    it; no number is ever read from a process that ran candidate code. Any doubt about
    the source (moved, changed, unreadable) withholds the numbers."""
    if pinned is None:
        return None
    result = {"source": "pinned expected summary", "check_id": pinned["check"],
              "protocol_hash": pinned.get("protocol_hash")}  # review 2 F5
    if gate is None:
        return {**result, "withheld": "check_not_passed"}
    result.update(gate)
    try:
        summary, digest, path = read_publication_source(pinned, environ)
    except (PublicationSourceError, OSError) as error:
        return {**result, "withheld": str(error) if isinstance(error, PublicationSourceError) else "unreadable"}
    if digest != pinned["sha256"] or path != pinned["path"] or summary != pinned["summary"]:
        return {**result, "withheld": "source_changed"}
    return {**result, "eval_summary": summary}


def _render(report: dict) -> str:
    lines = [f"# run {report['run_id']}: {report['state']}", "",
             f"- integration: `{report['integration_ref']}` = `{report['integration_head']}`",
             f"- plan base commit: `{report['plan_base_commit']}`", "",
             "| task | state | attempts | base | merge | route | CLI | checks |", "|---|---|---|---|---|---|---|---|"]
    for row in report["tasks"]:
        route = row["route"] or {}
        effort = route.get("effort") or route.get("model_reasoning_effort") or ""
        checks = ", ".join(f"{c['check_id']} {c['verdict']}" + (f" ({c['reason']})" if c["reason"] else "")
                           for c in row["checks"]) or "-"
        if row.get("deliverables_missing"):  # W14a
            checks += "; deliverables missing: " + ", ".join(row["deliverables_missing"])
        lines.append(f"| {row['task_id']} | {row['state']} | {row['attempts']} | `{(row['base_commit'] or '-')[:12]}` "
                     f"| `{(row['merge_commit'] or '-')[:12]}` | {route.get('tool', '-')} {route.get('model', '')} {effort} "
                     f"| {row['cli_version'] or '-'} | {checks} |")
    evaluation = report["evaluation"]
    lines += ["", "## Evaluation queries (aggregate)", ""]
    if evaluation is None:
        lines.append("Not published: the plan declares no publication.")
        return "\n".join(lines) + "\n"
    lines += [f"protocol_hash: `{evaluation.get('protocol_hash') or 'not recorded'}`", ""]
    if "withheld" in evaluation:
        lines.append(f"Withheld ({evaluation['withheld']}): the numbers are shown only after `{evaluation['check_id']}` "
                     "passed on an accepted result and the pinned expected summary is unchanged.")
    else:
        summary = evaluation["eval_summary"]
        lines += [f"Source: the expected summary pinned by the manager before the run, shown because "
                  f"`{evaluation['check_id']}` passed on `{evaluation['subject_commit'][:12]}` ({evaluation['task_id']}). "
                  f"Queries: {summary['queries']}, k: {summary['k']}.", "",
                  "| method | hit@3 | hits/queries | no-result queries |", "|---|---|---|---|"]
        for method in ("a", "b"):
            row = summary[method]
            lines.append(f"| {method.upper()} | {row['hit_at_3']} | {row['hits']}/{summary['queries']} | {row['no_result_queries']} |")
    return "\n".join(lines) + "\n"


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
