# pilot-001-mixed (W14)

계획 v1(`runs/pilot-001/`)의 사본이다. 바꾼 것: `run_id`(계획·명세), 지시문 첫 줄의 run 이름, `experiment-runner`의 route(`codex-default` → `claude-implement`), 그리고 평가 집계 공개 선언 `publication`(W13b, 기준 검사 `runner-consistency-v1`). 기준 commit은 계획대로 `04935d6`이다. W14는 `orch run-plan --parallel 2`로 search-module(Codex)과 experiment-runner(Claude)를 동시에 실행한다.
