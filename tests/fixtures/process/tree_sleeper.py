"""3대 프로세스 트리를 만드는 더미. 부모 -> 자식 -> 손자, 손자는 오래 잠든다.

각 세대가 자기 PID 를 한 줄씩 stdout 에 쓴다: "gen0 <pid>", "gen1 <pid>", "gen2 <pid>".
실행: python tree_sleeper.py          (부모)
      python tree_sleeper.py 1        (자식, 내부용)
      python tree_sleeper.py 2        (손자, 내부용)
"""
import os
import subprocess
import sys
import time

DEPTH = 2
SLEEP_SECONDS = 300


def main() -> None:
    generation = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    print(f"gen{generation} {os.getpid()}", flush=True)
    if generation < DEPTH:
        child = subprocess.Popen([sys.executable, __file__, str(generation + 1)],
                                 stdout=sys.stdout, stderr=sys.stderr, stdin=subprocess.DEVNULL)
        child.wait()
    else:
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    main()
