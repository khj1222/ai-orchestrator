"""Public interface checks. No private directory is required."""
import copy
import inspect
import sys

sys.dont_write_bytecode = True
if __package__:
    from . import common
else:
    import common


def check_method(method, module):
    function = getattr(module, "search", None)
    common.require(callable(function), f"{method}: missing search")
    signature = inspect.signature(function)
    try:
        signature.bind("x", [], 0)
        signature.bind(query="x", documents=[], k=0)
    except TypeError as error:
        raise common.Failure(f"{method}: search(query, documents, k) signature") from error
    docs = [{"doc_id": "d003", "text": "blue red", "extra": {"x": [1]}},
            {"doc_id": "d001", "text": "red"}, {"doc_id": "d002", "text": ""}]
    checks = [("ordinary", "red", docs, 2), ("all", "blue", docs, 8),
              ("empty-query", "", docs, 3), ("space-query", " \t\u2003", docs, 2),
              ("zero-k", "x", docs, 0), ("empty-documents", "x", [], 3),
              ("empty-text", "x", [{"doc_id": "", "text": ""}], 1)]
    for name, query, documents, k in checks:
        label = f"{method}/{name}"
        result = common.checked_call(function, query, documents, k, method, label)
        snapshot = copy.deepcopy(result)
        second = common.checked_call(function, query, documents, k, method, label)
        common.compare(second, snapshot, f"{label}: repeat")
        if not query.strip():
            zero = 0 if method == "a" else 0.0
            expected = [{"doc_id": d["doc_id"], "score": zero} for d in sorted(documents, key=lambda d: d["doc_id"])[:k]]
            common.compare(result, expected, label)
    bad_inputs = [("negative-k", "x", docs, -1), ("bool-k", "x", docs, True),
                  ("float-k", "x", docs, 1.0), ("string-k", "x", docs, "1"),
                  ("query-type", None, docs, 1), ("documents-type", "x", (), 1),
                  ("document-type", "x", [None], 1), ("missing-id", "x", [{"text": "x"}], 1),
                  ("missing-text", "x", [{"doc_id": "d001"}], 1),
                  ("id-type", "x", [{"doc_id": 1, "text": "x"}], 1),
                  ("text-type", "x", [{"doc_id": "d001", "text": 1}], 1),
                  ("duplicate", "x", [{"doc_id": "d001", "text": "x"}] * 2, 1),
                  ("validate-before-k-zero", "x", [None], 0),
                  ("validate-before-empty-docs", None, [], 0)]
    for name, query, documents, k in bad_inputs:
        try:
            function(query, documents, k)
        except ValueError:
            continue
        except Exception as error:
            raise common.Failure(f"{method}/{name}: expected ValueError, got {type(error).__name__}") from error
        raise common.Failure(f"{method}/{name}: missing ValueError")


def main():
    modules = common.import_methods()
    for method, module in modules.items():
        check_method(method, module)
    print("PASS: interface contract, A and B (import, signature, types, boundaries, errors, mutation, repeat)")


if __name__ == "__main__":
    raise SystemExit(common.entrypoint(main))
