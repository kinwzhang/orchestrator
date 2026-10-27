# Agents, processes and native platform support

## Invocation contract

`AgentRequest` gives the role, logical agent ID, attempt ID, canonical instruction file,
expected Markdown output file, working directory, model, timeout, additional Markdown
inputs, explicit external log directory and additional writable roots. Adapters never
select lifecycle transitions. Known configured secret literals and recognizable credential
patterns in the result artifact are sanitized and rejected for repair before returning success.
Unknown secret formats are not reliably detectable, and the agent writes its artifact before
this postflight inspection. A zero process exit is not an approval: workflow must
validate the output artifact and its contents.

`CodexAdapter` runs planning/finalization through `codex exec --json` with an explicit
model, workspace-write sandbox, output-last-message, working directory and approved
additional writable roots. `interactive_argv(request)` supplies an inherited-terminal
interactive planning command. This interactive launch is distinct from the asynchronous
headless run; workflow owns interactive interruption and discussion checkpoint validation.

`OpenCodeAdapter` runs workers/reviewers/resolvers through a fresh `opencode run`
with `--standalone --format json --model ... --auto`. No implicit session continuation
or shared background service is used. The standalone backend is required so cancelling
the owned CLI tree can terminate its backend. Explicitly denied permissions remain denied;
the adapter does not bypass the user's policy. A worker replacement uses the same coder
model in a fresh session and retains its worktree through workflow orchestration.

Capability preflight reads only `--version` and `exec/run --help`. Missing required flags
are errors, not silently degraded operations. These probes make no model calls. They
verify CLI capabilities, not credentials, provider model availability or billing access;
a provider rejection remains an infrastructure error for workflow to handle. No models
or credentials are inferred from an environment dump.

Installed versions probed on Fedora: Codex CLI 0.164.0 and OpenCode v2.1.4. OpenCode v2
reasoning variants use `provider/model#variant`; older CLI variants are accepted only
when the CLI advertises `--variant`. Codex reasoning uses explicit config override.
Unsupported reasoning mechanisms fail before dispatch.

## Evidence and live output

Each call produces sanitized `stdout.log` and `stderr.log` in its explicit external
attempt log directory. `ProcessRunner` emits process-start events carrying PID and
process creation time, then timestamped output events. Callbacks can be synchronous
or asynchronous; callback failure terminates the owned process tree rather than leaving
an unmanaged job behind. Persist start identity before treating an agent as dispatched.

The runner buffers complete lines before redaction, so a configured secret split across
OS reads is not partially written. Configured literal secrets and common token/password
patterns are removed before disk persistence or callbacks. An output line over 1 MiB is
rejected to bound buffering. This is a trusted-local tool: arbitrary unknown secret formats
cannot be guaranteed detectable. Never supply credential files as task inputs. Configure
literal redaction values from selected environment variables; never log the environment.

The result records exit code, duration, timeout reason, log paths, optional session ID,
usage supplied by recognizable JSON events and CLI version. Missing usage is an empty
mapping, never an estimated token count. Raw unredacted JSON is not persisted separately.

## Cancellation and restart

On Fedora, each invocation is launched in an isolated process session. Cancellation checks
the saved leader creation time and group identity while the leader is alive. If it exits
before a child closes inherited output pipes, the still-active invocation may signal only
a surviving member of that isolated session; a recovered handle requires verified process
identity instead. A stale saved PID alone never authorizes a process-group signal. On
Windows, a new process group is created and psutil terminates the verified process tree.
Timeout and task cancellation both perform cleanup; cancelling the coroutine propagates
cancellation after cleanup.

Workflow owns recovery: persist `ProcessHandle(pid, create_time)` from the start event,
check identity before touching a surviving process and retain worktrees and partial output.
PID alone is never sufficient. `reconcile_events(events)` inspects persisted process-start, descendant and completion events and returns alive/exited/reused identities without killing or relaunching anything. The runner records descendant identities periodically; workflow must escalate if surviving ownership cannot be established. A hard crash can precede descendant discovery, particularly on Windows, so do not silently relaunch an uncertain interrupted dispatch. Closing a terminal can terminate an interactive planner;
only already-written discussion decisions are recoverable.

## Shell and Windows shim policy

Normal commands use argument lists with `shell=False`. A shell-form check must explicitly
choose `bash` or `pwsh`; the runner does not translate shell scripts. Bash runs with `-lc`.
PowerShell runs with `-NoLogo -NoProfile -NonInteractive -Command`, and explicit native
exit-code propagation.

Executable lookup checks PATH and Windows `.exe`, `.cmd` and `.ps1` shims. Prefer a sibling
`.ps1` shim when an npm `.cmd` shim resolves, because PowerShell scripts avoid cmd.exe's
second expansion layer. PowerShell shim invocations use an encoded script containing
single-quoted, correctly escaped literal arguments, so dollar expressions and backticks
in paths/prompts are not evaluated as PowerShell code.

Batch-only `.cmd`/`.bat` shims are supported for simple arguments, including spaces.
Arguments containing cmd expansion/control characters or line breaks are rejected with
an explicit instruction to use an executable or `.ps1` shim. This limitation applies to
multiline agent prompts through batch-only installations; do not silently flatten prompts
or weaken quoting. Native Windows launch, long paths and process termination still require
verification on the user's Windows machine; mocked argv tests are not native proof.

## Review permissions and optional helpers

The docs reviewer receives an explicit relevance-only instruction. Its working directory
and input documents must be prepared by workflow as documentation-only, with no code,
source diffs or code-bearing logs. The adapter does not claim OS-level containment:
OpenCode permissions must restrict external reads where supported; prompts alone cannot
prevent a hostile agent from reading arbitrary user files.

Agent-native attachment is currently reported unavailable. The supported read-only live
view is `orch watch` plus sanitized logs; native attachment needs a version-specific
experiment before activation.

`launch_viewer` opens a tmux split inside an existing Fedora tmux session or a Windows
Terminal tab with PowerShell 7. If unavailable it returns the manual watch command.
`notify` supports a configured HTTP(S) webhook and Fedora `notify-send`; unavailable desktop
notifications, including native Windows toast support, return an explicit diagnostic.
Notification failures never erase reports or repeat merges. Webhook invocation sends only
a bounded, already-redacted summary; credentials/URLs are not included in failure text.

## Verification

Run `uv run pytest tests/agents` and `uv run ruff check src/orch/agents.py
src/orch/processes.py src/orch/platform_support.py tests/agents`.

The suite exercises split-read secret redaction, separate streams, nonzero exits,
timeout process-tree cleanup, cancellation, callback failure cleanup, shell exit status,
explicit model/variant command construction, fresh isolated sessions, missing capabilities,
Markdown input/output constraints, usage events and mocked PowerShell/batch shim quoting.
Real capability probes can run without launching a model. Real-agent and native Windows
acceptance runs remain separate acceptance gates.
