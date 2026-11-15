---
version: 1
slug: orch-round-2
branch: feature/orch-round-2
base_branch: main
workers:
  - id: w04
    title: Operator visibility
    instruction: workers/w04.md
    allowed_paths:
      - src/orch/cli.py
      - src/orch/watch.py
      - src/orch/reporting.py
      - tests/workflow/test_presentation.py
      - tests/e2e/test_round2_presentation.py
      - documentation/20261115_operator_visibility.md
    depends_on: []
  - id: w03
    title: Noncritical OpenCode configuration investigation
    instruction: workers/w03.md
    allowed_paths:
      - tests/agents/test_round2_docs_configuration.py
      - documentation/20261115_opencode_docs_configuration_investigation.md
    depends_on: []
reviewers:
  code: reviewer.md
  docs: doc-reviewer.md
checks:
  - name: tests
    argv: [uv, run, pytest, -q]
    timeout_seconds: 600
    applies_to: integration
  - name: lint
    argv: [uv, run, ruff, check, .]
    timeout_seconds: 120
    applies_to: both
limits:
  max_repair_attempts: 3
  max_review_rounds: 3
  max_reassignments: 1
  max_failed_repairs_per_assignment: 2
  max_issue_depth: 2
  worker_timeout_min: 45
  max_runtime_min: 480
  max_infrastructure_retries: 2
  concurrency: 2
---
# Delivery manifest

Scope, interfaces, acceptance and check disposition follow plan.md. Human execution approval is required.
