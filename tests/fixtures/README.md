# 테스트 fixture

실제 CLI 실행에서 받은 출력 샘플이다. 어댑터 테스트는 이 파일들로만 검증하며 실제 CLI를 호출하지 않는다.
채취일 2026-09-11, 출처는 설계 문서 폴더의 `08-g0-log.md`.

| 파일 | 내용 |
|---|---|
| `codex/exec-ok.jsonl` | `codex exec --json` 정상 실행. thread.started / turn.started / item.completed(agent_message) / turn.completed(usage) |
| `codex/exec-shell-stopped.jsonl` | 셸 명령 실행 중 프로세스 트리를 강제 종료한 실행. command_execution 항목 포함, 마지막 턴 미완료 |
| `claude/print-not-logged-in.json` | `claude -p --output-format json` 의 오류 결과. is_error=true, result 에 로그인 안내. 필드 구조는 정상 결과와 같다 |

정상 로그인 상태의 `claude -p` 성공 샘플은 아직 없다. 로그인 후 채취해 `claude/print-ok.json` 으로 추가한다.
