# Model preference commands

Date: 2026-10-30

This guide is for operators who inspect or change the models orch uses. The
commands edit validated TOML configuration only. They never approve, run or
resume a feature, never dispatch an agent, never contact a provider and never
read run state. Model IDs in examples are placeholders, not recommendations or
proof that a provider account supports them.

## Commands

```text
orch models show
orch models show --target <repository-path>
orch models show --feature <slug>

orch models set <role> --model <model-id> [--reasoning <effort>]
orch models set <role> --reasoning <effort>
orch models set replacement --reasoning high
orch models set replacement --reasoning inherit
orch models set <role> --model <model-id> --reasoning <effort> --scope repo
orch models set <role> ... --scope repo --target <repository-path>
```

The six roles are `planner`, `worker`, `replacement`, `code_reviewer`,
`docs_reviewer` and `conflict_resolver`. Role names, options and the root flag
are matched exactly; abbreviations and unknown roles fail.

Without a selector, `show` reads the global file and a `.orch.toml` discovered
from the current directory by ordinary Git root discovery. Outside a
repository it shows global-only preferences. `--target` selects a repository
and maps subdirectories to its root. `--feature` reads the registered feature's
canonical `feature.md` and its target repository. The selectors are mutually
exclusive.

`set` defaults to `--scope global`. Repository scope writes only the selected
repository's `.orch.toml`; outside a valid repository it fails. Feature
settings are never written by these commands, though they remain visible in
`show` and keep their existing precedence.

Both commands route before any run-state or configuration load, so they cannot
pause, resume or otherwise steer an active feature.

## Files and precedence

Preferences resolve global config first, then repository `.orch.toml`, then
explicit feature front matter. `show` prints absolute paths and the source of
each model and reasoning value: global, repository, feature, worker
inheritance, or unset. The platform configuration file is the same path every
reader and writer uses; inspect the printed path rather than guessing it.

Values are stored exactly as entered. Empty or whitespace-only values and
Unicode control characters, including the C1 range, are rejected. `inherit` is
a command word for clearing a replacement override; it is not a stored
reasoning value.

## Complete repository role declarations

A repository override for an ordinary role must declare both its model and its
reasoning. An absent role follows global settings; a partial or otherwise
invalid repository role is not merged field by field. Updating one field of an
existing complete repository role keeps the other explicit field, and omitting
a field never materializes an inherited value into the file.

Repository replacement declarations are different because replacement always
follows the final worker model. A repository replacement override contains the
fixed worker inheritance marker plus a reasoning value and never its own model:

```toml
[models.replacement]
inherit = "worker"
reasoning = "high"
```

`orch models set replacement --reasoning inherit --scope repo` removes that
repository override as a unit, so global replacement reasoning applies, or
worker reasoning when no global override exists. At global scope `inherit`
removes only the global reasoning override and retains worker-model
inheritance. Neither operation copies a worker model ID into the file.

## Selecting global preferences for one invocation

```text
orch --global-models models show --target <repository-path>
orch --global-models run <slug>
```

The root `--global-models` flag selects global model preferences instead of
repository role overrides for that invocation. It announces the choice and
does not change repository data directory, limits, viewer, notification or
redaction settings. Explicit feature model settings keep their existing
precedence, so the output names actual sources rather than claiming feature
pins were removed. Every permitted lifecycle command prints the announcement
before it acts or dispatches; it never silently switches the effective models.
The audited `recover-review` recovery command resolves the same preferences and
therefore prints the same announcement.

The flag is rejected with `models set`, because a write scope must stay
explicit, with `validate`, which does not select models, and with commands that
do not consume model preferences (`new`, `status`, `watch`, `pause`, `abort`,
`done` and `clean`). Unsupported commands and abbreviated spellings fail
without changing configuration or state.

## Invalid repository roles and consent

Each invocation reads configuration again. Without the flag, valid repository
model pairs are kept. For an identifiable invalid repository role, orch shows
the affected roles, reasons and a bounded table of the resolved effective model
and reasoning values with their sources, explains that global preferences are
being used for those roles, and only then asks a default-no question. A yes uses
the already-loaded snapshot; no, end-of-input or unavailable interactive input
exits nonzero with guidance to fix the file or use the flag. The same summary is
printed before the prompt on both inspection and lifecycle commands; denying it
performs no state load, dispatch or configuration mutation. Fallback never edits
configuration and never persists a decision.

Broken TOML is fatal before any confirmation, lock, staging file or dispatch,
even with the flag. Invalid unrelated settings, unknown roles and invalid
global settings also fail because they have no defined per-role fallback.
Inspection can still display genuinely unset preferences; it does not require
every role model to be configured or any provider to be authenticated.

Consent to fallback is not plan approval. If the effective settings of an
approved feature change, the next dispatch boundary requires renewed approval.

## Writes, shadowing and approval

A successful `set` names the changed file, role, explicit update and the
resulting effective view for the selected context. If a feature or repository
override shadows the written field, the output reports the higher-priority
source instead of implying the effective model changed.

Changed effective settings require renewed approval before dispatch. A setting
that is completely shadowed does not change the feature's effective model.
In-flight calls keep the settings they already resolved; orch does not restart
or hot-swap them. These commands never modify approval hashes, run state or the
feature registry.

Approval also pins the optional manifest `carry_forward_inventory` file, which
is reviewed input exactly like the optional `dependency_record`. Editing the
inventory after approval invalidates approval at the next stage boundary, even
though `models show`/`models set` never edit it themselves.

## Write safety and limits

Writes are strictly validated before replacement. Invalid existing content,
invalid proposals and permission errors leave the original bytes unchanged.
Diagnostics name the file, the declared field path and the validation type but
never reproduce the offending value, whether it is a webhook URL,
credential-shaped setting, an unrecognized model-role key or other configuration
or feature front-matter content. A key the schema does not declare, including one
placed inside a recognized role table, is withheld or shown only as an
unrecognized-field placeholder rather than by name. This also applies when
`models show --feature` reads an invalid registered `feature.md`. Orch locks the
actual configuration path with a sibling lock file, independent of any
data-directory override, captures the original identity and bytes, and compares
them again immediately before same-directory atomic replacement. Comments,
unrelated settings, unaffected role fields and raw relative paths are preserved.
Symbolic links, junctions, reparse points and linked parent directories are
refused for configuration, lock and staging write paths; a linked lock file is
rejected before it is opened. Read-only inspection is unaffected.

Cooperating orch processes cannot lose each other's updates. An external editor
that ignores the advisory lock can still race between the comparison and the
replacement; orch detects edits it observes before committing but cannot
provide a universal compare-and-swap guarantee against a hostile path swap. A
failure after the commit point is reported as committed rather than as
unchanged bytes.

Existing restrictions are retained on POSIX by preserving the file mode and
using an owner-only mode for new files. On Windows, orch copies the existing
file's discretionary access restrictions to the staging file before writing new
content, replaces the file with the native permission-preserving operation,
verifies file identity and the access restrictions before commit, and refuses
the write when they cannot be retained with the invoking user's access. It uses
per-user configuration and the selected repository only: no elevation,
machine-wide writes, ownership takeover or access broadening. The native
Windows implementation and its tests still must be exercised on native
Windows/PowerShell 7; Linux results do not establish ACL, reparse-point or
locking behaviour there.

## Related documentation

- `20261026_user_guide.md` — creating, approving and running features
- `20261026_contracts_and_configuration.md` — schemas, precedence and approval
