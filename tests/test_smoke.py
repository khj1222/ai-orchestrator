"""저장소 뼈대가 동작하는지 확인하는 최소 검사. 작업 W1 이전에 추가했다."""

from pathlib import Path

import orchestrator

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_package_imports():
    assert orchestrator.__version__


def test_fixtures_present():
    fixtures = REPO_ROOT / "tests" / "fixtures"
    assert (fixtures / "codex" / "exec-ok.jsonl").is_file()
    assert (fixtures / "codex" / "exec-shell-stopped.jsonl").is_file()
    assert (fixtures / "claude" / "print-not-logged-in.json").is_file()


def test_config_example_present():
    assert (REPO_ROOT / "config.example.json").is_file()
