# 검색 A/B 실험 규격 (protocol v1)

작성: 2026-09-20 (W8). 이 폴더는 첫 실험(03 §4 "고정된 소형 문서 모음에서 두 검색 방법을 구현하고 동일 조건의 비교 보고서를 만든다")에서 **검색 모듈 작업자와 실험 실행기 작업자가 공통으로 따르는 규격**이다. 규격은 작업 시작 전에 고정되며, 작업자는 규격을 바꾸지 않고 바꿔야 한다고 판단하면 제안만 보고한다.

| 파일 | 내용 | 읽는 사람 |
|---|---|---|
| [data-format.md](data-format.md) | 문서·질의 파일(JSONL) 형식, ID 규칙, 개발/평가 자료의 분리 | 모두 |
| [search-interface.md](search-interface.md) | `search(query, documents, k)` 규격: 토큰화, 방법 A·B 점수, 동점, k, 오류, 반환 형식, 손계산 예시 | 검색 모듈 작업자(구현), 실행기 작업자(호출 규약만) |
| [raw-results.md](raw-results.md) | 실행기의 명령 인터페이스, 원시 결과 JSONL, 지연 측정, 요약 지표와 보고서 표 | 실행기 작업자, 검증기 |
| [validate.py](validate.py) | 규격 문서와 개발 자료의 자기 검사, `protocol_hash` 계산 | 사람, 계획기 |

## 폴더 구조와 소유자

```
examples/search-ab/
  protocol/      규격 (이 폴더). W8에서 고정. 작업자는 읽기만.
  data/dev/      개발용 문서 30개·질의 10개. 공개. 작업자는 읽기만.
  methods/       검색 모듈 작업자 소유. a.py, b.py, __init__.py
  tests/unit/    검색 모듈 작업자 소유
  runner/        실험 실행기 작업자 소유
  tests/runner/  실험 실행기 작업자 소유
```

평가용 질의·정답·참조값은 이 폴더에도 저장소의 작업자 접근 경로에도 두지 않는다(W9, 저장소 루트 `verifier/`).

## 실행 환경

- Python 3.10 표준 라이브러리만. 외부 패키지·모델 API·네트워크 없음.
- 작업 디렉터리는 `examples/search-ab/`이다. 모듈 import는 이 디렉터리를 기준으로 한다(`from methods import a`, `python -m runner.run`).
- 파일은 UTF-8(BOM 없음), 줄 끝 LF.

## protocol_hash

작업 명세(04 §2)의 `protocol_hash`는 이 폴더의 모든 파일(`__pycache__` 제외)을 상대 경로 순으로 정렬해 `경로 + "\n" + 파일 바이트 + "\n"`을 이어 붙인 것의 SHA-256 16진수다. 계산은 `python protocol/validate.py --hash`. 규격 파일이 한 글자라도 바뀌면 해시가 바뀌므로, 작업자가 받은 규격과 검증기가 쓴 규격이 같은지 이 값으로 확인한다.

## 자기 검사

```bash
python examples/search-ab/protocol/validate.py
```

규격 문서에 미결 표기(validate.py 의 UNRESOLVED_MARKERS 목록)가 없는지, 개발 자료가 형식을 지키는지, 문서 ID가 중복되지 않는지, 질의의 관련 문서 ID가 모두 존재하는지 검사한다. 이것은 규격 자체의 검사이며 검색 모듈이나 실행기의 합격 판정이 아니다(합격 판정은 W9의 검증기가 한다).
