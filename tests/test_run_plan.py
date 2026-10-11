"""W10c: orch run-plan with an offline worker. No model CLI, no network."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from orchestrator import cli, records
from orchestrator.adapters.base import AttemptResult
from orchestrator.run_plan import integration_ref

WORKER = Path(__file__).parent / "fixtures/plan_worker.py"
HASH = "a" * 64


class PlanAdapter:
    def __init__(self, mode):
        self.mode = mode

    def capabilities(self):
        return frozenset({"edit_files", "stdin_prompt"})

    def build_command(self, spec, workspace, prompt_path):
        return [sys.executable, str(WORKER.resolve()), "--workspace", str(workspace), "--mode", self.mode]

    def parse_events(self, path):
        return AttemptResult(status="completed", final_message="worker claims success")

    def stop(self, handle, grace_seconds=3):
        return handle.stop(grace_seconds)


def factory(route, config, evidence, config_dir):
    return PlanAdapter(route["model"]), SimpleNamespace(last_message_path=evidence / "last-message.txt")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Plan Test",
                           "-c", "user.email=plan-test@example.invalid", "-c", "commit.gpgsign=false", *args],
                          capture_output=True, check=True, timeout=30).stdout.decode().strip()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


def artifact_check(checks, check_id, paths):
    code = "from pathlib import Path\n" + "".join(
        f"assert Path({p!r}).read_text().startswith('ok')\n" for p in paths)
    write(checks / f"{check_id}.json", {"check_id": check_id, "validator_version": "1",
                                        "command": ["{python}", "-c", code], "required_artifacts": paths})


@pytest.fixture
def case(tmp_path):
    repo = tmp_path / "target repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    write(repo / "seed.txt", "base")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    checks = tmp_path / "checks"
    write(checks / "scope.json", {"check_id": "scope", "validator_version": "1", "kind": "scope"})
    artifact_check(checks, "alpha-out", ["alpha/out.txt"])
    artifact_check(checks, "beta-out", ["beta/out.txt"])
    artifact_check(checks, "gamma-out", ["alpha/out.txt", "beta/out.txt", "gamma/out.txt"])
    config = {"paths": {"codex": "unused-codex"},
              "routes": {mode: {"tool": "codex", "model": mode}
                         for mode in ("ok", "slow", "fail-once", "fail-always")},
              "limits": {"timeout_seconds": 60, "max_attempts_total": 2}}

    def spec(task_id, depends_on, checks_, route="ok"):
        return {"schema_version": 1, "run_id": "pilot", "task_id": task_id, "plan_version": 1,
                "goal": f"make {task_id}", "depends_on": depends_on, "base_commit": base, "protocol_hash": HASH,
                "allowed_write_paths": [f"{task_id}/"], "deliverables": [f"{task_id}/out.txt"],
                "checks": checks_, "route_id": route}

    specs = {"alpha": spec("alpha", ["prep"], ["alpha-out", "scope"]),
             "beta": spec("beta", ["prep"], ["beta-out", "scope"]),
             "gamma": spec("gamma", ["alpha", "beta"], ["gamma-out", "scope"])}
    plan = {"schema_version": 1, "run_id": "pilot", "plan_version": 1, "base_commit": base,
            "protocol_hash": HASH,
            "tasks": [{"task_id": "prep", "status": "completed", "completed_commit": base}]
                     + [{"task_id": t, "spec": f"tasks/{t}.json", "instructions": f"instructions/{t}.md"}
                        for t in specs]}
    c = SimpleNamespace(repo=repo, base=base, checks=checks, config=config, specs=specs, plan=plan,
                        plan_dir=tmp_path / "plan", root=tmp_path / "state", tmp=tmp_path)
    c.instructions = {t: f"# {t} instructions\n\nBuild {t} carefully.\n" for t in specs}
    return c


def run(c, *extra):
    write(c.plan_dir / "plan.v1.json", c.plan)
    for task_id, value in c.specs.items():
        write(c.plan_dir / "tasks" / f"{task_id}.json", value)
    for task_id, text in c.instructions.items():
        write(c.plan_dir / "instructions" / f"{task_id}.md", text)
    write(c.tmp / "config.json", c.config)
    argv = ["run-plan", str(c.plan_dir / "plan.v1.json"), "--repo", str(c.repo), "--config",
            str(c.tmp / "config.json"), "--checks-dir", str(c.checks), "--run-root", str(c.root), *extra]
    return cli.main(argv, adapter_factory=factory)


def loaded(c):
    return records.load_run("pilot", c.root)


def prompt_of(c, run, task_id, index=-1):
    aid = run.tasks[task_id].attempts[index]
    return (c.root / "runs/pilot/attempts" / aid / "evidence/prompt.txt").read_text(encoding="utf-8")


def test_sequential_plan_integrates_with_no_ff_merges(case, capsys):
    assert run(case) == 0
    run_ = loaded(case)
    assert run_.state == "completed"
    assert {t: run_.tasks[t].state for t in case.specs} == dict.fromkeys(case.specs, "integrated")
    head = git(case.repo, "rev-parse", integration_ref("pilot"))
    assert run_.summary["integration_head"] == head
    assert [m["task_id"] for m in run_.summary["merges"]] == ["alpha", "beta", "gamma"]
    for merge in run_.summary["merges"]:  # every merge is a two-parent --no-ff commit
        assert len(git(case.repo, "rev-list", "--parents", "-n", "1", merge["merge_commit"]).split()) == 3
    assert git(case.repo, "show", f"{head}:gamma/out.txt").startswith("ok")
    assert git(case.repo, "rev-parse", "main") == case.base  # user's branch untouched
    assert "completed; integration" in capsys.readouterr().out


def test_dependent_task_starts_from_integration_commit(case):
    assert run(case) == 0
    run_ = loaded(case)
    gamma_base = run_.summary["tasks"]["gamma"]["base_commit"]
    beta_merge = run_.summary["merges"][1]["merge_commit"]
    assert gamma_base == beta_merge != case.base
    # gamma's scope check passed although alpha/ and beta/ changed since the plan base
    checks = {c.check_id: c.verdict for c in records.load_checks(case.root, "pilot")
              if c.attempt_id == run_.tasks["gamma"].accepted_attempt_id}
    assert checks == {"gamma-out": "pass", "scope": "pass"}


def test_completed_task_is_not_a_task_record(case):
    assert run(case) == 0
    run_ = loaded(case)
    assert "prep" not in run_.tasks
    assert run_.summary["completed_tasks"] == {"prep": case.base}


def test_prompt_carries_instructions_and_constraints(case):
    assert run(case) == 0
    text = prompt_of(case, loaded(case), "beta")
    assert text.startswith("# beta instructions\n\nBuild beta carefully.")
    assert '"deliverables": [\n    "beta/out.txt"\n  ]' in text
    assert "Previous attempt" not in text


def test_failed_check_retries_with_reason_only_feedback(case):
    case.specs["alpha"]["route_id"] = "fail-once"
    assert run(case) == 0
    run_ = loaded(case)
    alpha = run_.tasks["alpha"]
    assert len(alpha.attempts) == 2 and alpha.state == "integrated"
    retry = prompt_of(case, run_, "alpha", 1)
    assert "Previous attempt's registered checks (IDs, verdicts and reason codes only):" in retry
    assert "- alpha-out: fail (missing_artifact)" in retry
    assert "assert Path" not in retry and "Traceback" not in retry  # no verifier output
    assert "Previous attempt" not in prompt_of(case, run_, "alpha", 0)


def test_budget_exhausted_fails_and_dependents_never_start(case, capsys):
    case.specs["alpha"]["route_id"] = "fail-always"
    assert run(case) == 1
    run_ = loaded(case)
    assert run_.tasks["alpha"].state == "failed" and len(run_.tasks["alpha"].attempts) == 2
    assert run_.tasks["gamma"].state == "planned" and not run_.tasks["gamma"].attempts
    assert run_.state == "failed"
    assert "failed at alpha" in capsys.readouterr().out


def test_invalid_plan_starts_nothing(case, capsys):
    case.specs["beta"]["depends_on"] = ["gamma"]  # cycle
    assert run(case) == 1
    assert "dependency cycle" in capsys.readouterr().out
    assert not (case.root / "runs").exists()


def test_private_env_missing_starts_nothing(case, capsys, monkeypatch):
    monkeypatch.delenv("PLAN_TEST_PRIVATE", raising=False)
    case.plan.update(private_env=["PLAN_TEST_PRIVATE"],
                     leak_sources=[{"env": "PLAN_TEST_PRIVATE", "files": ["eval.jsonl"]}])
    case.config["environment"] = {"strip_env": ["PLAN_TEST_PRIVATE"]}
    assert run(case) == 1
    assert "leak check impossible" in capsys.readouterr().out
    assert not (case.root / "runs").exists()


def test_rerun_skips_integrated_tasks(case):
    assert run(case) == 0
    before = loaded(case)
    assert run(case) == 0
    after = loaded(case)
    assert {t: after.tasks[t].attempts for t in case.specs} == {t: before.tasks[t].attempts for t in case.specs}
    assert after.summary["merges"] == before.summary["merges"]


def test_changed_plan_is_refused(case, capsys):
    case.specs["alpha"]["route_id"] = "fail-always"
    assert run(case) == 1
    case.instructions["alpha"] += "Changed.\n"
    assert run(case) == 1
    assert "use a new run_id" in capsys.readouterr().out


def test_parallel_runs_independent_tasks_at_once(case):
    for task_id in ("alpha", "beta"):
        case.specs[task_id]["route_id"] = "slow"
    assert run(case, "--parallel", "2") == 0
    head = loaded(case).summary["integration_head"]

    def window(task_id):
        lines = dict(line.split("=") for line in git(case.repo, "show", f"{head}:{task_id}/out.txt").splitlines()[1:])
        return float(lines["start"]), float(lines["end"])

    (a_start, a_end), (b_start, b_end) = window("alpha"), window("beta")
    assert a_start < b_end and b_start < a_end  # the two runs overlapped
    run_ = loaded(case)
    assert run_.summary["tasks"]["alpha"]["base_commit"] == run_.summary["tasks"]["beta"]["base_commit"] == case.base


def test_parallel_limit(case, capsys):
    assert run(case, "--parallel", "3") == 1
    assert "--parallel must be between 1 and 2" in capsys.readouterr().out


def test_integration_failure_blocks_run(case, monkeypatch, capsys):
    from orchestrator import run_plan

    def conflict(self, task_id, attempt_id, subject):
        raise subprocess.CalledProcessError(1, ["git", "merge"])

    monkeypatch.setattr(run_plan._Integrator, "merge", conflict)
    assert run(case) == 2
    run_ = loaded(case)
    assert run_.state == "blocked" and run_.reason_code == "integration_conflict"
    assert run_.tasks["alpha"].state == "accepted"
    assert run_.tasks["gamma"].state == "planned"
    assert "accepted but not integrated" in capsys.readouterr().out
    monkeypatch.undo()
    assert run(case) == 0  # resume integrates and continues
    assert loaded(case).state == "completed"


# Codex review 2026-10-11 (reviews/W10c-codex-review.md), regressions F1-F3
def test_f1_later_task_accepted_first_waits_for_plan_order(case):
    case.specs["alpha"]["route_id"] = "slow"  # alpha finishes after beta
    assert run(case, "--parallel", "2") == 0
    run_ = loaded(case)
    assert [m["task_id"] for m in run_.summary["merges"]] == ["alpha", "beta", "gamma"]
    head = run_.summary["integration_head"]
    second_parents = [line.split()[2] for line in
                      git(case.repo, "rev-list", "--first-parent", "--parents", "--reverse",
                          f"{case.base}..{head}").splitlines()]
    assert second_parents == [m["subject_commit"] for m in run_.summary["merges"]]
    alpha_end = float(git(case.repo, "show", f"{head}:alpha/out.txt").splitlines()[2].split("=")[1])
    beta_end = float(git(case.repo, "show", f"{head}:beta/out.txt").splitlines()[2].split("=")[1])
    assert beta_end < alpha_end  # the order held although beta was accepted first


def test_f2_blocked_task_halts_new_starts_before_its_thread_returns(case, monkeypatch):
    from orchestrator import run_plan
    import time

    write(case.checks / "hang.json", {"check_id": "hang", "validator_version": "1", "timeout_seconds": 1,
                                      "command": ["{python}", "-c", "import time; time.sleep(30)"]})
    case.specs["alpha"]["checks"] = ["hang"]  # undecidable -> blocked(verification_undecidable)
    case.specs["delta"] = dict(case.specs["beta"], task_id="delta", allowed_write_paths=["delta/"],
                               deliverables=["delta/out.txt"], checks=["scope"])
    case.instructions["delta"] = "# delta\n"
    case.plan["tasks"].append({"task_id": "delta", "spec": "tasks/delta.json",
                               "instructions": "instructions/delta.md"})
    real = run_plan._attempt_loop

    def slow_to_return(store, spec, *args, **kwargs):
        if spec["task_id"] == "beta":  # finish only inside alpha's window below
            deadline = time.monotonic() + 60
            while records.load_run("pilot", case.root).tasks["alpha"].state != "blocked":
                assert time.monotonic() < deadline
                time.sleep(0.05)
        code = real(store, spec, *args, **kwargs)
        if spec["task_id"] == "alpha":
            time.sleep(6)  # record says blocked; thread not finished yet
        return code

    monkeypatch.setattr(run_plan, "_attempt_loop", slow_to_return)
    assert run(case, "--parallel", "2") == 2
    run_ = loaded(case)
    assert run_.tasks["alpha"].state == "blocked"
    assert run_.tasks["beta"].state == "accepted"  # finished, but waits behind alpha (F1)
    assert run_.summary["merges"] == []
    assert run_.tasks["delta"].state == "planned" and not run_.tasks["delta"].attempts


def test_f3_bytes_moved_across_file_boundary_are_refused(case, capsys):
    case.specs["alpha"]["route_id"] = "fail-always"
    assert run(case) == 1
    recorded = loaded(case).summary["plan_files"]
    assert set(recorded) == {"plan.v1.json", "tasks/alpha.json", "tasks/beta.json", "tasks/gamma.json",
                             "instructions/alpha.md", "instructions/beta.md", "instructions/gamma.md"}
    # instructions/alpha.md and tasks/beta.json are neighbours in file order: move one LF across.
    alpha_md, beta_json = case.plan_dir / "instructions/alpha.md", case.plan_dir / "tasks/beta.json"
    assert alpha_md.read_bytes().endswith(b"\n")
    alpha_md.write_bytes(alpha_md.read_bytes()[:-1])
    beta_json.write_bytes(b"\n" + beta_json.read_bytes())  # still the same JSON
    capsys.readouterr()
    code = cli.main(["run-plan", str(case.plan_dir / "plan.v1.json"), "--repo", str(case.repo), "--config",
                     str(case.tmp / "config.json"), "--checks-dir", str(case.checks), "--run-root",
                     str(case.root)], adapter_factory=factory)
    assert code == 1 and "use a new run_id" in capsys.readouterr().out


# W13b (W13 final review concerns 4, 5, 7)
EVAL = {"schema": "eval-summary-1", "k": 3, "queries": 20, "protocol_hash": HASH,
        "a": {"hit_at_3": 0.5, "hits": 10, "no_result_queries": 0},
        "b": {"hit_at_3": 0.6, "hits": 12, "no_result_queries": 1}}


def add_publisher(case, files):
    code = ("import json,pathlib,sys; out=pathlib.Path(sys.argv[1]); "
            "[(out/n).write_text(t, encoding='utf-8') for n, t in json.loads(sys.argv[2]).items()]")
    write(case.checks / "publisher.json", {"check_id": "publisher", "validator_version": "1",
                                           "command": ["{python}", "-c", code, "{publish}", json.dumps(files)]})
    case.specs["gamma"]["checks"].append("publisher")
def test_prompt_carries_full_task_specification(case):
    assert run(case) == 0
    text = prompt_of(case, loaded(case), "gamma")
    marker = "this JSON is its full content):\n"
    assert "tasks/gamma.json, which may not exist in this workspace" in text
    spec, _ = json.JSONDecoder().raw_decode(text[text.index(marker) + len(marker):])
    assert spec == case.specs["gamma"]


def test_route_and_cli_version_are_recorded(case):
    case.config["paths"]["codex"] = sys.executable  # answers --version like a CLI would
    case.config["routes"]["ok"].update(model_reasoning_effort="high", sandbox="workspace-write")
    assert run(case) == 0
    scope = loaded(case).summary["tasks"]["alpha"]
    assert scope["route"] == {"tool": "codex", "model": "ok", "effort": "high", "sandbox": "workspace-write",
                              "approve_for_me": False, "defaults_applied": ["approve_for_me"]}
    assert scope["cli_version"].startswith("Python 3.")


@pytest.mark.parametrize("tool,expected", [  # W13b Codex review F3: adapter defaults are recorded
    ("codex", {"sandbox": "read-only", "approve_for_me": False, "defaults_applied": ["sandbox", "approve_for_me"]}),
    ("claude", {"permission_mode": "acceptEdits", "defaults_applied": ["permission_mode"]}),
])
def test_resolved_route_includes_adapter_defaults(tool, expected):
    from orchestrator.run_plan import _resolved_route
    assert _resolved_route({"tool": tool, "model": "m"}) == {"tool": tool, "model": "m", "effort": "cli-default",
                                                              **expected}


def test_unavailable_cli_version_does_not_fail_the_run(case):
    assert run(case) == 0  # paths.codex = "unused-codex" does not exist
    assert loaded(case).summary["tasks"]["alpha"]["cli_version"] == "unavailable"


def test_run_report_lists_verdicts_merges_and_published_evaluation(case):
    add_publisher(case, {"eval-summary.json": json.dumps(EVAL)})
    assert run(case) == 0
    report = json.loads((case.root / "runs/pilot/run-report.json").read_text(encoding="utf-8"))
    assert report["state"] == "completed" and report["integration_head"] == loaded(case).summary["integration_head"]
    rows = {row["task_id"]: row for row in report["tasks"]}
    assert [c["check_id"] for c in rows["gamma"]["checks"]] == ["gamma-out", "scope", "publisher"]
    assert all(c["verdict"] == "pass" for row in rows.values() for c in row["checks"])
    assert rows["alpha"]["merge_commit"] == loaded(case).summary["merges"][0]["merge_commit"]
    assert report["evaluation"]["task_id"] == "gamma"
    assert report["evaluation"]["eval_summary"]["b"]["hit_at_3"] == 0.6
    text = (case.root / "runs/pilot/run-report.md").read_text(encoding="utf-8")
    assert "| B | 0.6 | 12/20 | 1 |" in text and "gamma-out pass" in text


@pytest.mark.parametrize("files,reason", [  # W13b Codex review F1
    ({"eval-summary.json": json.dumps(EVAL), "query-details.json": "QUERY-MARKER q99 d999"}, "unexpected_files"),
    ({"eval-summary.json": json.dumps({**EVAL, "a": {**EVAL["a"], "hit_at_3": "QUERY-MARKER"}})}, "invalid_schema"),
    ({"eval-summary.json": json.dumps({**EVAL, "query_rows": ["QUERY-MARKER"]})}, "invalid_schema"),
    ({"eval-summary.json": json.dumps({**EVAL, "protocol_hash": "b" * 64})}, "protocol_hash_mismatch"),
])
def test_run_report_shows_nothing_from_a_rejected_publication(case, files, reason):
    add_publisher(case, files)
    assert run(case) == 0
    report_json = (case.root / "runs/pilot/run-report.json").read_text(encoding="utf-8")
    report_md = (case.root / "runs/pilot/run-report.md").read_text(encoding="utf-8")
    assert json.loads(report_json)["evaluation"]["rejected"] == reason
    for text in (report_json, report_md):
        assert "QUERY-MARKER" not in text and "q99" not in text and "| A |" not in text


def test_failed_run_report_shows_the_actual_integration_head(case):  # W13b Codex review F2
    case.specs["beta"]["route_id"] = "fail-always"
    assert run(case) == 1
    report = json.loads((case.root / "runs/pilot/run-report.json").read_text(encoding="utf-8"))
    head = git(case.repo, "rev-parse", integration_ref("pilot"))
    assert head != case.base and report["integration_head"] == head
    assert f"= `{head}`" in (case.root / "runs/pilot/run-report.md").read_text(encoding="utf-8")


def test_report_is_rebuilt_for_a_finished_run_without_rerunning(case):  # W13b Codex review U1
    assert run(case) == 0
    before = loaded(case)
    for name in ("run-report.json", "run-report.md"):
        (case.root / "runs/pilot" / name).unlink()
    assert run(case) == 0
    after = loaded(case)
    assert {t: after.tasks[t].attempts for t in case.specs} == {t: before.tasks[t].attempts for t in case.specs}
    report = json.loads((case.root / "runs/pilot/run-report.json").read_text(encoding="utf-8"))
    assert report["state"] == "completed" and report["integration_head"] == before.summary["integration_head"]


def test_run_report_without_evaluation_and_on_failure(case):
    case.specs["alpha"]["route_id"] = "fail-always"
    assert run(case) == 1
    report = json.loads((case.root / "runs/pilot/run-report.json").read_text(encoding="utf-8"))
    rows = {row["task_id"]: row for row in report["tasks"]}
    assert report["state"] == "failed" and report["evaluation"] is None
    assert rows["alpha"]["checks"][0] == {"check_id": "alpha-out", "verdict": "fail", "reason": "missing_artifact"}
    assert "Not available" in (case.root / "runs/pilot/run-report.md").read_text(encoding="utf-8")


def test_run_task_still_terminal_on_failed_check(case):
    spec = dict(case.specs["alpha"], depends_on=[], route_id="fail-once", run_id="single")
    write(case.tmp / "single.json", spec)
    write(case.tmp / "config.json", case.config)
    code = cli.main(["run-task", str(case.tmp / "single.json"), "--repo", str(case.repo), "--config",
                     str(case.tmp / "config.json"), "--checks-dir", str(case.checks), "--run-root",
                     str(case.root)], adapter_factory=factory)
    assert code == 1
    task = records.load_run("single", case.root).tasks["alpha"]
    assert task.state == "failed" and len(task.attempts) == 1
