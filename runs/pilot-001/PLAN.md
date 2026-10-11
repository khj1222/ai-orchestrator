# pilot-001 계획 v1 (W10b)

작성: 2026-10-11, 기획자 실행 1회(`claude-fable-5-1`, xhigh). 04 §4 기획자 프롬프트의 출력 7항목을 따른다.
이 문서는 사람 검토용 설명이다. 실행기가 읽는 것은 `plan.v1.json`, `tasks/*.json`, `instructions/*.md` 다.

| 고정값 | |
|---|---|
| run_id / plan_version | `pilot-001` / 1 |
| base_commit | `ce22a70ccad5f518e74c85228f294690e1386710` (W10a 병합 + protocol 정정) |
| protocol_hash | `ea86373c26e72d61f9ca6a15e5636ea96e8496d0c3c2bd0e6bbc1a21e512b624` — 이 worktree 에서 `python examples/search-ab/protocol/validate.py --hash` 로 재계산해 일치 확인 |
| private_env / leak_sources | `ORCH_VERIFIER_PRIVATE` / `eval-queries.jsonl`, `behavior-cases.json` |
| route_id / limits | 실행 작업 4개 모두 `codex-default`, `timeout_seconds` 3600, `max_attempts_total` 2, `paid_api_allowed` false |

## 1. 목표와 완료 조건

**목표 (03 §4):** 고정된 소형 문서 모음(`data/dev/`, 문서 30·질의 10)에서 두 기준 검색 방법 A·B 를 protocol v1 규격대로 구현하고, 실험 실행기로 같은 조건에서 측정해 비교 보고서를 만든다. 새 알고리즘이 아니라 "명세대로 구현·공정 측정·재현" 을 확인하는 운영 흐름 시험이다. B 가 A 보다 나쁘거나 같아도 사실대로 기록하면 완료다.

**완료 조건 (03 §4 완료 기준 7개 → 이 계획에서의 증거):**

| 03 §4 | 증거 | 어디서 |
|---|---|---|
| 1 인터페이스·경계 처리·손계산 참조값 일치, 질의 무관 오구현 실패 | `interface-contract-v1`, `method-behavior-v1` pass | search-module, integrate·report 에서 재검증 |
| 2 모든 평가 질의에서 결과 생성, hit@3 계산 | `runner-consistency-v1` pass (검증기가 평가 질의로 실행기를 돌려 재생성·재계산 비교) | integrate |
| 3 준비 실행 후 반복 측정 중앙값 | `latency.json` 형식(raw-results.md §3)과 `runner-consistency-v1` | experiment-runner → integrate |
| 4 보고서 수치 = 재계산 수치, 원시 결과 = 직접 호출 재생성 | `runner-consistency-v1`; 최종 보고서의 숫자는 `str()` 전사 규칙 + 사람 검토 (등록 검사 없음, §6.4 제안) | integrate, report |
| 5 코드·데이터·규격·환경·명령·출력 해시 기록 | `manifest.json`(protocol_hash, 입력·출력 sha256, argv, python, platform), `INTEGRATION.md` | integrate |
| 6 깨끗한 공간 재실행 시 순위·지표 재현 | `results/dev` 와 `results/dev-rerun` 의 raw·summary 해시 일치(`INTEGRATION.md`) | integrate |
| 7 B 가 나빠도 그대로 보고 | report 지시문의 숫자 규칙과 해석 제한 | report |

**run 완료:** 실행 작업 4개가 모두 `accepted` 이고 마지막 commit(report)에서 등록 검사 4개가 pass. 그 뒤의 "Claude 최종 검토(의견)"(09 W13)는 상태를 바꾸지 않으며 이 계획의 작업이 아니다.

## 2. 사실, 가정, 확인할 점

### 2.1 확인한 사실

- protocol_hash 를 이 worktree 에서 재계산해 고정값과 일치했다. `validate.py` 자기 검사도 OK(문서 30, 질의 10).
- 등록 검사는 5개: `allowed-changes-v1`(scope), `interface-contract-v1`(methods `__init__`·a·b 요구), `method-behavior-v1`(a·b), `runner-consistency-v1`(a·b·`runner/run.py`), `orchestrator-self-tests-v1`(실행기 자체 테스트, 이 과제와 무관해 붙이지 않음).
- `config.example.json` 의 route 는 `codex-default`, `claude-implement`, `claude-review`. `strip_env` 에 `ORCH_VERIFIER_PRIVATE` 가 있다.
- 기준 commit 의 `examples/search-ab/` 에는 `methods/`, `runner/`, `tests/unit/`, `tests/runner/` 가 `.gitkeep` 만 두고 비어 있다.
- 저장소 루트 `pyproject.toml` 에 `[tool.pytest.ini_options]`(`testpaths=["tests"]`, `pythonpath=["."]`)가 있어 `examples/search-ab/` 에서 pytest 를 돌려도 rootdir 는 저장소 루트가 된다. 테스트 폴더에 `__init__.py` 가 없으면 테스트 파일 basename 이 전체에서 유일해야 하고, `tests/runner/` 에 `__init__.py` 를 두면 패키지 이름 `runner` 가 실제 `runner` 패키지와 충돌한다. → 두 작업자 지시문에 파일 이름 접두(`test_methods_*`, `test_runner_*`)와 `__init__.py`·`conftest.py` 금지를 넣었다.
- `.gitignore` 가 `runs/*` 를 제외한다. 이 계획 파일들은 `git add -f` 없이는 커밋되지 않는다.
- `run-task` 는 지금 `depends_on` 이 빈 명세만 받는다(README). 이 계획의 integrate·report 를 실행하려면 3단계(`orch run`) 가 필요하다.

### 2.2 가정

- A1. `runner-consistency-v1` 은 검증기가 평가 질의로 실행기를 **스스로** 돌려 비교하므로, 저장소에 커밋된 실행 결과 파일을 특정 경로에서 요구하지 않는다(required_artifacts 가 코드 3개뿐인 데서 추정). `results/dev/` 는 보고서와 재현 증거용이다.
- A2. P6 은 선행 작업을 **전이적으로** 본다(report 가 search-module 의 산출물에 의존하는 검사를 가져도 validate-plan 이 통과했으므로 사실로 확인됨).
- A3. 작업자(Codex)는 작업 공간에서 결과를 git commit 하며, 커밋되지 않은 변경은 결과 commit 에 들어가지 않는다(W7b 기록과 AGENTS.md 완료 보고 형식에서 추정). 지시문에 "모두 커밋" 을 넣었다.
- A4. 의존 작업(integrate, report)의 작업 공간은 선행 작업이 통합된 commit 에서 만들어지고, `allowed-changes-v1` 의 기준도 그 통합 commit 이다. 명세의 `base_commit` 은 지시대로 모두 `ce22a70` 으로 적었다(→ 확인할 점 C2).

### 2.3 확인할 점 (사람이 결정)

- C1. **(사람 검토로 수정됨, 아래 §10)** `prepare-protocol` 의 `completed_commit` 은 지시대로 `a1eed74`(짧은 해시) 이다. protocol 정정은 그 뒤 `ce22a70` 에 들어갔으므로 `completed_commit` 의 protocol 과 계획의 `protocol_hash` 는 같은 상태가 아니다. 기록상 문제가 없는지, 또 짧은 해시를 그대로 둘지 확정한다.
- C2. (A4) integrate·report 의 `allowed-changes-v1` 은 기준을 `ce22a70` 으로 잡으면 선행 작업의 `methods/`·`runner/` 변경 때문에 반드시 fail 한다. 3단계 실행기가 의존 작업의 범위 검사 기준을 "통합된 선행 commit" 으로 잡는지 확인한다. 못 잡으면 두 작업에서 `allowed-changes-v1` 을 빼는 계획 v2 가 필요하다.
- C3. (A1) `runner-consistency-v1` 이 커밋된 결과 파일을 특정 경로에서 읽는다면 그 경로를 알려 달라. 계획 v2 에서 integrate 의 `--out` 을 맞춘다.
- C4. experiment-runner 단독 채택 근거가 `allowed-changes-v1` 하나다(§6.4 제안 1·2). 이대로 가는지, 검사를 먼저 추가하는지.
- C5. integrate·report 에 코드 검사 3개를 재부착했다(통합 후 재검증, 최종 commit 에 모든 pass). 검증 시간이 늘어도 되는지, 줄일지.
- C6. report 의 route 는 지시대로 `codex-default` 다. 09 W13 의 "Claude 최종 검토(의견)" 는 계획 밖 단계로 두었다. 언제 누가 하는지.
- C7. `runs/*` 가 gitignore 라 이 계획을 버전 관리하려면 `git add -f runs/pilot-001` 이 필요하다.
- C8. W11(작업자 1명) 에서는 experiment-runner 가 search-module 뒤에 돌아 작업 공간에 실제 `methods/` 가 있을 수 있다. 지시문은 그 경우에도 가짜 모듈로 테스트하고 `methods/` 를 건드리지 않게 썼다. W11 에서 integrate 가 별도 작업으로 도는지, 사람이 대신 하는지 정한다.
- C9. 재시도(attempt 2) 때 작업자에게 넘길 피드백의 범위: 검사 ID·verdict·reason 만 넘기고 검증기 stdout 은 관리자가 비공개 값이 없음을 확인한 뒤에만 넘긴다는 규칙을 §6.2 에 적었다. 동의하는지.

## 3. 작업별 명세와 선행 관계

| task_id | 상태/route | depends_on | allowed_write_paths | deliverables | checks |
|---|---|---|---|---|---|
| `prepare-protocol` | completed (`ce22a70`) | — | — | `examples/search-ab/protocol/`, `examples/search-ab/data/dev/` | — |
| `search-module` | codex-default | prepare-protocol | `examples/search-ab/methods/`, `examples/search-ab/tests/unit/` | `methods/__init__.py`, `methods/a.py`, `methods/b.py`, `tests/unit/test_methods_search.py` | interface-contract-v1, method-behavior-v1, allowed-changes-v1 |
| `experiment-runner` | codex-default | prepare-protocol | `examples/search-ab/runner/`, `examples/search-ab/tests/runner/` | `runner/__init__.py`, `runner/run.py`, `tests/runner/test_runner_run.py` | allowed-changes-v1 |
| `integrate` | codex-default | search-module, experiment-runner | `examples/search-ab/results/` | `results/INTEGRATION.md`, `results/dev/` 6개, `results/dev-rerun/` 6개 (파일 13개 명시) | interface-contract-v1, method-behavior-v1, runner-consistency-v1, allowed-changes-v1 |
| `report` | codex-default | integrate | `examples/search-ab/report/` | `report/REPORT.md` | interface-contract-v1, method-behavior-v1, runner-consistency-v1, allowed-changes-v1 |

(표의 deliverables 는 `examples/search-ab/` 를 생략해 적었다. 명세 파일에는 저장소 루트 기준 전체 경로가 있다.)

작업 수는 지시의 5개 그대로다. 더 쪼개거나 합치지 않은 이유:
- search-module 을 A/B 로 나누면 통합 시점에 규격 불일치가 드러나지 않는다(03 §4 v0.5 분할 근거). 실행기와 모듈로 나누는 현재 분할이 이 실험의 핵심이다.
- integrate 를 생략하고 report 에 합치면 "실행기가 모듈을 import 해 완주" 라는 통합 증거와 보고서 작성이 한 commit 에 섞여, 실패 원인(통합 vs 보고서)을 가르기 어렵다.
- integrate 가 코드를 고치는 작업이 될 수는 없다. P5(쓰기 경로 소유자 1명)가 `methods/`·`runner/` 를 다른 작업에 주는 것을 막는다. 그래서 integrate 는 "실행·기록만" 하고, 고쳐야 하면 계획 v2 다(§6.2).

deliverables 를 디렉터리가 아니라 파일로 적은 이유: 디렉터리는 기준 commit 에 이미(`.gitkeep` 과 함께) 있어 산출물 존재 검사가 무의미해진다. 테스트 파일 이름을 하나 고정해 "테스트가 하나는 있다" 를 존재 검사로 잡았다.

## 4. 공통 규격과 공통 파일 담당

| 공통 파일 | 담당 | 이 run 에서 |
|---|---|---|
| `examples/search-ab/protocol/` (README, data-format, search-interface, raw-results, validate.py) | prepare-protocol(완료). 사람 | 고정. 해시 `ea86…`. 작업자는 읽기만, 변경 필요 시 제안만 |
| `examples/search-ab/data/dev/` | prepare-protocol(완료). 사람 | 고정. 모든 작업의 입력 |
| `checks/*.json`, `verifier/`, 비공개 폴더 | W9 / 관리자 | 어느 작업도 쓰지 않고 읽지 않는다. 지시문에 금지를 명시 |
| `examples/search-ab/tests/` 바로 아래 | 소유자 없음 | 아무도 파일을 두지 않는다(`conftest.py` 포함). 두 하위 폴더만 각 작업자 소유 |
| `runs/pilot-001/` | 기획자 → 사람 | 계획. 실행 중 변경하지 않는다. 바뀌면 v2 |

공통 규격의 핵심(지시문에서 절 번호로 참조): 함수 `search(query, documents, k)`, 토큰화 `set(s.lower().split())`, A `|Q∩D|`(int), B `|Q∩D|/|Q∪D|`(float, 합집합 비면 0.0), 정렬 `(-score, doc_id)`, 반환 길이 `min(k, n)`, 오류는 search-interface.md §3 네 가지만. 실행기 호출 규약은 §7, 출력 파일 6개는 raw-results.md §2~§6.

## 5. 병렬과 순서

```
prepare-protocol (completed)
   ├── search-module      ─┐   동시 실행 가능 (W12: Codex 2개 / W11: 한 작업자가 순서대로)
   └── experiment-runner  ─┘
               ↓ 두 작업 모두 accepted → 직렬 통합(실행기)
           integrate       (통합 commit 에서 실행·기록, 코드 수정 없음)
               ↓ accepted
           report
```

- search-module ∥ experiment-runner: 쓰기 경로가 겹치지 않고 서로 import 하지 않는다. 실행기 작업자는 가짜 `methods` 로 테스트한다.
- integrate 는 두 선행이 모두 accepted 이고 통합된 뒤에만 시작한다. report 는 integrate accepted 뒤.
- W11(작업자 1명)에서는 search-module → experiment-runner 순(09 W11)으로 같은 명세를 쓴다.

## 6. 고정할 검증 기준, 실패 처리, 중단 조건

### 6.1 검증 기준 (고정)

- 채택은 등록 검사 pass 만으로 결정한다. 작업자 보고·검토 의견은 상태를 바꾸지 않는다.
- 검사 정의와 검증기는 이 run 중 바꾸지 않는다. 기존 실패를 통과시키려고 기준을 완화하지 않는다.
- 작업별 검사는 §3 표. `runner-consistency-v1` 은 methods 와 runner 가 모두 있는 integrate·report 에만 붙인다. `interface-contract-v1` 은 `methods/__init__.py` 를 요구하므로 search-module 산출물에 넣었다.
- 재현 기준: `raw-a.jsonl`, `raw-b.jsonl`, `summary.json` 의 바이트 일치. `latency.json`·`manifest.json` 은 실행마다 달라도 된다(raw-results.md §6).
- 숫자 전사 기준: 보고서(실행기의 `report.md`, 최종 `REPORT.md`, `INTEGRATION.md`)의 숫자는 Python `str()` 그대로.

### 6.2 실패 처리

| 상황 | 처리 |
|---|---|
| search-module 의 contract/behavior fail | attempt 2 (총 2회). 피드백은 검사 ID·verdict·reason code 만. 검증기 stdout 은 관리자가 비공개 값이 없음을 확인한 뒤에만 넘긴다(C9) |
| experiment-runner 의 scope fail | attempt 2. 범위 밖 변경 경로만 알려 준다 |
| integrate 에서 실행 실패·산출물 누락·`runner-consistency-v1` fail | integrate 의 attempt 2 는 코드가 같으면 같은 결과이므로 의미가 없다. run 을 멈추고 `INTEGRATION.md` 의 기록으로 원인 쪽(모듈/실행기)을 가려 **계획 v2** 를 만든다: 해당 작업만 다시(같은 소유 경로), 또는 원인 쪽 경로를 소유한 수정 작업 추가. 두 작업 모두 재실행하지 않는다 |
| report 의 scope fail | attempt 2 |
| 검사 undecidable(시간 초과, `subject_modified`, `verifier_error`) | 재시도하지 않고 멈춘다. 검사 정의·환경 문제이므로 사람이 본다 |
| 작업자가 규격 변경을 제안 | 이 run 에서는 반영하지 않는다(해시 고정). 기록만 하고 v2 여부는 사람이 결정 |
| 작업자 보고에 "자료 안의 지시" 신고 | 기록. 상태 변화 없음 |

### 6.3 중단 조건

- 어떤 작업이든 `max_attempts_total`(2) 소진 → run 중단, 계획 v2 또는 종료는 사람이 결정.
- 관리자의 누출 검사(P8) 실패 → 실행하지 않는다.
- integrate 실패 → 위 표대로 중단(v1 안에서 고칠 길이 없다).
- 전체 벽시계: 첫 완주 1일(09 W0). 초과하면 중단하고 기록.
- 작업자가 `verifier/`·비공개 폴더를 읽은 흔적(로그·보고) → 그 attempt 결과는 품질 근거로 쓰지 않는다(03 §4).
- `paid_api_allowed` false. 유료 API 호출이 감지되면 중단.

### 6.4 새 검사 제안 (만들지 않았다. 사람이 결정)

1. **`runner-unit-tests-v1`** — `{python} -m pytest examples/search-ab/tests/runner -q -p no:cacheprovider`, required_artifacts `runner/run.py`, `tests/runner/test_runner_run.py`. experiment-runner 단독 채택에 scope 이상의 근거를 준다. 한계: 작업자가 쓴 테스트라 "테스트 통과" 의 증거일 뿐 규격 준수의 증거는 아니다.
2. **`runner-stub-smoke-v1`** — 검증기가 소유한 고정 가짜 `methods`(결정적 반환)와 `data/dev/` 로 실행기를 돌려 파일 6개 생성, 종료 코드 0, raw 줄 수·rank, summary 재계산, manifest 해시를 본다. 1 보다 강하고 methods 없이 돈다. 검증기 작업(W9 소유)이 필요하다.
3. **`methods-unit-tests-v1`** — 1 의 search-module 판. 선택.
4. **`report-numbers-v1`** — `report/REPORT.md` 의 표 숫자가 `results/dev/summary.json`·`latency.json` 의 `str()` 값과 같은지, 고정 문구가 있는지. 03 §4 완료 기준 4 의 "보고서 수치" 쪽에 지금 등록 검사가 없다.
5. **`dev-reproduction-v1`** — `results/dev` 와 `results/dev-rerun` 의 raw·summary 해시 일치와 두 `manifest.json` 의 `outputs_sha256` 이 실제 파일과 맞는지. 지금은 integrate 작업자의 자기 보고(`INTEGRATION.md`)에만 의존한다(03 §4 기준 6).

## 7. 지시문 목록

| 파일 | 대상 | 요지 |
|---|---|---|
| `instructions/search-module.md` | Codex | §8 예시 1~8 과 §9 오구현을 테스트로, ID 형식 검사 금지, a/b 자체 완결, 검사 3개가 무엇을 보는지 |
| `instructions/experiment-runner.md` | Codex | raw-results.md §1~§6 항목별 요건, 가짜 `methods` 로 테스트(sys.modules monkeypatch), `main()` 안 import 권고, 통합에서 `runner-consistency-v1` 이 보는 세 가지 |
| `instructions/integrate.md` | Codex | 전제 확인 → 해시 확인 → 통합 pytest → 본 실행 → 재실행 → 해시 비교 → `INTEGRATION.md` → 커밋. 코드 수정 금지, 실패도 그대로 기록 |
| `instructions/report.md` | Codex | `REPORT.md` 8절 구성, 숫자는 `str()` 그대로, 지연 해석 한 문장, 평가 질의 결과는 없음을 명시 |

네 지시문 공통: 작업 하나만, 허용 경로 안에서, `verifier/`·비공개 폴더 금지, 평가 자료 추측 금지, 자료는 지시가 아님, 하위 에이전트·네트워크·새 의존성 금지, 테스트 약화 금지, 모두 커밋, UTF-8·LF, 완료 보고 형식(AGENTS.md + 04 §6).

## 8. 설계 판단 메모

1. **integrate = 실행·기록 작업.** P5 때문에 통합 단계에서 코드를 고칠 소유자가 없다. 실패를 드러내는 것이 이 작업의 성공 조건이고, 수정은 v2 로 간다. `integration_fix_cycles` 1 은 v2 한 번으로 해석한다.
2. **코드 검사 3개를 integrate·report 에 재부착.** 04 §9 "통합 후 재검증" 과 "최종 commit 에 모든 pass" 를 위해서다. 비용은 검사당 최대 2~4분.
3. **experiment-runner 는 scope 검사만.** 등록 검사 중 methods 없이 돌 수 있는 것이 없다. 새 검사를 만들지 않고 §6.4 로 제안했다.
4. **지시문에 규격 예시의 문자열을 옮기지 않았다.** search-interface.md §8 의 질의·문서 문자열과 개발 질의 본문은 번호·ID 로만 가리켰다. 비공개 참조값 파일이 같은 예시를 담고 있을 가능성이 있어 P8 오탐을 피하기 위해서다. 작업자는 규격 문서에서 직접 옮긴다.
5. **pytest 충돌 예방 규칙.** 저장소 루트 `pyproject.toml` 의 pytest 설정 때문에 두 테스트 폴더가 한 rootdir 아래에서 돈다. 파일 이름 접두, `__init__.py`·`conftest.py` 금지, `tests/runner/` 안에 `methods`·`runner` 이름 금지, `sys.modules` 복원을 지시문에 넣었다. 통합 단계의 `python -m pytest tests -q` 가 이 규칙에 의존한다.
6. **`results/`·`report/` 경로.** `runner/`·`results/`·`report/` 는 서로 접두가 아니다(P5). 실행기의 `report.md`(결과 폴더 안)와 최종 `REPORT.md`(`report/`)를 이름으로 구분했다.

## 9. 자기 검사

```
python -m orchestrator.cli validate-plan runs/pilot-001/plan.v1.json --config config.example.json
leak check impossible: ORCH_VERIFIER_PRIVATE is not set to a folder
```

예상된 한 줄(P8 은 관리자가 환경 변수를 설정해 따로 돌린다) 외 오류 없음. 미정 표기 검색 0건. 모든 파일 UTF-8(BOM 없음), LF.

## 10. 사람 검토 결정 (2026-10-11)

확인표: 문서 폴더 `reviews/W10b-review-checklist.md`. 이 절은 Claude Code(관리자)가 사용자 답변을 받아 추가했다. 위 1~9절의 기획자 본문은 그대로 둔다.

- **C1 수정 (사용자 결정):** `prepare-protocol` 의 `completed_commit` 을 protocol 정정 commit 의 전체 해시 `ce22a70ccad5f518e74c85228f294690e1386710` 로 바꿨다. 계획의 `protocol_hash` 와 같은 규격 상태다. 처음의 `a1eed74` 는 기획자 지시문의 오기였다.
- **C2·C8 → W10c (사용자 동의):** 지금 `run-task` 는 `depends_on` 이 있는 명세를 거부하고, 의존 작업의 작업 공간·범위 검사 기준을 통합 commit 으로 잡지 못한다. W11 전에 코드 작업 W10c(계획 실행: 위상 순서 실행, 선행 결과 직렬 `--no-ff` 통합, 통합 commit 을 의존 작업의 기준으로)를 넣는다. 이 계획은 W10c 를 전제로 하며 수정하지 않는다.
- **C3 해결:** 관리자가 검증기 코드로 확인했다. `runner-consistency-v1` 은 실행기를 임시 폴더에 스스로 돌려 비교하고, 커밋된 결과 파일을 읽지 않는다. 가정 A1 은 사실이다.
- **C4~C7, C9:** 확인표의 관리자 의견(C4 v1 그대로, C5 유지, C6 계획 밖, C7 `git add -f` 로 커밋함, C9 동의)에 사용자가 이견을 밝히지 않았다. 명시 결정은 아니며, 바꾸려면 계획 v2 다.
