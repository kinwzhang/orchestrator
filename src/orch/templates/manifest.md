---
version: 1
slug: example
branch: feature/example
base_branch: main
workers:
  - id: w01
    title: Implementation
    instruction: workers/w01.md
    allowed_paths: [src/example/**, tests/example/**, documentation/example.md]
    depends_on: []
reviewers:
  code: reviewer.md
  docs: doc-reviewer.md
checks:
  - name: tests
    argv: [python, -m, pytest, -q]
limits:
  max_repair_attempts: 3
  max_review_rounds: 3
  max_reassignments: 1
  max_failed_repairs_per_assignment: 2
  max_issue_depth: 2
  worker_timeout_min: 45
  max_runtime_min: 480
  max_infrastructure_retries: 2
  concurrency: 4
---
# Execution manifest

The planner replaces example assignments and checks with the approved target project's contracts.
