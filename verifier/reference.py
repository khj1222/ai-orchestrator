"""Independent set-based reference. Hand-authored cases are its oracle."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
if __package__:
    from . import common
else:
    import common


def search(query, documents, k, method):
    if method not in ("a", "b"):
        raise ValueError("method must be a or b")
    if not isinstance(query, str) or not isinstance(k, int) or isinstance(k, bool) or k < 0:
        raise ValueError("query/k")
    if not isinstance(documents, list):
        raise ValueError("documents")
    ids = set()
    for document in documents:
        if not isinstance(document, dict) or not isinstance(document.get("doc_id"), str) or not isinstance(document.get("text"), str):
            raise ValueError("document fields")
        if document["doc_id"] in ids:
            raise ValueError("duplicate document ID")
        ids.add(document["doc_id"])
    query_terms = frozenset(query.lower().split())
    scored = []
    for document in documents:
        terms = frozenset(document["text"].lower().split())
        shared = len(query_terms.intersection(terms))
        total = len(query_terms.union(terms))
        score = shared if method == "a" else (shared / total if total else 0.0)
        scored.append({"doc_id": document["doc_id"], "score": score})
    return sorted(scored, key=lambda row: (-row["score"], row["doc_id"]))[:k]


def raw_results(documents, queries, method, k=3, function=None):
    rows = []
    for query in queries:
        result = function(query["text"], documents, k) if function else search(query["text"], documents, k, method)
        rows.append({"method": method, "query_id": query["query_id"], "k": k,
                     "results": [{"rank": rank, **item} for rank, item in enumerate(result, 1)]})
    return rows


def summary(raw, queries, k=3):
    common.require(bool(queries), "empty queries")
    result = {"k": k, "queries": len(queries)}
    for method in ("a", "b"):
        rows = raw[method]
        common.require(len(rows) == len(queries), f"{method}: missing raw queries")
        hits = empty = 0
        for row, query in zip(rows, queries):
            common.require(row["query_id"] == query["query_id"], "raw query order")
            hits += int(any(item["rank"] <= 3 and item["doc_id"] in query["relevant_doc_ids"] for item in row["results"]))
            empty += int(not row["results"])
        result[method] = {"hit_at_3": hits / len(queries), "hits": hits, "no_result_queries": empty}
    return result


def verify_hand_cases(cases):
    for case in cases:
        for method in ("a", "b"):
            actual = search(case["query"], case["documents"], case["k"], method)
            pairs = [[r["doc_id"], r["score"]] for r in actual]
            common.compare(pairs, case[f"expected_{method}"], f"{case['case_id']}/{method}")
    print(f"PASS: reference matches {len(cases)} hand-authored cases (A and B)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-expected", action="store_true", help="regenerate private eval-expected after hand-case validation")
    args = parser.parse_args()
    cases = common.load_cases()
    verify_hand_cases(cases)
    folder = common.private_dir()
    documents, queries = common.load_dataset(common.EXAMPLE / "data/dev/documents.jsonl", folder / "eval-queries.jsonl")
    raw = {method: raw_results(documents, queries, method) for method in ("a", "b")}
    values = {"raw-a.jsonl": raw["a"], "raw-b.jsonl": raw["b"], "summary.json": summary(raw, queries)}
    output = folder / "eval-expected"
    if args.write_expected:
        output.mkdir(exist_ok=True)
        for name, value in values.items():
            text = ("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in value) if name.endswith("jsonl")
                    else json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            (output / name).write_bytes(text.encode("utf-8"))
        print(f"WROTE: expected results for {len(queries)} evaluation queries")
    else:
        for name, value in values.items():
            reader = common.read_jsonl if name.endswith("jsonl") else common.read_json
            common.compare(reader(output / name, private=True), value, name)
        print(f"PASS: stored expected results match {len(queries)} evaluation queries")


if __name__ == "__main__":
    raise SystemExit(common.entrypoint(main))
