# ai-orchestrator

Codex CLI와 Claude Code를 작업 단위로 실행하고, 결과를 검증한 뒤 직렬로 통합하는 개인용 실행 관리자.

**상태 (2026-09-20): W7b 두 경로 통과.** Codex CLI 0.155.0-alpha.2.6(gpt-6-astra/high, 09-17)와 Claude Code 2.1.268(claude-opus-5/high, 09-20)로 각각 성공·실패·중지를 실제 검증했다. 자동 승인 심사 옵션 수정(`w7b-codex-live`)은 독립 검토 후 main에 병합됐고 전체 오프라인 회귀 검사 322개 통과. G1은 2026-09-20 사용자가 확정했다. 2단계(W8 규격과 개발 자료)부터 진행한다.

설계 문서와 개발 계획은 별도 폴더(`AI-Orchestrator-Research`)에 있다. 작업 단위는 그 폴더의 `09-dev-plan.md`에서 W 번호로 관리한다.

## 범위

- 목표는 한 PC, 한 저장소, 고정 계획, 최대 작업자 2명, 직렬 통합이다. 현재는 단일 작업 실행까지 구현했다.
- 작업(task)과 실행 시도(attempt)를 분리하고, 검증 결과를 정확한 commit 해시에 묶는다.
- 상태 전이는 등록된 검사 결과만으로 결정한다. 모델의 완료 보고나 검토 의견은 상태를 바꾸지 않는다.
- 자동 라우팅, 로컬 분류기, 화면, 원격 분산은 첫 버전 범위 밖이다.

## 요구 환경

- Windows, Python 3.10 이상, git.
- Codex CLI와 Claude Code가 설치되어 있고 각자 로그인되어 있어야 한다. 이 도구는 로그인 정보를 저장하거나 중계하지 않는다.
- 실행 전 `config.example.json`을 `config.json`으로 복사해 CLI 절대 경로와 모델 ID를 채운다. `config.json`은 버전 관리에 포함하지 않는다.
  - `paths`의 경로는 실행기가 실제로 돌아갈 셸에서 확인한 것이어야 한다. 버전·해시 폴더는 앱 업데이트로 바뀔 수 있다.
  - 데스크톱 앱이 MSIX 패키지로 설치된 경우, 그 앱이 띄운 프로세스가 보는 AppData 경로는 패키지 안으로 가상화되어 일반 셸에서는 존재하지 않는다. 반드시 실행기가 돌아갈 셸에서 확인한 경로를 쓴다.
  - `routes`의 각 키는 작업 명세의 `route_id`가 가리키는 실행 설정이다. 모델은 전체 ID로 적는다(별칭은 가리키는 대상이 바뀔 수 있다).

Codex의 `approve_for_me: true`는 CLI의 `--approve-for-me`를 사용해 필요한 권한 요청을 자동 심사로 보냅니다. `workspace-write`에서만 허용하며, 샌드박스를 해제하지 않습니다. Git worktree의 메타데이터가 작업 공간 밖에 있는 경우 결과 커밋에 이 설정이 필요할 수 있습니다. 옵션을 생략한 기존 설정의 동작은 유지됩니다. CLI가 해당 옵션을 지원하는지는 실행 전 `codex exec --help`로 확인합니다.

## 개발

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

## 구조

```text
orchestrator/          실행 관리자
  records.py           run/task/attempt/check 기록과 상태 전이        [W1]
  process.py           프로세스 실행·수집·트리 종료·식별 기록          [W2]
  workspace.py         git worktree 생성·삭제, 기준 commit 검사       [W5]
  verify.py            등록 검사 실행, verdict 기록                   [W6]
  integrate.py         직렬 merge, 통합 검사                         [3단계, 예정]
  planner.py           계획 파일 검증                                [W10, 예정]
  cli.py               orch validate / run-task / status / stop       [W7]
  adapters/
    base.py            어댑터 계약                                   [W3]
    codex.py           codex exec 어댑터                             [W3]
    claude.py          claude -p 어댑터                              [W4]
config.example.json    CLI 절대 경로, 모델 ID, 한도 (실제 config.json은 gitignore)
checks/                등록 검사 정의
examples/search-ab/    첫 과제 프로젝트                               [W8]
verifier/              비공개 참조값·평가 질의·검증 스크립트            [W9]
tests/                 실행기 자체의 테스트
  fixtures/            실제 CLI 출력 샘플 (어댑터 테스트용)
runs/                  실행 기록 (버전 관리 제외)
```

대괄호는 담당 작업 번호다. W8 이후의 예제·검증 자료와 통합 기능은 후속 작업이며 `orch run`은 아직 제공하지 않는다.

## 단일 작업 CLI

설치 후 `orch` 또는 `python -m orchestrator.cli`로 실행한다. 작업 명세·설정은 JSON이고, 등록 검사 정의는 `checks/README.md`를 따른다.

```text
orch validate task.json --repo <target-repo> --config <config.json> --checks-dir <checks-dir>
orch run-task task.json --repo <target-repo> --config <config.json> --checks-dir <checks-dir> --run-root <record-root>
orch status <run-id> --repo <target-repo> --run-root <record-root>
orch stop <run-id> <task-id> --repo <target-repo> --run-root <record-root>
```

`--run-root` 기본값은 대상 저장소이며 그 아래 `runs/`에 기록한다. 다른 저장소를 대상으로 할 때는 저장소 밖 기록 경로를 지정해 untracked 기록 때문에 작업 공간 생성이 거부되지 않도록 한다. 시작 전 대상 저장소는 clean이어야 한다. `validate`는 CLI 실행 파일의 존재까지 확인하지 않으므로 실제 호출 전에 경로를 확인한다.

등록 검사가 정확한 결과 commit에 대해 모두 통과해야 작업을 채택한다. 결과 commit은 `refs/orchestrator/<attempt-id>`에 보존된다. 프로세스 종료를 확인하지 못하면 blocked 상태와 작업 공간을 보존한다. 검증 단계 중 `stop`은 busy를 반환하며, Ctrl+C에 의한 검증 자식 정리는 아직 검증하지 않았다.

## W7b Claude 실행 기록 (2026-09-20)

`runs/g1-claude-20260920-setup/audit-summary.json`에 교차 확인 결과를 보관한다. 동일 접두어의 `-success`, `-failure`, `-stop` 폴더에 원본 로그와 검사 기록이 있다.

- 설정: `claude-opus-5`, effort high, `--permission-mode acceptEdits`, `--allowedTools Bash(git:*)`(중지 시나리오는 `Bash(python:*)` 추가), `--json-schema` 인라인. 제어 세션이 Claude Code 안이므로 `CLAUDECODE`·`CLAUDE_CODE_ENTRYPOINT`를 `strip_env`에 추가했다.
- 성공: 함수 9개를 보고한 결과 commit `da6c079`, 내용·범위 검사 pass, accepted, 종료 0, 9턴, permission 거부 0건.
- 실패: 없는 파일, missing_artifact, failed, 종료 1. CLI 자체는 종료 0이었지만 채택되지 않음.
- 중지: claude.exe 아래 bash·python 자손을 포함한 7개 프로세스를 관측, `orch stop` 후 잔존 0, cancelled, 실행기 종료 3. 강제 종료된 `claude -p`는 stdout을 남기지 않는다(0바이트).
- 최종 구조화 응답은 결과 JSON의 `structured_output` 필드에 있다. 어댑터는 `result` 문자열만 보존하므로 감사는 `evidence/stdout.log`를 직접 읽었다.
- 각 시나리오 시도 1회·600초 상한. 검증 중 stop/Ctrl+C와 실제 타임아웃·재시도는 시험하지 않음.

## W7b Codex 실행 기록 (2026-09-17)

최초 대상 저장소의 `runs/g1-codex-20260917-r3-setup/audit-summary.json`에 교차 확인 결과를 보관한다. 동일 접두어의 `-success`, `-failure`, `-stop` 폴더에 원본 로그와 검사 기록이 있다.

- 성공: 함수 9개를 보고한 결과 commit `ef21db3`, 내용·범위 검사 pass, accepted, 종료 0.
- 실패: 없는 파일, missing_artifact, failed, 종료 1. 모델 CLI 자체는 종료 0이었지만 채택되지 않음.
- 중지: Python 자식·손자를 포함한 8개 프로세스를 관측, orch stop 후 잔존 0, cancelled, 실행기 종료 3.
- 실제 시나리오는 각각 시도 1회·600초 상한. 기본 설정의 2회·3600초는 유지.
- 최초 커밋 권한 실패와 두 번째 CLI 옵션 충돌 기록도 보존. --approve-for-me는 -s와 함께 쓰지 않음.
- 검증 중 stop/Ctrl+C와 실제 타임아웃·재시도는 이번 범위에서 시험하지 않음.

수정 브랜치 병합 후 기존 `config.json`의 Codex route에 `approve_for_me: true`를 추가하면 검증한 커밋 승인 경로를 사용한다. 미병합 main은 새 설정 키를 지원하지 않으므로 기존 config.json은 변경하지 않았다. 실제 검증은 runs 아래 별도 설정으로 수행했다.
