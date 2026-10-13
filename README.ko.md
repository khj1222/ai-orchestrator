# ai-orchestrator

[English](README.md)

고정된 계획의 작업들을 코딩 에이전트 CLI로 실행하고, **등록 검사가 정확한 결과 commit에서 모두 통과하고 명세의 산출물이 그 commit에 실제로 있을 때만** 작업을 채택한 뒤, 채택된 결과를 하나씩 `git merge --no-ff`로 통합하는 개인용 실행 관리자다. 두 회사의 공식 CLI(OpenAI Codex CLI, Anthropic Claude Code)를 하위 프로세스로 띄우며, 각 CLI는 사용자 자신의 계정으로 로그인돼 있어야 한다.

연구용으로 만든 작은 도구를 작동 예제와 함께 공유하는 것이다. 제품이 아니며 Anthropic·OpenAI와 제휴하거나 그들의 보증·지원을 받지 않는다.

## 하는 것과 하지 않는 것

하는 것:
- 실행 전에 계획을 검사한다(`orch validate-plan`): 작업 그래프, 쓰기 경로 소유, 검사 배선, 비공개 평가 문자열의 누출.
- 의존 순서대로 작업을 최대 2개까지 동시에, 각자 고정된 기준 commit의 git worktree에서 실행한다(`orch run-plan`). 의존 작업은 선행 작업이 통합된 commit에서 시작한다.
- 채택은 등록 검사와 산출물 존재만으로 정한다. 작업자의 완료 보고, 종료 코드, 검토자 의견은 상태를 바꾸지 않는다.
- `run-plan`은 검사 fail이나 산출물 누락인 작업을 시도 예산 안에서 다시 시도하며, 작업자에게는 검사 ID·판정·사유 코드만 알려 준다(`run-task`에서는 종료 상태).
- 모든 시도, 프로세스 식별, 검사 판정, 병합을 기록하고 run 보고서를 쓴다.

하지 않는 것:
- 모델 자동 선택, 난이도별 라우팅, 화면, 여러 PC 분산.
- 시간·비용 절감의 측정이나 주장. 단일/병렬 효용 비교는 하지 않았다.
- 자격 증명의 저장·읽기·복사·중계. 각 CLI는 사용자 PC의 자기 로그인을 쓴다.

## 원칙

- **검사만이 결정한다.** 등록 검사가 정확한 결과 commit에서 모두 pass이고, 산출물이 그 commit에 일반 파일(mode 100644·100755 blob, gitlink·심볼릭 링크 제외, `/`로 끝나면 그 아래 일반 파일 하나 이상)로 있어야 채택한다.
- **입력을 고정한다.** 계획은 기준 commit과 protocol hash를 고정한다. 계획·명세·지시문 파일이 하나라도 바뀌면 같은 run을 재개하지 않는다.
- **쓰기 경로 소유자는 1명.** 두 작업이 겹치는 경로를 갖지 않으므로 통합에서 내용 충돌을 풀 일이 없다.
- **비공개 평가 자료는 저장소 밖, 작업자 환경 밖.** 보고서의 평가 숫자는 관리자가 실행 전에 고정한 기대 요약에서만 나오고, 지정한 검사가 pass해야 보인다. 원천이 바뀌거나 없으면 보고서에는 `withheld`만 적힌다.
- **작성자와 검토자를 다르게.** 개발 중 한 회사 에이전트가 쓴 코드는 다른 회사 에이전트가 검토했다.

## 요구 환경과 설치

- Windows, Python 3.10 이상, git. 실행 관리자와 실제 모델 CLI 실행은 Windows에서만 검증했다. Linux에서 시험한 범위는 알려진 한계 절에 있다.
- 실제 실행에는 공식 CLI가 설치돼 있고 각자 자기 계정으로 로그인돼 있어야 한다. Anthropic과 OpenAI 모두 자동화·제3자 도구 사용에는 API 키를 권장 인증 방식으로 안내한다. 대신 구독 로그인을 쓴다면, 구독을 자동화에 쓰는 것에 대한 각 회사 약관 확인은 사용자 책임이다. 한 로그인을 여러 기기에서 나눠 쓰거나 공개 CI에서 돌리지 않는다.
- 아래 명령은 Windows PowerShell 기준이다.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

PowerShell이 `Activate.ps1` 실행을 거부하면 `Set-ExecutionPolicy -Scope Process RemoteSigned`로 현재 셸에서만 허용한다.

`config.example.json`을 `config.json`(git 제외)으로 복사해 CLI 절대 경로와 모델 ID를 채운다.
- `paths`의 경로는 실행기가 실제로 돌아갈 셸에서 확인한 것이어야 한다. 버전·해시 폴더는 앱 업데이트로 바뀔 수 있다. 데스크톱 앱이 MSIX 패키지로 설치된 경우 그 앱이 띄운 프로세스가 보는 AppData 경로는 일반 셸과 다를 수 있다.
- `routes`의 각 키는 작업 명세의 `route_id`가 가리키는 실행 설정이다. 모델은 전체 ID로 적는다.
- Codex의 `approve_for_me: true`는 CLI의 `--approve-for-me`를 쓴다. `workspace-write`에서만 허용하며 샌드박스를 해제하지 않는다. Git worktree 메타데이터가 작업 공간 밖에 있으면 결과 커밋에 필요할 수 있다. CLI 지원 여부는 `codex exec --help`로 확인한다.
- Claude Code 작업자는 `allowed_tools`에 `Bash(git:*)`·`Bash(python:*)` 같은 허용 패턴이 있어야 테스트와 커밋을 할 수 있다. 이 앱(Claude Code) 안에서 실행기를 띄우면 `CLAUDECODE`·`CLAUDE_CODE_ENTRYPOINT`를 `strip_env`에 넣어 중첩 세션을 피한다.

## 예제 1 — 모델 CLI 없이 커밋된 결과 재현

저장소에는 실제 실행의 결과가 들어 있다(검색 방법 `examples/search-ab/methods/`, 실험 실행기 `examples/search-ab/runner/`, 출력, 보고서). CLI 없이 확인할 수 있다.

```powershell
python -m pytest
cd examples/search-ab
python -m pytest tests
python protocol/validate.py --hash
python -m runner.run --documents data/dev/documents.jsonl --queries data/dev/queries.jsonl --out ../../../search-ab-dev-run
python -c "import hashlib, pathlib; [print(n, hashlib.sha256(pathlib.Path('../../../search-ab-dev-run', n).read_bytes()).hexdigest()) for n in ('raw-a.jsonl', 'raw-b.jsonl', 'summary.json')]"
cd ../..
```

기대값:

| 출력 | sha256 |
|---|---|
| `protocol/validate.py --hash` | `ea86373c26e72d61f9ca6a15e5636ea96e8496d0c3c2bd0e6bbc1a21e512b624` |
| `raw-a.jsonl` | `11e767acaf0f6e549b8a64e1de85be13ee15769525338883f4fd0e8e0cd632a6` |
| `raw-b.jsonl` | `cb363cfba38f91e2cc5db531ccd1ff80257f047b2dbd1988bc5d02c6982afe3c` |
| `summary.json` | `10298c0e7f59952bb165679b4bc8a1976d6b6f743f495412648a64c0d58b38e2` |

같은 값이 `examples/search-ab/results/dev/`에 커밋돼 있다. `latency.json`·`manifest.json`은 실행마다 다르다(규격대로). `--out` 폴더는 아직 없어야 한다. 위 protocol hash는 Windows에서 계산한 값이며 다른 OS에서는 다르다(알려진 한계 참고).

## 예제 2 — 자기 CLI와 자기 평가 자료로 계획 전체 실행

여기서 쓴 평가 질의는 비공개이며 공개하지 않는다. 계획 전체를 돌리려면 자기 자료를 준비한다.
아래 명령은 저장소 루트에서 실행한다.

1. 저장소 **밖**에 비공개 폴더를 만들고 다음을 둔다.
   - `eval-queries.jsonl`: `examples/search-ab/data/dev/documents.jsonl`에 대한 평가 질의. 형식은 `examples/search-ab/protocol/data-format.md`.
   - `behavior-cases.json`: 손으로 계산한 사례와 A/B 기대 순위. `tests/test_verifier.py`에 이 형식의 공개 사례 8개가 있어 출발점으로 쓸 수 있다.
2. 환경 변수를 그 폴더로 지정하고 기대 결과를 만든다(먼저 손계산 사례를 모두 확인한다).
   ```powershell
   $env:ORCH_VERIFIER_PRIVATE = '<비공개 폴더>'
   python -B verifier/reference.py --write-expected
   ```
3. `runs/pilot-001-mixed-2/` 같은 계획을 새 폴더로 복사하고, `plan.v1.json`과 모든 `tasks/*.json`의 `run_id`를 바꾸고, `route_id`를 자기 `config.json`의 route에 맞춘다.
4. 검사하고 실행한다. 실행 기록은 저장소 밖에 둔다.
   ```powershell
   orch validate-plan runs/<새 run>/plan.v1.json --config config.json
   orch run-plan runs/<새 run>/plan.v1.json --config config.json --run-root <기록 폴더> --parallel 2
   ```
5. `<기록 폴더>/runs/<run-id>/run-report.md`를 읽는다. 통합 선은 ref `refs/orchestrator/runs/<run-id>/integration`이며 브랜치에 넣을지는 사용자가 정한다.

실제 실행은 CLI 구독이나 API 사용량을 쓴다.

## 검증한 것

2026년 10월, Windows PC 한 대, 계획 `runs/pilot-001/`(검색 모듈, 실험 실행기, 통합, 보고서):

| run | 작업자 | 결과 |
|---|---|---|
| W11 `pilot-001` | Codex, 순차 | completed, 4작업 모두 첫 시도 채택, 약 17분 |
| W12 `pilot-001-parallel` | Codex 2개 동시 | completed, 4/4 첫 시도, 약 13분 |
| W14 `pilot-001-mixed-2` | Codex + Claude Code 동시 | completed, 4/4 첫 시도, 약 18분 |

run마다 관리자가 판정, 변경 범위, 깨끗한 worktree에서의 출력 재현, 보고서 숫자와 결과 파일의 대조, 작업자 명령 기록의 검증기·비공개 폴더 접근(없음)을 따로 확인했다. 세 run의 원시 결과는 바이트 단위로 같다.

비공개 평가 질의 20개의 집계: **방법 A hit@3 = 0.3(6/20), 방법 B hit@3 = 0.3(6/20)**. 평가 질의 상당수가 정답 문서와 토큰이 겹치지 않도록 만들어졌고, 공백 분리 토큰화로는 찾지 못한다. 이 실험의 요점은 고정된 규격을 재현 가능하게 구현했다는 것이지 어느 방법이 좋다는 것이 아니다.

코드 작업 단위마다 다른 회사 에이전트가 검토했다(문서만 바꾼 정리는 제외). 검토로 찾아 고친 것: 병렬 완료 시 병합 순서, 다른 작업이 blocked된 뒤 새 작업이 시작되던 경쟁, 검증 대상 코드가 평가 세부를 공개할 수 있던 통로(숫자가 관리자 고정 원천에서만 나오도록 재설계), gitlink를 산출물로 인정하던 문제, 경로 앞 공백이 사라지던 문제 등.

## 알려진 한계와 실패 사례

- **첫 혼합 실행 실패.** Claude Code CLI 로그인이 만료돼 작업자가 0.18초 만에 끝났는데, 그 작업의 검사가 범위 검사뿐이라 빈 결과가 *채택*됐다. run을 중단하고 산출물 존재 규칙을 추가했다. 교훈: 범위 검사 통과는 내용에 대해 아무것도 증명하지 않는다.
- **격리는 차단이 아니라 사후 탐지다.** 작업자는 같은 OS 사용자로 돌아 원칙적으로 비공개 폴더를 읽을 수 있다. 도구는 환경 변수를 작업자에게서 지우고, 자료를 저장소 밖에 두고, 나중에 기록을 감사한다.
- 계획 누출 검사(P8)는 문자열 그대로만 찾는다. ID 대응으로 바꿔 쓴 정답은 통과한다.
- 범위 검사는 최종 commit만 본다. 허용 경로 밖에 썼다 지운 파일은 보이지 않는다.
- Claude Code 작업자는 설정한 허용 목록 아래에서 권한 거부(PowerShell 도구, `cd … &&` 연쇄)를 겪고 우회했다. 그만큼 느리다.
- 실제 모델 CLI를 쓴 실행은 Windows PC 한 대에서만 했다. 검증 중이나 두 작업 동시 실행 중의 Ctrl+C·`stop`은 충분히 시험하지 않았다.
- 실행 관리자는 Linux에서 작업자와 검사를 띄우지 못한다. `orchestrator/process.py`가 Windows 전용 프로세스 API(`subprocess.CREATE_NEW_PROCESS_GROUP`)를 쓰기 때문이다. macOS는 시험하지 않았지만 같은 코드 경로를 탄다. Linux 컨테이너(Python 3.12)에서 예제 1의 결과 해시 세 개(`raw-a.jsonl`, `raw-b.jsonl`, `summary.json`)는 같았고, 실행 관리자 테스트는 393 통과, 81 실패(프로세스 실행과 검사 판정), 21 건너뜀(Windows 전용)이었다.
- protocol hash는 OS에 따라 다르다. `protocol_hash()`가 `Path` 객체를 `sorted()`로 정렬하는데, Windows에서는 대소문자를 무시하고 다른 OS에서는 구분해서 `README.md`의 순서가 달라진다. 파일 내용은 같고, Linux 값은 `96b2671e8f9e961184f7de7059852854f184a0c9d29e9ad1112fd5c2d9023acc`이다. 이 저장소의 계획과 기록은 Windows 값을 쓴다.
- 실행 기록은 `orch status`(또는 같은 run 잠금)로만 읽는다. Windows에서는 다른 프로그램이 잠금 없이 기록 파일을 열어 두면 실행 관리자의 원자적 파일 교체가 `PermissionError`로 실패한다.
- 사용한 Claude Code CLI(2.1.268)는 `claude-opus-5-5` 같은 새 모델 ID를 받지 않는다.
- 재현 범위는 커밋된 산출물의 재실행이다. 모델 실행 자체는 결정적이지 않다.

## 상세 참조

### 단일 작업 CLI

설치 후 `orch` 또는 `python -m orchestrator.cli`로 실행한다. 작업 명세·설정은 JSON이고, 등록 검사 정의는 `checks/README.md`를 따른다.

```text
orch validate task.json --repo <target-repo> --config <config.json> --checks-dir <checks-dir>
orch run-task task.json --repo <target-repo> --config <config.json> --checks-dir <checks-dir> --run-root <record-root>
orch status <run-id> --repo <target-repo> --run-root <record-root>
orch stop <run-id> <task-id> --repo <target-repo> --run-root <record-root>
```

`--run-root` 기본값은 대상 저장소이며 그 아래 `runs/`에 기록한다. 다른 저장소를 대상으로 할 때는 저장소 밖 기록 경로를 지정해 untracked 기록 때문에 작업 공간 생성이 거부되지 않도록 한다. 시작 전 대상 저장소는 clean이어야 한다. `validate`는 CLI 실행 파일의 존재까지 확인하지 않으므로 실제 호출 전에 경로를 확인한다.

### 계획 검사

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
- P10 선택 필드 `publication`(`{"env", "file", "check"}`)은 평가 집계 공개를 선언한다. `env`는 `private_env`에 있어야 하고, `file`은 그 폴더 안의 기대 요약(`k`, `queries`, 방법별 `hit_at_3`·`hits`·`no_result_queries`, `hit_at_3 == hits/queries`)이며, `check`는 어떤 작업에 붙은 등록 검사여야 한다. 필드를 생략하면 공개하지 않는다는 뜻이고 `null`로 적으면 선언 오류다.

P8은 공백을 정규화한 문자열 일치만 본다. 지시문은 파일 전체를, 명세는 키와 값을 하나씩 따로 본다. "q99의 정답은 d999" 같은 ID 대응이나 바꿔 쓴 표현은 잡지 못하므로 누출을 막는 장치가 아니라 사후 탐지다. 비공개 파일의 키는 공개 형식 이름이라 비교 대상에서 뺀다. 오류 메시지는 비공개 파일 이름·행·순번만 적는다. P8을 돌리려면 검사하는 셸에 해당 환경 변수가 설정돼 있어야 하며, 없으면 통과가 아니라 "검사 불가" 오류다.

### 계획 실행

```text
orch run-plan runs/<run-id>/plan.v1.json --repo <target-repo> --config <config.json> --checks-dir <checks-dir> --run-root <record-root> [--parallel 2]
```

- 계획 검사에 오류가 하나라도 있으면 아무것도 시작하지 않는다. `private_env`는 작업자에게서 지울 환경 변수 이름 목록이며 값은 읽지 않는다. `leak_sources`와 `publication`이 가리키는 환경 변수는 관리자 셸에서 유효한 폴더를 가리켜야 한다.
- 작업은 계획의 위상 순서로 실행한다. 선행 작업이 모두 통합되면 그 시점의 통합 선(`refs/orchestrator/runs/<run-id>/integration`)에서 작업 공간을 만들고, 같은 commit을 범위 검사 기준으로 쓴다.
- 채택된 결과는 실행 순서대로 하나씩 `git merge --no-ff`로 통합 선에 합친다. 뒤 작업이 먼저 채택되면 앞 작업이 통합될 때까지 기다린다. 병합은 기록 폴더 아래 전용 worktree에서 하며 작성자는 `orchestrator`다. 사용자의 작업 폴더와 `main`은 건드리지 않는다.
- 채택에는 등록 검사 전부 pass와 함께 명세의 `deliverables`가 결과 commit에 일반 파일(mode 100644·100755)로 있어야 한다. 끝이 `/`이면 그 아래 일반 파일 하나 이상이다. gitlink와 심볼릭 링크는 파일로 세지 않는다. 없으면 `missing_deliverable`이다.
- 검사 fail이나 산출물 누락이고 시도 예산이 남았으면 같은 기준에서 다시 시도한다. 새 프롬프트에는 이전 시도의 검사 ID·판정·사유 코드만 덧붙이고 검증기 출력은 넘기지 않는다. `run-task`는 검사 fail을 종료 상태로 둔다.
- `--parallel 2`이면 시작 가능한 작업을 두 개까지 동시에 실행한다. 통합은 항상 하나씩이다.
- 어떤 작업이 failed·blocked·cancelled로 기록되는 즉시 새 작업을 시작하지 않고, 진행 중인 작업이 끝난 뒤 그 상태로 끝낸다. 같은 계획으로 다시 실행하면 통합된 작업은 건너뛴다. 계획·명세·지시문 파일별 sha256이 하나라도 바뀌었으면 같은 run_id로는 재개하지 않는다.
- 작업자 프롬프트에는 지시문 본문, 제약 JSON, 작업 명세 JSON 전체가 들어간다.
- 작업마다 해석된 route(어댑터 기본값 포함, `defaults_applied` 표시)와 CLI 버전을 기록한다.
- 끝나면 기록 폴더에 `run-report.json`·`run-report.md`를 쓴다. 작업별 상태·시도·검사 판정·산출물 누락·기준·병합 commit·route·CLI 버전, 통합 선 head, 그리고 `publication`이 있으면 평가 집계가 들어간다. 이 숫자는 검사 프로세스에서 나오지 않는다. 관리자가 실행 전에 비공개 기대 요약을 읽어 sha256과 함께 고정하고, 지정한 검사가 채택 결과에서 pass했으며 원천이 그대로일 때만 보여 준다. 아니면 `withheld` 사유만 적는다. 이미 끝난 run을 다시 실행하면 재개하지 않고 기록에서 보고만 다시 만든다.
- 범위 검사는 결과 commit의 최종 상태만 본다. 검증 단계 중 `stop`은 busy를 반환하고, Ctrl+C에 의한 검증 자식 정리는 검증하지 않았다.

## 구조

```text
orchestrator/          실행 관리자
  records.py           run/task/attempt/check 기록과 상태 전이
  process.py           프로세스 실행·수집·트리 종료·식별 기록
  workspace.py         git worktree 생성·삭제, 범위 검사
  verify.py            등록 검사 실행, verdict 기록
  plan.py              계획 검사 (orch validate-plan)
  run_plan.py          계획 실행, 직렬 통합, run 보고서
  cli.py               orch validate / validate-plan / run-task / run-plan / status / stop
  adapters/            codex exec·claude -p 어댑터
config.example.json    CLI 경로·모델 ID·한도 예시 (실제 config.json은 git 제외)
checks/                등록 검사 정의
verifier/              검증 스크립트 (비공개 자료는 저장소 밖)
examples/search-ab/    예제 과제: 규격, 개발 자료, 검색 방법, 실행기, 결과, 보고서
runs/                  예제 계획(pilot-001, -parallel, -mixed, -mixed-2). 실행 기록은 git 제외
tests/                 실행기 자체의 테스트 (오프라인, 모델 CLI 없음)
docs/                  개발 이력
```

개발 과정의 실행 이력(W7b 실제 CLI 검증 등)은 [docs/history.ko.md](docs/history.ko.md)에 있다.

## 라이선스

MIT. [LICENSE](LICENSE) 참고.
