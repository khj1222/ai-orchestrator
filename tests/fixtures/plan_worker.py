"""Offline worker for W10c: writes the deliverables named in its prompt and commits.

Modes: ok, slow (records start/end times), fail-once (writes nothing until the
prompt carries retry feedback), fail-always, rendezvous (W16a: waits until
PLAN_WORKER_PEERS workers have announced themselves in PLAN_WORKER_SYNC, so the
workers provably run at the same time; writes nothing if the peers never come).
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

MARKER = "Task constraints:\n"
FEEDBACK = "Previous attempt's registered checks"
RENDEZVOUS_SECONDS = 20


def rendezvous(name: str) -> bool:
    sync = Path(os.environ["PLAN_WORKER_SYNC"])
    peers = int(os.environ["PLAN_WORKER_PEERS"])
    (sync / f"{name}.started").write_text("", encoding="utf-8")
    deadline = time.monotonic() + RENDEZVOUS_SECONDS
    while time.monotonic() < deadline:
        if len(list(sync.glob("*.started"))) >= peers:
            return True
        time.sleep(0.05)
    return False


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
    met = args.mode == "rendezvous" and rendezvous(Path(constraints["deliverables"][0]).parts[0])
    write = args.mode in ("ok", "slow") or met or (args.mode == "fail-once" and FEEDBACK in prompt)
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
