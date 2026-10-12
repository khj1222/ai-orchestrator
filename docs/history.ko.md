# 개발 이력

[README.ko.md](../README.ko.md)에서 옮긴 개발 과정의 실행 기록이다. 각 기록이 가리키는 `runs/` 아래 실행 기록 폴더는 버전 관리에서 제외돼 있어 이 저장소에는 없다.

## 단계 요약

| 단계 | 내용 |
|---|---|
| W1–W7 | 기록·상태 전이, 프로세스 실행·종료, Codex·Claude 어댑터, worktree, 등록 검사, 단일 작업 CLI |
| W7b | 두 CLI로 실제 성공·실패·중지 시나리오 검증 (아래) |
| W8–W9 | 예제 과제 `examples/search-ab` 규격·개발 자료, 검증기와 비공개 평가 자료 |
| W10a–W10c | 계획 검사(`validate-plan`), 계획 실행·직렬 통합(`run-plan`) |
| W11, W12 | `pilot-001` 실제 완주: Codex 순차, Codex 2개 동시 |
| W13, W13b | 최종 검토, run 보고서의 route·CLI 버전·평가 집계(관리자 고정 원천) |
| W14a, W14 | 산출물 존재 규칙, Codex + Claude Code 혼합 완주 |

## W7b Claude 실행 기록 (2026-09-20)

- 설정: `claude-opus-5`, effort high, `--permission-mode acceptEdits`, `--allowedTools Bash(git:*)`(중지 시나리오는 `Bash(python:*)` 추가), `--json-schema` 인라인. 제어 세션이 Claude Code 안이므로 `CLAUDECODE`·`CLAUDE_CODE_ENTRYPOINT`를 `strip_env`에 추가했다.
- 성공: 함수 9개를 보고한 결과 commit `da6c079`, 내용·범위 검사 pass, accepted, 종료 0, 9턴, permission 거부 0건.
- 실패: 없는 파일, missing_artifact, failed, 종료 1. CLI 자체는 종료 0이었지만 채택되지 않음.
- 중지: claude.exe 아래 bash·python 자손을 포함한 7개 프로세스를 관측, `orch stop` 후 잔존 0, cancelled, 실행기 종료 3. 강제 종료된 `claude -p`는 stdout을 남기지 않는다(0바이트).
- 최종 구조화 응답은 결과 JSON의 `structured_output` 필드에 있다. 어댑터는 `result` 문자열만 보존하므로 감사는 `evidence/stdout.log`를 직접 읽었다.
- 각 시나리오 시도 1회·600초 상한. 검증 중 stop/Ctrl+C와 실제 타임아웃·재시도는 시험하지 않음.

## W7b Codex 실행 기록 (2026-09-17)

- 성공: 함수 9개를 보고한 결과 commit `ef21db3`, 내용·범위 검사 pass, accepted, 종료 0.
- 실패: 없는 파일, missing_artifact, failed, 종료 1. 모델 CLI 자체는 종료 0이었지만 채택되지 않음.
- 중지: Python 자식·손자를 포함한 8개 프로세스를 관측, `orch stop` 후 잔존 0, cancelled, 실행기 종료 3.
- 실제 시나리오는 각각 시도 1회·600초 상한. 기본 설정의 2회·3600초는 유지.
- 첫 시도는 결과 커밋 권한 부족으로, 두 번째는 CLI 옵션 충돌로 실패했다. 이후 Codex route에 `approve_for_me`(`--approve-for-me`)를 추가했다. 이 옵션은 `-s`와 함께 쓰지 않는다.
- 검증 중 stop/Ctrl+C와 실제 타임아웃·재시도는 이번 범위에서 시험하지 않음.
