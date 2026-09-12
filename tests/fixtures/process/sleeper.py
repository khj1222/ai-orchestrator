"""단순 장기 실행 더미. 인수로 초를 받으며 기본 300초 잠든다."""
import sys
import time

time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 300)
