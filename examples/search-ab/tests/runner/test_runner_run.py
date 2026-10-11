"""Runner tests use injected modules, never the real search implementations."""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import types

import pytest

from runner import run


EXPECTED_PROTOCOL_HASH = "ea86373c26e72d61f9ca6a15e5636ea96e8496d0c3c2bd0e6bbc1a21e512b624"
OUTPUT_NAMES = {
    "raw-a.jsonl", "raw-b.jsonl", "latency.json", "summary.json", "report.md", "manifest.json"
}
NOTE = (
    "토큰화는 대소문자 정규화 후 공백 분리이며 한국어 형태소 분석을 하지 않았다. "
    "지연은 같은 환경의 반복 측정 중앙값이며 작은 자료의 차이를 일반화하지 않는다."
)


def write_rows(path, rows):
    path.write_bytes("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8"))


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def inputs(tmp_path):
    documents = [
        {"doc_id": f"d{number:03}", "text": f"문서 {number}"}
        for number in range(1, 5)
    ]
    queries = [
        {"query_id": "q01", "text": "세 번째 관련", "relevant_doc_ids": ["d003"]},
        {"query_id": "q02", "text": "네 번째 관련", "relevant_doc_ids": ["d004"]},
        {"query_id": "q03", "text": "빈 결과 시험", "relevant_doc_ids": ["d001"]},
    ]
    documents_path, queries_path = tmp_path / "documents.jsonl", tmp_path / "queries.jsonl"
    write_rows(documents_path, documents)
    write_rows(queries_path, queries)
    out = tmp_path / "out"
    argv = ["--documents", str(documents_path), "--queries", str(queries_path), "--out", str(out)]
    return documents, queries, documents_path, queries_path, out, argv


@pytest.fixture
def fake_methods(monkeypatch):
    calls = []
    package = types.ModuleType("methods")
    package.__path__ = []
    modules = {}
    for method in ("a", "b"):
        module = types.ModuleType(f"methods.{method}")

        def search(text, documents, k, method=method):
            calls.append((method, text, documents, k))
            # Deliberately distinguish result order from document order.
            ordered = list(reversed(documents)) if method == "b" else documents
            return [
                {"doc_id": doc["doc_id"], "score": 7 if method == "a" else 1 / 7}
                for doc in ordered[:min(k, len(documents))]
            ]

        module.search = search
        setattr(package, method, module)
        monkeypatch.setitem(sys.modules, f"methods.{method}", module)
        modules[method] = module
    monkeypatch.setitem(sys.modules, "methods", package)
    return modules, calls


def assert_failed(argv, out, capsys):
    assert run.main(argv) != 0
    captured = capsys.readouterr()
    assert captured.err
    assert captured.out == ""
    assert not out.exists() or not list(out.iterdir())


def test_success_raw_summary_report_and_manifest(inputs, fake_methods, capsys, monkeypatch, tmp_path):
    documents, queries, documents_path, queries_path, out, argv = inputs
    modules, calls = fake_methods
    # Protocol resolution must work independently of the working directory.
    monkeypatch.chdir(tmp_path)
    assert run.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert {p.name for p in out.iterdir()} == OUTPUT_NAMES
    summary = read_json(out / "summary.json")
    latency = read_json(out / "latency.json")
    assert summary["k"] == 3
    assert summary["queries"] == len(queries)
    assert latency.keys() == {"unit", "timer", "warmup", "repeats", "queries", "a", "b"}
    assert (latency["unit"], latency["timer"]) == ("ms", "time.perf_counter")
    assert (latency["warmup"], latency["repeats"], latency["queries"]) == (1, 5, 3)
    assert len(calls) == 2 * len(queries) * (1 + 1 + 5)
    for _, _, passed_documents, k in calls:
        assert passed_documents == documents
        assert passed_documents is calls[0][2]
        assert k == 3
    for method, module in modules.items():
        expected = []
        for query in queries:
            results = module.search(query["text"], documents, 3)
            expected.append({
                "method": method, "query_id": query["query_id"], "k": 3,
                "results": [{"rank": index, **result} for index, result in enumerate(results, 1)],
            })
        raw_path = out / f"raw-{method}.jsonl"
        assert raw_path.read_bytes() == "".join(
            json.dumps(row, ensure_ascii=False) + "\n" for row in expected
        ).encode("utf-8")
        rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
        hits = sum(
            bool(set(query["relevant_doc_ids"]) & {r["doc_id"] for r in row["results"][:3]})
            for query, row in zip(queries, rows)
        )
        assert summary[method] == {"hit_at_3": hits / len(queries), "hits": hits, "no_result_queries": 0}
        assert len(latency[method]["runs_ms"]) == 5
        assert all(value == round(value, 3) and value >= 0 for value in latency[method]["runs_ms"])
        assert latency[method]["median_ms"] == statistics.median(latency[method]["runs_ms"])
    expected_tables = (
        "| 방법 | hit@3 | hits/queries | 결과 없는 질의 |\n"
        "|---|---|---|---|\n"
        f"| A | {summary['a']['hit_at_3']} | {summary['a']['hits']}/3 | 0 |\n"
        f"| B | {summary['b']['hit_at_3']} | {summary['b']['hits']}/3 | 0 |\n"
        "\n| 방법 | 지연 중앙값 (ms, 질의 3개 1회 실행) | 측정 횟수 |\n"
        "|---|---|---|\n"
        f"| A | {latency['a']['median_ms']} | 5 (준비 1) |\n"
        f"| B | {latency['b']['median_ms']} | 5 (준비 1) |\n"
    )
    assert captured.out == expected_tables
    assert (out / "report.md").read_text(encoding="utf-8") == expected_tables + "\n" + NOTE + "\n"
    manifest = read_json(out / "manifest.json")
    assert manifest.keys() == {
        "protocol_hash", "documents_sha256", "queries_sha256", "k", "argv", "python",
        "platform", "started_at", "ended_at", "outputs_sha256",
    }
    assert manifest["protocol_hash"] == EXPECTED_PROTOCOL_HASH
    assert manifest["documents_sha256"] == hashlib.sha256(documents_path.read_bytes()).hexdigest()
    assert manifest["queries_sha256"] == hashlib.sha256(queries_path.read_bytes()).hexdigest()
    assert manifest["argv"] == ["python", "-m", "runner.run", *argv]
    assert manifest["k"] == 3
    assert manifest["python"] == platform.python_version()
    assert manifest["platform"] == platform.platform()
    started, ended = (datetime.fromisoformat(manifest[key]) for key in ("started_at", "ended_at"))
    assert started.utcoffset() == ended.utcoffset() == timedelta(0)
    assert started <= ended
    assert set(manifest["outputs_sha256"]) == OUTPUT_NAMES - {"manifest.json"}
    for name, digest in manifest["outputs_sha256"].items():
        assert digest == hashlib.sha256((out / name).read_bytes()).hexdigest()
    for path in out.iterdir():
        raw = path.read_bytes()
        raw.decode("utf-8")
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in raw
        assert raw.endswith(b"\n")


def test_first_main_run_short_empty_and_untrimmed_results(inputs, fake_methods, monkeypatch, capsys):
    documents, queries, _, _, out, argv = inputs
    modules, _ = fake_methods
    # Nonconforming lengths exercise the runner's explicit pass-through contract.
    originals = {
        queries[0]["text"]: [{"doc_id": "d003", "score": 1 / 7}],
        queries[1]["text"]: [
            {"doc_id": document["doc_id"], "score": score}
            for document, score in zip(documents, [0, 99, -1, 0.123456789012345])
        ],
        queries[2]["text"]: [],
    }
    snapshots = deepcopy(originals)
    seen = set()

    def search(text, documents, k):
        if text in seen:
            return [{"doc_id": "d001", "score": 999}]
        seen.add(text)
        return originals[text]

    monkeypatch.setattr(modules["a"], "search", search)
    assert run.main(argv) == 0
    capsys.readouterr()
    rows = [json.loads(line) for line in (out / "raw-a.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [len(row["results"]) for row in rows] == [1, 4, 0]
    for row, query in zip(rows, queries):
        assert row["results"] == [
            {"rank": rank, **result} for rank, result in enumerate(snapshots[query["text"]], 1)
        ]
    assert originals == snapshots  # Adding ranks must not mutate module-owned dictionaries.
    assert read_json(out / "summary.json")["a"] == {
        "hit_at_3": 1 / 3, "hits": 1, "no_result_queries": 1,
    }


@pytest.mark.parametrize("warmup,repeats", [(0, 1), (2, 2)])
def test_timing_boundaries_and_method_order(inputs, fake_methods, monkeypatch, capsys, warmup, repeats):
    documents, queries, _, _, out, argv = inputs
    modules, _ = fake_methods
    events = []
    for method, module in modules.items():
        original = module.search

        def search(text, docs, k, method=method, original=original):
            events.append((method, text))
            return original(text, docs, k)

        monkeypatch.setattr(module, "search", search)
    intervals = [(10 * i, 10 * i + duration) for i, duration in enumerate([0.0012344, 0.0023454, 0.0034564, 0.0045674])]
    ticks = iter(tick for pair in intervals[:2 * repeats] for tick in pair)

    def timer():
        events.append("timer")
        return next(ticks)

    monkeypatch.setattr(run.time, "perf_counter", timer)
    argv += ["--warmup", str(warmup), "--repeats", str(repeats), "--k", "5"]
    assert run.main(argv) == 0
    stdout = capsys.readouterr().out
    expected = [(method, q["text"]) for q in queries for method in ("a", "b")]
    for method in ("a", "b"):
        one_run = [(method, q["text"]) for q in queries]
        expected.extend(one_run * warmup)
        for _ in range(repeats):
            expected.extend(["timer", *one_run, "timer"])
    assert events == expected
    latency = read_json(out / "latency.json")
    for index, method in enumerate(("a", "b")):
        runs = [round((end - start) * 1000, 3) for start, end in intervals[index * repeats:(index + 1) * repeats]]
        assert latency[method] == {"runs_ms": runs, "median_ms": statistics.median(runs)}
        assert f"| {method.upper()} | {statistics.median(runs)} | {repeats} (준비 {warmup}) |" in stdout
    assert read_json(out / "summary.json")["k"] == 5


@pytest.mark.parametrize("target,case", [
    (target, case)
    for target in ("documents", "queries")
    for case in ("duplicate", "empty", "extra_key", "missing_key", "bad_id", "bad_type",
                 "blank_text", "line_break", "too_long", "not_nfc", "unsorted", "not_object")
] + [("queries", case) for case in (
    "missing_relevant", "empty_relevant", "duplicate_relevant", "unsorted_relevant",
    "nonstring_relevant", "nonlist_relevant",
)])
def test_invalid_records_rejected_before_search(inputs, fake_methods, capsys, target, case):
    documents, queries, documents_path, queries_path, out, argv = inputs
    rows = deepcopy(documents if target == "documents" else queries)
    id_key = "doc_id" if target == "documents" else "query_id"
    if case == "duplicate":
        rows.append(rows[0])
    elif case == "empty":
        rows = []
    elif case == "extra_key":
        rows[-1]["extra"] = "not allowed"
    elif case == "missing_key":
        del rows[-1]["text"]
    elif case == "bad_id":
        rows[-1][id_key] = "invalid"
    elif case == "bad_type":
        rows[-1]["text"] = 1
    elif case == "blank_text":
        rows[-1]["text"] = "  "
    elif case == "line_break":
        rows[-1]["text"] = "a\nb"
    elif case == "too_long":
        rows[-1]["text"] = "a" * (301 if target == "documents" else 101)
    elif case == "not_nfc":
        rows[-1]["text"] = "e\u0301"
    elif case == "unsorted":
        rows.reverse()
    elif case == "not_object":
        rows[-1] = []
    else:
        rows[-1]["relevant_doc_ids"] = {
            "missing_relevant": ["d999"], "empty_relevant": [],
            "duplicate_relevant": ["d001", "d001"], "unsorted_relevant": ["d002", "d001"],
            "nonstring_relevant": [{}], "nonlist_relevant": "d001",
        }[case]
    write_rows(documents_path if target == "documents" else queries_path, rows)
    assert_failed(argv, out, capsys)
    assert fake_methods[1] == []


@pytest.mark.parametrize("target", ["documents", "queries"])
@pytest.mark.parametrize("case", ["json", "blank_line", "bom", "crlf", "last_lf", "utf8", "missing"])
def test_invalid_files_rejected(inputs, fake_methods, capsys, target, case):
    _, _, documents_path, queries_path, out, argv = inputs
    path = documents_path if target == "documents" else queries_path
    raw = path.read_bytes()
    if case == "missing":
        path.unlink()
    else:
        path.write_bytes({
            "json": raw + b"{\n", "blank_line": raw + b"\n", "bom": b"\xef\xbb\xbf" + raw,
            "crlf": raw.replace(b"\n", b"\r\n"), "last_lf": raw[:-1], "utf8": raw + b"\xff\n",
        }[case])
    assert_failed(argv, out, capsys)
    assert fake_methods[1] == []


@pytest.mark.parametrize("extra", [
    ["--k", "2"], ["--warmup", "-1"], ["--repeats", "0"], ["--k", "x"], ["--help"],
])
def test_invalid_options(inputs, fake_methods, capsys, extra):
    *_, out, argv = inputs
    assert_failed(argv + extra, out, capsys)
    assert fake_methods[1] == []


@pytest.mark.parametrize("missing", ["--documents", "--queries", "--out"])
def test_required_arguments(inputs, fake_methods, capsys, missing):
    *_, out, argv = inputs
    index = argv.index(missing)
    assert_failed(argv[:index] + argv[index + 2:], out, capsys)
    assert fake_methods[1] == []


def test_output_path_is_a_file(inputs, fake_methods, capsys):
    *_, out, argv = inputs
    out.write_bytes(b"keep\n")
    assert run.main(argv) != 0
    captured = capsys.readouterr()
    assert captured.err and not captured.out
    assert out.read_bytes() == b"keep\n"
    assert fake_methods[1] == []


def test_output_directory_creation_failure(inputs, fake_methods, monkeypatch, capsys):
    *_, out, argv = inputs
    original_mkdir = Path.mkdir

    def fail_mkdir(path, *args, **kwargs):
        if path == out:
            raise OSError("mkdir failed deliberately")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    assert run.main(argv) != 0
    captured = capsys.readouterr()
    assert "mkdir failed deliberately" in captured.err
    assert not captured.out
    assert not out.exists()


@pytest.mark.parametrize("nested", [False, True])
def test_existing_output_file_is_preserved(inputs, fake_methods, capsys, nested):
    *_, out, argv = inputs
    parent = out / "nested" if nested else out
    parent.mkdir(parents=True)
    existing = parent / "keep.txt"
    existing.write_bytes(b"keep\n")
    assert run.main(argv) != 0
    captured = capsys.readouterr()
    assert captured.err and not captured.out
    assert existing.read_bytes() == b"keep\n"
    assert [p for p in out.rglob("*") if p.is_file()] == [existing]
    assert fake_methods[1] == []


def test_empty_output_directory_accepted(inputs, fake_methods, capsys):
    *_, out, argv = inputs
    out.mkdir()
    assert run.main(argv) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("failure_call", [1, 4, 7])
def test_search_value_error_fails_main_and_timed_runs(inputs, fake_methods, monkeypatch, capsys, failure_call):
    *_, out, argv = inputs
    calls = 0

    def fail(text, documents, k):
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise ValueError("search failed deliberately")
        return []

    monkeypatch.setattr(fake_methods[0]["a"], "search", fail)
    assert run.main(argv) != 0
    captured = capsys.readouterr()
    assert "search failed deliberately" in captured.err
    assert not captured.out
    assert not out.exists()


@pytest.mark.parametrize("filename", sorted(OUTPUT_NAMES))
def test_write_failure_never_reports_success(inputs, fake_methods, monkeypatch, capsys, filename):
    *_, out, argv = inputs
    original_open = Path.open

    def fail_write(path, mode="r", *args, **kwargs):
        if path == out / filename and mode == "x":
            raise OSError("write failed deliberately")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_write)
    assert run.main(argv) != 0
    captured = capsys.readouterr()
    assert "write failed deliberately" in captured.err
    assert not captured.out
    assert not (out / "manifest.json").exists()


def test_missing_methods_raises_import_error(inputs, monkeypatch):
    *_, out, argv = inputs
    monkeypatch.setitem(sys.modules, "methods", None)
    with pytest.raises(ImportError):
        run.main(argv)
    assert not out.exists()


def test_public_dev_inputs_with_fake_modules(fake_methods, tmp_path, capsys):
    example_dir = Path(run.__file__).resolve().parents[1]
    out = tmp_path / "dev-out"
    assert run.main([
        "--documents", str(example_dir / "data/dev/documents.jsonl"),
        "--queries", str(example_dir / "data/dev/queries.jsonl"),
        "--out", str(out), "--warmup", "0", "--repeats", "1",
    ]) == 0
    assert capsys.readouterr().err == ""
    assert read_json(out / "summary.json")["queries"] == 10
