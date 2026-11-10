# Feature report: model-preferences-cli-carry-1

Phase: AWAITING_TEST
Stage: NOTIFY; cycle: 1

Candidate: 37f724dbdc15eb84c5c071c60cb9cd2a3540903f
Agent calls: 3; recorded agent time: 376.56 seconds.
Usage exposed for 3/3 calls; unknown usage is not zero.

## Recorded usage totals

These sum matching metric names only; provider units may differ.

- cache.read: 123618
- cache.write: 0
- input: 3444
- output: 333
- reasoning: 71

## Assignments

- w01: done; handover: /home/zwj808/.local/state/orch/runs/model-preferences-cli-carry-1-f984dda7/round-1/handover/w01-1.md
- review-code: done; handover: none
- review-docs: done; handover: none

## Issues

- code-w01-01 (finished; legacy resolved, root code-w01-01, depth 0): Linked config parents and linked lock files can bypass write-path refusal.
  - Assignment 1: 1 repairs, 0 failed repairs, 0 review rounds.
- code-w01-02 (finished; legacy resolved, root code-w01-02, depth 0): Windows atomic replacement does not preserve existing ACL restrictions.
  - Assignment 1: 1 repairs, 1 failed repairs, 1 review rounds.
- code-w01-03 (finished; legacy escalated, root code-w01-03, depth 0): Configuration validation errors can disclose input values.
  - Assignment 1: 2 repairs, 2 failed repairs, 2 review rounds.
  - Assignment 2: 2 repairs, 2 failed repairs, 2 review rounds.
- code-w01-04 (finished; legacy resolved, root code-w01-04, depth 0): Fallback consent is requested before showing resolved preferences.
  - Assignment 1: 1 repairs, 0 failed repairs, 0 review rounds.
- code-w01-05 (finished; legacy resolved, root code-w01-05, depth 0): --global-models is silently accepted on commands that do not select model preferences.
  - Assignment 1: 2 repairs, 1 failed repairs, 2 review rounds.
- code-w01-06 (finished; legacy resolved, root code-w01-06, depth 0): Preference validation allows Unicode C1 control characters despite the requirement to reject Unicode control characters.
  - Assignment 1: 1 repairs, 0 failed repairs, 1 review rounds.
- docs-w01-01 (finished; legacy resolved, root docs-w01-01, depth 0): The handover says the existing user guide was unchanged, but does not establish the required operator-entry-point link to the new command guide.
  - Assignment 1: 1 repairs, 0 failed repairs, 1 review rounds.

## Usage by call

- w01 (opencode-go/deepseek-v4.1-flash): 191.58s; usage: {'input': 361, 'output': 163, 'reasoning': 60, 'cache': {'read': 58624, 'write': 0}}
- review-code (opencode-go/muse-spark-1.3-contributor): 146.31s; usage: {'input': 350, 'output': 112, 'reasoning': 6, 'cache': {'read': 51057, 'write': 0}}
- review-docs (opencode-go/muse-spark-1.3-contributor): 38.67s; usage: {'input': 2733, 'output': 58, 'reasoning': 5, 'cache': {'read': 13937, 'write': 0}}

## Human test and evidence

Read the code review verdict's Manual test notes and test the feature branch.
See checklist.md, issues.md and activity_log.md in the run directory for dispatches, reviews, rework and recovery.
Only the feature branch is a permitted promotion target. Human testing is required before closing the feature.
