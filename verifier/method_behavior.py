"""Run the candidate against private, manually calculated cases."""
import sys

sys.dont_write_bytecode = True
if __package__:
    from . import common
else:
    import common


def main():
    cases = common.load_cases()
    modules = common.import_methods()
    failures = 0
    for case in cases:
        for method, module in modules.items():
            label = f"{case['case_id']}/{method}"
            try:
                result = common.checked_call(module.search, case["query"], case["documents"], case["k"], method, label)
                pairs = [[row["doc_id"], row["score"]] for row in result]
                common.compare(pairs, case[f"expected_{method}"], label)
            except (Exception, SystemExit) as error:
                print(f"FAIL {label}: {type(error).__name__}: {error}")
                failures += 1
    common.require(not failures, f"{failures} behavior comparisons failed")
    print(f"PASS: method behavior, {len(cases)} cases x 2 methods")


if __name__ == "__main__":
    raise SystemExit(common.entrypoint(main))
