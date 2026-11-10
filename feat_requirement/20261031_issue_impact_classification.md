# Issue classification and operator-facing impact estimates

## Requirement

Future reviewer findings and operator summaries must explain practical impact and acceptance consequences rather than presenting severity alone. This is a future approved implementation task, not an immediate change to live verdict schemas or current approved inputs.

## Classification

Use a primary category with optional secondary tags:

- Critical: a required core outcome cannot be achieved without fixing the issue. This category describes requirement failure, not automatically critical security severity.
- Nice To Fix: ordinary required workflows remain achievable, but a stated edge condition fails. Name the condition and affected requirement. The label never silently waives a binding acceptance criterion.
- Security: confidentiality, integrity, authorization or availability risk, including disclosure and unauthorized access. State demonstrated behavior, prerequisites, exposure surface, scope and uncertainty. Security is a category, not automatically maximum severity.
- Recommended: implementation or maintainability improvement without a demonstrated acceptance failure or security defect. Normally advisory.
- Verification Gap: evidence is missing; do not claim a defect has been demonstrated. Distinguish current blocking evidence from an explicitly deferred platform caveat.

Categories may overlap. Keep the existing blocker/major/minor severity axis distinct. Avoid labeling an issue Critical solely because it is Security or blocks an optional improvement.

## Required estimate per finding

Include primary category/tags, severity, affected requirement, trigger/preconditions, concrete consequence and affected users/data, demonstrated evidence and confidence, estimated exposure likelihood with assumptions (qualitative unless evidence supports numbers), and acceptance disposition: blocking, nonblocking caveat, or advisory. Explain why it blocks when applicable. Estimate repair effort only when enough implementation evidence exists; otherwise say unknown. Do not invent numerical risk scores or promise mitigation without evidence.

Give operators a brief plain-language explanation. A rare corner case can remain blocking if an explicit security/acceptance requirement covers it. Only human-approved scope changes may defer a required gate. Preserve reviewer ownership, historical verdicts and budget accounting. Introduce schema changes through a reviewed plan with legacy compatibility; do not add unsupported front-matter keys to current live verdicts.

## Motivating example

Current code-w01-03 is Security (conditional diagnostic disclosure). An untrusted extra configuration field name is echoed through a validation-location formatter. Sensitive text used as that key could appear in terminal output and captured logs. The finding demonstrates disclosure of supplied key text; it does not establish unauthorized access, remote exploitation or automatic credential collection. Ordinary valid configuration workflows can function. Its acceptance blocking status comes from the explicit diagnostic non-disclosure requirement; the present reviewer severity is major. Frequency is unmeasured and depends on malformed input containing sensitive key text and on who can access diagnostics.

## Acceptance

Review examples must distinguish a core workflow failure, a corner-case failure, a conditional security disclosure, an advisory refactor and missing native-platform evidence. Operator output must expose both category and acceptance disposition without changing lifecycle routing based on display labels alone. Existing severity and action policy remain authoritative until an approved implementation explicitly changes them.
