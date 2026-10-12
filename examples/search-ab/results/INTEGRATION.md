# 통합 실행 기록: pilot-001 / integrate / plan v1

## 범위와 작업 공간

- 실행 전 HEAD: `8cd7f9dd222a8bf75ffe99ef1bf7e259982c5bef`
- 지시문 base_commit: `ce22a70ccad5f518e74c85228f294690e1386710`
- 작업 디렉터리: `examples/search-ab/` (별도 표시한 저장소 조회·커밋은 저장소 루트).
- 전제 파일 `methods/__init__.py`, `methods/a.py`, `methods/b.py`, `runner/__init__.py`, `runner/run.py`: 모두 존재.
- 시작 시 Git 변경 없음. `results/`는 없었으며 두 출력 폴더에 각각 한 번만 실행했다.
- `tasks/integrate.json`은 존재하지 않았다. 사용자 지시문에 제공된 명세를 기준으로 실행했다.
- Python: `Python 3.10.11`
- platform.platform(): `Windows-10-10.0.26300-SP0`
- 사용한 환경 변수 이름: `PYTHONDONTWRITEBYTECODE`, `PYTHONIOENCODING`, `PYTEST_ADDOPTS`. 값은 기록하지 않는다.
- Python 바이트코드 및 pytest 캐시 생성을 억제했다. 테스트 선택·assertion·실행기 기본 인수는 변경하지 않았다.
- 코드·규격·입력 자료는 수정하지 않았다. 네트워크, 모델 API, 하위 에이전트, verifier 및 비공개 자료를 사용하지 않았다.

## 실행 명령과 종료 코드

아래 명령은 Python 표준 라이브러리 `subprocess.run(..., capture_output=True)`으로 순서대로 실행했다. stdout과 stderr를 각각 캡처했으며 Markdown에서는 줄 끝만 LF로 표시한다.

| 명령 | 종료 코드 |
|---|---|
| `python --version` | 0 |
| `python -c "import platform; print(platform.platform())"` | 0 |
| `python protocol/validate.py --hash` | 0 |
| `python -m pytest tests -q` | 0 |
| `python -m runner.run --documents data/dev/documents.jsonl --queries data/dev/queries.jsonl --out results/dev` | 0 |
| `python -m runner.run --documents data/dev/documents.jsonl --queries data/dev/queries.jsonl --out results/dev-rerun` | 0 |

### protocol_hash

기대값과 실제 출력은 모두 `ea86373c26e72d61f9ca6a15e5636ea96e8496d0c3c2bd0e6bbc1a21e512b624`로 **일치**한다. 두 manifest의 protocol_hash도 같다.

### `python --version`

종료 코드: 0

stdout:

```text
Python 3.10.11
```

stderr: 빈 문자열.

### `python -c "import platform; print(platform.platform())"`

종료 코드: 0

stdout:

```text
Windows-10-10.0.26300-SP0
```

stderr: 빈 문자열.

### `python protocol/validate.py --hash`

종료 코드: 0

stdout:

```text
ea86373c26e72d61f9ca6a15e5636ea96e8496d0c3c2bd0e6bbc1a21e512b624
```

stderr: 빈 문자열.

### `python -m pytest tests -q`

종료 코드: 0

stdout:

```text
........................................................................ [ 27%]
........................................................................ [ 54%]
........................................................................ [ 81%]
................................................                         [100%]
```

stderr: 빈 문자열.

### `python -m runner.run --documents data/dev/documents.jsonl --queries data/dev/queries.jsonl --out results/dev`

종료 코드: 0

stdout:

```text
| 방법 | hit@3 | hits/queries | 결과 없는 질의 |
|---|---|---|---|
| A | 1.0 | 10/10 | 0 |
| B | 1.0 | 10/10 | 0 |

| 방법 | 지연 중앙값 (ms, 질의 10개 1회 실행) | 측정 횟수 |
|---|---|---|
| A | 0.434 | 5 (준비 1) |
| B | 0.637 | 5 (준비 1) |
```

stderr: 빈 문자열.

### `python -m runner.run --documents data/dev/documents.jsonl --queries data/dev/queries.jsonl --out results/dev-rerun`

종료 코드: 0

stdout:

```text
| 방법 | hit@3 | hits/queries | 결과 없는 질의 |
|---|---|---|---|
| A | 1.0 | 10/10 | 0 |
| B | 1.0 | 10/10 | 0 |

| 방법 | 지연 중앙값 (ms, 질의 10개 1회 실행) | 측정 횟수 |
|---|---|---|
| A | 0.463 | 5 (준비 1) |
| B | 0.567 | 5 (준비 1) |
```

stderr: 빈 문자열.

## pytest 요약

`tests/unit/`과 `tests/runner/`를 함께 실행했고 종료 코드는 0이다. stdout에는 성공 표시 `.` 264개와 마지막 진행 줄 `................................................                         [100%]`가 있다. 실패·오류·건너뜀 출력 및 stderr는 없다.

`N passed in ...` 형식의 별도 요약 줄은 실제로 출력되지 않았다. 저장소 `pyproject.toml`의 pytest `addopts = "-q"`와 명령의 `-q`가 합쳐진 quiet 설정 때문으로 추정한다. 위 stdout을 그대로 보존하며, 출력되지 않은 시간이나 요약 줄을 만들지 않았다. 테스트는 한 번만 실행했다.

## 파일별 SHA-256 직접 비교

각 파일의 실제 바이트를 `hashlib.sha256(path.read_bytes()).hexdigest()`로 직접 계산했다. manifest에 적힌 값을 대신 사용하지 않았다.

| 파일 | dev 실제 SHA-256 | dev-rerun 실제 SHA-256 | 비교 |
|---|---|---|---|
| `raw-a.jsonl` | `11e767acaf0f6e549b8a64e1de85be13ee15769525338883f4fd0e8e0cd632a6` | `11e767acaf0f6e549b8a64e1de85be13ee15769525338883f4fd0e8e0cd632a6` | 일치 |
| `raw-b.jsonl` | `cb363cfba38f91e2cc5db531ccd1ff80257f047b2dbd1988bc5d02c6982afe3c` | `cb363cfba38f91e2cc5db531ccd1ff80257f047b2dbd1988bc5d02c6982afe3c` | 일치 |
| `latency.json` | `0d61b82adbf18f006af9b89b68e14204026a31ee8e030a93560b182f5ebcf13b` | `e3083e930b71374d60a40f0256bf3d7ed6b501e264004eb0d3d44bada9c9fc65` | 불일치 |
| `summary.json` | `10298c0e7f59952bb165679b4bc8a1976d6b6f743f495412648a64c0d58b38e2` | `10298c0e7f59952bb165679b4bc8a1976d6b6f743f495412648a64c0d58b38e2` | 일치 |
| `report.md` | `a06f739ab7c3d3fc4445e067ac7b8c8969c3150fad8a052a7fa4382b68a6aa18` | `1398e3d96e4b68caf480609a27f8a26da9fd1425ca671c9286c76fce5dfd5bb1` | 불일치 |
| `manifest.json` | `1364c424998f11c1d30c7403c615d1178f751c6156e0c380811771dc43397cc1` | `f8e877c86a3cc000d1d6d0747c1df2301f640353e580cf36d1ebbea47851f1de` | 불일치 |

**재현 비교 결론: 일치.** raw-results.md §6에서 지정한 `raw-a.jsonl`, `raw-b.jsonl`, `summary.json`이 모두 바이트 단위로 같다.

`latency.json`, `manifest.json`, `report.md`는 서로 다르다. §6은 앞의 두 파일에 실행별 차이를 허용하며, §5의 report.md에는 실행별 지연 중앙값이 포함되므로 재현 대상 3개 파일의 불일치로 보지 않는다.

### manifest.outputs_sha256 대조

| 폴더 | 파일 | manifest SHA-256 | 실제 바이트 해시와 비교 |
|---|---|---|---|
| `dev` | `raw-a.jsonl` | `11e767acaf0f6e549b8a64e1de85be13ee15769525338883f4fd0e8e0cd632a6` | 일치 |
| `dev` | `raw-b.jsonl` | `cb363cfba38f91e2cc5db531ccd1ff80257f047b2dbd1988bc5d02c6982afe3c` | 일치 |
| `dev` | `latency.json` | `0d61b82adbf18f006af9b89b68e14204026a31ee8e030a93560b182f5ebcf13b` | 일치 |
| `dev` | `summary.json` | `10298c0e7f59952bb165679b4bc8a1976d6b6f743f495412648a64c0d58b38e2` | 일치 |
| `dev` | `report.md` | `a06f739ab7c3d3fc4445e067ac7b8c8969c3150fad8a052a7fa4382b68a6aa18` | 일치 |
| `dev-rerun` | `raw-a.jsonl` | `11e767acaf0f6e549b8a64e1de85be13ee15769525338883f4fd0e8e0cd632a6` | 일치 |
| `dev-rerun` | `raw-b.jsonl` | `cb363cfba38f91e2cc5db531ccd1ff80257f047b2dbd1988bc5d02c6982afe3c` | 일치 |
| `dev-rerun` | `latency.json` | `e3083e930b71374d60a40f0256bf3d7ed6b501e264004eb0d3d44bada9c9fc65` | 일치 |
| `dev-rerun` | `summary.json` | `10298c0e7f59952bb165679b4bc8a1976d6b6f743f495412648a64c0d58b38e2` | 일치 |
| `dev-rerun` | `report.md` | `1398e3d96e4b68caf480609a27f8a26da9fd1425ca671c9286c76fce5dfd5bb1` | 일치 |

두 manifest 모두 출력 5개를 빠짐없이 기록하며 실제 파일의 SHA-256과 일치한다. manifest 자체는 outputs_sha256의 대상이 아니다.

입력 파일 바이트의 SHA-256도 직접 계산하여 두 manifest와 대조했다.

| 입력 | 실제 SHA-256 | dev manifest | dev-rerun manifest |
|---|---|---|---|
| documents.jsonl | `14fdc65bc21e6aa8ceb91378611eac63d4b41925e1c57e1fbf4260f60db4c5fd` | 일치 | 일치 |
| queries.jsonl | `3056ad34e84df4c392d5bcee1b2ef6615f58c74c9a2d2120422100693034fd40` | 일치 | 일치 |

## 발견한 문제와 원인 추정

- **검색 모듈·실행기 간 규격 해석 차이:** 이번 테스트·실행·해시 비교에서는 발견하지 못했다. 코드 읽기에서 runner/run.py는 methods.a·b의 search에 질의 text, documents, k를 넘기고 반환 순서·길이를 유지하면서 rank만 붙인다. 이는 search-interface.md §7 및 raw-results.md §2와 맞는 것으로 보인다(코드 읽기에 따른 판단). 별도 검증기의 직접 호출 재생성 검사는 실행하지 않았으므로 runner-consistency-v1의 pass를 주장하지 않는다.
- **명세 파일 누락:** 저장소 루트의 tasks/integrate.json 읽기에서 PowerShell ObjectNotFound / PathNotFound 오류가 발생했다. 검색 코드나 raw-results 규격 차이가 아니라 작업 공간에 명세 파일이 없는 문제이다. 사용자 지시문에 동일 작업의 상세 명세가 있어 진행했다.
- **조회 도구 부재:** `rg --files` 호출은 CommandNotFoundException으로 실행되지 않았다. Python pathlib로 지정된 methods, runner, tests, results만 조회했다. 의존성은 추가하지 않았다.
- **작업자의 임시 경로 오류:** pytest 임시 경로를 PYTEST_ADDOPTS로 전달할 때 Windows 역슬래시가 pytest 옵션 파싱에서 제거된 것으로 추정한다. 그 결과 `examples/search-ab/resultsintegration-pytest-wn_xlkh8pytest/`에 테스트 임시 파일이 생성되어 작업 지시문 §3의 허용 경로를 일시적으로 벗어났다. 이는 작업자 실행 래퍼의 오류이며 methods/runner의 규격 위반으로 판단하지 않는다. 시작 시 해당 경로는 없었고 모두 이번 테스트가 만든 파일임을 Git 상태로 확인했다. 삭제 전 절대 경로가 현재 작업 공간 내부이고 끝 이름이 정확한지 확인한 뒤 PowerShell Remove-Item -LiteralPath로 해당 임시 폴더만 제거해 원상복구했다. results/dev 및 results/dev-rerun은 삭제·수정·재생성하지 않았다. 후속 실행에서는 pytest 임시 경로 전달에 Path.as_posix()를 쓰는 것을 제안한다.
- 본 실행과 재실행에서 오류 메시지는 없고 stderr는 각각 빈 문자열이다. 부분 실패를 성공으로 처리하거나 출력 파일을 손으로 채우지 않았다.
- 읽은 자료에서 작업 범위나 검사를 변경하라는 악성 지시는 발견하지 못했다. 문서에 있는 예제 명령은 실행 권한으로 취급하지 않았다.

## 후속 보고서에 필요한 사실

아래 값은 JSON을 Python으로 읽어 `str()`로 출력한 그대로다. 품질이나 속도 우열을 해석하지 않는다.

```text
dev documents.jsonl 14fdc65bc21e6aa8ceb91378611eac63d4b41925e1c57e1fbf4260f60db4c5fd manifest_match=True
dev queries.jsonl 3056ad34e84df4c392d5bcee1b2ef6615f58c74c9a2d2120422100693034fd40 manifest_match=True
dev summary.json {'k': 3, 'queries': 10, 'a': {'hit_at_3': 1.0, 'hits': 10, 'no_result_queries': 0}, 'b': {'hit_at_3': 1.0, 'hits': 10, 'no_result_queries': 0}}
dev latency.json {'unit': 'ms', 'timer': 'time.perf_counter', 'warmup': 1, 'repeats': 5, 'queries': 10, 'a': {'runs_ms': [0.434, 0.431, 0.429, 0.483, 0.462], 'median_ms': 0.434}, 'b': {'runs_ms': [0.546, 0.666, 0.637, 0.761, 0.537], 'median_ms': 0.637}}
dev-rerun documents.jsonl 14fdc65bc21e6aa8ceb91378611eac63d4b41925e1c57e1fbf4260f60db4c5fd manifest_match=True
dev-rerun queries.jsonl 3056ad34e84df4c392d5bcee1b2ef6615f58c74c9a2d2120422100693034fd40 manifest_match=True
dev-rerun summary.json {'k': 3, 'queries': 10, 'a': {'hit_at_3': 1.0, 'hits': 10, 'no_result_queries': 0}, 'b': {'hit_at_3': 1.0, 'hits': 10, 'no_result_queries': 0}}
dev-rerun latency.json {'unit': 'ms', 'timer': 'time.perf_counter', 'warmup': 1, 'repeats': 5, 'queries': 10, 'a': {'runs_ms': [0.446, 0.444, 0.463, 0.732, 0.726], 'median_ms': 0.463}, 'b': {'runs_ms': [0.567, 0.614, 0.684, 0.559, 0.552], 'median_ms': 0.567}}
```

실행기 기본값 k=3, warmup=1, repeats=5를 두 실행 모두 사용했다. 개발용 자료만 입력했다. 토큰화와 지연 해석의 제한은 실행기가 생성한 각 report.md의 고정 문구를 따른다.

## 보조 명령 기록

다음은 준비·읽기·기록·정리용 명령이다. 여러 문장으로 구성한 PowerShell 호출의 종료 코드 0은 중간 비종료 오류의 성공을 의미하지 않는다.

| 명령 또는 호출 내용 | 종료 코드 / 결과 |
|---|---|
| 루트: `Get-Content -Raw AGENTS.md; Get-Content -Raw tasks/integrate.json; git status --short; git rev-parse HEAD` | PowerShell 0; tasks/integrate.json은 PathNotFound, 첫 읽기는 인코딩 깨짐; status 빈 출력 |
| UTF-8 재조회: `Get-Content -Raw -Encoding UTF8`로 AGENTS.md, protocol/README.md, protocol/raw-results.md, protocol/search-interface.md, data/dev/README.md 읽기 | PowerShell 0 |
| `rg --files examples/search-ab/methods examples/search-ab/runner examples/search-ab/tests examples/search-ab/results -g '!__pycache__/**'` | 실행되지 않음: CommandNotFoundException |
| 루트: `git log -3 --oneline` | 0 |
| `python -B -`: pathlib로 methods/runner/tests/results 파일 열거, Python 소스 읽기, 필수 5개 파일 is_file() 확인 | 0 |
| `Get-Content -Encoding UTF8 tests/runner/test_runner_run.py \| Select-Object -Skip 120 -First 65`; protocol/validate.py 및 protocol/data-format.md UTF-8 읽기 | PowerShell 0 |
| `python -B -`: 위 6개 실행 명령 subprocess 호출, tempfile 관리, hashlib 직접 비교 | 0 (각 하위 명령 종료 코드는 앞 표 참조) |
| `python -B -`: 현재 디렉터리와 저장소 루트의 pytest.ini/pyproject.toml/setup.cfg/tox.ini 존재 여부 확인 및 존재 파일 읽기 | 0; 루트 pyproject.toml만 존재 |
| `git status --short --untracked-files=all`; `git diff --stat` | 각각 0; 테스트 임시 경로 이탈 발견, tracked 변경 없음 |
| PowerShell: `Resolve-Path -LiteralPath`로 지정된 임시 경로 확인 및 작업 공간 내부/정확한 끝 이름 검사; `Remove-Item -LiteralPath $taskResolved -Recurse -Force`; `git status --short` | 0; results/만 untracked |
| `python -B -`: 입력 파일 SHA-256 계산, manifest 대조, summary.json/latency.json 값을 str()로 출력 | 0 |

조회 시 PowerShell 출력 인코딩만 UTF-8로 설정했고 파일을 변환하지 않았다. Python 임시 실행 코드는 stdin으로 전달했으며 저장소에 보조 프로그램을 추가하지 않았다.

## 산출물과 후보 제출

변경 파일은 다음 13개다.

- `INTEGRATION.md`
- `dev/raw-a.jsonl`
- `dev/raw-b.jsonl`
- `dev/latency.json`
- `dev/summary.json`
- `dev/report.md`
- `dev/manifest.json`
- `dev-rerun/raw-a.jsonl`
- `dev-rerun/raw-b.jsonl`
- `dev-rerun/latency.json`
- `dev-rerun/summary.json`
- `dev-rerun/report.md`
- `dev-rerun/manifest.json`

이 기록은 후보 제출이다. 명세 산출물 13개가 존재하지만 채택은 interface-contract-v1, method-behavior-v1, runner-consistency-v1, allowed-changes-v1의 별도 검증 결과로 결정된다. 해당 검사들을 실행했다고 보고하지 않는다. 다음 작업인 보고서는 시작하지 않았다.

## 커밋 준비 기록

- 루트에서 `git diff --check; git status --short --untracked-files=all; git add -- examples/search-ab/results; git diff --cached --name-only; git diff --cached --check`를 실행했다. PowerShell 전체 종료 코드는 0이지만 첫 git add는 아래 오류로 실패했다(해당 하위 프로세스 종료 코드는 별도 캡처하지 않음).

```text
fatal: Unable to create '<repo>/.git/worktrees/workspace/index.lock': Permission denied
```

- worktree의 Git 메타데이터 쓰기 권한을 확장해 `git add -- examples/search-ab/results`를 실행했고 종료 코드는 0이었다. 사용자에게 요청된 커밋을 위한 Git 메타데이터 변경이며 소스 파일은 변경하지 않았다.
- `python -B -` 검증 스크립트에서 산출물 13개 존재, 허용 경로만 스테이징됨, 실행기 출력 12개와 `git show :<경로>`의 바이트 동일성, outputs_sha256 10개 일치를 assertion으로 확인했다. 이어 실행한 `git diff --cached --check`가 종료 코드 2를 반환하여 스크립트는 종료 코드 1로 끝났다. 오류 출력은 다음과 같다.

```text
COMMAND git diff --cached --check EXIT 2
examples/search-ab/results/INTEGRATION.md:237: new blank line at EOF.
Traceback (most recent call last):
  File "<stdin>", line 27, in <module>
AssertionError
```

- INTEGRATION.md의 끝 빈 줄만 정리했다. 실행기 출력은 바꾸지 않았다. 위 실패 때문에 첫 스크립트의 후속 `git diff --exit-code`는 실행되지 않았다.
- 최종 준비·제출 명령은 `git add -- examples/search-ab/results`, 같은 읽기 전용 검증 스크립트(`git diff --cached --name-only`, `git show :<경로>`, `git diff --cached --check`, `git diff --exit-code` 포함), `git commit -m "Record pilot-001 integration runs and reproducibility"`, `git rev-parse HEAD`, `git status --short`, `git show --format= --name-only HEAD`이다. 이 기록 자체를 포함하는 commit 해시와 해당 명령의 최종 종료 코드는 완료 보고에 남긴다.
