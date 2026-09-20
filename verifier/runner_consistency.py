"""Execute the real runner and independently validate every output."""
from datetime import datetime
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
if __package__:
    from . import common, reference
else:
    import common
    import reference

OUTPUTS = {"raw-a.jsonl", "raw-b.jsonl", "latency.json", "summary.json", "report.md"}
NOTICE = ("토큰화는 대소문자 정규화 후 공백 분리이며 한국어 형태소 분석을 하지 않았다. "
          "지연은 같은 환경의 반복 측정 중앙값이며 작은 자료의 차이를 일반화하지 않는다.")


def check_latency(value, count):
    common.require(type(value) is dict and set(value) == {"unit", "timer", "warmup", "repeats", "queries", "a", "b"}, "latency: keys")
    for key, expected in {"unit": "ms", "timer": "time.perf_counter", "warmup": 1, "repeats": 5, "queries": count}.items():
        common.compare(value[key], expected, f"latency.{key}")
    for method in ("a", "b"):
        row = value[method]
        common.require(type(row) is dict and set(row) == {"runs_ms", "median_ms"}, f"latency.{method}: keys")
        runs = row["runs_ms"]
        common.require(type(runs) is list and len(runs) == 5, f"latency.{method}: repeats length")
        for number in [*runs, row["median_ms"]]:
            common.require(type(number) in (int, float) and math.isfinite(number) and number >= 0,
                           f"latency.{method}: nonfinite/negative/non-numeric value")
        common.require(all(round(n, 3) == n for n in runs), f"latency.{method}: runs must be rounded to 3 decimal places")
        common.require(row["median_ms"] == statistics.median(runs), f"latency.{method}: median mismatch")


def table_rows(text):
    return [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in text.splitlines() if line.strip().startswith(("| A |", "| B |"))]


def check_report(text, stdout, summary, latency):
    common.require(NOTICE in text, "report: required limitation notice missing")
    rows = table_rows(text)
    common.require(len(rows) == 4, "report: expected 2 A/B tables")
    common.compare(table_rows(stdout), rows, "stdout/report tables")
    for index, method in enumerate(("a", "b")):
        metric, timing = rows[index], rows[index + 2]
        common.require(len(metric) == 4 and len(timing) == 3 and metric[0] == timing[0] == method.upper(),
                       f"report.{method}: table shape")
        common.require(float(metric[1]) == summary[method]["hit_at_3"], f"report.{method}: hit@3")
        common.require(metric[2] == f"{summary[method]['hits']}/{summary['queries']}"
                       and metric[3] == str(summary[method]["no_result_queries"]), f"report.{method}: counts")
        common.require(float(timing[1]) == latency[method]["median_ms"] and timing[2] == "5 (준비 1)",
                       f"report.{method}: timing")


def main():
    private = common.private_dir()
    # Load the private oracle before importing or executing any candidate code.
    cases = common.load_cases()
    reference.verify_hand_cases(cases)
    documents_path = common.EXAMPLE / "data/dev/documents.jsonl"
    queries_path = private / "eval-queries.jsonl"
    documents, queries = common.load_dataset(documents_path, queries_path)
    expected = {method: reference.raw_results(documents, queries, method) for method in ("a", "b")}
    expected_summary = reference.summary(expected, queries)
    for method in ("a", "b"):
        common.compare(common.read_jsonl(private / "eval-expected" / f"raw-{method}.jsonl", private=True),
                       expected[method], f"stored reference/{method}")
    common.compare(common.read_json(private / "eval-expected/summary.json", private=True), expected_summary, "stored reference/summary")
    before = {"documents_sha256": common.sha256(documents_path), "queries_sha256": common.sha256(queries_path),
              "protocol_hash": common.protocol_hash()}
    common.require((common.EXAMPLE / "runner/run.py").is_file(), "runner/run.py missing")
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    # Checks run under W6's process-tree timeout. Do not kill only the runner
    # parent here, leaving descendants outside that lifecycle.
    with tempfile.TemporaryDirectory(prefix="orch-search-check-") as temporary:
        output = Path(temporary) / "result"
        command = [sys.executable, "-B", "-m", "runner.run", "--documents", "data/dev/documents.jsonl",
                   "--queries", str(queries_path), "--out", str(output)]
        execution = subprocess.run(command, cwd=common.EXAMPLE, env=environment, capture_output=True)
        common.require(execution.returncode == 0,
                       f"runner exit {execution.returncode}: {execution.stderr.decode('utf-8', errors='replace')}")
        common.require(output.is_dir() and {p.name for p in output.iterdir()} == OUTPUTS | {"manifest.json"},
                       "runner: missing or extra output files")
        raw = {}
        for method in ("a", "b"):
            raw[method] = common.read_jsonl(output / f"raw-{method}.jsonl")
            # JSON key ordering is not specified; row ordering and exact numeric
            # types/values are. Compare parsed rows, never tolerant float values.
            common.compare(raw[method], expected[method], f"raw-{method} row")
        actual_summary = common.read_json(output / "summary.json")
        common.compare(actual_summary, reference.summary(raw, queries), "summary")
        latency = common.read_json(output / "latency.json")
        check_latency(latency, len(queries))
        report = common.read_text(output / "report.md")
        check_report(report, execution.stdout.decode("utf-8"), actual_summary, latency)
        manifest = common.read_json(output / "manifest.json")
        common.require(type(manifest) is dict and set(manifest) == {
            "protocol_hash", "documents_sha256", "queries_sha256", "k", "argv", "python", "platform",
            "started_at", "ended_at", "outputs_sha256"}, "manifest: keys")
        common.compare(manifest["k"], 3, "manifest.k")
        common.require(type(manifest["argv"]) is list and bool(manifest["argv"])
                       and all(type(s) is str for s in manifest["argv"]), "manifest.argv")
        for key in ("python", "platform"):
            common.require(type(manifest[key]) is str and bool(manifest[key]), f"manifest.{key}")
        start, end = (datetime.fromisoformat(manifest[key]) for key in ("started_at", "ended_at"))
        common.require(start.tzinfo is not None and end.tzinfo is not None and end >= start, "manifest: timestamps")
        for key, value in before.items():
            common.compare(manifest[key], value, f"manifest.{key}")
        common.require(common.sha256(documents_path) == before["documents_sha256"]
                       and common.sha256(queries_path) == before["queries_sha256"]
                       and common.protocol_hash() == before["protocol_hash"], "runner modified input or protocol")
        common.compare(manifest["outputs_sha256"], {name: common.sha256(output / name) for name in OUTPUTS}, "manifest.outputs_sha256")
        hash_run = subprocess.run([sys.executable, "-B", "protocol/validate.py", "--hash"], cwd=common.EXAMPLE,
                                  env=environment, capture_output=True)
        common.require(hash_run.returncode == 0, "protocol hash command failed")
        common.compare(hash_run.stdout.decode("utf-8").strip(), before["protocol_hash"], "protocol hash command")
        # Also compare direct calls of the actual search modules. A runner that
        # fabricates correct rows while ignoring broken modules must fail.
        modules = common.import_methods()
        for method, module in modules.items():
            def call(query, docs, k):
                return common.checked_call(module.search, query, docs, k, method, f"direct/{method}")
            common.compare(reference.raw_results(documents, queries, method, function=call), raw[method], f"direct/{method}")
    print(f"PASS: runner consistency, {len(queries)} queries (raw, direct calls, summary, report, hashes, latency)")


if __name__ == "__main__":
    raise SystemExit(common.entrypoint(main))
