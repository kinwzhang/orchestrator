# Model preference commands — first self-hosted orch feature

Date: 2026-10-27  
Suggested feature slug: `model-preferences-cli`  
Status: requirement ready for orch planning; no implementation or run started.

## 1. User outcome

Let me inspect and change orch's model preferences from the terminal without editing TOML. Preferences are selected by role, and changes use the existing validated configuration layers. This feature is the first live delivery task executed by the reviewed orch application itself.

Keep the feature small: ordinary CLI commands, no interactive settings UI, provider integration framework, model benchmarking or automatic model selection.

## 2. Prerequisites and authoritative behavior

The separate replacement-reasoning change must be reviewed and integrated before this feature is approved for execution. Replacement inherits the effective worker model while optionally overriding reasoning. Do not reimplement that prerequisite here or silently omit it.

Use the current reviewed implementation and the approved bootstrap decisions as context. The latest user instructions override older statements that replacement must inherit worker reasoning. Preserve native Fedora and Windows/PowerShell 7 support.

The user's preferences for examples and acceptance fixtures are:

| Role | Model | Reasoning |
|---|---|---|
| planner | gpt-6.1-sol | low |
| worker | opencode/deepseek-v4.1-flash | low |
| replacement | inherits effective worker model | high |
| code_reviewer | opencode/gpt-6-luna | medium |
| docs_reviewer | opencode/gpt-6-luna | medium |
| conflict_resolver | opencode/gpt-6-luna | medium |

These are configurable preferences, not hard-coded application defaults. Model listing is not proof of account availability or provider support for reasoning variants.

## 3. Commands

### Inspect

```text
orch models show
orch models show --target <repository-path>
orch models show --feature <slug>
```

- With no selector, show global defaults plus any repository overrides discovered in the current directory using the existing repository/config discovery policy. Outside a repository, show global defaults only.
- `--target` explicitly selects repository context. `--feature` uses the registered feature and its canonical feature settings. These selectors are mutually exclusive.
- Show all six roles with effective model, effective reasoning (or provider default/unset), and the source of each value: global, repository, feature, or worker inheritance.
- Include relevant absolute configuration paths. Show that replacement model inheritance is separate from its optional reasoning override.
- Missing preferences are displayed as unset; inspecting configuration does not require all role models, authenticated providers, or installed agent CLIs.
- Show is read-only. It must not create a config file, start a feature, approve a plan, or invoke agents/providers. Human-readable Markdown table output is sufficient; JSON output is out of scope.

### Update

```text
orch models set <role> --model <model-id> [--reasoning <effort>]
orch models set <role> --reasoning <effort>
orch models set replacement --reasoning high
orch models set replacement --reasoning inherit
```

Optional selectors for set:

```text
--scope global|repo        # default: global
--target <repository-path> # only valid with repo scope
```

- Accept exactly the six role names listed above. Reject unknown roles and misspelled options with actionable errors.
- At least one change is required. Omitted fields remain unchanged at the selected layer; do not materialize inherited defaults into the file.
- Global writes use orch's existing platform configuration path. Repository writes use the selected target's `.orch.toml`, or the current repository if no target is provided. Reject repo scope outside a valid target repository.
- Do not write feature.md in this version. Feature-specific overrides remain supported by the existing feature configuration mechanism and are visible through show.
- Non-replacement roles accept model and/or reasoning. Model IDs and effort strings must be nonempty, free of control characters, and passed as data; do not hard-code today's provider/model catalog or silently normalize model IDs.
- Replacement does not accept `--model`; explain that its model comes from worker. It accepts a reasoning override. `--reasoning inherit` removes that layer's replacement reasoning override, revealing any lower-priority override or worker reasoning. The success message must show what it actually resolves to; removing a repository override does not necessarily erase a global high setting.
- Reserve `inherit` for this replacement behavior; reject it as a literal effort for other roles with an explanation.
- Successful output names the changed file, role, explicit update and effective result for the selected context. Explain when a higher-priority setting shadows the update.
- Never claim that schema validation establishes provider/model availability or reasoning support. No provider request, credential lookup, model discovery or paid call occurs during show/set.

## 4. Preserve configuration and write safely

- Use the same configuration models, merge precedence and path policy as ordinary execution: global < repository < explicit feature settings. Do not create a second interpretation of role preferences.
- Preserve unrelated keys/settings, comments and unaffected formatting in an existing TOML file. Use a format-preserving editing approach; the planner may justify a small dependency if necessary.
- Validate the proposed file and effective configuration before replacing it. Invalid TOML, unsupported settings, invalid role updates or permissions errors leave the original bytes unchanged and exit nonzero.
- If the target file does not exist, create the necessary parent directory and a minimal valid file containing only the requested override.
- Serialize concurrent model updates with a lock associated with the actual target config path, independent of run data-directory overrides. Prevent lost updates between separate orch processes. Detect incompatible external edits before replacement rather than overwriting them.
- Use an atomic same-directory write/replace and preserve existing file permission restrictions. Do not write a config file through an unexpected symlink or leak its contents in error output; define the symlink policy in the plan.
- No credentials belong in model settings. Do not dump unrelated configuration, environment values, auth files or credential-bearing webhook values to stdout/logs.

## 5. Approval and running-feature behavior

- Updating preferences must not approve, resume or dispatch anything, edit an approval hash, or mutate internal feature/run state.
- The existing approval mechanism remains authoritative: if the effective settings of an approved feature change, its next dispatch boundary requires renewed approval. Keep in-flight calls on their already-resolved settings; do not restart or hot-swap an active call.
- A setting completely shadowed by a feature override must not be described as changing that feature's effective model. Show provenance clearly enough that the user can understand why an update did not affect it.
- Report that changed effective settings require reapproval. Do not silently modify all features or enumerate/mutate their states merely to update global preferences.

## 6. Acceptance criteria and tests

1. Show works outside a repository with a missing global config, lists unset roles, and writes nothing.
2. Global set creates a valid minimal file in a temporary config directory; subsequent show reports the value and its source.
3. Updating one role/field preserves all unrelated settings, existing comments, other role fields and restricted file permissions.
4. Repo set affects only the target `.orch.toml`; global defaults remain unchanged. Show demonstrates global/repo/feature precedence and field-level provenance.
5. Replacement inherits the final worker model, can independently use high reasoning, and follows worker model changes without changing its override. Resolving it never mutates the worker spec.
6. Clearing replacement reasoning via inherit exposes the correct lower-layer setting. Reject replacement model updates without modifying the file.
7. Invalid TOML/settings, unknown roles, missing updates, invalid selectors, empty/control-character values, lock failures and permission errors give useful errors and preserve existing bytes.
8. Concurrent subprocess updates to different roles do not lose either update. Interrupted writes leave an intact old or new file. Detect a conflicting external edit under the documented policy.
9. An approved feature with a changed effective preference cannot dispatch until reapproved. Changing a fully shadowed setting does not incorrectly change that feature's effective model. Neither show nor set itself dispatches or approves.
10. CLI tests prove provider/agent adapters are never called by these commands. Include meaningful end-to-end CLI tests with isolated config homes/data directories; never alter the developer's real preferences during tests.
11. Existing tests and repository-wide Ruff pass. New command behavior and path/locking policy are exercised on native Fedora and native Windows/PowerShell 7 before claiming both platforms verified. Record unavailable platform gates honestly.
12. User documentation explains commands, precedence, replacement inheritance, actual file locations, shadowing and reapproval, with copyable examples. AGENTS.md changes are needed only if contributor guidance changes.

Deterministic tests must use temporary files and stubs with no paid calls. Test errors and cross-process safety, not merely the implementation's private helper return values.

## 7. Self-hosted delivery and operational acceptance

Use a pinned, reviewed orch runner that includes the replacement-reasoning prerequisite. Keep that executing installation separate from the source/worktrees being changed so the feature cannot hot-modify its own runner halfway through the run. Record runner version/commit and interpreter/agent CLI versions.

The user will start the feature with this requirement file against the clean orchestrator feature checkout, using `feature/orchestrator-bootstrap` as the base branch, not the original dirty main checkout. The planner defines at most four workers with disjoint scopes and explicit interfaces; prefer one or two workers if that is sufficient. Bootstrap ownership guidance is context, not a requirement to launch four sessions for a small change.

Use step mode for the first self-hosted run. Demonstrate through orch itself:

1. Interactive planning writes canonical Markdown artifacts and checkpoints discussion.
2. No implementation dispatch occurs before explicit plan approval.
3. Orch creates/uses owned worktrees and dispatches workers from instruction files; physical Markdown checklist, activity log and issue register remain available.
4. Deterministic checks run before reviewers. Code review checks the immutable integration candidate against this requirement; docs review assesses relevance using only permitted code-free documentation/handovers.
5. Both verdicts pass for the same final candidate, or orch performs bounded rework/escalation under the approved issue budgets.
6. Only the task's feature branch is promoted; no main merge, push, deployment or destructive cleanup.
7. Human tests the resulting commands and records the final outcome. Native Windows and live-agent limits are recorded separately from deterministic/Linux results.

This is a real model-using run and consumes the user's configured provider quota. Do not start it merely by reading or planning this requirement. Explicit feature approval and run commands remain the normal gates. Do not manufacture a defect to force rework in this task; bounded rework can be observed if a real finding occurs, with deterministic tests covering the routing otherwise.

Keep handovers, check feedback, verdicts and final reports in the shared external run directory. Agents act on Markdown paths, never internal JSON or prior conversational context.

## 8. Deliverables and out of scope

Deliver implementation, meaningful tests, relevant user documentation and Markdown agent handovers/verdicts/final report through the normal orch workflow. The planner must specify exact modules, scopes and interfaces; this requirement does not prescribe a new architecture.

Out of scope: interactive model picker/TUI, provider login/auth management, automatic model discovery or benchmarking, automatic substitutions, independent replacement model selection, commands that rewrite feature.md, batch feature mutation, API-key storage, and changes to repair/reassignment budgets. Existing unrelated bootstrap findings are not implicitly reopened by this feature.


## Human-approved stage-one platform acceptance amendment

The operator explicitly directs that stage one deliver a fully usable orch workflow on native Fedora. This amendment overrides earlier requirements that make unavailable native Windows evidence a blocking gate for this stage.

Native Windows/PowerShell 7 ACL preservation, junction/reparse-point refusal, locking, replacement, interruption and conflict verification is deferred as a documented caveat. Preserve Windows compatibility intent, existing safeguards and tests. Do not claim Windows verified or mark skipped tests as passed. Actual known implementation defects remain findings; this exception concerns missing native evidence only.

For this run, code-w01-02 is no longer a blocking stage-one prerequisite insofar as it requests unavailable native Windows acceptance evidence. The owning code reviewer may resolve the current-stage blocker by explicitly recording the human-approved acceptance deferral, with the Windows caveat retained in delivery documentation and the final report. This is an acceptance-scope disposition, not technical verification of Windows behavior. Preserve prior verdicts and issue/counter history. Do not resolve code-owned issues through the docs role or manual state edits.

Code-w01-03 and code-w01-05 remain implementation repair requirements. Existing Fedora checks, security requirements, review ownership, issue budgets, dual-review candidate gates and human final acceptance remain in force. Missing Windows evidence alone must not trigger repeated Fedora repair attempts or block current-stage promotion. Retain a named Windows verification follow-up before any later claim of verified Windows support.

These approved-input changes require orch approve before continuation and fresh review against the amended acceptance. No approval, reviewer verdict, run state or counter is changed by this amendment itself.


## Human-approved diagnostic-location risk acceptance

The operator explicitly accepts the remaining diagnostic-location defects for Fedora stage one: malformed YAML syntax errors may report a line one earlier than the actual Markdown source line, and duplicate/non-string-key errors may omit available numeric locations. These are nonblocking diagnostic-usability caveats for this stage. Accurate/complete location reporting is deferred; retain a named follow-up and do not claim it fixed.

This amendment supersedes safe-location/type acceptance criteria only insofar as they require accurate or complete numeric locations in these identified cases. Strict rejection, bounded safe diagnostics and non-disclosure of supplied input remain required. This is not acceptance of source, key or value disclosure.

Independent code reassessment confirmed the original source-disclosure requirement satisfied in isolated candidate 1ef6041c2f0d01712d9b038aa5bc5dd57d214b12. That evidence does not certify the older live integration candidate de066bc7663cab6d3cd5d896b0c8e0b8b7174bce, which lacks the repair. Integrate the reviewed non-disclosure fix through an authorized, reviewed candidate/recovery path before accepting current-stage code-w01-03. The owning code reviewer must confirm the actual resulting candidate contains the fix and disposition the remaining location defects explicitly as human-accepted caveats. Docs review cannot resolve code-owned findings.

Preserve all historical verdicts, exhausted original/replacement allowances, escalation lineage and operator decisions. No counters or state may be edited manually. This amendment does not grant another automated repair allowance, authorize unrelated changes, or waive LIVE-REVIEW-003 transaction-accounting integrity. Approved-input changes require reapproval and current-candidate review through the supported recovered workflow. Preserve Fedora-first acceptance and the separate deferred Windows verification caveat.
