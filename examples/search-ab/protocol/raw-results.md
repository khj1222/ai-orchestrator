# 실행기 출력 규격: 원시 결과·지연·요약 (protocol v1)

실험 실행기(`runner/`)는 문서·질의를 읽고 두 방법을 실행해 아래 파일을 만든다. 실행기는 개발용 도구이며 검증기가 아니다. 검증기(W9)는 같은 입력으로 검색 모듈을 직접 호출해 원시 결과를 다시 만들고 실행기의 파일과 비교한다. 따라서 **실행기는 결과를 가공하지 않고 받은 그대로 기록한다.**

## 1. 명령 인터페이스

```bash
python -m runner.run --documents <documents.jsonl> --queries <queries.jsonl> --out <dir> [--k 3] [--warmup 1] [--repeats 5]
```

| 인수 | 기본값 | 의미 |
|---|---|---|
| `--documents` | 필수 | data-format.md §2 형식의 파일 |
| `--queries` | 필수 | data-format.md §3 형식의 파일 |
| `--out` | 필수 | 출력 폴더. 없으면 만들고, 이미 파일이 있으면 시작하지 않고 오류(덮어쓰지 않음) |
| `--k` | `3` | 질의당 반환 개수. 요약 지표는 상위 3개를 보므로 `3` 미만은 오류 |
| `--warmup` | `1` | 지연 측정 전 버리는 준비 실행 횟수 (0 이상) |
| `--repeats` | `5` | 지연 측정 실행 횟수 (1 이상) |

- 작업 디렉터리는 `examples/search-ab/`. 표준 라이브러리만.
- 종료 코드 `0`은 아래 파일이 모두 만들어졌을 때만. 입력 오류(data-format.md §5)·`ValueError`·파일 쓰기 실패는 `0`이 아닌 종료 코드와 stderr 메시지. 부분 출력이 남아 있어도 성공으로 보지 않는다.
- stdout에는 요약 표(§5)만 출력한다. 질의별 결과는 stdout이 아니라 파일에 쓴다.

## 2. 원시 결과 `raw-a.jsonl`, `raw-b.jsonl`

방법마다 파일 하나. 질의 파일의 줄 순서대로 한 줄에 하나:

```json
{"method": "a", "query_id": "q01", "k": 3, "results": [{"rank": 1, "doc_id": "d001", "score": 4}, {"rank": 2, "doc_id": "d005", "score": 1}, {"rank": 3, "doc_id": "d002", "score": 1}]}
```

| 키 | 규칙 |
|---|---|
| `method` | `"a"` 또는 `"b"`. 파일 이름과 일치 |
| `query_id` | 질의 파일의 값 그대로 |
| `k` | 호출에 쓴 `k` |
| `results` | `search()`가 돌려준 목록을 **순서·길이 그대로**, 각 원소에 `rank`(1부터 연속 정수)를 붙인 것. `score`는 돌려받은 값 그대로(A는 정수, B는 부동소수점). JSON 직렬화는 `json.dumps(obj, ensure_ascii=False)` 기본 설정(부동소수점은 Python `repr`) |

원시 결과는 지연 측정과 별개로 **한 번** 실행한 결과다(§3의 반복 실행 결과를 쓰지 않는다. 결정적 함수이므로 값은 같아야 하지만, 기록은 명시적으로 첫 본 실행 것으로 한다).

## 3. 지연 `latency.json`

```json
{
  "unit": "ms",
  "timer": "time.perf_counter",
  "warmup": 1,
  "repeats": 5,
  "queries": 10,
  "a": {"runs_ms": [1.9, 1.8, 1.8, 1.9, 1.8], "median_ms": 1.8},
  "b": {"runs_ms": [2.3, 2.2, 2.2, 2.3, 2.2], "median_ms": 2.2}
}
```

- 한 "실행"은 한 방법으로 질의 파일의 **모든 질의**를 순서대로 `search()`하는 것이다. 시간은 첫 호출 직전부터 마지막 호출 직후까지(`time.perf_counter`, 밀리초). 파일 읽기·쓰기 시간은 넣지 않는다.
- 방법 A의 준비 실행 `warmup`회 → 측정 `repeats`회, 그 다음 방법 B를 같은 방식으로. 두 방법을 번갈아 재지 않는다.
- `median_ms`는 `statistics.median(runs_ms)`. 개별 값은 소수점 아래 3자리로 반올림해 기록한다.
- 작은 자료에서의 미세한 차이는 일반화하지 않는다. 보고서에는 중앙값만 표로 쓰고 해석은 한 문장 이내로 제한한다.

## 4. 요약 지표 `summary.json`

```json
{
  "k": 3,
  "queries": 10,
  "a": {"hit_at_3": 0.7, "hits": 7, "no_result_queries": 0},
  "b": {"hit_at_3": 0.7, "hits": 7, "no_result_queries": 0}
}
```

- `hit_at_3`: 질의 중 **상위 3개 결과(`rank <= 3`)에 `relevant_doc_ids`의 문서가 하나라도 있는 질의**의 비율. 분모는 전체 질의 수. 결과가 3개 미만이면 있는 것만 본다. 결과가 0개인 질의는 실패로 세고 `no_result_queries`에 더한다.
- `hits`는 분자(정수). `hit_at_3 = hits / queries`를 반올림 없이 기록한다.
- 관련 문서는 질의 파일의 `relevant_doc_ids`에서만 읽는다. 실행기는 관련성을 스스로 판단하지 않는다.
- 이 지표는 검색 품질의 절대 수준이 아니라 "명세대로 구현·공정 측정·재현"을 확인하는 용도다. B가 A보다 나쁘거나 같아도 그대로 기록한다.

## 5. 보고서 표 `report.md`와 stdout

`report.md`는 아래 표 두 개와 고정 문구로 구성한다. stdout에도 같은 표를 출력한다.

```
| 방법 | hit@3 | hits/queries | 결과 없는 질의 |
|---|---|---|---|
| A | 0.7 | 7/10 | 0 |
| B | 0.7 | 7/10 | 0 |

| 방법 | 지연 중앙값 (ms, 질의 10개 1회 실행) | 측정 횟수 |
|---|---|---|
| A | 1.8 | 5 (준비 1) |
| B | 2.2 | 5 (준비 1) |
```

고정 문구: "토큰화는 대소문자 정규화 후 공백 분리이며 한국어 형태소 분석을 하지 않았다. 지연은 같은 환경의 반복 측정 중앙값이며 작은 자료의 차이를 일반화하지 않는다."

## 6. 실행 기록 `manifest.json`

```json
{
  "protocol_hash": "<protocol/validate.py --hash 의 값>",
  "documents_sha256": "<documents.jsonl 파일 바이트의 SHA-256>",
  "queries_sha256": "<queries.jsonl 파일 바이트의 SHA-256>",
  "k": 3,
  "argv": ["python", "-m", "runner.run", "..."],
  "python": "3.10.11",
  "platform": "Windows-10-...",
  "started_at": "2026-09-20T10:00:00+00:00",
  "ended_at": "2026-09-20T10:00:01+00:00",
  "outputs_sha256": {"raw-a.jsonl": "...", "raw-b.jsonl": "...", "latency.json": "...", "summary.json": "...", "report.md": "..."}
}
```

`protocol_hash`는 `protocol/` 폴더를 README.md §"protocol_hash" 규칙으로 계산한 값이며, 실행기가 직접 계산해도 되고 `validate.py --hash`를 호출해도 된다. 재현 확인은 `outputs_sha256` 중 `raw-*.jsonl`·`summary.json`의 일치로 한다(`latency.json`·`manifest.json`은 실행마다 달라질 수 있다).

## 7. 검증기가 보는 것 (참고)

검증기는 (1) 같은 입력으로 `methods.a`·`methods.b`를 직접 호출해 `raw-*.jsonl`을 재생성해 비교하고, (2) `raw-*.jsonl`과 질의 파일에서 `summary.json`을 다시 계산해 비교하며, (3) `manifest.json`의 해시가 실제 파일과 맞는지 본다. 실행기 작업자는 검증기 코드를 보지 않고 이 문서만으로 구현한다.
