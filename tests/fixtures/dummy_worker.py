"""Offline worker for W7: edits, commits, fails, or waits with a child process."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--mode", default="success")
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    if args.child:
        time.sleep(120)
        return 0
    prompt = sys.stdin.read()
    if not prompt:
        return 4
    if args.mode == "sleep":
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child"],
                                 stdin=subprocess.DEVNULL)
        print(json.dumps({"worker_pid": os.getpid(), "child_pid": child.pid}), flush=True)
        try:
            time.sleep(120)
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
        return 0
    if args.mode != "missing":
        output = args.workspace / "allowed/output.txt"
        output.parent.mkdir(exist_ok=True)
        output.write_text("verified output\n", encoding="utf-8")
    git = ["git", "-C", str(args.workspace), "-c", "user.name=Dummy Worker",
           "-c", "user.email=dummy@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "add", "--all"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "--allow-empty", "-m", "dummy result"], check=True, capture_output=True)
    print(json.dumps({"reported_status": "completed"}), flush=True)
    return 9 if args.mode == "nonzero" else 0


if __name__ == "__main__":
    raise SystemExit(main())
