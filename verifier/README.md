# 검색 A/B 검증기 (W9)

Python 3.10 표준 라이브러리만 사용한다. 후보 산출물을 실제로 실행하며 작업자의 완료 문구는 판정에 쓰지 않는다. 경로는 스크립트의 위치에서 계산하므로 현재 디렉터리가 저장소 루트가 아니어도 된다. 등록 검사 정의는 `checks/`의 세 JSON 파일이다. 기존 `allowed-changes-v1`도 작업 명세에 별도로 포함한다.

| 검사 ID | 스크립트 | 비공개 자료 | 등록 시간 제한 |
|---|---|---|---|
| interface-contract-v1 | interface_contract.py | 필요 없음 | 120초 |
| method-behavior-v1 | method_behavior.py | behavior-cases.json | 120초 |
| runner-consistency-v1 | runner_consistency.py | 아래 자료 전부 | 240초 |

## 실행과 종료 코드

```powershell
# 검증을 실행하는 셸에서, 실제 비공개 폴더를 지정한다.
$env:ORCH_VERIFIER_PRIVATE = (Resolve-Path <private-folder>).Path
python -B verifier/interface_contract.py
python -B verifier/method_behavior.py
python -B verifier/runner_consistency.py
python -B verifier/reference.py
python -m pytest tests/test_verifier.py -v
```

- 0: 모든 해당 검사 통과.
- 1: 계약 위반, 값 불일치, 잘못된 JSON/자료, 후보 파일 누락·예외. stdout에 실패 위치·사례 ID·순위·기대값을 출력한다.
- 2: `ORCH_VERIFIER_PRIVATE` 미설정/폴더 누락/필요한 비공개 파일 누락. stderr에 원인을 출력한다. W6은 이를 fail로 기록한다.

등록 검사로 실행하면 W6이 전체 검사 프로세스 트리를 시간 제한으로 관리한다. runner_consistency는 runner 자식만 죽이는 별도의 제한을 두지 않는다. 직접 실행할 때에도 정지하거나 끝나지 않는 후보를 대상으로 하려면 같은 프로세스 트리 제한을 제공해야 한다.

`interface_contract`는 private 환경 변수 없이 실행된다. import의 출력과 Python audit event로 관측되는 파일 읽기/쓰기·프로세스 실행 등의 부수 효과를 감지한다. Python import machinery가 소스·바이트코드를 읽는 것은 허용한다. 이 감지는 운영체제 샌드박스가 아니며 임의의 네이티브 코드나 고의적인 검증기 변조에 대한 방어를 보장하지 않는다.

`method_behavior`는 전체 사례를 비교하고 오류들을 모아서 실패한다. int/bool/float는 타입까지 구별한다. `reference.py`는 후보 모듈을 import하지 않으며, 독립 집합 계산과 수동 기대값을 비교한다.

`runner_consistency`는 저장소 밖 임시 폴더에 출력하게 실제 `python -m runner.run`을 실행한다. 기본 k=3, warmup=1, repeats=5 경로를 확인한다. 원시 결과를 독립 참조값·저장된 기대값·후보 검색 모듈 직접 호출과 비교한다. 요약을 원시 결과에서 다시 계산하고 보고서/표준 출력의 표, 모든 산출물 해시, 입력 해시, 규격 해시, 지연 배열과 중앙값을 확인한다. 지연의 절대 크기로 성능 합격선을 정하지 않는다. 파일 쓰기와 캐시는 검증 worktree에 남기지 않는다.

## 비공개 폴더

이 폴더는 코드 저장소 밖에 둔다. 스크립트나 JSON 정의에 개인 경로를 하드코딩하지 않는다.

| 파일 | 용도 |
|---|---|
| eval-queries.jsonl | 평가 질의 20개와 관련 문서 ID. 대상 문서는 개발용 문서 30개와 같다 |
| eval-queries-notes.md | 질의별 관련성 근거, 사용자 확인 대상 |
| behavior-cases.json | 손계산 사례와 A/B 기대값 |
| behavior-cases-worked.md | 사례별 Q, D, 교집합, 합집합, 나눗셈 근거 |
| eval-expected/raw-a.jsonl, raw-b.jsonl | 독립 참조 구현으로 생성한 기대 원시 결과 |
| eval-expected/summary.json | 기대 요약 |
| audit_inputs.py, validation-summary.json | 자료 구성·공개 테스트 사례 복사 일치 확인 및 그 결과. 검사 실행에 필수는 아님 |

비공개 데이터 원문과 전체 손계산 표는 Git에 넣지 않는다. 다만 W9 지시문에서 요구한 손계산 사례 8개는 `tests/test_verifier.py`에 복사되어 **공개 자기 검사 자료**가 된다. 이 8개는 숨겨진 평가 사례라고 주장하지 않는다. 테스트는 실제 비공개 폴더 없이 임시 자료만으로 실행된다.

**접근 분리는 아직 완성되지 않았다.** Git 밖 보관은 commit/worktree 복제에 포함되지 않게 할 뿐, 같은 OS 사용자로 실행하는 작업자의 파일 읽기를 차단하지 않는다. 또한 이 검증기 코드와 공개 테스트는 Git 안에 있어 일반 worktree에서 보인다. W10에서 작업자에게 검증기 코드를 보이지 않게 할 별도 배포/접근 정책이 필요하다. 작업자 route의 strip_env에서 `ORCH_VERIFIER_PRIVATE`를 제거하고, 별도 사용자/샌드박스 권한 분리 또는 작업자 종료 후에만 평가 자료를 제공하는 절차를 정해야 한다. 변수 제거만으로 접근 격리를 달성했다고 보지 않는다. 이 구현에서 orchestrator/ 또는 route 설정은 바꾸지 않았다.

## 손계산 사례 추가와 기대 결과 갱신

1. 규격을 읽고 기존 참조 구현 출력과 독립적으로 Q/D/I/U와 A/B 값을 손으로 적는다. `behavior-cases-worked.md`에 계산 과정을 남긴다.
2. `behavior-cases.json`에 `{case_id, query, documents, k, expected_a, expected_b}`를 추가한다. 기대값은 정렬된 `[doc_id, score]` 쌍의 목록이며 A는 정수, B는 실수다. 같은 case_id는 금지한다.
3. `python -B verifier/reference.py`로 손계산과 참조 구현이 일치하는지 확인한다. 불일치하면 둘 중 어느 쪽이 잘못됐는지 다시 계산한다. 통과시키려고 참조 출력으로 수동 기대값을 덮어쓰지 않는다.
4. 사용자 확인을 거쳐 평가 자료가 변경됐다면 `python -B verifier/reference.py --write-expected`로 eval-expected를 명시적으로 다시 만든다. 이 명령도 손계산 전체를 통과한 후에만 기록한다. 일반 검사 실행은 기대 파일을 생성하거나 수정하지 않는다.
5. 잘못된 구현을 넣어 실제로 거부되는지 자체 테스트를 추가한다. 사례 ID와 명세 위반을 확인하며, 정답 구현만 통과시키는 테스트에 그치지 않는다.

## 규격 해석과 남은 한계

- raw JSONL은 줄/질의/결과의 순서, rank, 값, 숫자 타입을 정확히 비교한다. JSON 객체 키의 삽입 순서는 규격에 고정돼 있지 않으므로 파싱 후 비교하며, 키 순서 차이를 실패로 보지 않는다. SHA-256은 실제 파일 바이트 기준으로 별도 확인한다.
- 지연은 기록된 runs_ms(각각 소수점 3자리 반올림)의 중앙값을 검사한다. 반올림 전 원시 시간의 중앙값을 기록하면 불일치할 수 있다. 호출 시점·횟수 자체의 계측 진실성은 출력 파일만으로 증명하지 못한다.
- W8 문서는 평가 자료가 저장소 루트 verifier/에 있다고 적지만, 이번 지시문의 더 최신 결정에 따라 환경 변수로 찾는 저장소 밖 폴더를 사용한다. protocol/을 고치지 않았으므로 W10에서 문서 충돌을 정리해야 한다.
- 보고서 숫자는 소수점 표기 자릿수의 차이를 허용하지만 수치와 counts는 정확히 일치해야 한다. 원시 결과에는 float 허용 오차를 두지 않는다.
- 이 작업에는 실제 검색 모듈/실행기 구현이 없다. 자체 테스트의 임시 최소 구현으로 검사기를 검증했으며 실제 W12/W13 산출물의 통과는 이후 등록 검사로 판정한다.
