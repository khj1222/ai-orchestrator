# ai-orchestrator

[한국어](README.ko.md)

A small, local orchestrator that runs coding-agent CLIs on a fixed plan of tasks, accepts a task only when
**registered checks pass on the exact result commit and every declared deliverable exists**, and integrates
accepted results one by one with `git merge --no-ff`. It drives the official command-line tools of two vendors
(OpenAI Codex CLI and Anthropic Claude Code) as subprocesses, each logged in with the user's own account.

This is a personal research tool shared as a worked example. It is not a product, and it is not affiliated with,
endorsed by, or supported by Anthropic or OpenAI.

## What it does and does not do

Does:

- Validates a plan (task graph, write-path ownership, check wiring, leakage of private evaluation strings) before
  anything runs (`orch validate-plan`).
- Runs tasks in dependency order, up to two at a time, each in its own git worktree from a fixed base commit
  (`orch run-plan`). A dependent task starts from the integration commit of its prerequisites.
- Decides acceptance only from registered checks and deliverable existence. A worker's "done" message, its exit
  code, or a reviewer's opinion never changes a task's state.
- `run-plan` retries a task whose checks failed or whose deliverables are missing, within its attempt budget,
  telling the worker only check IDs, verdicts and reason codes. (`run-task` treats these as final.)
- Records every attempt, process identity, check verdict and merge, and writes a run report.

Does not:

- Choose models automatically, route by difficulty, show a UI, or distribute work across machines.
- Measure or claim time or cost savings. No utility comparison of single vs. parallel runs has been made.
- Store, read, copy or forward credentials. Each CLI uses its own login on the user's machine.

## Principles

- **Checks decide, nothing else.** A task is accepted only when all its registered checks pass on the exact result
  commit and its deliverables exist in that commit as regular files (blobs of mode 100644 or 100755; gitlinks
  and symlinks do not count; a deliverable ending in `/` needs at least one regular file below it).
- **Fixed inputs.** Plans pin a base commit and a protocol hash; a run refuses to resume if any plan, spec or
  instruction file changed.
- **One owner per write path.** Two tasks never own overlapping paths, so integration never needs to resolve content.
- **Private evaluation data stays outside the repository** and outside the workers' environment. Evaluation
  numbers in a report come from an expected summary the manager pinned before the run, shown only after the
  declared check passed; if that source changed or is missing, the report says `withheld` instead.
- **Author and reviewer differ.** During development, code written with one vendor's agent was reviewed by the
  other's.

## Requirements

- Windows, Python 3.10+, git. The manager and real model-CLI runs were verified on Windows only; for what was
  tried on Linux, see Known limitations.
- For real runs: the official CLIs installed and logged in with your own accounts. Both Anthropic and OpenAI
  document API keys as the recommended authentication for automated and third-party use. If you use your
  subscription login instead, check your providers' terms for automated use; that is your responsibility. Do not
  share one login between machines or run it in public CI.
- Commands below are for Windows PowerShell.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

If PowerShell refuses to run `Activate.ps1`, allow it for the current shell only with
`Set-ExecutionPolicy -Scope Process RemoteSigned`.

Copy `config.example.json` to `config.json` (git-ignored) and fill in absolute CLI paths and model IDs.

Claude routes run with `--safe-mode` by default (`"safe_mode": true`, Claude Code 2.1.169 or later). It turns off
hooks, MCP servers, plugins, skills, `CLAUDE.md` and auto memory, both your own and any `.claude/settings.json` or
`.mcp.json` a worker writes into its workspace, while login, built-in tools and permissions work as usual. The target
repository's `CLAUDE.md` is then not read automatically, so put the rules a worker needs in its instructions or name
the file there. Admin-managed (policy) settings still apply, including any hooks they define. Set
`"safe_mode": false` on a route to turn it off.

## Example 1 — reproduce the committed results without any model CLI

The repository contains the result of a real run: two search methods (`examples/search-ab/methods/`), an experiment
runner (`examples/search-ab/runner/`), their outputs and a report. You can check them offline.

```powershell
python -m pytest
cd examples/search-ab
python -m pytest tests
python protocol/validate.py --hash
python -m runner.run --documents data/dev/documents.jsonl --queries data/dev/queries.jsonl --out ../../../search-ab-dev-run
python -c "import hashlib, pathlib; [print(n, hashlib.sha256(pathlib.Path('../../../search-ab-dev-run', n).read_bytes()).hexdigest()) for n in ('raw-a.jsonl', 'raw-b.jsonl', 'summary.json')]"
cd ../..
```

Expected:

| Output | sha256 |
|---|---|
| `protocol/validate.py --hash` | `ea86373c26e72d61f9ca6a15e5636ea96e8496d0c3c2bd0e6bbc1a21e512b624` |
| `raw-a.jsonl` | `11e767acaf0f6e549b8a64e1de85be13ee15769525338883f4fd0e8e0cd632a6` |
| `raw-b.jsonl` | `cb363cfba38f91e2cc5db531ccd1ff80257f047b2dbd1988bc5d02c6982afe3c` |
| `summary.json` | `10298c0e7f59952bb165679b4bc8a1976d6b6f743f495412648a64c0d58b38e2` |

The same values are committed in `examples/search-ab/results/dev/`. `latency.json` and `manifest.json` differ per
run by design. The `--out` folder must not exist yet. The protocol hash above is the value computed on Windows;
on other operating systems it differs (see Known limitations).

## Example 2 — run the whole plan with your own CLIs and your own evaluation data

The evaluation queries used here are private and not published. To run the plan end to end you supply your own.
Run the commands below from the repository root.

1. Create a private folder **outside** the repository containing:
   - `eval-queries.jsonl` — your evaluation queries over `examples/search-ab/data/dev/documents.jsonl`, in the
     format of `examples/search-ab/protocol/data-format.md`.
   - `behavior-cases.json` — hand-computed cases with expected A/B rankings. `tests/test_verifier.py` contains
     eight public cases in this format you can start from.
2. Point the environment variable at it and generate the expected results (the tool first checks every hand case):
   ```powershell
   $env:ORCH_VERIFIER_PRIVATE = '<private-folder>'
   python -B verifier/reference.py --write-expected
   ```
3. Copy a plan, e.g. `runs/pilot-001-mixed-2/`, to a new folder, change `run_id` in `plan.v1.json` and every
   `tasks/*.json`, and adjust `route_id`s to routes in your `config.json`.
4. Validate, then run. Keep run records outside the repository.
   ```powershell
   orch validate-plan runs/<your-run>/plan.v1.json --config config.json
   orch run-plan runs/<your-run>/plan.v1.json --config config.json --run-root <records-folder> --parallel 2
   ```
5. Read `<records-folder>/runs/<run-id>/run-report.md`. The integration line is the ref
   `refs/orchestrator/runs/<run-id>/integration`; merging it into a branch is up to you.

Real runs use your CLI subscriptions or API quota.

## What was verified

All on one Windows PC, October 2026, with the plan in `runs/pilot-001/` (search module, experiment runner,
integration, report):

| Run | Workers | Result |
|---|---|---|
| W11 `pilot-001` | Codex, sequential | completed, 4/4 tasks accepted on the first attempt, ~17 min |
| W12 `pilot-001-parallel` | Codex ×2 in parallel | completed, 4/4 on the first attempt, ~13 min |
| W14 `pilot-001-mixed-2` | Codex + Claude Code in parallel | completed, 4/4 on the first attempt, ~18 min |

For each run the manager independently checked the verdicts, the diff scope, a clean-worktree reproduction of the
outputs, the report numbers against the result files, and the workers' command logs for access to the verifier or
the private folder (none found). The raw results of all three runs are byte-identical.

Evaluation aggregate on the 20 private queries: **method A hit@3 = 0.3 (6/20), method B hit@3 = 0.3 (6/20)**.
Many evaluation queries deliberately share no tokens with their relevant documents, which whitespace tokenisation
cannot match. The point of the experiment is that the pipeline implements a fixed specification reproducibly, not
that either method is good.

Each code work unit was reviewed by the other vendor's agent (documentation-only changes were not). Reviews found
and fixed, among others: merge order under parallel completion, a race that let a new task start after another was
blocked, a channel through which code under test could publish evaluation details (redesigned so numbers come only
from a manager-pinned source), gitlinks counted as deliverables, and path whitespace lost in parsing.

## Known limitations and failures

- **First mixed run failed.** The Claude Code CLI login had expired; the worker exited in 0.18 s and, because the
  task's only check was a scope check, the empty result was *accepted*. The run was stopped and the
  deliverable-existence rule was added. Lesson: a passing scope check proves nothing about content.
- **Isolation is detection, not prevention.** Workers run as the same OS user and can, in principle, read the private
  folder. The tool strips the variable from workers, keeps data outside the repo, and audits logs afterwards.
- The plan-leak check only finds verbatim strings; an answer restated as an ID mapping would pass.
- The scope check sees the final commit only; a file written outside the allowed paths and deleted again is not seen.
- Claude Code workers hit permission denials (PowerShell tool, `cd … &&` chains) under the configured allow-list
  and worked around them; this slows them down.
- Runs with real model CLIs were done on one Windows PC only. Ctrl+C and `stop` during verification or with two
  tasks running were not fully tested.
- The manager cannot start workers or checks on Linux: `orchestrator/process.py` uses a Windows-only process API
  (`subprocess.CREATE_NEW_PROCESS_GROUP`). macOS was not tried; the same code path applies. In a Linux container
  (Python 3.12), Example 1's three result hashes (`raw-a.jsonl`, `raw-b.jsonl`, `summary.json`) matched, and the
  manager's tests gave 393 passed, 81 failed (process launch and check verdicts), 21 skipped (Windows-only).
- The protocol hash depends on the OS. `protocol_hash()` orders files with `sorted()` on `Path` objects, which
  ignores case on Windows and not elsewhere, so `README.md` lands in a different position. The file contents are the
  same; on Linux the hash is `96b2671e8f9e961184f7de7059852854f184a0c9d29e9ad1112fd5c2d9023acc`. Plans and records
  in this repository use the Windows value.
- Read run records only through `orch status` (or the same run lock). On Windows, another program that keeps a
  record file open without the lock makes the manager's atomic file replace fail with `PermissionError`.
- The Claude worker in the mixed plan above used Claude Code CLI 2.1.268 with `claude-opus-5`; that version does not accept
  `claude-opus-5-5`. After updating to 2.1.287 (October 2026), one single-task run on `claude-opus-5-5` was accepted
  with both registered checks passing; a full plan run on it has not been done.
- Claude routes use `--safe-mode` by default since October 2026. With Claude Code 2.1.287 and `claude-opus-5-5`,
  one single-task run with it was accepted with both registered checks passing. In a direct check, hooks in a
  workspace `.claude/settings.json` ran without `--safe-mode` and did not run with it. A full plan run with it has
  not been done.
- Results reproduce by re-running on the committed outputs; model runs themselves are not deterministic.

## Reference

| Command | Purpose |
|---|---|
| `orch validate task.json` / `orch run-task task.json` | one task without dependencies |
| `orch validate-plan plan.json` | plan rules P1–P10 (read-only) |
| `orch run-plan plan.json [--parallel 2]` | run a plan, integrate, write `run-report.{json,md}` |
| `orch status <run-id>` / `orch stop <run-id> <task-id>` | inspect or stop a run |

Plan rules: P1 no unresolved `TO_BE_` values; P2 IDs unique and specs agree with the plan; P3 known dependencies,
no cycles; P4 each spec valid; P5 one owner per write path; P6 every check's required artifacts come from the task or
its prerequisites; P7 private variables never reach workers; P8 no private string copied into instructions or specs;
P9 instructions readable; P10 an optional `publication` names a private expected summary and the check that gates it.

The detailed reference (plan format, run semantics, records) is in [README.ko.md](README.ko.md), Korean.
Check definitions: [checks/README.md](checks/README.md). Search A/B protocol: [examples/search-ab/protocol/](examples/search-ab/protocol/).

## Layout

```text
orchestrator/        manager: records, process, workspace, verify, cli, plan, run_plan, adapters/{codex,claude}
checks/              registered check definitions
verifier/            verification scripts (private data lives outside the repository)
examples/search-ab/  the example project: protocol, dev data, methods, runner, results, report
runs/                committed example plans (pilot-001, -parallel, -mixed, -mixed-2); run records are git-ignored
tests/               tests of the manager itself (offline, no model CLI)
docs/                development history
```

## License

MIT. See [LICENSE](LICENSE).
