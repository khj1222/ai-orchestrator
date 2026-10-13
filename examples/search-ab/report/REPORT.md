# 검색 A/B 개발용 실험 최종 보고서

## 목적과 범위

고정된 소형 문서 모음에서 두 기준 방법을 규격대로 구현하고 같은 조건으로 측정해 재현을 확인한 실험이다. 새 알고리즘 제안이 아니며, 이 자료는 성능 일반화를 위한 표본이 아니다. 이 보고서는 통합 작업이 남긴 개발용 결과를 옮기며 실행기나 검색 모듈을 다시 실행하지 않았다.

## 설정

아래 경로는 `examples/search-ab/`를 기준으로 한다.

| 항목 | 값 | 출처 |
|---|---|---|
| base commit | `04935d6f54a475e6906d9be8c851a878c1f80313` | 작업 지시문 |
| protocol_hash | `ea86373c26e72d61f9ca6a15e5636ea96e8496d0c3c2bd0e6bbc1a21e512b624` | `results/dev/manifest.json`의 `protocol_hash` |
| 자료 | `data/dev/documents.jsonl`, `data/dev/queries.jsonl` | `results/dev/manifest.json`의 `argv` |
| 문서 수 | 30 | `data/dev/README.md` |
| 질의 수 | 10 | `results/dev/summary.json`·`latency.json`의 `queries`, `data/dev/README.md` |
| k | 3 | `results/dev/summary.json`의 `k` |
| warmup | 1 | `results/dev/latency.json`의 `warmup` |
| repeats | 5 | `results/dev/latency.json`의 `repeats` |
| python | `3.10.11` | `results/dev/manifest.json`의 `python` |
| platform | `Windows-10-10.0.26300-SP0` | `results/dev/manifest.json`의 `platform` |

문서는 프로젝트를 위해 작성한 짧은 한국어 생활 정보이며, 개발용 질의에는 사람이 정한 관련 문서 ID가 있다. 내용의 사실 정확성을 보증하는 자료는 아니다.

작업 디렉터리 `examples/search-ab/`에서 통합 작업이 실행한 명령은 `results/dev/manifest.json`의 `argv`를 순서대로 옮긴 다음과 같다.

```text
python -m runner.run --documents data/dev/documents.jsonl --queries data/dev/queries.jsonl --out results/dev
```

명령에서 생략된 인수의 실제 값은 위 설정 표에 기록했다. 지연 측정은 한 방법으로 모든 질의를 순서대로 호출하는 실행을 단위로 하며, `time.perf_counter`로 첫 호출 직전부터 마지막 호출 직후까지 재고 파일 읽기·쓰기는 제외한다. A의 준비 실행과 측정을 마친 다음 B도 같은 방식으로 측정한다.

## 방법

토큰화는 `set(s.lower().split())`로 유니코드 소문자화 후 연속된 유니코드 공백을 기준으로 분리하고 반복 토큰은 한 번만 센다. 구두점을 떼지 않아 `방법.`과 `방법`이 다르고, 한국어 조사가 붙은 `원두는`과 `원두`도 다르며, 형태소 분석·어간 추출·유니코드 정규화는 하지 않는다(입력 자료가 NFC라고 가정한다).

질의 토큰 집합을 Q, 문서 토큰 집합을 D라고 할 때 A의 점수는 `len(Q & D)`이며 반환 점수는 정수다. B의 점수는 `len(Q & D) / len(Q | D)`로 Python 나눗셈의 부동소수점 값을 반올림하지 않고 사용하며, 합집합이 비면 영점의 부동소수점 값을 반환한다.

두 방법 모두 `(-score, doc_id)`로 정렬하므로 점수 내림차순이며 동점은 문자열 `doc_id` 오름차순이다. 영점 문서도 순위에 포함하며, 앞에서 `min(k, len(documents))`개를 반환한다. k는 bool을 제외한 음이 아닌 정수이고, k가 영이거나 문서 목록이 비면 빈 목록을 반환한다. 빈 질의는 모든 문서가 영점이므로 `doc_id` 오름차순을 따른다.

구현을 읽어 확인한 바로는 `methods/a.py`와 `methods/b.py`가 입력을 검사한 뒤 이 점수와 정렬 규칙을 적용한다. `runner/run.py`는 반환 순서와 길이를 유지하며 `rank`만 붙이고, 지연 측정과 별개의 첫 본 실행 결과를 기록한다.

## 결과

다음 표와 고정 문구는 `results/dev/report.md`에서 그대로 옮겼다. JSON 파일을 `json.load`로 읽고 각 값을 Python `str()`로 바꿔 표의 각 칸과 대조했으며 모두 같음을 확인했다. 반올림, 퍼센트 변환, 유효숫자 조정, 단위 변환 또는 새 지표 계산은 하지 않았다.

| 방법 | hit@3 | hits/queries | 결과 없는 질의 |
|---|---|---|---|
| A | 1.0 | 10/10 | 0 |
| B | 1.0 | 10/10 | 0 |

| 방법 | 지연 중앙값 (ms, 질의 10개 1회 실행) | 측정 횟수 |
|---|---|---|
| A | 0.434 | 5 (준비 1) |
| B | 0.637 | 5 (준비 1) |

토큰화는 대소문자 정규화 후 공백 분리이며 한국어 형태소 분석을 하지 않았다. 지연은 같은 환경의 반복 측정 중앙값이며 작은 자료의 차이를 일반화하지 않는다.

첫 표에서 A·B 행의 `hit@3`는 `summary.json`의 `a.hit_at_3`·`b.hit_at_3`, `hits/queries`는 각 방법의 `hits`와 공통 `queries`, 결과 없는 질의는 각 방법의 `no_result_queries`와 대조했다. 둘째 표의 중앙값은 `latency.json`의 `a.median_ms`·`b.median_ms`, 측정 횟수와 준비 횟수는 `repeats`·`warmup`, 머리글의 질의 수는 `queries`와 대조했고 `summary.json`의 질의 수와도 같았다.

hit@3는 상위 3개 안에 `relevant_doc_ids`에 속한 문서가 하나라도 있는 질의의 비율이며, `hits`는 해당 질의 수이고 분모는 전체 질의 수다. 결과가 부족하면 있는 결과만 보고, 결과가 없는 질의는 실패로 세며 `no_result_queries`에도 포함한다. 관련성은 질의 파일의 관련 문서 ID로만 판단한다. 이 지표는 명세대로 구현하고 공정하게 측정하여 재현을 확인하기 위한 용도다.

## 재현

`results/dev`와 `results/dev-rerun`의 각 `manifest.json`에 있는 `outputs_sha256`을 비교했고, 아래 대상 모두 `results/INTEGRATION.md`의 실제 바이트 해시 비교 기록과 일치했다. 보고서 작성 시에도 해당 파일의 바이트 해시와 manifest 값을 대조했다.

| 파일 | dev SHA-256 | dev-rerun SHA-256 | 비교 |
|---|---|---|---|
| `raw-a.jsonl` | `11e767acaf0f6e549b8a64e1de85be13ee15769525338883f4fd0e8e0cd632a6` | `11e767acaf0f6e549b8a64e1de85be13ee15769525338883f4fd0e8e0cd632a6` | 일치 |
| `raw-b.jsonl` | `cb363cfba38f91e2cc5db531ccd1ff80257f047b2dbd1988bc5d02c6982afe3c` | `cb363cfba38f91e2cc5db531ccd1ff80257f047b2dbd1988bc5d02c6982afe3c` | 일치 |
| `summary.json` | `10298c0e7f59952bb165679b4bc8a1976d6b6f743f495412648a64c0d58b38e2` | `10298c0e7f59952bb165679b4bc8a1976d6b6f743f495412648a64c0d58b38e2` | 일치 |

규격에 따른 재현 대상은 위 원시 결과와 요약 파일이며 모두 같다. `latency.json`은 일치를 요구하지 않으며 실제 해시는 서로 다르다. `manifest.json`은 실행별 차이가 허용되고, 지연 중앙값을 포함하는 `report.md` 역시 통합 기록에서 서로 다르다. 재실행의 지표 수치는 결과 표나 해석에 사용하지 않았다.

## 해석과 한계

개발용 질의 기준 hit@3는 A가 1.0, B가 1.0이며 서로 같다. 지연 해석은 결과 절의 고정 문구에 한정한다.

공백 분리 토큰화는 형태소 분석을 하지 않아 조사가 붙은 어절을 다른 토큰으로 취급한다. 따라서 의미상 관련 있는 문서라도 토큰이 겹치지 않아 낮은 점수를 받을 수 있다. 자료가 작고 수치는 개발용 질의 기준이며, 관련 문서는 작성자의 의미 판단에 따른다. **평가용 질의의 결과는 이 보고서에 없고 검증기가 따로 계산한다.**

## 산출물 목록과 해시

다음은 `results/dev/manifest.json`의 `outputs_sha256`을 그대로 옮긴 것이다.

| 산출물 | SHA-256 |
|---|---|
| `results/dev/raw-a.jsonl` | `11e767acaf0f6e549b8a64e1de85be13ee15769525338883f4fd0e8e0cd632a6` |
| `results/dev/raw-b.jsonl` | `cb363cfba38f91e2cc5db531ccd1ff80257f047b2dbd1988bc5d02c6982afe3c` |
| `results/dev/latency.json` | `0d61b82adbf18f006af9b89b68e14204026a31ee8e030a93560b182f5ebcf13b` |
| `results/dev/summary.json` | `10298c0e7f59952bb165679b4bc8a1976d6b6f743f495412648a64c0d58b38e2` |
| `results/dev/report.md` | `a06f739ab7c3d3fc4445e067ac7b8c8969c3150fad8a052a7fa4382b68a6aa18` |

입력 자료 해시는 같은 manifest의 `documents_sha256`·`queries_sha256` 값이다.

| 입력 | manifest 키 | SHA-256 |
|---|---|---|
| `data/dev/documents.jsonl` | `documents_sha256` | `14fdc65bc21e6aa8ceb91378611eac63d4b41925e1c57e1fbf4260f60db4c5fd` |
| `data/dev/queries.jsonl` | `queries_sha256` | `3056ad34e84df4c392d5bcee1b2ef6615f58c74c9a2d2120422100693034fd40` |

실행 기록은 `results/dev/manifest.json`, 재현 비교 자료는 `results/dev-rerun/`, 통합 기록은 `results/INTEGRATION.md`, 이 최종 보고서는 `report/REPORT.md`다. manifest 자체는 `outputs_sha256`의 대상이 아니다.

## 미해결 문제와 제안

`results/INTEGRATION.md`에 기록된 문제와 처리 상태는 다음과 같다.

- 검색 모듈과 실행기 사이의 규격 해석 차이는 통합 테스트·실행·해시 비교에서 발견하지 못했다. 별도 검증기의 직접 호출 재생성 검사는 실행하지 않았으므로 `runner-consistency-v1` 통과를 주장하지 않는다.
- `tasks/integrate.json`이 없어 `ObjectNotFound` / `PathNotFound` 오류가 발생했고 사용자 지시문의 상세 명세로 진행했다. 이번 보고서 작업에서도 지정된 `tasks/report.json`이 없어 제공된 작업 지시문을 기준으로 작성했다.
- 조회 도구 `rg`가 없어 `CommandNotFoundException`이 발생했으며 Python `pathlib`로 지정 범위만 조회했다. 의존성은 추가하지 않았다.
- pytest 임시 경로를 전달할 때 Windows 역슬래시가 옵션 파싱에서 제거된 것으로 추정되는 작업자 래퍼 오류로 허용 범위 밖에 임시 파일이 생성되었다. 통합 작업에서 해당 임시 폴더만 삭제하여 원상복구했으며 개발용 결과는 수정하거나 재생성하지 않았다. 후속 실행에서는 임시 경로를 `Path.as_posix()`로 전달하자는 제안이 기록되어 있다.
- 통합 커밋 준비 중 Git 메타데이터의 `index.lock` 생성 권한 오류는 권한 확장 후 처리했고, 통합 기록 끝의 빈 줄로 발생한 `git diff --cached --check` 오류는 그 빈 줄만 정리했다. 실행기 출력은 바꾸지 않았다.

통합 기록에 따르면 본 실행과 재실행의 stderr는 빈 문자열이었으며 실행 오류는 없었다. 위 기록 외에 규격·검사 변경 제안은 없다. 이 보고서는 후보 제출이며 채택 여부는 별도 검증이 결정한다.
