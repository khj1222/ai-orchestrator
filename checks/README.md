# 등록 검사 정의 (checks/)

검사 하나가 파일 하나다: `checks/<check_id>.json`. 파일 이름과 안의 `check_id`가 같아야 한다.
`check_id`는 영숫자·`_`·`-`만, 64자 이내(예: `interface-contract-v1`). 버전이 바뀌면 새 ID를 만든다.
작업자는 이 폴더를 수정하지 못한다. 작업 명세의 `checks` 목록은 여기 있는 ID를 가리킨다.

검사는 작업자 worktree가 아니라 **대상 commit에서 새로 만든 검증 worktree**에서 돈다.
`orchestrator/verify.py`의 `run_checks`가 실행하고, 결과는 `records.Check`(verdict pass / fail / undecidable)다.

## kind = "command" (기본)

```json
{
  "check_id": "unit-tests-v1",
  "kind": "command",
  "validator_version": "1",
  "command_id": "pytest-unit",
  "command": ["{python}", "-m", "pytest", "tests/unit", "-q"],
  "cwd": ".",
  "timeout_seconds": 600,
  "required_artifacts": ["methods/a.py", "methods/b.py"],
  "description": "단위 테스트. 산출물이 없으면 명령을 돌리지 않고 fail."
}
```

| 필드 | 뜻 |
|---|---|
| `command` | argv 리스트. 셸 없이 실행한다. `{python}`은 검증기의 `sys.executable`, `{worktree}`는 검증 worktree 절대 경로로 바뀐다. `{publish}`는 그 검사의 증거 폴더 안 `publish/` 경로로 바뀐다(작업자에게 보이지 않는다. 검사가 pass이면 프로세스가 끝난 뒤 실행기가 그 폴더를 한 번 읽어, 파일이 `eval-summary.json` 하나이고 형식이 정확한 평가 집계(eval-summary-1)일 때만 검증한 값으로 다시 쓴 봉인 사본 `published.json`과 sha256을 판정 기록에 남긴다. 아니면 사유 코드만 `publish_rejected`로 남긴다. 검증 대상 코드가 검증기 프로세스 안에서 돌기 때문에 pass 판정만으로는 공개 내용을 믿지 않는다). 절대 경로를 적지 않는다 |
| `cwd` | worktree 기준 상대 경로. 기본 `.` |
| `timeout_seconds` | 초과하면 프로세스 트리를 종료하고 **undecidable** |
| `required_artifacts` | worktree 기준 상대 경로. 하나라도 없으면 명령을 돌리지 않고 **fail** (`missing_artifact`) |
| `validator_version`, `command_id` | Check 기록에 그대로 들어간다. `TO_BE_`로 시작하면 정의 오류 |

판정: 종료 코드 0 → pass. 그 밖 → fail. 시작 실패·시간 초과 → undecidable.
명령은 W2 `process.start`로 돌아 `ANTHROPIC_API_KEY`·`OPENAI_API_KEY`·`CODEX_API_KEY`가 제거되고 `PYTHONUTF8=1`이 붙는다.

## kind = "scope"

```json
{
  "check_id": "allowed-changes-v1",
  "kind": "scope",
  "validator_version": "1",
  "command_id": "workspace-scope",
  "allowed_write_paths": null,
  "cache_patterns": ["__pycache__/", ".pytest_cache/", "*.pyc"],
  "description": "기준 commit 이후 변경이 허용 경로 안에만 있는지. null 이면 run_checks 인수의 허용 경로를 쓴다."
}
```

W5 `detect_out_of_scope_changes(worktree, base_commit, allowed)`를 검증 worktree에서 돈다.
`inspection_error`가 있으면 **undecidable**(빈 결과라도 통과가 아니다). `cache_patterns`에 맞는 경로는 위반에서 빼고 `cache_paths_ignored`로 따로 기록한다. 남은 위반이 있으면 fail, 없으면 pass.
`allowed_write_paths` 항목은 접두어다. 디렉터리는 `src/`처럼 슬래시로 끝낸다.

## 증거

`run_checks(evidence_root=...)` 아래 `checks/<check_id>/`에 `stdout.bin`, `stderr.bin`, `command.json`(argv·시각·process.json·stop 결과), `environment.json`(검증기·검사 버전, Python, 제거한 환경 변수 이름), `scope.json`, `reason.txt`가 남는다. 검증 worktree는 `evidence_root/verify-worktree`라 증거 폴더와 겹치지 않는다. `Check.evidence`의 경로는 `evidence_root` 기준 상대 경로다. `Check.subject_manifest_hash`는 대상 commit의 **tree 해시**다. 파일 내용이 같으면 같다.

`Check.subject_commit`이 지금 검증하려는 commit과 다르면 그 pass는 쓸 수 없다(`is_check_current`).

## 격리 규칙

- 검사가 검증 worktree의 **소스를 바꾸면**(HEAD 이동, 추적 파일 수정·삭제·스테이지) 그 검사는 `undecidable`(`subject_modified`)이고 원래 판정은 `evidence["original_verdict"]`에 남는다. 검사 정의를 고쳐야 한다.
- 검사가 무엇이든 남기면(untracked·ignored 포함) 다음 검사는 **새 worktree**에서 돈다.
- 시간 초과 후 프로세스 트리 종료를 확인하지 못하면(`stop_state != "stopped"`) 남은 검사는 `verification_aborted`로 모두 `undecidable`이고, worktree는 지우지 않고 `VerificationRun.worktree_path`에 남긴다.
- 검증기 자체의 예외는 `verifier_error`로 그 검사만 `undecidable`이다. `run_checks`는 예외를 던지지 않는다.
