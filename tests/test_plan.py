"""W10a: orch validate-plan. Offline; private data comes from tmp_path only."""

import copy
import json
from pathlib import Path

import pytest

from orchestrator import cli
from orchestrator.plan import validate_plan

HASH = "a" * 64
SECRET = "합성 비밀 질의를"


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")


def spec(task_id, paths, deliverables, checks, depends_on=()):
    return {
        "schema_version": 1, "run_id": "pilot-001", "task_id": task_id, "plan_version": 1,
        "goal": f"{task_id} goal", "depends_on": list(depends_on), "base_commit": "abc123",
        "protocol_hash": HASH, "allowed_write_paths": paths, "deliverables": deliverables,
        "checks": checks, "route_id": "codex-default",
        "limits": {"timeout_seconds": 600, "max_attempts_total": 2},
    }


@pytest.fixture
def case(tmp_path):
    checks = tmp_path / "checks"
    write(checks / "scope-v1.json", {"check_id": "scope-v1", "kind": "scope", "validator_version": "1",
                                     "command_id": "scope"})
    write(checks / "module-v1.json", {"check_id": "module-v1", "kind": "command", "validator_version": "1",
                                      "command": ["{python}", "-c", "pass"],
                                      "required_artifacts": ["ex/methods/a.py"]})
    write(checks / "runner-v1.json", {"check_id": "runner-v1", "kind": "command", "validator_version": "1",
                                      "command": ["{python}", "-c", "pass"],
                                      "required_artifacts": ["ex/methods/a.py", "ex/runner/run.py"]})
    private = tmp_path / "private"
    write(private / "eval-queries.jsonl",
          json.dumps({"query_id": "q99", "text": SECRET, "relevant_doc_ids": ["d999"]}, ensure_ascii=False) + "\n")
    config = {
        "paths": {"codex": "codex.exe"},
        "routes": {"codex-default": {"tool": "codex", "model": "m", "sandbox": "workspace-write"}},
        "limits": {"timeout_seconds": 600, "max_attempts_total": 2},
        "environment": {"strip_env": ["ORCH_VERIFIER_PRIVATE"], "extra_env": {"PYTHONUTF8": "1"}},
    }
    plan_dir = tmp_path / "runs" / "pilot-001"
    specs = {
        "search-module": spec("search-module", ["ex/methods/"], ["ex/methods/a.py"], ["module-v1", "scope-v1"],
                              ["prepare-protocol"]),
        "experiment-runner": spec("experiment-runner", ["ex/runner/"], ["ex/runner/run.py"], ["scope-v1"],
                                  ["prepare-protocol"]),
        "integrate": spec("integrate", ["ex/report/"], ["ex/report/"], ["runner-v1"],
                          ["search-module", "experiment-runner"]),
    }
    plan = {
        "schema_version": 1, "run_id": "pilot-001", "plan_version": 1, "base_commit": "abc123",
        "protocol_hash": HASH, "private_env": ["ORCH_VERIFIER_PRIVATE"],
        "leak_sources": [{"env": "ORCH_VERIFIER_PRIVATE", "files": ["eval-queries.jsonl"]}],
        "tasks": [{"task_id": "prepare-protocol", "status": "completed", "completed_commit": "abc123",
                   "deliverables": ["ex/protocol/"]}]
                 + [{"task_id": t, "spec": f"tasks/{t}.json", "instructions": f"instructions/{t}.md"}
                    for t in specs],
    }
    c = type("Case", (), {})()
    c.tmp, c.checks, c.config, c.plan_dir = tmp_path, checks, config, plan_dir
    c.plan, c.specs = plan, specs
    c.instructions = {t: f"# {t}\n\nDo the {t} task. Do not read verifier/ or the private folder.\n"
                      for t in specs}
    c.environ = {"ORCH_VERIFIER_PRIVATE": str(private)}
    return c


def run(c):
    write(c.plan_dir / "plan.v1.json", c.plan)
    for task_id, value in c.specs.items():
        write(c.plan_dir / "tasks" / f"{task_id}.json", value)
    for task_id, text in c.instructions.items():
        write(c.plan_dir / "instructions" / f"{task_id}.md", text)
    return validate_plan(c.plan_dir / "plan.v1.json", c.config, c.checks, environ=c.environ)


def errors_of(c):
    errors, order = run(c)
    assert errors and order == []
    return "\n".join(errors)


def test_valid_plan_passes_with_dependency_order(case):
    errors, order = run(case)
    assert errors == []
    assert order.index("integrate") > max(order.index("search-module"), order.index("experiment-runner"))
    assert order[0] == "prepare-protocol"


# P1
def test_unresolved_value_in_plan_is_rejected(case):
    case.plan["base_commit"] = "TO_BE_PINNED"
    assert "unresolved TO_BE_" in errors_of(case)


@pytest.mark.parametrize("key, value", [("protocol_hash", "ABC"), ("schema_version", 2), ("plan_version", 0),
                                        ("run_id", "../x"), ("extra", 1)])
def test_plan_field_errors(case, key, value):
    case.plan[key] = value
    assert "plan:" in errors_of(case)


def test_unresolved_value_in_instructions_is_rejected(case):
    case.instructions["integrate"] += "route: TO_BE_SELECTED\n"
    assert "integrate: instructions contain an unresolved TO_BE_" in errors_of(case)


# P2
def test_duplicate_task_id_ignoring_case(case):
    case.plan["tasks"].append({"task_id": "Search-Module", "spec": "tasks/search-module.json",
                               "instructions": "instructions/search-module.md"})
    assert "Search-Module: duplicate task_id" in errors_of(case)


@pytest.mark.parametrize("key", ["run_id", "base_commit", "protocol_hash", "plan_version", "task_id"])
def test_spec_must_agree_with_plan(case, key):
    case.specs["search-module"][key] = "b" * 64 if key == "protocol_hash" else (
        2 if key == "plan_version" else "other")
    assert f"search-module: spec {key} does not match the plan" in errors_of(case)


def test_spec_and_instruction_paths_stay_inside_plan_folder(case):
    case.plan["tasks"][1]["spec"] = "../outside.json"
    assert "search-module: spec must be a relative path" in errors_of(case)


# P3
def test_unknown_dependency(case):
    case.specs["integrate"]["depends_on"] = ["search-module", "ghost"]
    assert "integrate: depends on unknown task ghost" in errors_of(case)


def test_self_dependency(case):
    case.specs["integrate"]["depends_on"] = ["integrate"]
    assert "integrate: depends on itself" in errors_of(case)


def test_cycle_is_reported_with_path(case):
    case.specs["search-module"]["depends_on"] = ["integrate"]
    text = errors_of(case)
    assert "dependency cycle:" in text and "search-module" in text and "integrate" in text


# P4
def test_each_spec_passes_task_validation(case):
    case.specs["experiment-runner"]["route_id"] = "missing-route"
    assert "experiment-runner: route_id is not a configured route" in errors_of(case)


def test_run_task_still_requires_empty_depends_on(case):
    errors = cli.validate_task(case.specs["integrate"], case.config, case.checks)
    assert "depends_on must be an empty array in W7" in errors


# P5
def test_overlapping_write_paths_have_two_owners(case):
    case.specs["experiment-runner"]["allowed_write_paths"] = ["ex/runner/", "EX\\methods\\shared.py"]
    assert "write path owned twice: ex/methods/ (search-module) and EX\\methods\\shared.py" in errors_of(case)


def test_deliverable_outside_own_paths(case):
    case.specs["experiment-runner"]["deliverables"] = ["ex/runner/run.py", "ex/methods/b.py"]
    assert "experiment-runner: deliverable ex/methods/b.py is outside" in errors_of(case)


def test_sibling_prefix_is_not_overlap(case):
    case.specs["experiment-runner"]["allowed_write_paths"] = ["ex/runner/", "ex/methods-extra/"]
    errors, _ = run(case)
    assert errors == []


# P6 (W9 review U4)
def test_check_needing_other_task_artifact_is_misplaced(case):
    case.specs["experiment-runner"]["checks"] = ["runner-v1"]
    assert ("experiment-runner: check runner-v1 needs ex/methods/a.py, which neither this task nor its "
            "prerequisites deliver") in errors_of(case)


def test_completed_task_deliverables_count_for_dependents(case):
    write(case.checks / "proto-v1.json", {"check_id": "proto-v1", "kind": "command", "validator_version": "1",
                                          "command": ["{python}", "-c", "pass"],
                                          "required_artifacts": ["ex/protocol/README.md"]})
    case.specs["search-module"]["checks"].append("proto-v1")
    errors, _ = run(case)
    assert errors == []


# P7 (W9 review U1)
def test_private_env_must_be_stripped_from_workers(case):
    case.config["environment"]["strip_env"] = []
    assert "private_env ORCH_VERIFIER_PRIVATE is not in config environment.strip_env" in errors_of(case)


def test_private_env_must_not_be_restored(case):
    case.config["environment"]["extra_env"]["orch_verifier_private"] = "x"
    assert "must not appear in environment.extra_env" in errors_of(case)


def test_private_env_without_leak_sources(case):
    case.plan["leak_sources"] = []
    assert "leak_sources is empty" in errors_of(case)


# P8
def test_private_string_in_instructions_is_found_without_printing_it(case):
    case.instructions["search-module"] += "예시 질의: 합성   비밀\n질의를\n"
    text = errors_of(case)
    assert "instructions/search-module.md: contains a private string from eval-queries.jsonl:1.text" in text
    assert SECRET not in text


def test_private_string_in_spec_is_found(case):
    case.specs["search-module"]["goal"] = f"answer {SECRET}"
    assert "tasks/search-module.json: contains a private string" in errors_of(case)


def test_private_string_hidden_by_json_escapes_is_found(case):
    case.specs["search-module"]["goal"] = f"answer {SECRET}"
    run(case)
    path = case.plan_dir / "tasks" / "search-module.json"
    path.write_text(json.dumps(case.specs["search-module"], ensure_ascii=True), encoding="utf-8")
    errors, _ = validate_plan(case.plan_dir / "plan.v1.json", case.config, case.checks, environ=case.environ)
    assert any("tasks/search-module.json: contains a private string" in e for e in errors)


def test_short_private_values_are_ignored(case):
    case.instructions["search-module"] += "IDs look like d003.\n"
    errors, _ = run(case)
    assert errors == []


def test_leak_check_without_private_folder_is_an_error_not_a_pass(case):
    case.environ = {}
    assert "leak check impossible: ORCH_VERIFIER_PRIVATE is not set" in errors_of(case)


def test_unreadable_private_file_is_an_error(case):
    case.plan["leak_sources"][0]["files"].append("missing.json")
    assert "leak check impossible: cannot read private file missing.json" in errors_of(case)


def test_leak_source_file_must_be_a_plain_name(case):
    case.plan["leak_sources"][0]["files"] = ["../eval-queries.jsonl"]
    assert "plain file names" in errors_of(case)


# P9
def test_missing_instructions(case):
    del case.instructions["integrate"]
    assert "integrate: instructions unreadable" in errors_of(case)


def test_empty_instructions(case):
    case.instructions["integrate"] = "  \n"
    assert "integrate: instructions are empty" in errors_of(case)


# CLI
def test_cli_validate_plan(case, monkeypatch, capsys):
    run(case)
    write(case.tmp / "config.json", case.config)
    monkeypatch.setenv("ORCH_VERIFIER_PRIVATE", case.environ["ORCH_VERIFIER_PRIVATE"])
    argv = ["validate-plan", str(case.plan_dir / "plan.v1.json"), "--repo", str(case.tmp)]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "Valid plan: 4 tasks (3 executable)" in out and "order: prepare-protocol" in out
    bad = copy.deepcopy(case.plan)
    bad["protocol_hash"] = "x"
    write(case.plan_dir / "plan.v1.json", bad)
    assert cli.main(argv) == 1
    assert "protocol_hash" in capsys.readouterr().out
