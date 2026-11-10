---
version: 1
slug: model-preferences-cli-carry-1
branch: feature/model-preferences-cli-carry-1
base_branch: repair/carry-forward-20261110-integration
workers:
- id: w01
  title: Model preference commands, safe configuration edits and acceptance tests
  instruction: workers/w01.md
  allowed_paths:
  - src/orch/contracts.py
  - src/orch/config.py
  - src/orch/models.py
  - src/orch/cli.py
  - src/orch/workflow.py
  - pyproject.toml
  - uv.lock
  - tests/contracts/test_model_preferences.py
  - tests/contracts/test_replacement_reasoning.py
  - tests/e2e/test_models_cli.py
  - tests/workflow/test_model_preferences_approval.py
  - documentation/20261026_user_guide.md
  - documentation/20261026_contracts_and_configuration.md
  - documentation/20261030_model_preferences_cli.md
  depends_on: []
reviewers:
  code: reviewer.md
  docs: doc-reviewer.md
checks:
- name: pytest
  argv:
  - uv
  - run
  - pytest
  - -q
  timeout_seconds: 900
  applies_to: both
- name: ruff
  argv:
  - uv
  - run
  - ruff
  - check
  - .
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
  concurrency: 1
carry_forward_inventory: carry_forward_inventory.md
---
# Execution manifest

Carry-forward run for the model-preferences product scope on the reviewed
reconciled assembly. One worker for newly arising work assessment only;
imported-history issues are disposition-only and are never dispatched,
repaired, or recharged under this manifest. The approved
`carry_forward_inventory.md` binds the preserved predecessor history,
counters, and exhausted allowances; any byte change invalidates approval.
No imported-history repair, reassignment, or allowance renewal is authorized.
Human acceptance/deferral of imported dispositions follows the separately
recorded current-acceptance act, not worker dispatch.
