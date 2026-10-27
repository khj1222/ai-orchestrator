"""W10c: orch run-plan with an offline worker. No model CLI, no network."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from orchestrator import cli, records, run_plan
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
                         for mode in ("ok", "slow", "fail-once", "fail-always", "rendezvous")},
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


def run(c, *extra, adapter_factory=factory):
    write(c.plan_dir / "plan.v1.json", c.plan)
    for task_id, value in c.specs.items():
        write(c.plan_dir / "tasks" / f"{task_id}.json", value)
    for task_id, text in c.instructions.items():
        write(c.plan_dir / "instructions" / f"{task_id}.md", text)
    write(c.tmp / "config.json", c.config)
    argv = ["run-plan", str(c.plan_dir / "plan.v1.json"), "--repo", str(c.repo), "--config",
            str(c.tmp / "config.json"), "--checks-dir", str(c.checks), "--run-root", str(c.root), *extra]
    return cli.main(argv, adapter_factory=adapter_factory)


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


def test_parallel_runs_independent_tasks_at_once(case, monkeypatch):
    # W16a: the workers meet at a rendezvous instead of sleeping, so the run only
    # succeeds if both worker processes are alive at the same time (no clock race).
    sync = case.tmp / "sync"
    sync.mkdir()
    monkeypatch.setenv("PLAN_WORKER_SYNC", str(sync))
    monkeypatch.setenv("PLAN_WORKER_PEERS", "2")
    for task_id in ("alpha", "beta"):
        case.specs[task_id]["route_id"] = "rendezvous"
    assert run(case, "--parallel", "2") == 0
    assert sorted(p.name for p in sync.iterdir()) == ["alpha.started", "beta.started"]
    # A retried worker would count the other task's old marker, so only first attempts prove overlap.
    assert [len(loaded(case).tasks[t].attempts) for t in ("alpha", "beta")] == [1, 1]
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
def test_f1_later_task_accepted_first_waits_for_plan_order(case, monkeypatch):
    from orchestrator import run_plan
    import time

    # W16a: alpha starts only after beta's acceptance is on record (read under the
    # run lock), so "a later task is accepted first" holds by construction.
    real = run_plan._attempt_loop
    seen = []

    def alpha_after_beta(store, spec, *args, **kwargs):
        if spec["task_id"] == "alpha":
            deadline = time.monotonic() + 60
            while True:
                with cli.RunStore(case.root, "pilot").lock(create=False):
                    beta = records.load_run("pilot", case.root).tasks["beta"].state
                if beta in ("accepted", "integrated"):
                    seen.append(beta)
                    break
                assert time.monotonic() < deadline
                time.sleep(0.05)
        return real(store, spec, *args, **kwargs)

    monkeypatch.setattr(run_plan, "_attempt_loop", alpha_after_beta)
    assert run(case, "--parallel", "2") == 0
    assert seen == ["accepted"]  # beta was accepted, and not yet integrated, before alpha ran
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

    def alpha_state():
        # Read under the run lock like `orch status`: an unlocked reader holding the file
        # open makes the manager's os.replace fail on Windows (W15a).
        with cli.RunStore(case.root, "pilot").lock(create=False):
            return records.load_run("pilot", case.root).tasks["alpha"].state

    def slow_to_return(store, spec, *args, **kwargs):
        if spec["task_id"] == "beta":  # finish only inside alpha's window below
            deadline = time.monotonic() + 60
            while alpha_state() != "blocked":
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
EXPECTED = {"k": 3, "queries": 20,
            "a": {"hit_at_3": 0.5, "hits": 10, "no_result_queries": 0},
            "b": {"hit_at_3": 0.6, "hits": 12, "no_result_queries": 1}}
TRUSTED_ENV = "PLAN_TEST_TRUSTED"


def add_publication(case, monkeypatch, *, gate_command=("{python}", "-c", "pass"), expected=EXPECTED):
    """W13b re-review R1: numbers come from a private file the manager pins, gated by a check."""
    private = case.tmp / "trusted"
    write(private / "eval-expected/summary.json", expected)
    write(private / "eval.jsonl", json.dumps({"text": "a private evaluation sentence"}) + "\n")
    monkeypatch.setenv(TRUSTED_ENV, str(private))
    case.plan.update(private_env=[TRUSTED_ENV], leak_sources=[{"env": TRUSTED_ENV, "files": ["eval.jsonl"]}],
                     publication={"env": TRUSTED_ENV, "file": "eval-expected/summary.json", "check": "gate"})
    case.config["environment"] = {"strip_env": [TRUSTED_ENV]}
    write(case.checks / "gate.json", {"check_id": "gate", "validator_version": "1", "command": list(gate_command)})
    case.specs["gamma"]["checks"].append("gate")
    return private


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
                              "approve_for_me": False, "disable_features": [], "agents_enabled": None,
                              "defaults_applied": ["approve_for_me", "disable_features", "agents_enabled"]}
    assert scope["cli_version"].startswith("Python 3.")


@pytest.mark.parametrize("tool,expected", [  # W13b Codex review F3: adapter defaults are recorded
    ("codex", {"sandbox": "read-only", "approve_for_me": False, "disable_features": [], "agents_enabled": None,
               "defaults_applied": ["sandbox", "approve_for_me", "disable_features", "agents_enabled"]}),
    ("claude", {"permission_mode": "acceptEdits", "safe_mode": True,
                "defaults_applied": ["permission_mode", "safe_mode"]}),
])
def test_resolved_route_includes_adapter_defaults(tool, expected):
    from orchestrator.run_plan import _resolved_route
    assert _resolved_route({"tool": tool, "model": "m"}) == {"tool": tool, "model": "m", "effort": "cli-default",
                                                              **expected}


def test_resolved_route_records_safe_mode_opt_out():  # W16c
    from orchestrator.run_plan import _resolved_route
    assert _resolved_route({"tool": "claude", "model": "m", "safe_mode": False,
                            "permission_mode": "plan"}) == {
        "tool": "claude", "model": "m", "effort": "cli-default", "permission_mode": "plan",
        "safe_mode": False, "defaults_applied": []}


def test_unavailable_cli_version_does_not_fail_the_run(case):
    assert run(case) == 0  # paths.codex = "unused-codex" does not exist
    assert loaded(case).summary["tasks"]["alpha"]["cli_version"] == "unavailable"


def test_run_report_lists_verdicts_merges_and_trusted_evaluation(case, monkeypatch):
    add_publication(case, monkeypatch)
    assert run(case) == 0
    report = json.loads((case.root / "runs/pilot/run-report.json").read_text(encoding="utf-8"))
    assert report["state"] == "completed" and report["integration_head"] == loaded(case).summary["integration_head"]
    rows = {row["task_id"]: row for row in report["tasks"]}
    assert [c["check_id"] for c in rows["gamma"]["checks"]] == ["gamma-out", "scope", "gate"]
    assert all(c["verdict"] == "pass" for row in rows.values() for c in row["checks"])
    assert rows["alpha"]["merge_commit"] == loaded(case).summary["merges"][0]["merge_commit"]
    evaluation = report["evaluation"]
    assert evaluation["source"] == "pinned expected summary" and evaluation["task_id"] == "gamma"
    assert evaluation["eval_summary"] == EXPECTED
    text = (case.root / "runs/pilot/run-report.md").read_text(encoding="utf-8")
    assert "| B | 0.6 | 12/20 | 1 |" in text and "gamma-out pass" in text


def test_expected_summary_is_pinned_before_any_worker_runs(case, monkeypatch):
    private = add_publication(case, monkeypatch)
    assert run(case) == 0
    pinned = loaded(case).summary["publication"]
    assert pinned["summary"] == EXPECTED and pinned["check"] == "gate"
    assert pinned["sha256"] == hashlib.sha256((private / "eval-expected/summary.json").read_bytes()).hexdigest()


def test_numbers_never_come_from_a_check_process(case, monkeypatch):  # W13b re-review R1
    # A gate check that writes encoded numbers wherever it can changes nothing published.
    code = ("import json,pathlib; fake={'k':3,'queries':65536,'a':{'hit_at_3':28985/65536,'hits':28985,"
            "'no_result_queries':12668},'b':{'hit_at_3':25657/65536,'hits':25657,'no_result_queries':12337}}; "
            "[pathlib.Path(p).write_text(json.dumps(fake)) for p in ('eval-summary.json','summary.json')]")
    add_publication(case, monkeypatch, gate_command=("{python}", "-c", code))
    assert run(case) == 0
    report_json = (case.root / "runs/pilot/run-report.json").read_text(encoding="utf-8")
    assert json.loads(report_json)["evaluation"]["eval_summary"] == EXPECTED
    assert "28985" not in report_json and "65536" not in report_json


def test_numbers_are_withheld_when_the_gate_check_fails(case, monkeypatch):
    add_publication(case, monkeypatch, gate_command=("{python}", "-c", "raise SystemExit(3)"))
    assert run(case) == 1
    report = json.loads((case.root / "runs/pilot/run-report.json").read_text(encoding="utf-8"))
    assert report["evaluation"]["withheld"] == "check_not_passed" and "eval_summary" not in report["evaluation"]
    assert "| A |" not in (case.root / "runs/pilot/run-report.md").read_text(encoding="utf-8")


def test_numbers_are_withheld_when_the_pinned_source_changed(case, monkeypatch):
    private = add_publication(case, monkeypatch)
    assert run(case) == 0
    write(private / "eval-expected/summary.json", {**EXPECTED, "a": {"hit_at_3": 0.55, "hits": 11, "no_result_queries": 0}})
    (case.root / "runs/pilot/run-report.json").unlink()
    assert run(case) == 0  # finished run: report rebuilt, nothing re-run
    report = json.loads((case.root / "runs/pilot/run-report.json").read_text(encoding="utf-8"))
    assert report["evaluation"]["withheld"] == "source_changed"


@pytest.mark.parametrize("damage,reason", [  # W13b re-review 2 F4
    ("delete", "source_missing"), ("not-json", "invalid_json"), ("extra-key", "invalid_summary"),
    ("env-unset", "source_env_not_set"),
])
@pytest.mark.parametrize("report_kept", [True, False])
def test_finished_run_report_withholds_when_the_source_is_unusable(case, monkeypatch, damage, reason, report_kept):
    private = add_publication(case, monkeypatch)
    assert run(case) == 0
    before = loaded(case)
    source = private / "eval-expected/summary.json"
    if damage == "delete":
        source.unlink()
    elif damage == "not-json":
        source.write_text("{not json", encoding="utf-8")
    elif damage == "extra-key":
        write(source, {**EXPECTED, "query_rows": ["q11"]})
    else:
        monkeypatch.delenv(TRUSTED_ENV)
    if not report_kept:
        for name in ("run-report.json", "run-report.md"):
            (case.root / "runs/pilot" / name).unlink()
    assert run(case) == 0  # finished run: report rebuilt, nothing restarted
    after = loaded(case)
    assert {t: after.tasks[t].attempts for t in case.specs} == {t: before.tasks[t].attempts for t in case.specs}
    report = json.loads((case.root / "runs/pilot/run-report.json").read_text(encoding="utf-8"))
    assert report["evaluation"]["withheld"] == reason and "eval_summary" not in report["evaluation"]
    text = (case.root / "runs/pilot/run-report.md").read_text(encoding="utf-8")
    assert "| A |" not in text and "q11" not in text


def test_evaluation_names_the_protocol_hash(case, monkeypatch):  # W13b re-review 2 F5
    add_publication(case, monkeypatch, gate_command=("{python}", "-c", "raise SystemExit(3)"))
    assert run(case) == 1
    withheld = json.loads((case.root / "runs/pilot/run-report.json").read_text(encoding="utf-8"))["evaluation"]
    assert withheld["protocol_hash"] == HASH
    assert f"protocol_hash: `{HASH}`" in (case.root / "runs/pilot/run-report.md").read_text(encoding="utf-8")


def test_published_evaluation_names_the_protocol_hash(case, monkeypatch):  # W13b re-review 2 F5
    add_publication(case, monkeypatch)
    assert run(case) == 0
    evaluation = json.loads((case.root / "runs/pilot/run-report.json").read_text(encoding="utf-8"))["evaluation"]
    assert evaluation["protocol_hash"] == HASH and evaluation["eval_summary"] == EXPECTED
    assert f"protocol_hash: `{HASH}`" in (case.root / "runs/pilot/run-report.md").read_text(encoding="utf-8")


def test_invalid_publication_source_starts_nothing(case, monkeypatch, capsys):
    add_publication(case, monkeypatch, expected={**EXPECTED, "queries": 65536})  # hit_at_3 no longer hits/queries
    assert run(case) == 1
    assert "publication source unusable: invalid_summary" in capsys.readouterr().out
    assert not (case.root / "runs").exists()


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
    assert "Not published" in (case.root / "runs/pilot/run-report.md").read_text(encoding="utf-8")


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


# W14a: deliverables must exist in the result commit
def test_scope_only_task_without_deliverables_retries_then_fails(case):
    case.specs["alpha"]["checks"] = ["scope"]
    case.specs["alpha"]["route_id"] = "fail-always"  # commits nothing
    assert run(case) == 1
    run_ = loaded(case)
    alpha = run_.tasks["alpha"]
    assert alpha.state == "failed" and alpha.reason_code == "missing_deliverable" and len(alpha.attempts) == 2
    assert "- deliverables: fail (missing_deliverable)" in prompt_of(case, run_, "alpha", 1)
    assert run_.tasks["gamma"].state == "planned" and not run_.tasks["gamma"].attempts
    report = json.loads((case.root / "runs/pilot/run-report.json").read_text(encoding="utf-8"))
    row = next(r for r in report["tasks"] if r["task_id"] == "alpha")
    assert row["deliverables_missing"] == ["alpha/out.txt"]
    assert "deliverables missing: alpha/out.txt" in (case.root / "runs/pilot/run-report.md").read_text(encoding="utf-8")


def test_missing_deliverable_feedback_lets_the_retry_succeed(case):
    case.specs["alpha"]["checks"] = ["scope"]
    case.specs["alpha"]["route_id"] = "fail-once"  # writes deliverables only after feedback
    assert run(case) == 0
    alpha = loaded(case).tasks["alpha"]
    assert alpha.state == "integrated" and len(alpha.attempts) == 2


# W16f: the CLI paths of the routes the plan's executable tasks use are checked
# before any run record. The real adapter_for runs, but every configured "CLI" is
# missing or a text file that cannot start, so no model CLI ever runs.

def not_a_program(path):
    write(path, "not a program\n")
    return path


def test_missing_cli_path_refuses_plan_before_any_record(case, capsys):
    missing = case.tmp / "Codex" / "bin" / "old-hash" / "codex.exe"
    case.config["paths"]["codex"] = str(missing)
    assert run(case, adapter_factory=cli.adapter_for) == 1
    out = capsys.readouterr().out
    assert f"config.paths.codex does not name an existing file: {missing}" in out and "<hash>" in out
    assert not (case.root / "runs").exists()


def test_plan_checks_the_union_of_its_routes_tools(case, capsys):
    not_a_program(case.tmp / "codex.txt")
    missing = case.tmp / "missing-claude.exe"
    case.config["paths"].update(codex="codex.txt", claude=str(missing))
    case.config["routes"]["claude-ok"] = {"tool": "claude", "model": "claude-opus-5"}
    case.specs["gamma"]["route_id"] = "claude-ok"  # only the last task uses Claude
    assert run(case, adapter_factory=cli.adapter_for) == 1
    out = capsys.readouterr().out
    assert f"config.paths.claude does not name an existing file: {missing}" in out
    assert "config.paths.codex" not in out
    assert not (case.root / "runs").exists()


def test_plan_does_not_check_a_tool_no_task_uses(case, capsys):
    not_a_program(case.tmp / "codex.txt")
    case.config["paths"].update(codex="codex.txt", claude=str(case.tmp / "missing-claude.exe"))
    case.config["routes"]["claude-unused"] = {"tool": "claude", "model": "claude-opus-5"}
    assert run(case, adapter_factory=cli.adapter_for) == 1  # the text file cannot start
    assert "config.paths.claude" not in capsys.readouterr().out
    run_ = loaded(case)
    assert run_.tasks["alpha"].reason_code == "attempt_budget_exhausted"
    assert all(run_.attempts[a].reason_code == "start_failed" for a in run_.tasks["alpha"].attempts)


# W16g: a plan that fails because a worker could not start names the executable.

def test_plan_start_failure_names_executable_on_screen(case, capsys):
    fake = not_a_program(case.tmp / "codex.txt")
    case.config["paths"]["codex"] = str(fake)
    assert run(case, adapter_factory=cli.adapter_for) == 1
    lines = capsys.readouterr().out.splitlines()
    status = [i for i, line in enumerate(lines) if "failed at alpha" in line]
    assert len(status) == 1
    note = lines[status[0] + 1]
    assert note.startswith("alpha: last attempt could not start (")
    assert Path(note.split("): ", 1)[1]).samefile(fake)


# W16h (Codex post-review F2): the summary names one state by priority, but every
# stopped task's start failure is shown. Records are built directly; nothing runs.

def stopped_run(c, states):
    write(c.plan_dir / "plan.v1.json", c.plan)
    for task_id, value in c.specs.items():
        write(c.plan_dir / "tasks" / f"{task_id}.json", value)
        write(c.plan_dir / "instructions" / f"{task_id}.md", c.instructions[task_id])
    plan = records.Run("pilot", "1", state="blocked", reason_code="review_pause",
                       tasks={t: records.Task(t, "1", state=s) for t, s in states.items()})
    plan.summary.update(plan_files=run_plan._plan_files(c.plan_dir / "plan.v1.json", c.plan),
                        plan_base_commit=c.base, completed_tasks={"prep": c.base},
                        tasks={t: {} for t in states}, merges=[])
    for task_id, state in states.items():
        if state == "planned":
            continue
        aid = "attempt-" + task_id
        task = plan.tasks[task_id]
        task.attempts = [aid]
        task.reason_code = "attempt_budget_exhausted" if state == "failed" else "user_stop"
        plan.attempts[aid] = records.Attempt(aid, task_id, "ok", state=state,
                                             reason_code="start_failed" if state == "failed" else "user_stop")
        if state == "failed":
            write(c.root / "runs" / "pilot" / "attempts" / aid / "evidence" / "start-error.json",
                  {"error_type": "OSError", "executable": f"{task_id}-program"})
    records.save_run(plan, c.root)
    store = cli.RunStore(c.root, "pilot")
    return run_plan._finish(store, list(c.specs), run_plan._Integrator(store, c.repo, c.base))


def test_plan_start_failure_notes_cover_every_failed_task(case, capsys):
    assert stopped_run(case, {"alpha": "failed", "beta": "failed", "gamma": "planned"}) == 1
    out = capsys.readouterr().out
    assert "alpha: last attempt could not start (OSError): alpha-program" in out
    assert "beta: last attempt could not start (OSError): beta-program" in out


def test_plan_start_failure_note_survives_a_cancelled_summary(case, capsys):
    assert stopped_run(case, {"alpha": "cancelled", "beta": "failed", "gamma": "planned"}) == 3
    out = capsys.readouterr().out
    assert "pilot: cancelled at alpha (user_stop)" in out  # summary and exit priority unchanged
    assert "beta: last attempt could not start (OSError): beta-program" in out
    assert "alpha: last attempt" not in out
