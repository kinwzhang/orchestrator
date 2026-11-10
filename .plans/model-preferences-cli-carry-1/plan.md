# Carry-forward run plan — model-preferences scope on the reviewed assembly

## Problem

The predecessor run `model-preferences-cli-5ec7f5ee` is stopped at phase
ESCALATED (schema v1) with solved product work, one historically escalated
issue (`code-w01-03`, allowances exhausted), and missing original per-entry
decisions that new code cannot recreate. The reconciled assembly `a966156`
(independently reviewed, pass/pass) carries the product behavior, the
disclosure sanitization, the lifecycle guarantees, and the one-shot importer
with its enforced dispatch gate. This run continues the product scope under
explicit predecessor linkage with history preserved and allowances intact.

## Scope (settled D1–D5)

Import the reviewed inventory, verify it, record the separately authorized
current-acceptance disposition for `code-w01-03`, then operate the product
scope ordinarily (new-work assessment, review, acceptance). Out of scope:
reopening/repairing/recharging imported history, blind product rework,
general checkpoint/archive redesign, live recovery of the old run, disclosure
integration beyond the reviewed assembly, Windows verification, paid/live
agent calls from tests.

## Interfaces and contracts

- Importer: `orch.carry_forward.import_carry_forward` (intent →
  per-issue import/audit → verify → completion); operation key
  `carry_forward_import`; digest canonicalization per the committed interface;
  refusal before any write on digest/lineage/counter mismatch.
- Dispatch gate: `require_carry_forward_import` / `carry_forward_gate_reason`
  first in `Engine.work` and `Engine.run_worker`; refusal reason
  `carry_forward_import_pending` with zero side effects; inert when the
  manifest field is `None`.
- Approval binding: `Manifest.carry_forward_inventory` in `approved_files()`,
  hashed by `approval_hashes()`; any inventory byte change breaks approval.
- Human disposition: `Engine.record_human_disposition` →
  `apply_human_disposition` is the only surface for escalated continuation or
  scoped acceptance/deferral (reviewers cannot construct one implicitly).
- Worker scope: single `w01` (code role) for newly arising work assessment
  only; imported-history issues are disposition-only and are never dispatched
  (dispatch consumes attempts while review refuses — residual E1).
- Reviewer scope: code reviewer judges new work + preservation against code;
  docs reviewer judges relevance/completeness from code-free inputs only;
  mixed new-work/history verdicts refuse pre-write (residual E2), so findings
  are scoped to new work.
- Lifecycle: schema v2; `pending`/`in_repair`/`finished`/`escalated` with
  guarded transitions; counters/assignments/history kept separate from
  activity; per-review entry application with resume-before-dispatch.

## Dependencies and scheduling

No worker dependencies (`depends_on: []`). Dispatch is refused run-wide until
the approved import completes (gate). Verification dependents wait without
consuming attempts; unresolved blocking issues still refuse promotion.

## Verification and acceptance

Full `uv run pytest -q` + `uv run ruff check .` at every candidate; the
assembled base already stands at 521 passed / 7 skipped with both independent
verdicts bound. New-run acceptance requires: exact candidate SHA, current
approved inputs, independent verdicts on this run's candidate where new work
is concerned, verification results, and human Fedora outcome. Windows and
live-operation acceptance remain separately recorded.

## Open issues

None blocking plan approval. Known limitations carried forward: lifecycle
history/activity not replayed (audited, not replayed); ordinal provenance
`None` with fail-closed reconstruction; 4 live-evidence skips + 2
Windows-only skips; no Fedora/Windows/live acceptance claimed.
