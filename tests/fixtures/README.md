# 테스트 fixture

실제 CLI 실행에서 받은 출력 샘플이다. 어댑터 테스트는 이 파일들로만 검증하며 실제 CLI를 호출하지 않는다.
채취일 2026-09-11, 출처는 설계 문서 폴더의 `08-g0-log.md`. 예외: `codex/exec-collab-wait-*.jsonl`은 2026-10-25(W18b E4)에 받았고, `codex/exec-collab-synthetic.jsonl`은 손으로 만들었다.

| 파일 | 내용 |
|---|---|
| `codex/exec-ok.jsonl` | `codex exec --json` 정상 실행. thread.started / turn.started / item.completed(agent_message) / turn.completed(usage) |
| `codex/exec-shell-stopped.jsonl` | 셸 명령 실행 중 프로세스 트리를 강제 종료한 실행. command_execution 항목 포함, 마지막 턴 미완료 |
| `codex/exec-collab-wait-enabled.jsonl` | 실제 출력(2026-10-25, CLI 0.163.0-alpha.11.2, W18b E4). 도우미 에이전트를 띄우라는 프롬프트, `multi_agent` 켬. `collab_tool_call`(`tool` = `wait`, `receiver_thread_ids` 빈 목록) 하나와 최종 답 READY. `spawn_agent` 항목은 없다 |
| `codex/exec-collab-wait-disabled.jsonl` | 같은 프롬프트에 `--disable multi_agent --disable multi_agent_v2`. 켬과 같은 모양이다(`wait` 하나, READY) |
| `codex/exec-collab-synthetic.jsonl` | **손으로 만든 파일(실제 출력 아님, W18b).** 위 실제 항목 모양(`sender_thread_id`·`receiver_thread_ids`·`agents_states`)을 따라 `spawn_agent` 두 개와 `wait` 하나, 모르는 항목 종류 하나를 넣었다. 실제 `spawn_agent` 항목은 아직 받지 못했다 |
| `claude/print-ok.json` | `claude -p --output-format json` 성공. is_error=false, terminal_reason=completed, result="OK". 첫 호출이라 cache_creation 7259 + cache_read 15445 토큰이 붙었다 |
| `claude/print-ok-cached.json` | 같은 성공이지만 캐시가 살아 있는 상태. total_cost_usd 가 첫 호출의 약 1/7 |
| `claude/print-not-logged-in.json` | 오류 결과. is_error=true, terminal_reason=api_error, result 에 로그인 안내 |

**세 Claude 파일의 최상위 필드 구조는 같다.** 성공과 실패를 `result` 문자열로 구분하지 말고 `is_error` 와 `terminal_reason` 으로 판정해라.

**강제 종료된 `claude -p` 의 샘플은 없다.** 중지되면 stdout 과 stderr 가 모두 비기 때문이다. JSON 은 실행이 끝날 때 한 번에 쓰이므로 부분 출력조차 남지 않는다. 빈 출력을 파싱 실패로 다루지 말고 중지 가능성으로 다뤄야 한다. Codex 의 JSONL 은 이벤트마다 흘려 쓰이므로 중간까지의 기록이 남는다는 점에서 다르다. 자세한 내용은 설계 문서의 `08-g0-log.md` 4.2절.
