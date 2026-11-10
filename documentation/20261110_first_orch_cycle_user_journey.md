# First orch delivery cycle: user journey

This walkthrough describes the first completed orch delivery journey: adding model-preference commands to orch itself. The journey combined orch-managed execution with manually coordinated planning, implementation and review. The final carry-forward cycle completed worker execution, checks, independent review and human acceptance through orch.

## 1. Describe the desired outcome

The user began with a Markdown requirement: inspect and update model preferences through the CLI, with clear configuration precedence and safe writes.

The requirement defined the intended behavior and acceptance criteria. Markdown provided the shared reference for the planner, workers, reviewers and user.

## 2. Configure the agent roles

The user selected models for planning, coding, code review, documentation review and conflict resolution. Replacement workers inherited the coder model with a separate reasoning preference.

During the final cycle, DeepSeek V4.1 Flash handled the worker assignment and Muse Spark 1.3 Contributor handled the reviews through OpenCode Go. Model preferences became part of the approved execution settings.

## 3. Create an isolated feature workspace

The user invoked orch new with a feature name, target repository, requirement and base branch.

orch created an owned feature checkout and a canonical plan directory. Run evidence lived outside the source worktrees, while implementation changes stayed isolated from main.

The user could locate both the plan and run directories through orch status.

## 4. Discuss and approve the plan

The planner translated the requirement into a concrete delivery plan. Discussion settled scope, acceptance, worker responsibilities, dependencies and verification.

The user reviewed the plan and explicitly approved it. This created the boundary between proposing work and executing it. Approved inputs and effective settings were recorded so later changes could require renewed approval.

## 5. Coordinate the implementation

The broader journey included manually dispatched workers, an integrator and independent reviewers. Separate worktrees and committed handovers supported clear ownership and dependency handoffs.

For the final carry-forward run, already-produced work and its issue history were retained. The run linked to its predecessor and preserved historical allowances rather than treating the existing work as a fresh assignment.

## 6. Advance the orch-managed cycle

The user chose step mode and advanced the run with orch next.

| Stage | User experience |
|---|---|
| PREPARE | orch checked capabilities and prepared worker worktrees. |
| WORK | The configured worker assessed the approved assignment and produced a handover. |
| CHECK | orch assembled a candidate and ran the configured verification checks. |
| REVIEW | Code and documentation reviewers independently evaluated the candidate within their respective scopes. |
| AWAITING_TEST | Automated delivery paused for the user's acceptance testing. |
| DONE | The user accepted the delivery and closed the cycle. |

Step mode made each handoff visible. It also let the user inspect the current state before advancing.

## 7. Observe progress while agents work

While an orch next invocation was running, the user could open another terminal and use orch status or orch watch.

These views exposed the current stage, agent activity, attempts, check results, issue dispositions and evidence locations. Physical Markdown reports and handovers provided durable records beyond the terminal session.

## 8. Adjust preferences with explicit approval

The user changed reviewer models during the journey while keeping the worker on DeepSeek.

The running agent kept its existing settings. The effective configuration change required renewed approval before subsequent execution used the new model preferences. This connected convenient configuration updates with an explicit execution boundary.

## 9. Review the delivered result as a user

After checks and independent reviews completed, orch reported AWAITING_TEST with HUMAN_TEST as the next action.

For this CLI feature, human acceptance involved inspecting the delivered commands and their output. For a web application, the same stage could involve starting the application from the delivered worktree, opening its UI and exercising the acceptance scenarios.

The user remained responsible for accepting the outcome. Automated checks and agent reviews supported that decision.

## 10. Close the cycle

The user issued orch done for model-preferences-cli-carry-1.

The final status was DONE / NOTIFY, cycle 1, with no next action. The worker had completed, both reviewers had completed, and all seven carried-forward issues were recorded as finished with their historical counters preserved.

The feature was delivered under the agreed Fedora-stage scope. Completion did not automatically merge into main, deploy, push or clean up retained workspaces. Deferred platform verification remained documented.

## Short demo narration

“I start with a Markdown requirement and choose the models for each agent role. orch creates an isolated feature workspace, and I discuss and approve the delivery plan. Workers implement within assigned scopes, checks validate the assembled candidate, and independent reviewers assess code and documentation. I can follow progress from another terminal and inspect the recorded evidence. When orch reaches the human-test gate, I try the delivered feature myself. If it meets the acceptance criteria, I mark it done. This first journey included manual coordination alongside orch; the final cycle demonstrated the worker-to-check-to-review-to-human-acceptance flow.”

## Completion reference

- Feature: model-preferences CLI.
- Completed run: model-preferences-cli-carry-1-f984dda7.
- Reviewed delivery candidate: 37f724dbdc15eb84c5c071c60cb9cd2a3540903f.
- Final observed status: DONE / NOTIFY / cycle 1; next NONE.
- The first journey was manually assisted; it is not presented as fully autonomous end-to-end delivery.
