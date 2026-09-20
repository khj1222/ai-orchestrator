"""Shared validation utilities; no worker implementation or private answers."""
from __future__ import annotations

import copy
import hashlib
import importlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import unicodedata
from contextlib import redirect_stdout, redirect_stderr

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "search-ab"
sys.dont_write_bytecode = True


class EnvironmentProblem(Exception):
    """Missing verifier configuration or private input."""


class Failure(Exception):
    """Invalid data or a candidate that violates the contract."""


def require(condition, message):
    if not condition:
        raise Failure(message)


def private_dir():
    value = os.environ.get("ORCH_VERIFIER_PRIVATE")
    if not value:
        raise EnvironmentProblem("ORCH_VERIFIER_PRIVATE is not set")
    path = Path(value).resolve()
    if not path.is_dir():
        raise EnvironmentProblem("ORCH_VERIFIER_PRIVATE directory is missing")
    return path


def read_text(path, *, private=False):
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError as error:
        cls = EnvironmentProblem if private else Failure
        raise cls(f"missing file: {Path(path).name}") from error
    require(not data.startswith(b"\xef\xbb\xbf"), f"{Path(path).name}: BOM")
    require(b"\r" not in data, f"{Path(path).name}: expected LF")
    return data.decode("utf-8")


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise Failure(f"nonfinite JSON value: {value}")


def decode(text):
    return json.loads(text, object_pairs_hook=_object, parse_constant=_constant)


def read_json(path, *, private=False):
    return decode(read_text(path, private=private))


def read_jsonl(path, *, private=False):
    text = read_text(path, private=private)
    require(bool(text) and text.endswith("\n"), f"{Path(path).name}: empty or missing final LF")
    lines = text[:-1].split("\n")
    require(all(line.strip() for line in lines), f"{Path(path).name}: blank line")
    return [decode(line) for line in lines]


def check_text(text, limit, label):
    require(type(text) is str and bool(text.strip()), f"{label}: empty/non-string text")
    require(len(text) <= limit and "\n" not in text and "\r" not in text,
            f"{label}: length or line break")
    require(unicodedata.is_normalized("NFC", text), f"{label}: expected NFC")


def load_dataset(documents_path, queries_path, *, private_queries=True):
    documents = read_jsonl(documents_path)
    queries = read_jsonl(queries_path, private=private_queries)
    ids = []
    for row in documents:
        require(type(row) is dict and set(row) == {"doc_id", "text"}, "document keys")
        identifier = row["doc_id"]
        require(type(identifier) is str and re.fullmatch(r"d[0-9]{3}", identifier), "document ID")
        check_text(row["text"], 300, identifier)
        ids.append(identifier)
    require(ids == sorted(set(ids)), "document IDs must be unique and sorted")
    qids = []
    for row in queries:
        require(type(row) is dict and set(row) == {"query_id", "text", "relevant_doc_ids"}, "query keys")
        identifier = row["query_id"]
        require(type(identifier) is str and re.fullmatch(r"q[0-9]{2}", identifier), "query ID")
        check_text(row["text"], 100, identifier)
        relevant = row["relevant_doc_ids"]
        require(type(relevant) is list and bool(relevant) and all(type(x) is str for x in relevant),
                f"{identifier}: relevant IDs")
        require(relevant == sorted(set(relevant)) and set(relevant) <= set(ids),
                f"{identifier}: invalid relevant IDs")
        qids.append(identifier)
    require(qids == sorted(set(qids)), "query IDs must be unique and sorted")
    return documents, queries


def compare(actual, expected, label):
    """Compare recursively, including numeric types and list order."""
    require(type(actual) is type(expected),
            f"{label}: type {type(actual).__name__}, expected {type(expected).__name__}")
    if type(expected) is dict:
        require(set(actual) == set(expected), f"{label}: keys {sorted(actual)}, expected {sorted(expected)}")
        for key in expected:
            compare(actual[key], expected[key], f"{label}.{key}")
    elif type(expected) is list:
        require(len(actual) == len(expected), f"{label}: length {len(actual)}, expected {len(expected)}")
        for rank, (a, e) in enumerate(zip(actual, expected), 1):
            compare(a, e, f"{label}[{rank}]")
    else:
        require(actual == expected, f"{label}: {actual!r}, expected {expected!r}")


def check_results(result, documents, k, method, label):
    require(type(result) is list and len(result) == min(k, len(documents)), f"{label}: result length/type")
    ids = {d["doc_id"] for d in documents}
    seen = set()
    score_type = int if method == "a" else float
    for rank, row in enumerate(result, 1):
        require(type(row) is dict and set(row) == {"doc_id", "score"}, f"{label}[{rank}]: result keys")
        identifier, score = row["doc_id"], row["score"]
        require(type(identifier) is str and identifier in ids and identifier not in seen,
                f"{label}[{rank}]: invalid/duplicate doc_id")
        require(type(score) is score_type and math.isfinite(score) and score >= 0,
                f"{label}[{rank}]: score type/range")
        require(method == "a" or score <= 1, f"{label}[{rank}]: B score above 1")
        seen.add(identifier)
    require(result == sorted(result, key=lambda x: (-x["score"], x["doc_id"])), f"{label}: sort order")


def load_cases():
    cases = read_json(private_dir() / "behavior-cases.json", private=True)
    require(type(cases) is list and bool(cases), "behavior cases must be a nonempty list")
    seen = set()
    for case in cases:
        require(type(case) is dict and set(case) == {"case_id", "query", "documents", "k", "expected_a", "expected_b"},
                "behavior case keys")
        identifier = case["case_id"]
        require(type(identifier) is str and identifier and identifier not in seen, "case ID missing/duplicate")
        seen.add(identifier)
        require(type(case["query"]) is str and type(case["k"]) is int and case["k"] >= 0,
                f"{identifier}: query/k")
        docs = case["documents"]
        require(type(docs) is list, f"{identifier}: documents")
        for doc in docs:
            require(type(doc) is dict and set(doc) == {"doc_id", "text"}
                    and all(type(v) is str for v in doc.values()), f"{identifier}: document")
        require(len({d['doc_id'] for d in docs}) == len(docs), f"{identifier}: duplicate document ID")
        for method in ("a", "b"):
            pairs = case[f"expected_{method}"]
            require(type(pairs) is list and all(type(p) is list and len(p) == 2 for p in pairs),
                    f"{identifier}: expected pairs")
            check_results([{"doc_id": p[0], "score": p[1]} for p in pairs], docs, case["k"], method, identifier)
    return cases


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def protocol_hash():
    folder = EXAMPLE / "protocol"
    require((folder / "validate.py").is_file(), "protocol/validate.py missing")
    digest = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\n")
        digest.update(path.read_bytes() + b"\n")
    return digest.hexdigest()


def import_methods():
    """Observe Python-level import I/O; this is detection, not an OS sandbox."""
    for name in ("__init__.py", "a.py", "b.py"):
        require((EXAMPLE / "methods" / name).is_file(), f"methods/{name} missing")
    sys.path.insert(0, str(EXAMPLE))
    active = [True]

    def audit(event, args):
        if not active[0]:
            return
        if event in {"subprocess.Popen", "os.system", "socket.connect", "socket.bind", "os.remove", "os.rename", "os.mkdir"}:
            raise Failure(f"import side effect: {event}")
        if event == "open":
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
            require(not writing, "import side effect: file write")
            frame = sys._getframe(1)
            loader = False
            while frame:
                filename = frame.f_code.co_filename
                if "importlib" in filename and frame.f_code.co_name in {"get_data", "get_code"}:
                    loader = True
                    break
                if str(EXAMPLE) in filename:
                    break
                frame = frame.f_back
            require(loader, "import side effect: file read")

    sys.addaudithook(audit)
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            modules = {name: importlib.import_module(f"methods.{name}") for name in ("a", "b")}
    finally:
        active[0] = False
    require(not stdout.getvalue() and not stderr.getvalue(), "import side effect: output")
    return modules


def checked_call(function, query, documents, k, method, label):
    argument = copy.deepcopy(documents)
    result = function(query, argument, k)
    compare(argument, documents, f"{label}: input mutation")
    check_results(result, documents, k, method, label)
    return result


def entrypoint(function):
    try:
        function()
    except EnvironmentProblem as error:
        print(f"ENVIRONMENT: {error}", file=sys.stderr)
        return 2
    except (Exception, SystemExit) as error:
        print(f"FAIL: {type(error).__name__}: {error}")
        return 1
    return 0
