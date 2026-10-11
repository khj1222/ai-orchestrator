"""Run the two search methods and record their unmodified results."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import statistics
import sys
import time
import unicodedata


REPORT_NOTE = (
    "토큰화는 대소문자 정규화 후 공백 분리이며 한국어 형태소 분석을 하지 않았다. "
    "지연은 같은 환경의 반복 측정 중앙값이며 작은 자료의 차이를 일반화하지 않는다."
)


def _read_rows(path: Path, id_key: str, limit: int) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
        raise ValueError(f"{path}: expected UTF-8 without BOM and LF line endings")
    if not raw or not raw.endswith(b"\n"):
        raise ValueError(f"{path}: input must be nonempty and end with LF")
    text = raw.decode("utf-8")
    keys = {id_key, "text"}
    if id_key == "query_id":
        keys.add("relevant_doc_ids")
    pattern = r"d[0-9]{3}" if id_key == "doc_id" else r"q[0-9]{2}"
    rows = []
    seen = set()
    previous = ""
    for number, line in enumerate(text.split("\n")[:-1], 1):
        where = f"{path}:{number}"
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{where}: invalid JSON: {error.msg}") from error
        if not isinstance(row, dict) or set(row) != keys:
            raise ValueError(f"{where}: keys must be exactly {sorted(keys)}")
        identifier = row[id_key]
        if not isinstance(identifier, str) or not re.fullmatch(pattern, identifier):
            raise ValueError(f"{where}: invalid {id_key}")
        if identifier in seen:
            raise ValueError(f"{where}: duplicate {id_key}: {identifier}")
        if identifier <= previous:
            raise ValueError(f"{where}: {id_key} must be in ascending order")
        seen.add(identifier)
        previous = identifier
        value = row["text"]
        if (
            not isinstance(value, str)
            or not value.strip()
            or "\n" in value
            or "\r" in value
            or len(value) > limit
            or not unicodedata.is_normalized("NFC", value)
        ):
            raise ValueError(f"{where}: invalid text (nonempty NFC, no line breaks, max {limit})")
        rows.append(row)
    return rows, hashlib.sha256(raw).hexdigest()


def _read_inputs(documents_path: Path, queries_path: Path):
    documents, documents_hash = _read_rows(documents_path, "doc_id", 300)
    queries, queries_hash = _read_rows(queries_path, "query_id", 100)
    doc_ids = {document["doc_id"] for document in documents}
    for query in queries:
        relevant = query["relevant_doc_ids"]
        if (
            not isinstance(relevant, list)
            or not relevant
            or any(not isinstance(doc_id, str) for doc_id in relevant)
            or len(set(relevant)) != len(relevant)
            or relevant != sorted(relevant)
            or any(doc_id not in doc_ids for doc_id in relevant)
        ):
            raise ValueError(f"{queries_path}: {query['query_id']}: invalid relevant_doc_ids")
    return documents, queries, documents_hash, queries_hash


def _protocol_hash() -> str:
    protocol_dir = Path(__file__).resolve().parents[1] / "protocol"
    digest = hashlib.sha256()
    files = sorted(
        path for path in protocol_dir.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    for path in files:
        digest.update(path.relative_to(protocol_dir).as_posix().encode("utf-8"))
        digest.update(b"\n")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def _measure(search, texts: list[str], documents: list[dict], k: int,
             warmup: int, repeats: int) -> dict:
    for _ in range(warmup):
        for text in texts:
            search(text, documents, k)
    runs = []
    for _ in range(repeats):
        started = time.perf_counter()
        for text in texts:
            search(text, documents, k)
        ended = time.perf_counter()
        runs.append(round((ended - started) * 1000, 3))
    return {"runs_ms": runs, "median_ms": statistics.median(runs)}


def _summarize(rows: list[dict], queries: list[dict]) -> dict:
    hits = sum(
        any(result["rank"] <= 3 and result["doc_id"] in query["relevant_doc_ids"]
            for result in row["results"])
        for row, query in zip(rows, queries)
    )
    return {
        "hit_at_3": hits / len(queries),
        "hits": hits,
        "no_result_queries": sum(not row["results"] for row in rows),
    }


def _tables(summary: dict, latency: dict) -> str:
    count = summary["queries"]
    lines = ["| 방법 | hit@3 | hits/queries | 결과 없는 질의 |", "|---|---|---|---|"]
    for method in ("a", "b"):
        values = summary[method]
        lines.append(
            f"| {method.upper()} | {values['hit_at_3']} | {values['hits']}/{count} | "
            f"{values['no_result_queries']} |"
        )
    lines.extend([
        "", f"| 방법 | 지연 중앙값 (ms, 질의 {count}개 1회 실행) | 측정 횟수 |",
        "|---|---|---|",
    ])
    for method in ("a", "b"):
        lines.append(
            f"| {method.upper()} | {latency[method]['median_ms']} | "
            f"{latency['repeats']} (준비 {latency['warmup']}) |"
        )
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str) -> None:
    # Exclusive creation also protects against a file appearing after preflight.
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False) + "\n"


def _execute(args: argparse.Namespace, argv: list[str]) -> str:
    started_at = datetime.now(timezone.utc).isoformat()
    if args.out.exists():
        if not args.out.is_dir() or any(
            path.is_file() or path.is_symlink() for path in args.out.rglob("*")
        ):
            raise ValueError(f"{args.out}: output directory already contains files or is not a directory")
    documents, queries, documents_hash, queries_hash = _read_inputs(
        args.documents, args.queries
    )
    from methods import a, b

    protocol_hash = _protocol_hash()
    methods = {"a": a.search, "b": b.search}
    raw = {"a": [], "b": []}
    # Save a separate first main run; timed calls never supply recorded results.
    for query in queries:
        for method, search in methods.items():
            results = search(query["text"], documents, args.k)
            raw[method].append({
                "method": method, "query_id": query["query_id"], "k": args.k,
                "results": [dict(rank=rank, **result)
                            for rank, result in enumerate(results, 1)],
            })
    texts = [query["text"] for query in queries]
    latency = {
        "unit": "ms", "timer": "time.perf_counter", "warmup": args.warmup,
        "repeats": args.repeats, "queries": len(queries),
    }
    summary = {"k": args.k, "queries": len(queries)}
    for method, search in methods.items():
        latency[method] = _measure(search, texts, documents, args.k, args.warmup, args.repeats)
        summary[method] = _summarize(raw[method], queries)
    tables = _tables(summary, latency)
    outputs = {
        "raw-a.jsonl": "".join(_json_text(row) for row in raw["a"]),
        "raw-b.jsonl": "".join(_json_text(row) for row in raw["b"]),
        "latency.json": _json_text(latency),
        "summary.json": _json_text(summary),
        "report.md": tables + "\n" + REPORT_NOTE + "\n",
    }
    args.out.mkdir(parents=True, exist_ok=True)
    for name, content in outputs.items():
        _write(args.out / name, content)
    manifest = {
        "protocol_hash": protocol_hash,
        "documents_sha256": documents_hash, "queries_sha256": queries_hash,
        "k": args.k, "argv": ["python", "-m", "runner.run", *argv],
        "python": platform.python_version(), "platform": platform.platform(),
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "outputs_sha256": {
            name: hashlib.sha256((args.out / name).read_bytes()).hexdigest()
            for name in outputs
        },
    }
    _write(args.out / "manifest.json", _json_text(manifest))
    return tables


def main(argv: list[str]) -> int:
    # Protocol v1 reserves stdout for tables and exit code zero for six outputs.
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--documents", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=5)
    try:
        args = parser.parse_args(argv)
        if args.k < 3 or args.warmup < 0 or args.repeats < 1:
            parser.error("require --k >= 3, --warmup >= 0, --repeats >= 1")
    except SystemExit as error:
        return int(error.code)
    try:
        tables = _execute(args, argv)
    except (ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(tables, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
