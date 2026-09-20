"""Self-check of the protocol folder and the public dev data (protocol v1).

This is not the verifier (W9). It checks that the specification has no unresolved
markers and that data/dev files follow data-format.md. Standard library only.

    python protocol/validate.py          # run checks, exit 1 on any error
    python protocol/validate.py --hash   # print protocol_hash only
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

PROTOCOL_DIR = Path(__file__).resolve().parent
EXAMPLE_DIR = PROTOCOL_DIR.parent
DEV_DIR = EXAMPLE_DIR / "data" / "dev"

UNRESOLVED_MARKERS = ("TO_BE_", "TBD", "추후 결정", "나중에 정한다")
DOC_ID = re.compile(r"^d[0-9]{3}$")
QUERY_ID = re.compile(r"^q[0-9]{2}$")
DEV_DOCUMENTS = 30
DEV_QUERIES = 10
MAX_DOC_CHARS = 300
MAX_QUERY_CHARS = 100


def protocol_hash() -> str:
    digest = hashlib.sha256()
    files = sorted(p for p in PROTOCOL_DIR.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts)
    for path in files:
        digest.update(path.relative_to(PROTOCOL_DIR).as_posix().encode("utf-8"))
        digest.update(b"\n")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def _read_text_file(path: Path, errors: list[str]) -> str | None:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        errors.append(f"{path.name}: UTF-8 BOM present")
    if b"\r" in raw:
        errors.append(f"{path.name}: CR byte present (line endings must be LF)")
    if raw and not raw.endswith(b"\n"):
        errors.append(f"{path.name}: last line must end with LF")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        errors.append(f"{path.name}: not UTF-8 ({error.reason})")
        return None


def _read_jsonl(path: Path, errors: list[str]) -> list[dict]:
    text = _read_text_file(path, errors)
    if text is None:
        return []
    rows: list[dict] = []
    for number, line in enumerate(text.split("\n")[:-1], start=1):
        if not line.strip():
            errors.append(f"{path.name}:{number}: blank line")
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            errors.append(f"{path.name}:{number}: invalid JSON ({error.msg})")
            continue
        if not isinstance(row, dict):
            errors.append(f"{path.name}:{number}: not a JSON object")
            continue
        rows.append(row)
    return rows


def _check_string(value: object, where: str, limit: int, errors: list[str]) -> None:
    if not isinstance(value, str):
        errors.append(f"{where}: text must be a string")
        return
    if not value.strip():
        errors.append(f"{where}: text is empty")
    if "\n" in value or "\r" in value:
        errors.append(f"{where}: text contains a line break")
    if len(value) > limit:
        errors.append(f"{where}: text longer than {limit} characters ({len(value)})")
    if not unicodedata.is_normalized("NFC", value):
        errors.append(f"{where}: text is not NFC-normalized")


def check_protocol_docs(errors: list[str]) -> None:
    for path in sorted(PROTOCOL_DIR.glob("*.md")):
        text = _read_text_file(path, errors)
        if text is None:
            continue
        for marker in UNRESOLVED_MARKERS:
            if marker in text:
                errors.append(f"{path.name}: unresolved marker {marker!r}")


def check_documents(errors: list[str]) -> dict[str, str]:
    path = DEV_DIR / "documents.jsonl"
    if not path.is_file():
        errors.append("data/dev/documents.jsonl missing")
        return {}
    rows = _read_jsonl(path, errors)
    if len(rows) != DEV_DOCUMENTS:
        errors.append(f"documents.jsonl: expected {DEV_DOCUMENTS} rows, found {len(rows)}")
    seen: dict[str, str] = {}
    previous = ""
    for index, row in enumerate(rows, start=1):
        where = f"documents.jsonl row {index}"
        if set(row) != {"doc_id", "text"}:
            errors.append(f"{where}: keys must be exactly doc_id, text (got {sorted(row)})")
        doc_id = row.get("doc_id")
        if not isinstance(doc_id, str) or not DOC_ID.match(doc_id):
            errors.append(f"{where}: doc_id must match ^d[0-9]{{3}}$")
            continue
        if doc_id in seen:
            errors.append(f"{where}: duplicate doc_id {doc_id}")
        if doc_id <= previous:
            errors.append(f"{where}: doc_id {doc_id} not in ascending order")
        previous = doc_id
        _check_string(row.get("text"), where, MAX_DOC_CHARS, errors)
        seen[doc_id] = row.get("text") if isinstance(row.get("text"), str) else ""
    return seen


def check_queries(documents: dict[str, str], errors: list[str]) -> list[dict]:
    path = DEV_DIR / "queries.jsonl"
    if not path.is_file():
        errors.append("data/dev/queries.jsonl missing")
        return []
    rows = _read_jsonl(path, errors)
    if len(rows) != DEV_QUERIES:
        errors.append(f"queries.jsonl: expected {DEV_QUERIES} rows, found {len(rows)}")
    seen: set[str] = set()
    previous = ""
    for index, row in enumerate(rows, start=1):
        where = f"queries.jsonl row {index}"
        if set(row) != {"query_id", "text", "relevant_doc_ids"}:
            errors.append(f"{where}: keys must be exactly query_id, text, relevant_doc_ids (got {sorted(row)})")
        query_id = row.get("query_id")
        if not isinstance(query_id, str) or not QUERY_ID.match(query_id):
            errors.append(f"{where}: query_id must match ^q[0-9]{{2}}$")
            continue
        if query_id in seen:
            errors.append(f"{where}: duplicate query_id {query_id}")
        if query_id <= previous:
            errors.append(f"{where}: query_id {query_id} not in ascending order")
        previous = query_id
        seen.add(query_id)
        _check_string(row.get("text"), where, MAX_QUERY_CHARS, errors)
        relevant = row.get("relevant_doc_ids")
        if not isinstance(relevant, list) or not relevant:
            errors.append(f"{where}: relevant_doc_ids must be a non-empty list")
            continue
        if any(not isinstance(d, str) for d in relevant):
            errors.append(f"{where}: relevant_doc_ids must contain strings")
            continue
        if len(set(relevant)) != len(relevant):
            errors.append(f"{where}: relevant_doc_ids has duplicates")
        if relevant != sorted(relevant):
            errors.append(f"{where}: relevant_doc_ids not in ascending order")
        for doc_id in relevant:
            if doc_id not in documents:
                errors.append(f"{where}: relevant doc {doc_id} does not exist")
        text = row.get("text")
        if isinstance(text, str) and any(text.strip() == doc.strip() for doc in documents.values()):
            errors.append(f"{where}: query text is identical to a document text")
    return rows


def overlap_table(documents: dict[str, str], queries: list[dict]) -> str:
    """Whitespace-token overlap per query, printed for the human review (not a pass/fail rule)."""
    lines = ["query_id  max|Q&D|/|Q|  best_doc  relevant", "--------  ------------  --------  --------"]
    for row in queries:
        text = row.get("text")
        if not isinstance(text, str):
            continue
        q_tokens = set(text.lower().split())
        best_id, best = "-", 0.0
        for doc_id, doc in documents.items():
            share = len(q_tokens & set(doc.lower().split())) / len(q_tokens) if q_tokens else 0.0
            if share > best or (share == best and best_id == "-"):
                best_id, best = doc_id, share
        lines.append(f"{row.get('query_id'):8}  {best:12.2f}  {best_id:8}  {','.join(row.get('relevant_doc_ids', []))}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if argv == ["--hash"]:
        print(protocol_hash())
        return 0
    if argv:
        print(__doc__, file=sys.stderr)
        return 2
    errors: list[str] = []
    check_protocol_docs(errors)
    documents = check_documents(errors)
    queries = check_queries(documents, errors)
    for error in errors:
        print("ERROR:", error)
    if errors:
        print(f"{len(errors)} error(s)")
        return 1
    print(f"OK: protocol docs clean, {len(documents)} documents, {len(queries)} queries")
    print("protocol_hash:", protocol_hash())
    print()
    print("Token overlap (whitespace tokens, for the human rewrite check):")
    print(overlap_table(documents, queries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
