"""Verifier self-tests use public toy fixtures, never the private directory."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from orchestrator.verify import load_check_definition, run_checks
from verifier import reference

ROOT = Path(__file__).resolve().parents[1]

# Authored independently of verifier.reference. Both functions live only in
# this test file; generated candidate modules exist under tmp_path.
GOOD = '''
def _validate(query, documents, k):
    if not isinstance(query, str) or not isinstance(k, int) or isinstance(k, bool) or k < 0:
        raise ValueError("bad query/k")
    if not isinstance(documents, list):
        raise ValueError("bad documents")
    ids = []
    for document in documents:
        if not isinstance(document, dict) or not isinstance(document.get("doc_id"), str) or not isinstance(document.get("text"), str):
            raise ValueError("bad document")
        ids.append(document["doc_id"])
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate ID")

def search_a(query, documents, k):
    _validate(query, documents, k)
    q = set(query.lower().split())
    rows = [{"doc_id": d["doc_id"], "score": sum(t in q for t in set(d["text"].lower().split()))} for d in documents]
    return sorted(rows, key=lambda row: (-row["score"], row["doc_id"]))[:k]

def search_b(query, documents, k):
    _validate(query, documents, k)
    q = set(query.lower().split())
    rows = []
    for d in documents:
        terms = set(d["text"].lower().split())
        numerator = sum(t in q for t in terms)
        denominator = len(q) + len(terms) - numerator
        score = numerator / denominator if denominator else 0.0
        rows.append({"doc_id": d["doc_id"], "score": score})
    return sorted(rows, key=lambda row: (-row["score"], row["doc_id"]))[:k]
'''

# Explicit copies of eight hand-calculated cases requested by W9. These eight
# are public self-test fixtures; they are not claimed to be hidden holdouts.
HAND_CASES = [
    {"case_id": "normal-pair-red", "query": "red blue gold", "documents": [
        {"doc_id": "d003", "text": "green"}, {"doc_id": "d002", "text": "blue gold"}, {"doc_id": "d001", "text": "red blue"}],
     "k": 3, "expected_a": [["d001", 2], ["d002", 2], ["d003", 0]],
     "expected_b": [["d001", 0.6666666666666666], ["d002", 0.6666666666666666], ["d003", 0.0]]},
    {"case_id": "normal-pair-green", "query": "green", "documents": [
        {"doc_id": "d003", "text": "green"}, {"doc_id": "d002", "text": "blue gold"}, {"doc_id": "d001", "text": "red blue"}],
     "k": 3, "expected_a": [["d003", 1], ["d001", 0], ["d002", 0]],
     "expected_b": [["d003", 1.0], ["d001", 0.0], ["d002", 0.0]]},
    {"case_id": "repeat-both", "query": "red red blue", "documents": [
        {"doc_id": "d002", "text": "red red red"}, {"doc_id": "d001", "text": "red blue blue"}],
     "k": 2, "expected_a": [["d001", 2], ["d002", 1]], "expected_b": [["d001", 1.0], ["d002", 0.5]]},
    {"case_id": "tie-reversed", "query": "x", "documents": [
        {"doc_id": "d003", "text": "x y"}, {"doc_id": "d002", "text": "x z"}, {"doc_id": "d001", "text": "x w"}],
     "k": 3, "expected_a": [["d001", 1], ["d002", 1], ["d003", 1]],
     "expected_b": [["d001", 0.5], ["d002", 0.5], ["d003", 0.5]]},
    {"case_id": "tie-shuffled-fraction", "query": "a b", "documents": [
        {"doc_id": "d003", "text": "a c"}, {"doc_id": "d001", "text": "b d"}, {"doc_id": "d002", "text": "a e"}],
     "k": 3, "expected_a": [["d001", 1], ["d002", 1], ["d003", 1]],
     "expected_b": [["d001", 0.3333333333333333], ["d002", 0.3333333333333333], ["d003", 0.3333333333333333]]},
    {"case_id": "empty-union", "query": "", "documents": [{"doc_id": "d001", "text": ""}],
     "k": 1, "expected_a": [["d001", 0]], "expected_b": [["d001", 0.0]]},
    {"case_id": "punctuation-particle", "query": "원두 방법", "documents": [
        {"doc_id": "d003", "text": "원두는 방법."}, {"doc_id": "d002", "text": "원두 방법."}, {"doc_id": "d001", "text": "원두 방법"}],
     "k": 3, "expected_a": [["d001", 2], ["d002", 1], ["d003", 0]],
     "expected_b": [["d001", 1.0], ["d002", 0.3333333333333333], ["d003", 0.0]]},
    {"case_id": "k-zero", "query": "a", "documents": [{"doc_id": "d001", "text": "a"}],
     "k": 0, "expected_a": [], "expected_b": []},
]


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def write_json(path, value):
    write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_jsonl(path, rows):
    write(path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def install_methods(repo, source=GOOD, suffix=""):
    folder = repo / "examples/search-ab/methods"
    write(folder / "__init__.py", "")
    for method in ("a", "b"):
        write(folder / f"{method}.py", source + f"\nsearch = search_{method}\n" + suffix)


@pytest.fixture
def project(tmp_path):
    repo, private = tmp_path / "repo", tmp_path / "private"
    shutil.copytree(ROOT / "verifier", repo / "verifier", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "examples/search-ab/protocol", repo / "examples/search-ab/protocol", ignore=shutil.ignore_patterns("__pycache__"))
    install_methods(repo)
    write_json(private / "behavior-cases.json", HAND_CASES)
    documents = sorted(copy.deepcopy(HAND_CASES[0]["documents"]), key=lambda d: d["doc_id"])
    queries = [{"query_id": "q11", "text": "red blue gold", "relevant_doc_ids": ["d001", "d002"]},
               {"query_id": "q12", "text": "green", "relevant_doc_ids": ["d003"]}]
    write_jsonl(repo / "examples/search-ab/data/dev/documents.jsonl", documents)
    write_jsonl(private / "eval-queries.jsonl", queries)
    functions = {}
    exec(GOOD, functions)
    raw = {}
    summary = {"k": 3, "queries": 2}
    for method in ("a", "b"):
        rows = [{"method": method, "query_id": q["query_id"], "k": 3,
                 "results": [{"rank": i, **r} for i, r in enumerate(functions[f"search_{method}"](q["text"], documents, 3), 1)]}
                for q in queries]
        raw[method] = rows
        write_jsonl(private / "eval-expected" / f"raw-{method}.jsonl", rows)
        summary[method] = {"hit_at_3": 1.0, "hits": 2, "no_result_queries": 0}
    write_json(private / "eval-expected/summary.json", summary)
    return repo, private


def run_check(project, script, *, configured=True, fault=None, extra=()):
    repo, private = project
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    environment.pop("ORCH_VERIFIER_PRIVATE", None)
    if configured:
        environment["ORCH_VERIFIER_PRIVATE"] = str(private)
    if fault:
        environment["TEST_RUNNER_FAULT"] = fault
    return subprocess.run([sys.executable, "-B", str(repo / "verifier" / f"{script}.py"), *extra],
                          cwd=repo.parent, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)


@pytest.mark.parametrize("script", ["interface_contract", "method_behavior"])
def test_correct_candidate(project, script):
    result = run_check(project, script, configured=script != "interface_contract")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
    assert not list(project[0].rglob("__pycache__"))


@pytest.mark.parametrize("fault,case_id", [
    ("tie", "tie-reversed"), ("repeat", "repeat-both"),
    ("denominator", "normal-pair-red"), ("constant", "normal-pair-red"),
])
def test_incorrect_methods_fail(project, fault, case_id):
    source = GOOD
    if fault == "tie":
        source = source.replace('(-row["score"], row["doc_id"])', '-row["score"]')
    elif fault == "repeat":
        source = source.replace('for t in set(d["text"].lower().split())', 'for t in d["text"].lower().split()')
    elif fault == "denominator":
        source = source.replace('len(q) + len(terms) - numerator', 'len(terms)')
    else:
        source = source.replace('sorted(rows, key=lambda row: (-row["score"], row["doc_id"]))[:k]', 'rows[:k]')
    install_methods(project[0], source)
    result = run_check(project, "method_behavior")
    assert result.returncode == 1
    assert case_id in result.stdout


@pytest.mark.parametrize("script", ["method_behavior", "runner_consistency"])
def test_private_configuration_required(project, script):
    result = run_check(project, script, configured=False)
    assert result.returncode == 2
    assert "ORCH_VERIFIER_PRIVATE" in result.stderr


@pytest.mark.parametrize("script", ["method_behavior", "runner_consistency"])
def test_missing_private_file_is_environment_error(project, script):
    (project[1] / "behavior-cases.json").unlink()
    result = run_check(project, script)
    assert result.returncode == 2
    assert "behavior-cases.json" in result.stderr


@pytest.mark.parametrize("case", HAND_CASES, ids=lambda c: c["case_id"])
def test_reference_matches_hand_arithmetic(case):
    for method in ("a", "b"):
        actual = reference.search(case["query"], case["documents"], case["k"], method)
        assert [[r["doc_id"], r["score"]] for r in actual] == case[f"expected_{method}"]
        assert all(type(r["score"]) is (int if method == "a" else float) for r in actual)


@pytest.mark.parametrize("side_effect", [
    'print("side effect")',
    'open(__file__, encoding="utf-8").read()',
    'open("forbidden.txt", "w").write("bad")',
])
def test_import_side_effect_fails(project, side_effect):
    install_methods(project[0], GOOD, side_effect + "\n")
    result = run_check(project, "interface_contract", configured=False)
    assert result.returncode == 1
    assert "import side effect" in result.stdout
    assert not (project[0].parent / "forbidden.txt").exists()


@pytest.mark.parametrize("fault", ["bool-score", "float-a", "int-b", "mutation", "extra-key", "wrong-error", "nondeterministic"])
def test_contract_rejects_faults(project, fault):
    repo = project[0]
    if fault == "wrong-error":
        install_methods(repo, GOOD.replace("raise ValueError", "raise TypeError"))
    else:
        suffix = {
            "bool-score": 'for row in result: row["score"] = True',
            "float-a": 'for row in result: row["score"] = float(row["score"])',
            "int-b": 'for row in result: row["score"] = int(row["score"])',
            "mutation": 'documents.reverse()',
            "extra-key": 'for row in result: row["extra"] = 1',
            "nondeterministic": 'if result and _count % 2: result[0]["score"] += 1',
        }[fault]
        wrapper = '\n_original = search\n_count = 0\ndef search(query, documents, k):\n    global _count\n    _count += 1\n    result = _original(query, documents, k)\n    ' + suffix + '\n    return result\n'
        for method in ("a", "b"):
            if fault == "float-a" and method == "b" or fault == "int-b" and method == "a":
                continue
            file = repo / f"examples/search-ab/methods/{method}.py"
            write(file, file.read_text(encoding="utf-8") + wrapper)
    result = run_check(project, "interface_contract")
    assert result.returncode == 1, result.stdout


@pytest.mark.parametrize("score", [True, 2.0])
def test_behavior_rejects_numerically_equal_wrong_types(project, score):
    file = project[0] / "examples/search-ab/methods/a.py"
    source = file.read_text(encoding="utf-8")
    source += f'\n_old = search\ndef search(query, documents, k):\n    rows = _old(query, documents, k)\n    if rows: rows[0]["score"] = {score!r}\n    return rows\n'
    write(file, source)
    result = run_check(project, "method_behavior")
    assert result.returncode == 1
    assert "normal-pair-red" in result.stdout


def test_repeat_check_snapshots_reused_return_container(project):
    file = project[0] / "examples/search-ab/methods/a.py"
    source = file.read_text(encoding="utf-8")
    source += '''
_old = search
_shared = []
_count = 0
def search(query, documents, k):
    global _count
    _count += 1
    _shared[:] = _old(query, documents, k)
    if _shared: _shared[0]["score"] += _count
    return _shared
'''
    write(file, source)
    result = run_check(project, "interface_contract")
    assert result.returncode == 1
    assert "repeat" in result.stdout


FAKE_RUNNER = '''
import argparse, hashlib, json, os, platform, runpy, sys
from pathlib import Path
from methods import a, b
p = argparse.ArgumentParser()
p.add_argument("--documents"); p.add_argument("--queries"); p.add_argument("--out")
args = p.parse_args()
out = Path(args.out); out.mkdir()
docs = [json.loads(line) for line in Path(args.documents).read_text(encoding="utf-8").splitlines()]
queries = [json.loads(line) for line in Path(args.queries).read_text(encoding="utf-8").splitlines()]
def dump(name, value):
    (out / name).write_bytes((json.dumps(value, ensure_ascii=False) + "\\n").encode("utf-8"))
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
summary = {"k": 3, "queries": len(queries)}
fault = os.environ.get("TEST_RUNNER_FAULT")
for method, module in [("a", a), ("b", b)]:
    rows = []
    hits = 0
    for q in queries:
        result = module.search(q["text"], docs, 3)
        if fault == "ignore-modules":
            saved = [json.loads(line) for line in (Path(args.queries).parent / "eval-expected" / f"raw-{method}.jsonl").read_text(encoding="utf-8").splitlines()]
            result = [{"doc_id": r["doc_id"], "score": r["score"]} for r in next(row for row in saved if row["query_id"] == q["query_id"])["results"]]
        hits += int(any(r["doc_id"] in q["relevant_doc_ids"] for r in result))
        rows.append({"method": method, "query_id": q["query_id"], "k": 3,
                     "results": [{"rank": i, **r} for i, r in enumerate(result, 1)]})
    summary[method] = {"hit_at_3": hits / len(queries), "hits": hits, "no_result_queries": 0}
    if fault == "raw" and method == "a": rows[0]["results"][0]["score"] += 1
    if fault == "raw-type" and method == "a": rows[0]["results"][0]["score"] = float(rows[0]["results"][0]["score"])
    if fault == "raw-order": rows.reverse()
    (out / f"raw-{method}.jsonl").write_bytes(("".join(json.dumps(r, ensure_ascii=False) + "\\n" for r in rows)).encode("utf-8"))
if fault == "summary": summary["a"]["hits"] = 0
dump("summary.json", summary)
latency = {"unit": "ms", "timer": "time.perf_counter", "warmup": 1, "repeats": 5, "queries": len(queries),
           "a": {"runs_ms": [1.0, 2.0, 3.0, 4.0, 5.0], "median_ms": 3.0},
           "b": {"runs_ms": [2.0, 3.0, 4.0, 5.0, 6.0], "median_ms": 4.0}}
if fault == "median": latency["a"]["median_ms"] = 9.0
if fault == "repeats": latency["a"]["runs_ms"].pop()
if fault == "bool-latency": latency["a"]["runs_ms"][0] = True
dump("latency.json", latency)
report = "| 방법 | hit@3 | hits/queries | 결과 없는 질의 |\\n|---|---|---|---|\\n"
for m in ("a", "b"):
    s = summary[m]
    report += f"| {m.upper()} | {s['hit_at_3']} | {s['hits']}/{len(queries)} | 0 |\\n"
report += f"\\n| 방법 | 지연 중앙값 (ms, 질의 {len(queries)}개 1회 실행) | 측정 횟수 |\\n|---|---|---|\\n"
for m in ("a", "b"): report += f"| {m.upper()} | {latency[m]['median_ms']} | 5 (준비 1) |\\n"
print(report)
report += "\\n토큰화는 대소문자 정규화 후 공백 분리이며 한국어 형태소 분석을 하지 않았다. 지연은 같은 환경의 반복 측정 중앙값이며 작은 자료의 차이를 일반화하지 않는다.\\n"
if fault == "report": report = report.replace("| 1.0 |", "| 0.0 |")
(out / "report.md").write_bytes(report.encode("utf-8"))
manifest = {"protocol_hash": runpy.run_path("protocol/validate.py")["protocol_hash"](),
            "documents_sha256": digest(args.documents), "queries_sha256": digest(args.queries),
            "k": 3, "argv": sys.argv, "python": platform.python_version(), "platform": platform.platform(),
            "started_at": "2026-09-20T00:00:00+00:00", "ended_at": "2026-09-20T00:00:01+00:00",
            "outputs_sha256": {f.name: digest(f) for f in out.iterdir()}}
if fault == "hash": manifest["outputs_sha256"]["raw-a.jsonl"] = "0" * 64
if fault == "protocol-hash": manifest["protocol_hash"] = "0" * 64
if fault == "input-hash": manifest["queries_sha256"] = "0" * 64
if fault == "missing-hash": del manifest["outputs_sha256"]["report.md"]
dump("manifest.json", manifest)
'''


def install_runner(project):
    folder = project[0] / "examples/search-ab/runner"
    write(folder / "__init__.py", "")
    write(folder / "run.py", FAKE_RUNNER)


def test_runner_pass(project):
    install_runner(project)
    before = {str(p.relative_to(project[0])): p.read_bytes() for p in project[0].rglob("*") if p.is_file()}
    result = run_check(project, "runner_consistency")
    assert result.returncode == 0, result.stdout + result.stderr
    after = {str(p.relative_to(project[0])): p.read_bytes() for p in project[0].rglob("*") if p.is_file()}
    assert before == after


@pytest.mark.parametrize("fault,reason", [
    ("raw", "raw-a"), ("raw-type", "type"), ("raw-order", "query_id"), ("summary", "summary"),
    ("median", "median"), ("repeats", "repeats length"), ("bool-latency", "non-numeric"),
    ("hash", "outputs_sha256"), ("protocol-hash", "protocol_hash"), ("input-hash", "queries_sha256"),
    ("missing-hash", "outputs_sha256"), ("report", "tables"),
])
def test_runner_tampering_fails(project, fault, reason):
    install_runner(project)
    result = run_check(project, "runner_consistency", fault=fault)
    assert result.returncode == 1, result.stdout + result.stderr
    assert reason in result.stdout


def test_runner_publishes_only_aggregates_after_pass(project, tmp_path):  # W13b
    install_runner(project)
    publish = tmp_path / "publish"
    result = run_check(project, "runner_consistency", extra=("--publish", str(publish)))
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads((publish / "eval-summary.json").read_text(encoding="utf-8"))
    assert set(data) == {"schema", "k", "queries", "protocol_hash", "a", "b"}
    assert data["schema"] == "eval-summary-1"
    for method in ("a", "b"):
        assert set(data[method]) == {"hit_at_3", "hits", "no_result_queries"}
        assert data[method]["hit_at_3"] == data[method]["hits"] / data["queries"]
    expected = json.loads((project[1] / "eval-expected/summary.json").read_text(encoding="utf-8"))
    assert {m: data[m] for m in ("a", "b")} == {m: expected[m] for m in ("a", "b")}
    text = json.dumps({k: v for k, v in data.items() if k != "protocol_hash"})  # hex could contain "d003"
    for line in (project[1] / "eval-queries.jsonl").read_text(encoding="utf-8").splitlines():
        query = json.loads(line)
        assert query["text"] not in text and query["query_id"] not in text
        assert not any(doc in text for doc in query["relevant_doc_ids"])


def test_runner_failure_publishes_nothing(project, tmp_path):  # W13b
    install_runner(project)
    publish = tmp_path / "publish"
    result = run_check(project, "runner_consistency", fault="summary", extra=("--publish", str(publish)))
    assert result.returncode == 1
    assert not (publish / "eval-summary.json").exists()


def test_stored_oracle_cannot_be_silently_ignored(project):
    install_runner(project)
    path = project[1] / "eval-expected/raw-a.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["results"][0]["score"] = 99
    write_jsonl(path, rows)
    result = run_check(project, "runner_consistency")
    assert result.returncode == 1
    assert "stored reference/a" in result.stdout


def test_runner_cannot_hide_incorrect_search_modules(project):
    install_runner(project)
    install_methods(project[0], GOOD.replace('len(q) + len(terms) - numerator', 'len(terms)'))
    result = run_check(project, "runner_consistency", fault="ignore-modules")
    assert result.returncode == 1
    assert "direct/b" in result.stdout


@pytest.mark.parametrize("filename", ["eval-queries.jsonl", "eval-expected/raw-a.jsonl", "eval-expected/summary.json"])
def test_runner_requires_all_private_inputs(project, filename):
    (project[1] / filename).unlink()
    result = run_check(project, "runner_consistency")
    assert result.returncode == 2
    assert "missing file" in result.stderr


@pytest.mark.parametrize("check_id,script", [
    ("interface-contract-v1", "interface_contract"), ("method-behavior-v1", "method_behavior"),
    ("runner-consistency-v1", "runner_consistency"),
])
def test_registered_definitions(check_id, script):
    definition = load_check_definition(ROOT / "checks", check_id)
    assert definition.check_id == check_id
    publish = ("--publish", "{publish}") if script == "runner_consistency" else ()  # W13b
    assert definition.command == ("{python}", "-B", f"{{worktree}}/verifier/{script}.py", *publish)
    assert definition.timeout_seconds >= 120
    if script == "runner_consistency":
        assert "examples/search-ab/runner/run.py" in definition.required_artifacts
        assert definition.validator_version == "2"


def test_reference_cli_validates_private_oracle(project):
    result = run_check(project, "reference")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "8 hand-authored cases" in result.stdout


def test_registered_checks_run_in_fresh_verification_worktree(project, monkeypatch):
    repo, private = project
    install_runner(project)
    # Match the target repository, including on Windows core.autocrlf=true.
    write(repo / ".gitattributes", "* text=auto eol=lf\n")
    monkeypatch.setenv("ORCH_VERIFIER_PRIVATE", str(private))

    def git(*args):
        command = ["git", "-C", str(repo), "-c", "user.name=W9 Self Test",
                   "-c", "user.email=w9@example.invalid", "-c", "commit.gpgsign=false", *args]
        return subprocess.run(command, check=True, capture_output=True, timeout=30).stdout.decode().strip()

    git("init", "--initial-branch=main")
    git("add", ".")
    git("commit", "-m", "test candidate")
    commit = git("rev-parse", "HEAD")
    ids = ["interface-contract-v1", "method-behavior-v1", "runner-consistency-v1", "allowed-changes-v1"]
    result = run_checks(repo, commit, ids, ROOT / "checks", repo.parent / "evidence", "w9-self-test",
                        base_commit=commit, allowed_write_paths=["examples/search-ab/methods/", "examples/search-ab/runner/"])
    assert [c.verdict for c in result.checks] == ["pass"] * 4, [(c.check_id, c.evidence) for c in result.checks]
    assert all(c.subject_commit == commit and c.attempt_id == "w9-self-test" for c in result.checks)
    assert not result.worktree_path
