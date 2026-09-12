"""출력 더미. stdout 과 stderr 에 서로 다른 내용을 쓰고, 환경 변수 존재 여부와 stdin 내용을 stdout 에 찍는다.

인수로 받은 이름마다 "ENV <이름> set|unset" 을, PYTHONUTF8 값을 "PYTHONUTF8 <값>" 으로 쓴다.
stdin 을 끝까지 읽어 "STDIN <바이트수> <내용>" 을 쓴다. stdin 이 닫혀 있으면 즉시 EOF 다.
stderr 에는 "ERR line" 두 줄을 쓴다.
"""
import os
import sys


def main() -> None:
    out = sys.stdout.buffer
    for name in sys.argv[1:]:
        out.write(f"ENV {name} {'set' if name in os.environ else 'unset'}\n".encode())
    out.write(f"PYTHONUTF8 {os.environ.get('PYTHONUTF8', '<absent>')}\n".encode())
    data = sys.stdin.buffer.read()
    out.write(b"STDIN " + str(len(data)).encode() + b" " + data + b"\n")
    out.flush()
    sys.stderr.buffer.write(b"ERR line 1\nERR line 2\n")
    sys.stderr.buffer.flush()


if __name__ == "__main__":
    main()
