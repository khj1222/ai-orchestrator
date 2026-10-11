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
  plan.py              계획 파일 검증 (orch validate-plan)             [W10a]
  cli.py               orch validate / validate-plan / run-task / run-plan / status / stop  [W7, W10a, W10c]
  run_plan.py          계획 실행, 직렬 통합                           [W10c]
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

## 계획 검사

여러 작업으로 된 계획은 실행 전에 `orch validate-plan`으로 검사한다. 읽기 전용이며 Git과 프로세스를 실행하지 않는다.

```text
orch validate-plan runs/<run-id>/plan.v1.json --repo <target-repo> --config <config.json> --checks-dir <checks-dir>
```

계획은 JSON이다. `schema_version`(1), `run_id`, `plan_version`, `base_commit`, `protocol_hash`(64자리 hex), `private_env`(작업자에게 넘기지 않을 환경 변수 이름), `leak_sources`(`{"env", "files"}`: 그 환경 변수가 가리키는 폴더의 비공개 JSON/JSONL 파일), `tasks`를 둔다. 실행 작업은 `{"task_id", "spec", "instructions"}`로 작업 명세와 지시문 파일을 계획 파일 폴더 기준 상대 경로로 가리킨다. 이미 끝난 작업은 `{"task_id", "status": "completed", "completed_commit", "deliverables"}`로 적고 실행하지 않는다.

검사 항목:
- P1 계획 필드, 그리고 계획·명세의 모든 키와 문자열 값, 지시문 어디에도 `TO_BE_`가 없다(문자열 중간 포함).
- P2 task_id 고유(대소문자 무시). 명세의 `task_id`·`run_id`·`plan_version`·`base_commit`·`protocol_hash`가 계획과 타입까지 같다(`true`나 `1.0`은 `1`이 아니다).
- P3 알려진 선행 작업만 참조하고 순환이 없다.
- P4 각 명세가 `validate`를 통과한다(`depends_on`만 계획 기준).
- P5 쓰기 경로 소유자는 1명이다. 실제 범위 검사(`workspace.detect_out_of_scope_changes`)가 문자열 접두로 판정하므로 `ex/m`과 `ex/methods-extra/`도 겹친다고 본다(대소문자 무시). 산출물은 자기 경로 안에 있어야 하고, 이 판정은 실제 검사처럼 대소문자를 구분한다.
- P6 작업에 붙은 검사의 `required_artifacts`를 그 작업이나 선행 작업이 만든다. 산출물이 `/`로 끝나면 디렉터리로 보고 하위 경로까지 덮는다. 그 밖에는 그 파일 하나만 덮는다.
- P7 `private_env`와 `leak_sources`의 환경 변수가 모두 `environment.strip_env`에 있고 `extra_env`에 없다. `leak_sources`의 변수는 `private_env`에도 적어야 한다.
- P8 비공개 파일의 8자 이상 문자열 값이 지시문이나 명세의 키·값 하나 안에 그대로 없다.
- P9 지시문 파일이 읽히고 비어 있지 않다.

P8은 공백을 정규화한 문자열 일치만 본다. 지시문은 파일 전체를, 명세는 키와 값을 하나씩 따로 본다(서로 다른 필드를 이어 붙여 비교하지 않는다). "q99의 정답은 d999" 같은 ID 대응이나 바꿔 쓴 표현은 잡지 못하므로 누출을 막는 장치가 아니라 사후 탐지다. 비공개 파일의 키는 `relevant_doc_ids` 같은 공개 형식 이름이라 비교 대상에서 뺀다. 오류 메시지는 비공개 파일 이름·행·순번만 적고, 비공개 파일의 값과 키는 출력하지 않는다. P8을 돌리려면 검사하는 셸에 해당 환경 변수가 설정돼 있어야 하며, 없으면 통과가 아니라 "검사 불가" 오류다.

선택 필드 `publication`(`{"env", "file", "check"}`)은 평가 집계 공개를 선언한다(P10). `env`는 `private_env`에 있어야 하고, `file`은 그 폴더 안의 기대 요약(`k`, `queries`, 방법별 `hit_at_3`·`hits`·`no_result_queries`, `hit_at_3 == hits/queries`)이며, `check`는 어떤 작업에 붙은 등록 검사여야 한다. 검증 대상 코드가 실행되는 프로세스의 출력은 공개 숫자의 근거로 쓰지 않는다(W13b 재검토 R1). 필드를 생략하면 공개하지 않는다는 뜻이고, `null`로 적으면 선언 오류다. 공개 보고에는 계획의 `protocol_hash`가 함께 적힌다. `run-task`는 지금도 `depends_on`이 빈 명세만 받는다.

## 계획 실행

검사를 통과한 계획은 `orch run-plan`으로 실행한다. 계획 검사(`validate-plan`)에 오류가 하나라도 있으면 아무것도 시작하지 않는다. 비공개 자료 누출 검사 때문에, 계획에 `private_env`가 있으면 관리자 셸에 그 환경 변수가 설정돼 있어야 한다.

```text
orch run-plan runs/<run-id>/plan.v1.json --repo <target-repo> --config <config.json> --checks-dir <checks-dir> --run-root <record-root> [--parallel 2]
```

- 작업은 계획의 위상 순서로 실행한다. 선행 작업이 모두 통합되면 그 시점의 통합 선(`refs/orchestrator/runs/<run-id>/integration`)에서 작업 공간을 만들고, 같은 commit을 범위 검사 기준으로 쓴다.
- 채택된 결과는 검사기가 정한 실행 순서대로 하나씩 `git merge --no-ff`로 통합 선에 합친다. 뒤 작업이 먼저 채택되면 앞 작업이 통합될 때까지 accepted로 기다린다. 앞 작업이 실패하면 그 run에서는 합치지 않는다. 병합은 기록 폴더 아래 전용 worktree에서 하며, 병합 commit 작성자는 `orchestrator`다. 사용자의 작업 폴더와 `main`은 건드리지 않는다. 통합 선을 `main`에 넣는 것은 사람이 한다.
- 등록 검사가 fail이고 시도 예산이 남았으면 같은 기준에서 다시 시도한다. 새 프롬프트에는 이전 시도의 검사 ID·verdict·reason code만 덧붙이고, 검증기 출력은 넘기지 않는다. `run-task`는 지금처럼 검사 fail을 종료 상태로 둔다.
- `--parallel 2`이면 시작 가능한 작업을 두 개까지 동시에 실행한다. 통합은 항상 하나씩이다.
- 어떤 작업이 failed·blocked·cancelled로 기록되는 즉시(그 작업의 스레드가 끝나기 전이라도) 새 작업을 시작하지 않고, 진행 중인 작업이 끝난 뒤 그 상태로 끝낸다. 같은 계획으로 다시 실행하면 통합된 작업은 건너뛰고 이어서 진행한다. 계획·명세·지시문 파일별 sha256(`summary.json`의 `plan_files`)이 하나라도 바뀌었으면 같은 run_id로는 재개하지 않는다.
- 이미 끝난 계획 작업(`status: completed`)은 실행 기록의 작업으로 만들지 않고 `summary.json`의 `completed_tasks`에 commit만 적는다.
- 동시 실행 중 Ctrl+C·`stop`은 작업 하나에 대해 `run-task`가 보장하는 범위까지만 확인했다.
- 작업자 프롬프트에는 지시문 본문, 제약 JSON, 작업 명세 JSON 전체가 들어간다. 계획은 기준 commit 뒤에 커밋되므로 지시문이 가리키는 명세 파일이 작업 공간에 없을 수 있다(W13b).
- 작업마다 해석된 route(설정값이 없으면 어댑터 기본값을 쓰고 `defaults_applied`에 표시, effort가 없으면 `cli-default`)와 CLI 버전(`--version` 첫 줄, 실패하면 `unavailable`)을 `summary.json`의 작업 항목에 남긴다(W13b).
- 끝나면 기록 폴더에 `run-report.json`·`run-report.md`를 쓴다. 작업별 상태·시도·검사 판정·기준·병합 commit·route·CLI 버전, 통합 선 head, 그리고 계획이 `publication`을 선언했으면 평가 질의 집계값이 들어간다. 이 숫자는 검사 프로세스에서 나오지 않는다. 관리자가 실행 전에 비공개 기대 요약 파일을 직접 읽어 sha256과 함께 고정하고, 지정한 등록 검사가 채택된 결과에서 pass했으며 그 파일이 고정 때와 같을 때만 보여 준다. 아니면 `withheld` 사유만 적는다. 통합 HEAD는 완료·실패와 관계없이 보고 시점의 실제 ref 값이다. 이미 끝난 run을 다시 실행하면 재개하지 않고 기록에서 보고만 다시 만든다. 이 경로는 계획 검사보다 먼저 돌므로, 공개 원천을 더는 읽을 수 없어도(삭제·형식 오류·환경 변수 없음) 보고가 갱신되며 그때 숫자는 `withheld`가 된다(W13b).
- 범위 검사(`allowed-changes-v1`)는 결과 commit의 최종 상태만 본다. 작업 중 허용 경로 밖에 잠깐 썼다 지운 파일은 탐지하지 않으며 작업자 보고에 기댄다(W13 우려 6).

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
