# I measured whether a coding-agent orchestrator beats handing the whole task to one session

[한국어](single-session-comparison.ko.md)

The result first. On three small tasks, the runs that passed every hidden test were 6 of 6 for a single session and 6 of 9 for the orchestrator. The orchestrator took about twice as long. Under the rule I fixed beforehand, the verdict is "undecided".

I did this out of plain curiosity and the sample is small. But the decision rules were fixed before any run and the grading used tests the workers were never given, so I am writing down the method and the limits along with the numbers.

## What I built

[ai-orchestrator](../README.md) is a personal run manager. It takes a fixed plan that a strong model has written in advance, already split into several tasks, hands those tasks to coding-agent CLIs (Codex CLI, Claude Code), and accepts a task only when every registered check passes on the exact result commit. Accepted results are merged one at a time. The principle is that a worker saying "done" counts for nothing; only the checks decide.

Once it worked, the obvious question was left. Does splitting and checking like this give better results than just giving the whole task to one session?

## How I measured it

- **Three tasks.** Each is a small Python library defined by one specification document (210 to 250 lines): a converter from a Markdown subset to HTML, a CSV reader with a schema checker, and a tool that summarises game logs with statistics.
- **Three ways of running.** (1) Single session: give it the specification and let it do everything. (2) Orchestrator with one worker: three or four plan tasks, one after another. (3) Orchestrator with two workers: the same plan, up to two tasks at once.
- **Fifteen runs.** Per task: two single-session runs, one run with one worker, two runs with two workers.
- **Every worker was the same model.** Codex CLI with `gpt-6-astra` (high), subagents turned off. The plans were written by Claude, once per task; I reviewed the points each plan flagged for checking, approved them, and froze the plans.
- **Graded by hidden tests.** 67 to 79 cases per task. They were not given to the workers or to the session that wrote the plan. A run counts as complete only if it passes all of them.
- **Decision rules fixed before the runs.** "It works" if the orchestrator has the higher completion rate on at least two tasks and the lower on none. "It does not work" if the single session is higher on at least two. Anything else is "undecided". For parallelism, only tasks completed under both the one-worker and the two-worker condition are compared, and the times of incomplete runs are left out. The median wall-clock time with two workers has to be at least 20% shorter than with one.

## Results

| Task | Single session | Orchestrator (1 worker ×1, 2 workers ×2) | Single-session time | Orchestrator time |
|---|---|---|---|---|
| Markdown converter | 2/2 complete | 3/3 complete | 6–7 min | 14–18 min |
| CSV checker | 2/2 complete | 0/3 complete (each passed 73 of 74) | about 4 min | 8–11 min |
| Game log statistics | 2/2 complete | 3/3 complete | about 5 min | 9–10 min |

- **Accuracy:** the orchestrator was better on no task, equal on two and worse on one. By the rule, undecided.
- **Time:** even without the time to write the plan, the orchestrator's wall-clock time was 1.82 to 2.38 times the single session's, by per-task means. Adding one round of plan writing (about 18 to 21 minutes per task, an approximate figure from my notes) to one run and comparing that with the single session's mean run time gives roughly 5 to 7 times. The time I spent reviewing the plans is not included.
- **Tokens:** comparing per-task means, the workers used 2.6 to 4.5 times as many input tokens and 1.9 to 2.5 times as many output tokens as the single sessions. In every run between 89% and 97% of the input was cached input, so this is not a price ratio. Tokens used separately by Codex's automatic approval review are not in these numbers.
- **Parallelism:** the 20% threshold was met on 0 of the 2 tasks that could be compared. Only one task had two workers actually running at the same time with every run complete, and there the median wall-clock time was 17.6% shorter.
- All 33 plan tasks across the nine orchestrator runs were accepted on the first attempt. There were no retries and no failed merges.

## What I learned

**The single sessions passed every hidden test.** In these runs the single sessions passed all the hidden tests in four to seven minutes, which left no headroom for an observed improvement on this completion-rate metric. All this experiment says is that on these three tasks, splitting was not needed to pass these hidden tests.

**Passing every registered check does not mean the result is right.** On the CSV task, all three orchestrator runs passed every registered check and still missed the same single hidden case. In all three, one module left that case out in the same way, while another module in the same run checked the same rule more broadly. In the plan, the instructions for the task that owned that module listed the cases to check more narrowly than the instructions for the other task did. The two single-session runs, working from the specification alone, handled it.

The way the plan enumerated cases may have played a part. That is not established. The same model may simply have read the same instructions the same way three times, or a single session that sees the whole task may have benefited from putting its validation code in one place. With one plan per task I could not tell these apart. What is established is only that in these runs the worker-written tests and the registered checks did not catch the omission. The implementation and its tests may have shared one interpretation.

**Only the independent tasks in a plan can run in parallel.** One of the three plans was fully sequential, so there was nothing to run in parallel. In the other two, only the first two of four tasks were independent. In the four runs where two workers actually overlapped, they overlapped for 101 to 135 seconds.

## Things I learned on the side

**If you want a single-session baseline, verify that subagents are really off.** With Codex CLI 0.163, a worker still started a helper agent when given `--disable multi_agent`. Worse, the spawn did not appear in the `exec --json` output, only in the stored session logs. With 0.164, `-c agents.enabled=false` did stop it. This is the kind of fact that has to be rechecked for every version.

**Having a different vendor's model review the work caught real mistakes.** Claude wrote the result report and the public README text; Codex reviewed them by recomputing the numbers from the raw records. The verdict and the numbers matched, but four points in the report and two in the README text did not hold up. Two of them:

- The draft took "the sum of worker run times minus wall-clock time" to be the time the workers overlapped. Wall-clock time includes setup, checks and merges during which no worker is running, so that is the wrong calculation. The real figure comes from intersecting the start and end times.
- The English README draft said "17.6% faster". A 17.6% reduction in time, read as speed, is 21.3% faster, which would look as if the 20% threshold had been met.

## Limits

- Three tasks and fifteen runs. This shows a direction and supports no claim of statistical significance.
- The single session completed everything, so the comparison hit the ceiling. I did not measure tasks too large for one session's context or time limit.
- One plan per task, and one run per task for the one-worker condition. I cannot say how much plans vary in quality or how much run times vary.
- The plan, the task split, the registered checks and parallelism were compared with the single session as one package. I could not separate the effect of each. No retry ever happened, so the value of retries was not measured either.
- Completion is all or nothing. The direction on the CSV task was decided by 1 case out of 74.
- The hidden tests were written by Codex from the specification alone, and the workers were Codex too. The plans were written by Claude. I cannot rule out that models from the same family simply share reading habits.
- Codex CLI updated itself during the experiment, so the version differs between tasks. Within a task all three ways of running used the same version.
- The tasks, hidden tests and experiment tooling are not public. This post alone is not enough to reproduce the result.
- Token counts are what the session logs show, not money.

## So

On these three tasks, the single sessions had equal or higher hidden-test completion rates, shorter run times, and fewer recorded worker tokens. For an orchestrator to be worth it, I suspect the task has to be too big for one session, or there has to be a reason other than accuracy: cross-review by different models, long unattended runs, records and audit. I did not measure any of that.

I have closed the experiment here. The tool, the results and the known limitations are in the [repository README](../README.md). If you have measured something similar, I would like to hear how it turned out.

## How this experiment and write-up were made

I have built this project together with coding agents from the start, and the experiment was no different. I set the question and the direction. Claude Code did the experiment design, managed the runs, and wrote the result report and the draft of this write-up. Codex wrote the hidden tests from the specifications alone, and reviewed the report and this write-up by recomputing the numbers from the run records. I checked and approved the design, the plans, the runs and the merges at each step. So much of what this post says "I" did was done by an agent with my approval.
