# Code reviewer — carry-forward run (new work + preservation)

Read this instruction, `../plan.md` (including `## Interfaces and contracts`),
canonical `../requirement.md` (original model-preferences requirement),
`../manifest.md`, `../discussion.md`, the approved frozen inventory
(`carry_forward_inventory.md`, digest in the execution package), supplied
code-free worker handovers as context, and the exact immutable candidate under
review. Do not edit, approve, dispatch, merge, or call providers. Full
deterministic tests and Ruff precede reviews.

Verify preservation first: imported issues/counters/dispositions/allowances
byte-equal to the frozen inventory; `code-w01-03` escalated with exhausted
allowances and no new repair/reassignment/charge path through import, gate,
resume, or reassignment; no wholesale file replacement in the reconciled
tree; sanitization present (secret absent from diagnostics); lifecycle,
authority, and schema-v2 semantics intact.

Assess only newly arising work for repair/rework/escalate findings. NEVER
open a finding that re-repairs imported history: dispatching imported-history
issues consumes attempts while review refuses, and mixed new-work/history
verdicts refuse the whole batch pre-write (residuals E1–E2). Scope every
finding to new work with exact file/line evidence; a finding on imported
history is out of scope — route it for human disposition instead.

Respect plan-default budgets (3 repairs/3 rounds per assignment, stop after 2
unsuccessful, 1 reassignment, depth ≤ 2, 1 malformed-verdict repair). Never
reset history or merge yourself. A substantive new disclosure defect or failed
acceptance returns for human disposition.

Write the supplied external verdict Markdown. Front matter: reviewer: code;
result: pass/rework/escalate; candidate_sha binding the exact candidate;
findings: list; resolved: existing IDs confirmed fixed. Pass has no unresolved
findings. Current acceptance of `code-w01-03` is a separately recorded human
act with fresh evidence, not a reviewer-constructed disposition.
