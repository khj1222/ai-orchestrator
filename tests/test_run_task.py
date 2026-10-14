"""W7 real local Git/Python integration tests. No model CLI or network calls."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from orchestrator import cli, process, records, verify, workspace
from orchestrator.adapters.base import AttemptResult


DUMMY = Path(__file__).parent / "fixtures/dummy_worker.py"


class DummyAdapter:
    def __init__(self, mode):
        self.mode = mode

    def capabilities(self):
        return frozenset({"edit_files", "stdin_prompt"})

    def build_command(self, spec, workspace, prompt_path):
        assert prompt_path.is_absolute() and workspace not in prompt_path.parents
        assert spec.last_message_path.is_absolute() and workspace not in spec.last_message_path.parents
        return [sys.executable, str(DUMMY.resolve()), "--workspace", str(workspace), "--mode", self.mode]

    def parse_events(self, path):
        return AttemptResult(status="completed", final_message="worker claims success")

    def stop(self, handle, grace_seconds=3):
        return handle.stop(grace_seconds)


def dummy_factory(route, config, evidence, config_dir):
    return DummyAdapter(route["model"]), SimpleNamespace(last_message_path=evidence / "last-message.txt")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=CLI Test",
                           "-c", "user.email=cli@example.invalid", "-c", "commit.gpgsign=false", *args],
                          capture_output=True, check=True, timeout=30).stdout.decode().strip()


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def case(tmp_path):
    repo = tmp_path / "target repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    (repo / "seed.txt").write_text("base", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    checks = tmp_path / "checks"
    checks.mkdir()
    write(checks / "artifact.json", {"check_id": "artifact", "validator_version": "1",
                                     "command": ["{python}", "-c", "from pathlib import Path; assert Path('allowed/output.txt').read_text().strip() == 'verified output'"],
                                     "required_artifacts": ["allowed/output.txt"]})
    write(checks / "scope.json", {"check_id": "scope", "validator_version": "1", "kind": "scope"})
    config = {"paths": {"codex": "unused-codex"},
              "routes": {"offline": {"tool": "codex", "model": "success"}},
              "limits": {"timeout_seconds": 30, "max_attempts_total": 2}}
    spec = {"schema_version": 1, "run_id": "run-1", "task_id": "task-1", "goal": "Make the output.",
            "depends_on": [], "base_commit": base, "allowed_write_paths": ["allowed/"],
            "deliverables": ["allowed/output.txt"], "checks": ["artifact", "scope"], "route_id": "offline"}
    config_path, spec_path = tmp_path / "config.json", tmp_path / "task.json"
    write(config_path, config)
    write(spec_path, spec)
    return SimpleNamespace(repo=repo, base=base, checks=checks, config=config, spec=spec,
                           config_path=config_path, spec_path=spec_path, root=tmp_path / "state")


def args(c, command="run-task"):
    write(c.spec_path, c.spec)
    write(c.config_path, c.config)
    return [command, str(c.spec_path), "--repo", str(c.repo), "--config", str(c.config_path),
            "--run-root", str(c.root), "--checks-dir", str(c.checks)]


def execute(c):
    return cli.main(args(c), adapter_factory=dummy_factory)


def loaded(c):
    run = records.load_run(c.spec["run_id"], c.root)
    return run, run.tasks[c.spec["task_id"]]


def evidence(c, attempt):
    return c.root / "runs" / c.spec["run_id"] / "attempts" / attempt.attempt_id / "evidence"


def test_success_accepts_only_checked_commit_and_retains_ref(case):
    assert execute(case) == 0
    run, task = loaded(case)
    assert (run.state, task.state) == ("completed", "accepted")
    assert task.accepted_attempt_id == task.attempts[0]
    attempt = run.attempts[task.accepted_attempt_id]
    assert attempt.state == "exited"
    manifest = json.loads(Path(attempt.result_manifest).read_text())
    assert git(case.repo, "rev-parse", manifest["ref"]) == manifest["subject_commit"]
    assert git(case.repo, "show", f"{manifest['ref']}:allowed/output.txt") == "verified output"
    checks = records.load_checks(case.root, run.run_id)
    assert len(checks) == 2 and all(c.verdict == "pass" for c in checks)
    assert all(c.subject_commit == manifest["subject_commit"] for c in checks)
    assert not Path(attempt.workspace).exists()
    assert "workspace" not in git(case.repo, "worktree", "list", "--porcelain")
    assert git(case.repo, "status", "--porcelain") == ""
    assert git(case.repo, "diff", "--name-only", case.base, manifest["subject_commit"]) == "allowed/output.txt"
    assert (evidence(case, attempt) / "prompt.txt").is_file()


def test_worker_claim_of_success_does_not_override_failed_check(case):
    case.config["routes"]["offline"]["model"] = "missing"
    assert execute(case) == 1
    run, task = loaded(case)
    attempt = run.attempts[task.attempts[0]]
    assert attempt.process["exit_code"] == 0
    assert json.loads((evidence(case, attempt) / "adapter-result.json").read_text())["status"] == "completed"
    assert task.state == "failed" and task.accepted_attempt_id is None
    assert any(c.verdict == "fail" for c in records.load_checks(case.root, run.run_id))
    assert execute(case) == 1 and len(loaded(case)[1].attempts) == 1


def test_worker_nonzero_exit_can_be_accepted_when_checks_pass(case):
    case.config["routes"]["offline"]["model"] = "nonzero"
    assert execute(case) == 0
    run, task = loaded(case)
    assert run.attempts[task.accepted_attempt_id].process["exit_code"] == 9
    assert task.state == "accepted"


def test_accepted_rerun_does_not_start_or_accept_again(case, monkeypatch):
    assert execute(case) == 0
    first = loaded(case)[1].accepted_attempt_id
    monkeypatch.setattr(process, "start", lambda *a, **kw: pytest.fail("duplicate start"))
    monkeypatch.setattr(records, "accept_attempt", lambda *a: pytest.fail("duplicate acceptance"))
    assert execute(case) == 0
    assert loaded(case)[1].attempts == [first]


@pytest.mark.parametrize("change", ["placeholder", "dependency", "route", "check", "check-shape",
                                    "schema", "path", "empty-checks", "nan-timeout"])
def test_validate_rejects_invalid_inputs_without_starting_process(case, monkeypatch, change):
    if change == "placeholder":
        case.spec["goal"] = "TO_BE_SET"
    elif change == "dependency":
        case.spec["depends_on"] = ["another"]
    elif change == "route":
        case.spec["route_id"] = "missing"
    elif change == "check":
        case.spec["checks"] = ["missing"]
    elif change == "check-shape":
        write(case.checks / "artifact.json", {"check_id": "artifact", "kind": []})
    elif change == "schema":
        case.spec["schema_version"] = True
    elif change == "path":
        case.spec["allowed_write_paths"] = ["../escape"]
    elif change == "empty-checks":
        case.spec["checks"] = []
    else:
        case.spec["limits"] = {"timeout_seconds": float("nan")}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("validate started a process"))
    assert cli.main(args(case, "validate")) == 1
    assert not case.root.exists()


def test_validate_does_not_resolve_commit(case, monkeypatch):
    case.spec["base_commit"] = "not-yet-existing"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("Git during validate"))
    assert cli.main(args(case, "validate")) == 0
    assert not case.root.exists()


def test_undecidable_blocks_and_preserves_verification_metadata(case):
    write(case.checks / "artifact.json", {"check_id": "artifact", "validator_version": "1",
                                         "command": ["definitely-missing-verifier-program"]})
    assert execute(case) == 2
    run, task = loaded(case)
    assert (run.state, task.state) == ("blocked", "blocked")
    assert task.reason_code == "verification_undecidable"


def test_dirty_repository_blocks_before_worker_launch(case):
    (case.repo / "seed.txt").write_text("user editing", encoding="utf-8")
    assert execute(case) == 2
    run, task = loaded(case)
    assert task.state == "blocked"
    assert "uncommitted" in run.summary["detail"]
    assert (case.repo / "seed.txt").read_text() == "user editing"


def test_start_failure_exhausts_budget_without_illegal_task_transition(case, monkeypatch):
    def fail(*a, **kw):
        raise FileNotFoundError("injected launch failure")
    monkeypatch.setattr(process, "start", fail)
    assert execute(case) == 1
    run, task = loaded(case)
    assert task.state == "failed" and len(task.attempts) == 2
    assert all(run.attempts[aid].state == "failed" for aid in task.attempts)
    assert execute(case) == 1
    assert len(loaded(case)[1].attempts) == 2


def test_status_is_read_only(case):
    assert execute(case) == 0
    before = {p: p.read_bytes() for p in case.root.rglob("*") if p.is_file()}
    assert cli.main(["status", "run-1", "--run-root", str(case.root)]) == 0
    assert cli.main(["status", "run-1", "task-1", "--run-root", str(case.root)]) == 0
    assert {p: p.read_bytes() for p in case.root.rglob("*") if p.is_file()} == before


def seed_attempt(c, snapshot, state="running"):
    task = records.Task("task-1", "1", state="running", attempts=["previous"])
    attempt = records.Attempt("previous", task.task_id, "offline", state=state, process=snapshot)
    run = records.Run("run-1", "1", tasks={task.task_id: task}, attempts={attempt.attempt_id: attempt})
    records.save_run(run, c.root)


def test_restart_after_dead_process_creates_new_attempt(case, tmp_path):
    out, err = tmp_path / "dead.out", tmp_path / "dead.err"
    handle = process.start([sys.executable, "-c", "pass"], case.repo, out, err)
    handle.wait(10)
    seed_attempt(case, handle.snapshot())
    assert execute(case) == 0
    run, task = loaded(case)
    assert run.attempts["previous"].state == "unknown"
    assert run.attempts["previous"].reason_code == "restart_unverifiable"
    assert len(task.attempts) == 2 and task.accepted_attempt_id != "previous"


def test_live_identity_refuses_duplicate_attempt(case, tmp_path):
    handle = process.start([sys.executable, "-c", "import time; time.sleep(120)"], case.repo,
                           tmp_path / "live.out", tmp_path / "live.err")
    try:
        seed_attempt(case, handle.snapshot())
        assert execute(case) == 2
        assert loaded(case)[1].attempts == ["previous"]
        assert handle.poll() is None
    finally:
        handle.stop()


def test_enumeration_failure_blocks_without_restarting(case, monkeypatch):
    seed_attempt(case, {"pid": 12345, "started_at": "2026-09-12T00:00:00Z", "argv_hash": "test"})
    def unavailable(*a):
        raise process.ProcessEnumerationError("injected")
    monkeypatch.setattr(process, "identity_matches", unavailable)
    monkeypatch.setattr(process, "start", lambda *a, **kw: pytest.fail("unsafe restart"))
    assert execute(case) == 2
    run, task = loaded(case)
    assert task.state == "blocked" and run.attempts["previous"].state == "unknown"
    assert execute(case) == 2


def test_created_and_starting_recovery_respect_transition_table(case):
    seed_attempt(case, None, "created")
    assert execute(case) == 0
    assert loaded(case)[0].attempts["previous"].state == "failed"


def test_starting_without_identity_is_not_restarted(case):
    seed_attempt(case, None, "starting")
    assert execute(case) == 2
    run, task = loaded(case)
    assert task.attempts == ["previous"] and run.attempts["previous"].state == "unknown"


def test_restart_without_budget_finishes_failed(case, tmp_path):
    case.spec["limits"] = {"max_attempts_total": 1}
    handle = process.start([sys.executable, "-c", "pass"], case.repo, tmp_path / "out", tmp_path / "err")
    handle.wait(10)
    seed_attempt(case, handle.snapshot())
    assert execute(case) == 1
    run, task = loaded(case)
    assert task.state == "failed" and task.attempts == ["previous"]


def test_actual_timeout_stops_tree_and_consumes_attempt_budget(case):
    case.config["routes"]["offline"]["model"] = "sleep"
    case.spec["limits"] = {"timeout_seconds": 0.3, "max_attempts_total": 2}
    assert execute(case) == 1
    run, task = loaded(case)
    assert task.state == "failed" and len(task.attempts) == 2
    for aid in task.attempts:
        attempt = run.attempts[aid]
        assert attempt.state == "failed" and attempt.reason_code == "timeout"
        info = json.loads((evidence(case, attempt) / "stdout.log").read_text().strip())
        assert process.query_process(info["worker_pid"]) is None
        assert process.query_process(info["child_pid"]) is None
        assert not Path(attempt.workspace).exists()


def test_unknown_timeout_preserves_workspace_and_never_retries(case, monkeypatch):
    class Handle:
        started_at = "2026-09-12T00:00:00Z"
        def snapshot(self):
            return {"pid": 12345, "argv_hash": "fake", "started_at": self.started_at}
        def wait(self, timeout):
            raise TimeoutError()
        def stop(self):
            return SimpleNamespace(to_dict=lambda: {"state": "unknown"})
    monkeypatch.setattr(process, "start", lambda *a, **kw: Handle())
    assert execute(case) == 2
    run, task = loaded(case)
    assert len(task.attempts) == 1 and task.state == "blocked"
    attempt = run.attempts[task.attempts[0]]
    assert attempt.state == "unknown" and Path(attempt.workspace).exists()
    workspace.remove_worktree(case.repo, Path(attempt.workspace), force=True)


def test_verification_abort_keeps_retained_workspace_and_prevents_restart(case, monkeypatch):
    def aborted(**kw):
        wt = workspace.create_worktree(kw["repo"], kw["subject_commit"], kw["evidence_root"] / "verify-worktree")
        return verify.VerificationRun(kw["subject_commit"], None, checks=[
            records.Check("artifact", kw["attempt_id"], kw["subject_commit"], "", "1", "artifact", None, "undecidable")],
            aborted_reason="injected unknown termination", worktree_path=str(wt))
    monkeypatch.setattr(verify, "run_checks", aborted)
    assert execute(case) == 2
    run, task = loaded(case)
    retained = Path(run.summary["verification"]["worktree_path"])
    assert retained.exists() and run.summary["verification_aborted"]
    assert execute(case) == 2 and len(loaded(case)[1].attempts) == 1
    workspace.remove_worktree(case.repo, retained, force=True)


def test_real_concurrent_run_and_stop_does_not_overwrite_cancellation(case, tmp_path):
    case.config["routes"]["offline"]["model"] = "sleep"
    command = [sys.executable, str(Path(__file__).resolve()), "--driver", *args(case)]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    output = tmp_path / "runner.log"
    with output.open("wb") as stream:
        runner = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, env=env)
    try:
        deadline = time.monotonic() + 20
        attempt = None
        while time.monotonic() < deadline:
            if runner.poll() is not None:
                pytest.fail(output.read_text(encoding="utf-8", errors="replace"))
            try:
                with cli.RunStore(case.root, "run-1").lock(create=False):
                    run, task = loaded(case)
                attempt = run.attempts[task.attempts[-1]]
                log = evidence(case, attempt) / "stdout.log"
                if attempt.state == "running" and log.exists() and log.stat().st_size:
                    break
            except (FileNotFoundError, IndexError, KeyError):
                pass
            time.sleep(0.1)
        else:
            pytest.fail("worker did not reach running")
        info = json.loads(log.read_text().strip())
        assert execute(case) == 2  # OS run lease refuses a second controller.
        result = subprocess.run([sys.executable, "-m", "orchestrator.cli", "stop", "run-1", "task-1",
                                 "--run-root", str(case.root)], capture_output=True, env=env, timeout=30)
        assert result.returncode == 0, result.stdout
        assert runner.wait(15) == 3, output.read_text(encoding="utf-8", errors="replace")
        run, task = loaded(case)
        assert (run.state, task.state, run.attempts[attempt.attempt_id].state) == ("cancelled", "cancelled", "cancelled")
        assert process.query_process(info["worker_pid"]) is None
        assert process.query_process(info["child_pid"]) is None
        assert not Path(attempt.workspace).exists()
        assert execute(case) == 3
    finally:
        if runner.poll() is None:
            cli.stop_task(case.root, "run-1", "task-1")
            runner.terminate()
            runner.wait(10)


def test_adapter_resolution_maps_effort_and_places_schema_outside_worktree(case, tmp_path):
    source = tmp_path / "schema.json"
    write(source, {"type": "object"})
    dest = tmp_path / "evidence"
    dest.mkdir()
    adapter, spec = cli.adapter_for({"tool": "codex", "model": "configured-model",
                                    "model_reasoning_effort": "high", "ephemeral": False,
                                    "output_schema_path": "schema.json"}, case.config, dest, tmp_path)
    assert spec.effort == "high" and not spec.ephemeral
    assert spec.output_schema_path == dest / "output-schema.json"
    assert spec.last_message_path == dest / "last-message.txt"


def test_claude_route_uses_spec_fields_and_copies_schema(case, tmp_path):
    write(tmp_path / "schema.json", {"type": "object"})
    output = tmp_path / "output"
    output.mkdir()
    config = {"paths": {"claude": "unused-claude"}}
    adapter, spec = cli.adapter_for({"tool": "claude", "model": "configured-model", "effort": "high",
                                    "permission_mode": "plan", "session_persistence": False,
                                    "allowed_tools": ["Read"], "json_schema_path": "schema.json"},
                                   config, output, tmp_path)
    prompt = output / "prompt.txt"
    prompt.write_text("task", encoding="utf-8")
    argv = adapter.build_command(spec, case.repo, prompt)
    assert "--no-session-persistence" in argv and "--json-schema" in argv
    assert spec.json_schema_path == output / "output-schema.json"
    assert spec.allowed_tools == ("Read",) and spec.permission_mode == "plan"


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_terminal_run_state_is_reconciled_after_partial_save(case, state):
    task = records.Task("task-1", "1", state=state, reason_code="terminal")
    run = records.Run("run-1", "1", tasks={task.task_id: task})
    records.save_run(run, case.root)
    assert execute(case) == (3 if state == "cancelled" else 1)
    run, task = loaded(case)
    assert run.state == task.state == state and not task.attempts


@pytest.mark.parametrize("unavailable", [False, True])
def test_stop_does_not_kill_when_identity_is_missing_or_unobservable(case, monkeypatch, unavailable):
    seed_attempt(case, {"pid": 12345, "started_at": "2026-09-12T00:00:00Z", "argv_hash": "test"})
    before = {p: p.read_bytes() for p in case.root.rglob("*.json")}
    def identity(*a):
        if unavailable:
            raise process.ProcessEnumerationError("injected")
        return False
    monkeypatch.setattr(process, "identity_matches", identity)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("unexpected taskkill"))
    assert cli.main(["stop", "run-1", "task-1", "--run-root", str(case.root)]) == 2
    assert {p: p.read_bytes() for p in case.root.rglob("*.json")} == before


def test_changed_spec_cannot_reuse_existing_run(case):
    assert execute(case) == 0
    case.spec["goal"] = "A different task"
    assert execute(case) == 1
    assert len(loaded(case)[1].attempts) == 1


def test_config_cannot_restore_stripped_credential_environment(case):
    case.config["environment"] = {"extra_env": {"OPENAI_API_KEY": "placeholder"}}
    assert cli.main(args(case, "validate")) == 1
    assert not case.root.exists()


def test_interrupted_verification_does_not_start_another_worker(case, monkeypatch):
    task = records.Task("task-1", "1", state="verifying", attempts=["previous"])
    previous = records.Attempt("previous", task.task_id, "offline", state="exited")
    run = records.Run("run-1", "1", tasks={task.task_id: task}, attempts={"previous": previous})
    records.save_run(run, case.root)
    monkeypatch.setattr(process, "start", lambda *a, **kw: pytest.fail("unsafe restart"))
    assert execute(case) == 2
    assert loaded(case)[1].state == "blocked" and len(loaded(case)[1].attempts) == 1
    assert execute(case) == 2  # The inspection requirement must survive another invocation.
    assert loaded(case)[1].attempts == ["previous"]


if __name__ == "__main__" and sys.argv[1:2] == ["--driver"]:
    raise SystemExit(cli.main(sys.argv[2:], adapter_factory=dummy_factory))


@pytest.mark.parametrize("value,sandbox,valid", [
    (True, "workspace-write", True),
    (False, "read-only", True),
    ("true", "workspace-write", False),
    (True, "read-only", False),
    (True, "danger-full-access", False),
])
def test_validate_auto_review_route_before_launch(case, value, sandbox, valid):
    route = case.config["routes"]["offline"]
    route.update(approve_for_me=value, sandbox=sandbox)
    errors = cli.validate_task(case.spec, case.config, case.checks)
    assert (not errors) is valid
    if valid:
        adapter, resolved = cli.adapter_for(route, case.config, case.root, case.repo)
        assert resolved.approve_for_me is value


OMITTED = object()


@pytest.mark.parametrize("value,valid", [(OMITTED, True), (True, True), (False, True),
                                         ("false", False), (0, False), (None, False)])
def test_validate_claude_safe_mode_route_before_launch(case, tmp_path, value, valid):  # W16c
    case.config["paths"]["claude"] = "unused-claude"
    route = {"tool": "claude", "model": "m"}
    if value is not OMITTED:
        route["safe_mode"] = value  # None is an explicit JSON null (Codex W16c review suggestion 4)
    case.config["routes"]["offline"] = route
    errors = cli.validate_task(case.spec, case.config, case.checks)
    assert (not errors) is valid
    if valid:
        prompt = tmp_path / "prompt.txt"
        prompt.write_text("task", encoding="utf-8")
        adapter, resolved = cli.adapter_for(route, case.config, tmp_path, case.repo)
        argv = adapter.build_command(resolved, case.repo, prompt)
        assert ("--safe-mode" in argv) is (value is not False)


# W14a: 04 §9 "submission: deliverables exist" (W14 first attempt was accepted with none)
def test_scope_only_task_without_deliverables_is_not_accepted(case):
    case.spec["checks"] = ["scope"]  # the only check passes on an empty result
    case.config["routes"]["offline"]["model"] = "missing"  # worker commits nothing
    assert execute(case) == 1
    run, task = loaded(case)
    assert task.state == "failed" and task.reason_code == "missing_deliverable"
    assert task.accepted_attempt_id is None and len(task.attempts) == 1  # W7: no retry
    record = json.loads((evidence(case, run.attempts[task.attempts[0]]) / "deliverables.json").read_text(encoding="utf-8"))
    assert record["missing"] == ["allowed/output.txt"] and record["inspection_error"] is False


def test_partially_missing_deliverables_fail(case):
    case.spec["checks"] = ["scope"]
    case.spec["deliverables"] = ["allowed/output.txt", "allowed/second.txt"]
    assert execute(case) == 1
    run, task = loaded(case)
    assert task.reason_code == "missing_deliverable"
    assert run.summary["deliverables_missing"] == ["allowed/second.txt"]


def test_directory_deliverable_needs_a_file_below_it(case):
    case.spec["checks"] = ["scope"]
    case.spec["deliverables"] = ["allowed/"]
    assert execute(case) == 0  # the worker writes allowed/output.txt
    assert loaded(case)[1].state == "accepted"


# W14a Codex review: only regular-file blobs count, and paths are read raw
def tree_repo(tmp_path, files=(), gitlinks=(), symlinks=()):
    repo = tmp_path / "tree repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-m", "seed")
    seed = git(repo, "rev-parse", "HEAD")
    for name in files:
        blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=b"content",
                              capture_output=True, check=True).stdout.decode().strip()
        git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    for name in gitlinks:
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{seed},{name}")
    for name in symlinks:
        blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=b"seed.txt",
                              capture_output=True, check=True).stdout.decode().strip()
        git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},{name}")
    git(repo, "commit", "-m", "result")
    return repo, git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("deliverables,kwargs,missing", [
    (["alpha/module"], {"gitlinks": ["alpha/module"]}, ["alpha/module"]),                    # F1 file
    (["alpha/results/"], {"gitlinks": ["alpha/results/module"]}, ["alpha/results/"]),       # F1 directory
    (["alpha/results/"], {"gitlinks": ["alpha/results/module"], "files": ["alpha/results/out.txt"]}, []),
    (["alpha/link"], {"symlinks": ["alpha/link"]}, ["alpha/link"]),                          # symlink is not a file
    ([" artifact/result.txt"], {"files": [" artifact/result.txt"]}, []),                     # F2 exact, first entry
    (["artifact/result.txt"], {"files": [" artifact/result.txt"]}, ["artifact/result.txt"]),  # F2 look-alike
    ([" artifact/result.txt"], {"files": [" a-first.txt", " artifact/result.txt"]}, []),     # F2 not first
])
def test_deliverable_tree_entries(tmp_path, deliverables, kwargs, missing):
    repo, subject = tree_repo(tmp_path, **kwargs)
    assert cli._missing_deliverables(repo, subject, deliverables) == missing


def test_tree_inspection_error_blocks_instead_of_accepting(case, monkeypatch):
    real = cli._git_raw

    def fault(repo, *args):
        if args and args[0] == "ls-tree":
            raise subprocess.CalledProcessError(17, ["git", *args])
        return real(repo, *args)

    monkeypatch.setattr(cli, "_git_raw", fault)
    case.spec["checks"] = ["scope"]
    assert execute(case) == 2
    task = loaded(case)[1]
    assert task.state == "blocked" and task.reason_code == "deliverable_inspection_error"
    assert task.accepted_attempt_id is None
