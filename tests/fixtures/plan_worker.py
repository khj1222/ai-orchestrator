"""Offline worker for W10c: writes the deliverables named in its prompt and commits.

Modes: ok, slow (records start/end times), fail-once (writes nothing until the
prompt carries retry feedback), fail-always.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

MARKER = "Task constraints:\n"
FEEDBACK = "Previous attempt's registered checks"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--mode", default="ok")
    args = parser.parse_args()
    prompt = sys.stdin.read()
    constraints, _ = json.JSONDecoder().raw_decode(prompt[prompt.index(MARKER) + len(MARKER):])
    start = time.time()
    if args.mode == "slow":
        time.sleep(1.5)
    write = args.mode in ("ok", "slow") or (args.mode == "fail-once" and FEEDBACK in prompt)
    if write:
        for item in constraints["deliverables"]:
            path = args.workspace / (item + "out.txt" if item.endswith("/") else item)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"ok\nstart={start}\nend={time.time()}\n", encoding="utf-8")
    git = ["git", "-C", str(args.workspace), "-c", "user.name=Plan Worker",
           "-c", "user.email=plan@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "add", "--all"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "--allow-empty", "-m", "plan worker result"], check=True, capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
