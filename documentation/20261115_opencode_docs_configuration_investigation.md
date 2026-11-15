# OpenCode documentation-review configuration investigation

- Date: 2026-11-15
- Status: investigation complete; noncritical, no production policy change
- Scope: backlog item 13, round-two requirement A4
- Installed CLI under test: OpenCode v2.3.0 on native Fedora Linux
- Evidence: new deterministic tests in `tests/agents/test_round2_docs_configuration.py`,
  existing CLI-backed integration test, and sanitized local probes with synthetic
  settings and isolated home directories

## Question

A documentation-review launch was reported to fail before any agent process started,
because configuration validation claimed that a later-loaded OpenCode configuration
added an allow rule after the generated documentation-review denials. The investigation
had to determine whether inherited user settings, OpenCode loading behavior, or an
overly broad validator check caused the refusal, and to confirm that the documentation
reviewer can read only its approved Markdown inputs and write only its verdict.

## Outcome summary

The refusal is a conservative safety decision, not evidence of a permission bypass.
No probe showed source or unapproved Markdown becoming readable, and no probe showed a
write outside the verdict path. The refusal arises from how the resolved configuration
report is ordered and how the validator interprets that order, not from an unsafe
effective policy.

Two distinct triggers reproduce the same refusal message with synthetic settings:

1. The generated profile document is reported twice, and the second copy is treated
   as a later configuration whose allow rules appear after the denials.
2. A separate configuration document that contains allow rules is genuinely loaded
   after the generated profile.

The first trigger is the most plausible explanation for the reported failure because it
involves the generated file itself, not a user configuration. It is an artifact of
resolved-configuration provenance, not of the generated policy's content.

## How the profile is prepared

The docs role receives a curated working directory outside every Git checkout. That
directory holds copies of the selected Markdown context. The generated profile is
written to a private temporary child directory of that curated directory, and the
reviewer process runs from that child directory. The profile allows reading only the
instruction file, the curated Markdown inputs and the verdict path, allows editing only
the verdict path, denies the wildcard, denies external directories except the exact
parent patterns needed for the inputs and verdict, disables shell, search, task, skill,
web and language-server tools, and disables plugins and MCP servers.

The adapter removes any inherited inline configuration content, points the explicit
configuration variable at the generated file, and points the configuration-directory
variable at an empty private directory. That empty override is important: the probes
showed it replaces the user-level configuration directory layer, so a real user
configuration is not inherited into this launch.

## Observed loading order and provenance

Probes used synthetic configurations only, with isolated home and state directories, an
empty private configuration directory, and a generated profile. No user settings were
read for their values and no credentials were printed. Observations:

- A user-level configuration document, when present, is listed before the user-level
  configuration directory.
- The generated profile is listed after those user-level sources when no directory
  override is used.
- When the directory override is used, the user-level document and directory disappear
  from the report and the generated profile becomes the effective document.
- When the generated file sits in the process working directory and is also selected
  explicitly, the same document is reported twice.
- When the generated file is outside the process working directory and selected
  explicitly, it is reported once.
- The version under test resolves the standalone invocation and the default service
  invocation differently: the standalone report showed the duplicate while the
  service-backed report showed a single document.

The last observation matters because the actual reviewer run uses a standalone
invocation, while the pre-launch validation call does not request standalone mode. The
validation and the run can therefore resolve configuration in different modes.

Representative sanitized provenance for the duplicate case, with values reduced to
structural facts:

```text
sources: 3
0 directory   private config-directory override   permissions: none
1 document    generated profile                   allow rules: exact read/write grants
2 document    generated profile                   allow rules: exact read/write grants
```

## Why the validator refuses

The validator locates the generated profile in the report, confirms the expected deny
rules and exact grants, and then inspects every document listed after the profile. It
refuses if any later document enables plugins or MCP servers, or contains any allow
rule at all. This is deliberately order-sensitive and deliberately blunt: a later
allow rule of any scope is treated as an attempt to widen access after the denials.

That logic is correct for a genuinely different later configuration. It cannot tell a
different later configuration apart from a second provenance entry for the identical
generated file. In the duplicate case the later entry contains the profile's own
allow rules, so the validator refuses with the message that a later configuration adds
an allow rule. The refusal happens before any process is created, so it is a
conservative refusal rather than a demonstrated weakness in the effective policy.

Isolated versus inherited outcomes, all reproduced deterministically in the new tests:

- A synthetic user-level allow rule listed before the profile is accepted, because the
  generated profile's denials remain the effective final state.
- The same allow rule listed after the profile is refused.
- A later allow rule that names one additional exact Markdown file, rather than the
  wildcard, is refused in exactly the same way. The refusal therefore does not depend on
  the later rule being broad, which is appropriate for a safety check but also means any
  later widening is refused rather than merged.
- The generated profile listed twice is refused.
- A later plugin or MCP document is refused.
- A report that omits the generated profile is refused.
- The generated profile's own grants are only the approved Markdown reads and the
  exact verdict write; source files and unapproved Markdown are absent from the grants.

## Containment evidence

- The CLI-backed integration test passes in this environment with a local fake
  provider: the source read is denied, the verdict is written at its exact path and
  the source sentinel never appears in the transcript or logs.
- The generated profile denies the wildcard, denies all listed tools, and disables
  plugins and MCP servers.
- Read and edit grants are exact file paths, not directories or patterns; external
  directory grants are the exact parent patterns needed for the copied inputs and the
  verdict.
- The profile accepts Markdown only: a non-Markdown instruction, supplied input or
  verdict path is refused before launch, so the review cannot be widened by renaming a
  source file into the curated set.
- The configuration directory override excludes the user-level configuration layer in
  the probes; inline configuration content is removed before launch.
- The new deterministic tests assert the exact grants and refusals without weakening
  the existing integration test, and they call the real validator rather than
  bypassing it.

This evidence supports containment of the documentation reviewer. It does not prove
that every future CLI version preserves the same loading order.

## Distinguishing a refusal from a bypass

A bypass would require evidence that a denied read, write, tool or plugin became
effective. None was observed. Every refusal occurred before process creation, and the
only successful runtime probe denied the source read and wrote only the verdict.
Therefore the correct characterization is: a safe, conservative refusal caused by
resolved-configuration duplication and order-sensitive validation.

## Limitations

- The installed CLI is v2.3.0; the reported failure occurred earlier, and the exact
  historical CLI build is not available here for re-probing. The historical cause is
  therefore strongly indicated but not proven.
- Why the service-backed report lists one document while the standalone report lists
  two is observed but not fully explained; it may be service caching or a deduplication
  difference. This is an OpenCode behavior, not something the adapter controls.
- The probes are Linux-only. No native Windows configuration-loading evidence was
  produced, and none is claimed.
- The user's real configuration was not inspected for values; only synthetic settings
  and isolated homes were used. A real later allow rule remains possible in some
  environments, and refusing it is the intended behavior.
- No production policy or validator change was made; none is authorized by this
  investigation.

## Proposed follow-up

For separate coordinator review, not implemented here:

1. Make the pre-launch validation resolve configuration in the same mode as the
   reviewer run, so validation and execution cannot disagree about provenance.
2. Treat repeated entries for the identical generated profile as one source rather than
   as a later widening configuration, while still refusing genuinely different later
   allow rules, plugins and MCP servers.
3. Record sanitized source count and source kinds in refusal evidence so a future
   failure can be distinguished from inherited settings without exposing values.
4. Track the duplicate-source behavior as a CLI compatibility observation, since future
   versions may change it.

These are proposals only. The current behavior fails closed, which is acceptable for
the noncritical investigation even though the reported failure is not fully explained
for its historical environment.

## Verification

The new deterministic test file passed on this environment, the existing CLI-backed
documentation-review permission test passed, and the repository-wide test and lint
results are recorded in the worker handover. Passing tests here do not constitute
native Windows evidence or a permanent guarantee of future CLI loading order.
