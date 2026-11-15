# Enhancement backlog after the first delivery

This is the retained summary of the discovery checklist. Detailed historical repair records are archived. Linked requirement files remain the reference for future approved work; a backlog entry does not authorize implementation or live dispatch.

| # | Area | Disposition | Requirement |
|---|---|---|---|
| 1 | Planner discussion before readiness | Deferred workflow improvement | Historical LIVE-PLANNING-001 in archive |
| 2 | Dependency-driven scheduling / earliest-ready jobs | Full requirement pending; minimum graph behavior delivered | [Requirement](../feat_requirement/20261030_dependency_driven_scheduling.md) |
| 3 | Provider/dispatch diagnostics | Deferred | Historical LIVE-DISPATCH-001 in archive |
| 4 | System-local watch timestamps | Pending | [Requirement](../feat_requirement/20261031_watch_local_timezone.md) |
| 5 | Status after next returns | Pending | [Requirement](../feat_requirement/20261031_next_status_summary.md) |
| 6 | Finding category and impact estimates | Pending | [Requirement](../feat_requirement/20261031_issue_impact_classification.md) |
| 7 | Planner-led escalation recovery | Future workflow | [Requirement](../feat_requirement/20261031_planner_led_escalation_recovery.md) |
| 8 | Finding dependency assessment | Minimum graph behavior delivered; fuller assessor workflow pending | [Requirement](../feat_requirement/20261101_finding_dependency_triage.md) |
| 9 | Per-issue lifecycle and resumable review application | Implemented, reviewed and activated; current delivery completed | [Requirement](../feat_requirement/20261101_per_issue_explicit_progress.md) |
| 10 | Resolve foreseeable overlap before dispatch | Applied to current reconciliation; automated enforcement not claimed | [Requirement](../feat_requirement/20261110_pre_dispatch_overlap_resolution.md) |
| 11 | Tabular status | Pending | [Requirement](../feat_requirement/20261110_status_table_layout.md) |
| 12 | Accurate/complete YAML error locations | Accepted nonblocking stage-one caveat; disclosure fixed | Historical diagnostic-location decision in archive |
| 13 | OpenCode documentation-review configuration compatibility | Round-two investigation; noncritical | Checklist below |

## Round-two investigation checklist

- [ ] Investigate the OpenCode documentation-review permission test failure observed on 20261115. The documentation reviewer should verify worker documentation and handovers against the assigned instructions; source-code access is unnecessary. The test failed during pre-launch configuration validation because OpenCode reported a later-loaded configuration containing an allow rule. Identify the configuration and determine whether the cause is inherited local settings, CLI loading behavior, or an overly broad validation check. Preserve the documentation-only review boundary and verify that the reviewer can read its approved inputs and write its verdict. Treat this as a noncritical investigation, not an established permission bypass or a configuration change during an active review. Reproduction: uv run pytest -q tests/agents/test_agents.py::test_opencode_docs_policy_denies_source_read_and_allows_verdict. Baseline: full suite 535 passed, 7 skipped, 1 failed; lint passed.

The model-preferences delivery completed via model-preferences-cli-carry-1. Predecessor lineage and exhausted allowances were preserved; the carried disclosure acceptance has an explicit reconciled audit. Native Windows remains a separate unverified follow-up. Do not interpret completed work as permission to reset budgets or claim historical recovery.
