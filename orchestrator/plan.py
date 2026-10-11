"""W10a plan validation: read-only checks over a whole plan before any task runs.

A plan (schema_version 1) lists tasks. An executable task points to a task
specification and a worker instruction file, both relative to the plan file's
folder. A completed task (status "completed") is recorded but never run.

Rules (04 §9, 09 §3 W10):
  P1 plan fields; no TO_BE_ anywhere          P6 check artifacts reachable from deliverables
  P2 unique IDs; specs agree with the plan    P7 private env stripped from workers
  P3 known dependencies, no cycles            P8 no private strings in instructions/specs
  P4 each spec passes validate_task           P9 instruction files readable and nonempty
  P5 one owner per write path

TO_BE_ is searched as a substring of every key and string value. P5 follows the
string-prefix rule workspace.detect_out_of_scope_changes enforces. In P6 a deliverable ending in
"/" is a directory; any other deliverable is a single file.

P8 only finds verbatim strings (whitespace-normalised) within one instruction
file or one spec key/value. It cannot see an answer restated as an ID mapping,
so it is detection, not a guarantee. Private files contribute string values only
(their keys are schema names). Error messages give the private file name, line
and ordinal, never a string or key from that file.
Nothing here runs Git or any process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

from . import process, verify
from .cli import _id, _read, _relative, _text, validate_task

PROTOCOL_HASH_RE = re.compile(r"[0-9a-f]{64}")
LEAK_MIN_LENGTH = 8
PLAN_KEYS = {"schema_version", "run_id", "plan_version", "base_commit", "protocol_hash",
             "private_env", "leak_sources", "tasks"}
EXECUTABLE_KEYS = {"task_id", "spec", "instructions"}
COMPLETED_KEYS = {"task_id", "status", "completed_commit", "deliverables"}


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _may_share(a: str, b: str) -> bool:
    """Could one changed path be allowed by both prefixes? workspace.detect_out_of_scope_changes uses
    plain string prefixes, so "ex/m" also allows "ex/methods-extra/"; compare caselessly
    to stay conservative on Windows."""
    a, b = _norm(a).casefold(), _norm(b).casefold()
    return a.startswith(b) or b.startswith(a)


def _allowed(prefix: str, path: str) -> bool:
    """The exact rule workspace.detect_out_of_scope_changes enforces (case-sensitive string prefix)."""
    return _norm(path).startswith(_norm(prefix))


def _delivers(deliverable: str, artifact: str) -> bool:
    """A deliverable ending in "/" is a directory and covers paths below it; any other
    deliverable is one file and covers only itself."""
    deliverable, artifact = _norm(deliverable), _norm(artifact)
    return artifact.startswith(deliverable) if deliverable.endswith("/") else artifact == deliverable


def _env_name(value) -> bool:
    return _text(value) and "=" not in value


def _squash(text: str) -> str:
    return " ".join(text.split())


def _fields(value, out: list[str]) -> list[str]:
    """Every JSON key and string value, each kept separate so that matches never
    span two fields."""
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            out.append(key)
            _fields(item, out)
    elif isinstance(value, list):
        for item in value:
            _fields(item, out)
    return out


def _has_marker(value) -> bool:
    return any("TO_BE_" in field for field in _fields(value, []))


def _values(value, out: list[str]) -> list[str]:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, (dict, list)):
        for item in (value.values() if isinstance(value, dict) else value):
            _values(item, out)
    return out


def _load_private(folder: Path, name: str, errors) -> list[tuple[str, str]]:
    """Private string values (LEAK_MIN_LENGTH or longer; keys are schema names such as
    relevant_doc_ids and stay public) with a location made only of the file name, line
    and ordinal, so a diagnostic never repeats private text, keys included."""
    found: list[tuple[str, str]] = []

    def collect(record, where):
        strings = [_squash(s) for s in _values(record, []) if len(s.strip()) >= LEAK_MIN_LENGTH]
        found.extend((s, f"{where} string {n}") for n, s in enumerate(strings, 1))

    try:
        text = (folder / name).read_text(encoding="utf-8-sig")
        if name.endswith(".jsonl"):
            for number, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    collect(json.loads(line), f"{name} line {number}")
        else:
            collect(json.loads(text), name)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        errors.append(f"leak check impossible: cannot read private file {name} ({type(error).__name__})")
    return found


def _cycle(graph: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node):
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if nxt not in state:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in graph:
        if node not in state:
            found = visit(node)
            if found:
                return found
    return None


def _order(ids: list[str], graph: dict[str, list[str]]) -> list[str]:
    done: list[str] = []
    while len(done) < len(ids):
        ready = [i for i in ids if i not in done and all(d in done for d in graph.get(i, []))]
        if not ready:
            return []
        done.append(ready[0])
    return done


def validate_plan(plan_path: Path, config: dict, checks_dir: Path,
                  environ=None) -> tuple[list[str], list[str]]:
    """Return (errors, execution order). The order is empty when errors exist."""
    environ = os.environ if environ is None else environ
    plan_path = Path(plan_path)
    base = plan_path.parent
    try:
        plan = _read(plan_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return [f"plan unreadable: {type(error).__name__}"], []
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"], []
    if not isinstance(config, dict):
        return ["config must be a JSON object"], []
    errors: list[str] = []

    # P1 plan fields
    if _has_marker(plan):
        errors.append("plan: unresolved TO_BE_ value")
    unknown = set(plan) - PLAN_KEYS
    if unknown:
        errors.append(f"plan: unknown fields: {', '.join(sorted(unknown))}")
    if type(plan.get("schema_version")) is not int or plan["schema_version"] != 1:
        errors.append("plan: schema_version must be 1")
    if not _id(plan.get("run_id")):
        errors.append("plan: run_id must be a portable record ID")
    if type(plan.get("plan_version")) is not int or plan["plan_version"] < 1:
        errors.append("plan: plan_version must be a positive integer")
    if not _text(plan.get("base_commit")):
        errors.append("plan: base_commit must be a nonempty string")
    if not (isinstance(plan.get("protocol_hash"), str) and PROTOCOL_HASH_RE.fullmatch(plan["protocol_hash"])):
        errors.append("plan: protocol_hash must be 64 lowercase hex characters")
    private_env = plan.get("private_env", [])
    if not isinstance(private_env, list) or not all(_env_name(v) for v in private_env):
        errors.append("plan: private_env must be a list of environment variable names")
        private_env = []
    leak_sources = plan.get("leak_sources", [])
    if (not isinstance(leak_sources, list) or not all(
            isinstance(s, dict) and set(s) == {"env", "files"} and _env_name(s["env"])
            and isinstance(s["files"], list) and s["files"]
            and all(_text(f) and f == Path(f).name and f not in (".", "..") for f in s["files"])
            for s in leak_sources)):
        errors.append("plan: leak_sources must be a list of {env, files} with plain file names")
        leak_sources = []
    elif private_env and not leak_sources:
        errors.append("plan: private_env is set but leak_sources is empty; P8 would check nothing")
    for source in leak_sources:
        if source["env"].casefold() not in {v.casefold() for v in private_env}:
            errors.append(f"plan: leak_sources env {source['env']} must also be listed in private_env")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errors.append("plan: tasks must be a nonempty list")
        return errors, []

    # P2 entries, specs, instructions (P4, P9 inline)
    entries: dict[str, dict] = {}
    specs: dict[str, dict] = {}
    texts: dict[str, str] = {}  # instruction file -> text, scanned whole by P8
    spec_fields: dict[str, list[str]] = {}  # spec file -> keys and values, scanned one by one
    seen: set[str] = set()
    for index, entry in enumerate(tasks):
        if not isinstance(entry, dict) or not _id(entry.get("task_id")):
            errors.append(f"tasks[{index}]: task_id must be a portable record ID")
            continue
        task_id = entry["task_id"]
        if task_id.casefold() in seen:
            errors.append(f"{task_id}: duplicate task_id")
            continue
        seen.add(task_id.casefold())
        entries[task_id] = entry
        if "status" in entry:
            if entry["status"] != "completed" or set(entry) - COMPLETED_KEYS:
                errors.append(f"{task_id}: a completed task has only task_id, status=completed, "
                              "completed_commit and deliverables")
            if not _text(entry.get("completed_commit")):
                errors.append(f"{task_id}: completed_commit must be a nonempty string")
            done = entry.get("deliverables", [])
            if not isinstance(done, list) or not all(_relative(v) for v in done):
                errors.append(f"{task_id}: deliverables must be a list of relative paths")
            continue
        if set(entry) != EXECUTABLE_KEYS:
            errors.append(f"{task_id}: an executable task has exactly task_id, spec and instructions")
            continue
        for key in ("spec", "instructions"):
            if not _relative(entry[key]):
                errors.append(f"{task_id}: {key} must be a relative path inside the plan folder")
        if not (_relative(entry["spec"]) and _relative(entry["instructions"])):
            continue
        try:
            raw = (base / entry["spec"]).read_text(encoding="utf-8-sig")
            spec = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append(f"{task_id}: spec unreadable: {type(error).__name__}")
            continue
        # decoded keys and values, one by one: \u escapes cannot hide a string and
        # two neighbouring fields cannot form one
        spec_fields[entry["spec"]] = _fields(spec, [])
        if not isinstance(spec, dict):
            errors.append(f"{task_id}: spec must be a JSON object")
            continue
        specs[task_id] = spec
        if _has_marker(spec):
            errors.append(f"{task_id}: spec contains an unresolved TO_BE_ value")
        for key in ("task_id", "run_id", "plan_version", "base_commit", "protocol_hash"):
            expected = task_id if key == "task_id" else plan.get(key)
            value = spec.get(key)
            if type(value) is not type(expected) or value != expected:  # true != 1, 1.0 != 1
                errors.append(f"{task_id}: spec {key} does not match the plan")
        errors.extend(f"{task_id}: {e}" for e in validate_task(
            spec, config, checks_dir, plan_task_ids=frozenset(t.get("task_id") for t in tasks
                                                               if isinstance(t, dict))))
        try:
            instructions = (base / entry["instructions"]).read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as error:
            errors.append(f"{task_id}: instructions unreadable: {type(error).__name__}")
            continue
        if not instructions.strip():
            errors.append(f"{task_id}: instructions are empty")
        if "TO_BE_" in instructions:
            errors.append(f"{task_id}: instructions contain an unresolved TO_BE_ value")
        texts[entry["instructions"]] = instructions

    # P3 dependencies
    graph: dict[str, list[str]] = {t: [] for t in entries}
    for task_id, spec in specs.items():
        deps = spec.get("depends_on")
        if not isinstance(deps, list):
            continue
        for dep in deps:
            if dep == task_id:
                errors.append(f"{task_id}: depends on itself")
            elif dep not in entries:
                errors.append(f"{task_id}: depends on unknown task {dep}")
            else:
                graph[task_id].append(dep)
    cycle = _cycle(graph)
    if cycle:
        errors.append(f"dependency cycle: {' -> '.join(cycle)}")

    # P5 one owner per write path; deliverables inside own paths
    owned = [(t, p) for t, s in specs.items() if isinstance(s.get("allowed_write_paths"), list)
             for p in s["allowed_write_paths"] if _relative(p)]
    for i, (task_a, path_a) in enumerate(owned):
        for task_b, path_b in owned[i + 1:]:
            if task_a != task_b and _may_share(path_a, path_b):
                errors.append(f"write path owned twice: {path_a} ({task_a}) and {path_b} ({task_b})")
    for task_id, spec in specs.items():
        allowed = [p for p in spec.get("allowed_write_paths") or [] if _relative(p)]
        for item in spec.get("deliverables") or []:
            if _relative(item) and not any(_allowed(p, item) for p in allowed):
                errors.append(f"{task_id}: deliverable {item} is outside its allowed_write_paths")

    # P6 command-check artifacts must come from this task or its prerequisites
    def deliverables(task_id):
        if task_id in specs:
            return [d for d in specs[task_id].get("deliverables") or [] if _relative(d)]
        return [d for d in entries[task_id].get("deliverables") or [] if _relative(d)]

    for task_id, spec in specs.items():
        closure, todo = set(), [task_id]
        while todo:
            node = todo.pop()
            if node not in closure:
                closure.add(node)
                todo.extend(graph.get(node, []))
        available = [d for node in closure for d in deliverables(node)]
        for check_id in spec.get("checks") or []:
            try:
                definition = verify.load_check_definition(checks_dir, check_id)
            except verify.CheckDefinitionError:
                continue  # already reported by validate_task
            for artifact in definition.required_artifacts:
                if not any(_delivers(d, artifact) for d in available):
                    errors.append(f"{task_id}: check {check_id} needs {artifact}, which neither this "
                                  "task nor its prerequisites deliver")

    # P7 private environment variables never reach workers
    environment = config.get("environment", {})
    if isinstance(environment, dict):
        stripped = {v.casefold() for v in [*process.DEFAULT_STRIP_ENV,
                                           *(environment.get("strip_env") or [])] if isinstance(v, str)}
        extra = environment.get("extra_env") or {}
        extra_names = {k.casefold() for k in extra} if isinstance(extra, dict) else set()
        # a leak source's folder is private too, whether or not private_env lists it
        for name in dict.fromkeys([*private_env, *(s["env"] for s in leak_sources)]):
            if name.casefold() not in stripped:
                errors.append(f"private_env {name} is not in config environment.strip_env")
            if name.casefold() in extra_names:
                errors.append(f"private_env {name} must not appear in environment.extra_env")

    # P8 no private string copied into instructions or specs
    private: list[tuple[str, str]] = []
    for source in leak_sources:
        value = environ.get(source["env"])
        folder = Path(value) if value else None
        if folder is None or not folder.is_dir():
            errors.append(f"leak check impossible: {source['env']} is not set to a folder")
            continue
        for name in source["files"]:
            private.extend(_load_private(folder, name, errors))
    for label, text in texts.items():
        flat = _squash(text)
        for where in sorted({where for secret, where in private if secret in flat}):
            errors.append(f"{label}: contains a private string from {where}")
    for label, values in spec_fields.items():
        flat = [_squash(v) for v in values]
        for where in sorted({where for secret, where in private if any(secret in v for v in flat)}):
            errors.append(f"{label}: contains a private string from {where}")

    return errors, ([] if errors else _order(list(entries), graph))
