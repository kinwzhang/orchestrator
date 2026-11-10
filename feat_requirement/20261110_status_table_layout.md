# Tabular orch status output

## Operator requirement

Present orch status in tables for easier scanning. Preserve a compact run summary and clear plan/run paths. This is a future presentation task, not a change to the active carry-forward run's approved inputs.

## Presentation

Run summary includes slug, phase, stage, cycle and next action; prominently show a blocker or required human action where present.

An agent table includes agent/role, current activity or status, attempt and checks. Keep absent values explicit/blank consistently; never imply old checks apply to a new attempt.

An issue table includes ID, lifecycle, assignment/reassignment and attempt detail, owner, blocker/dependency information where available, and a concise summary. Show legacy disposition separately or with a clear annotation without obscuring current lifecycle. Allowance counters and activity are not lifecycle states. Preserve finished/deferred distinctions and relevant root/parent links; long explanations may appear below the table.

Review-entry application/cursor remains distinct from issue lifecycle; expose pending application/gaps when relevant without relabeling issue status.

Use terminal-width-aware layout with wrapping, sensible column priorities and no loss of issue identity, current lifecycle or blocker meaning. Support narrow terminals and plain/no-color redirected output. Prefer existing terminal rendering dependencies; do not add a dependency merely for a static status table if existing facilities suffice. Do not require color to understand status.

Show deterministic ordering suitable for comparing consecutive outputs. Empty agent/issue sets should render a clear compact message. Avoid unreadably expanding the screen with raw agent logs; logs remain the responsibility of watch/detail surfaces.

## Safety and verification

Status remains read-only: no workflow advancement, locks that alter execution, state/journal/approval mutation or model calls. Preserve command exit behavior. Exercise representative long summaries, empty sections, legacy annotations, pending review application and blocker states at narrow/wide widths with temporary fixtures. Test semantic field preservation rather than brittle full-screen snapshots.

If combined with the future requirement to show status after orch next returns, reuse the same renderer without changing lifecycle behavior. Local-time formatting follows the existing dashboard timezone requirement where timestamps are displayed.

No source implementation, new live dispatch, schema migration or scope amendment is authorized by recording this requirement.
