# Contracts and configuration

The orchestrator reads agent contracts as UTF-8 Markdown with YAML front matter
delimited by lines containing `---`. Unknown keys, duplicate or non-string keys,
unsafe YAML tags, invalid enums, unsafe paths, and inconsistent verdicts fail
validation. Errors identify the contract file. Human prose follows the closing
delimiter. Agent instructions must be complete in files; internal JSON is never
an agent input.

Use `orch validate <kind> <file>` to validate a contract. Supported contracts
include feature, manifest, discussion, handover and verdict. Templates ship in
`src/orch/templates/`; replace placeholders before execution. A handover must
describe actual changes and include an `## Interfaces and contracts` body
section; the reader enforces the section heading. Schema validation does not
judge semantic correctness; that belongs to reviewers.

## Configuration precedence

Global `config.toml` uses the platform config directory (`platformdirs`),
followed by target-repository `.orch.toml`, followed by explicit feature front
matter. Each TOML layer is validated and errors name its source file. Relative
configured paths are resolved from the containing file. `data_dir` defaults to
the platform state directory, outside target worktrees; a run contains
`state.json`, durable machine events, logs and Markdown evidence. Keep this
directory on a local durable filesystem. Back up unfinished worktrees and run
state together.

Configure roles explicitly:

```toml
[models.planner]
model = "YOUR_CODEX_MODEL"
reasoning = "OPTIONAL_PLANNER_REASONING"

[models.worker]
model = "PROVIDER/CODER_MODEL"
reasoning = "OPTIONAL_WORKER_REASONING"

[models.code_reviewer]
model = "PROVIDER/STRONG_REVIEW_MODEL"
reasoning = "OPTIONAL_REVIEW_REASONING"

[models.docs_reviewer]
model = "PROVIDER/SEMANTIC_MODEL"
reasoning = "OPTIONAL_REVIEW_REASONING"

[models.conflict_resolver]
model = "PROVIDER/STRONG_CODING_MODEL"
reasoning = "OPTIONAL_RESOLVER_REASONING"

[models.replacement]
inherit = "worker"
reasoning = "OPTIONAL_REPLACEMENT_REASONING"
```

These are placeholders, not available model recommendations. Replace reasoning
placeholders with values supported by the selected adapter, or omit the
setting to leave that adapter's default. The selected CLI must support the
supplied provider/model IDs. Replacement always resolves to the effective
worker model after global, repository, and feature overrides and starts a new
session. A replacement cannot specify a separate model: its configuration
accepts only `inherit = "worker"` and an optional reasoning override. Resolving
a replacement returns a fresh model specification, so a stronger replacement
reasoning setting does not change ordinary worker dispatches. Without an
override the replacement retains the worker's complete model and reasoning.
Optional reasoning settings must be supported by the selected adapter;
unsupported settings fail before dispatch rather than silently downgrade.
Never put API keys in feature or config files. Authentication remains in the
agent CLI's existing credential mechanism; configure log redaction separately
for secrets supplied through environment variables.

## Scope and approval

Scopes use anchored POSIX repository-relative glob paths regardless of the host
platform. `*` matches within one component; `**` spans directories, including
zero directory components. Backslashes, drive letters, absolute paths and
traversal are rejected. Scopes cannot include `.plans`; workers have disjoint
scopes. Ambiguous wildcard intersections are conservatively rejected: use
explicit subdirectories instead. Symlink resolution cannot escape the target
root. Windows case collisions are rejected even when planning on Fedora.

Approval covers the feature settings, requirement snapshot, discussion, plan,
manifest, reviewer instructions and every worker instruction. It also covers
the normalized effective model/limit settings including a replacement
reasoning override, run mode, plan/history options,
configured checks and reviewer paths, and notification/viewer/redaction policy.
Only environment variable names are included; secret values are never hashed
into or copied to approval metadata. `contracts.approval_hashes` computes
the recorded hashes; `contracts.detect_approval_changes` reports edits, missing
inputs and any input not covered by the recorded approval. Report/history
outputs and approval metadata are excluded. Effective settings must be pinned
so a global configuration edit cannot silently change an approved run. Edits
invalidate approval at the next stage boundary; re-approval preserves only
work whose relevant inputs remain valid.

Use argv arrays for portable checks. A string command must select
`shell = "bash"` or `shell = "pwsh"`; arbitrary shell scripts are not translated
between platforms. Do not set a shell policy on an argv command.

## Persistence and operator evidence

State updates use a revision check under a file lock, a same-directory temporary
file, flushing and atomic replacement. On Linux the containing directory is
also synced; Windows replacement retries briefly when a reader transiently
holds the destination. A single active-run lock prevents concurrent runs and
per-feature locks serialize commands. Watch reads snapshots without taking the
writer lock. Filesystem/OS durability remains subject to local disk guarantees;
process shutdown cannot recover bytes an agent never wrote.

The shared persistence interface is:

| Operation | Contract |
|---|---|
| Load | `StateStore.load(run_id)` returns a validated version-one run state. Missing files raise the operating-system error; malformed state and unsupported or missing schema versions raise a validation error that names the state file. |
| Save | `StateStore.save(state, expected_revision=None)` returns a copied state with the next revision. If omitted, the supplied state's revision is the expected revision; a mismatch is rejected rather than overwriting newer work. |
| Event | `StateStore.event(run_id, kind, **details)` atomically records an event with a monotonically assigned integer ID and UTC timestamp, then refreshes the activity log. |
| Operation | `StateStore.record_operation(state, key, value)` persists and journals an operation record. Call once with intent before a side effect and again with result after it; the stable key merges both records while retaining the original first-seen time. |
| Lock | `StateStore.lock(name="active", timeout=0)` is an exclusive context manager. Run and lock names are restricted to portable letters, numbers, hyphens and underscores. |

Run state uses schema version one and a nonnegative revision. Unsupported schema
versions are rejected; there is no implicit migration. The `save` method's
expected-revision default is the revision currently present on the supplied
state. Callers that retain stale state must pass the revision they expect and
handle the validation error rather than retrying an overwrite.

Each run continuously maintains physical `checklist.md`, `activity_log.md` and
`issues.md`. The checklist records phase/stage/round, next action and worker
status, role, attempt, branch/worktree, checks, handover/feedback pointers,
retry count and blockers when available. The activity log renders the
timestamped machine event journal with stable event IDs. The issue register
preserves parent/root lineage, depth, status, repair/review counters per
assignment, reassignment history and evidence links. State and event journals
are authoritative; Markdown mirrors can be regenerated after a crash without
duplicating event IDs. Operation intent is persisted before a side effect and
completion is recorded afterward. Do not edit internal state to steer a run;
use CLI commands and approved Markdown instructions.

Per assignment, allow up to three repairs and three review rounds, stopping
after two unsuccessful repairs. One fresh replacement receives its own
allowances while retaining issue identity/history. Repair-failure accounting is
distinct from review-round accounting: an approval-only revalidation may accept
a fresh verdict for the current inputs without charging a second failed repair
for unchanged worker work. The per-assignment assessment mark records the repair
outcome already judged; a missing mark is derived from completed review
transactions and retained evidence, and ambiguous or contradictory histories are
refused rather than reinterpreted from counters. A finding recorded before the
first repair establishes an initial-work assessment but does not consume a
failed-repair allowance. A root issue can have child
and grandchild issues; discovering a great-grandchild escalates the root
without dispatching its fix. A malformed verdict gets one repair call before
escalation.

## Error categories

`contracts.error_category` maps failures to stable categories for workflow
routing: `validation`, `approval_required`, `infrastructure`, `check`,
`conflict` and `interrupted`. Contract/schema errors are validation failures;
input edits require approval; failed deterministic commands are check failures;
git conflicts and operator cancellation/timeout remain distinguishable from
infrastructure errors. The workflow owns the lifecycle transition; adapters
do not select phases.
