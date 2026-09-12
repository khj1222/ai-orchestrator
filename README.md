# ai-orchestrator

Codex CLI와 Claude Code를 작업 단위로 실행하고, 결과를 검증한 뒤 직렬로 통합하는 개인용 실행 관리자.

**상태: 개발 시작 단계. 아직 동작하는 기능이 없다.** 이 저장소는 비공개이며, 작동하는 예제 하나를 완주한 뒤에 공개할 계획이다.

설계 문서와 개발 계획은 별도 폴더(`AI-Orchestrator-Research`)에 있다. 작업 단위는 그 폴더의 `09-dev-plan.md`에서 W 번호로 관리한다.

## 범위

- 한 PC, 한 저장소, 고정 계획, 최대 작업자 2명, 직렬 통합.
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
  integrate.py         직렬 merge, 통합 검사                         [3단계]
  planner.py           계획 파일 검증                                [W10]
  cli.py               orch validate / run-task / run / status / stop [W7]
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

대괄호는 담당 작업 번호다. 아직 만들어지지 않은 파일이 대부분이다.
