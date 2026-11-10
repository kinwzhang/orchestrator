---
version: 1
predecessor_run_id: model-preferences-cli-5ec7f5ee
predecessor_candidate_sha: "de066bc7663cab6d3cd5d896b0c8e0b8b7174bce"
inventory_digest: "ceac35efa00a6744b38d4832f8862e8b5737a767bfa82c43bdd5ce686380f297"
source_shas:
  active_runner: "66a8bdfa752e6163d5d9fc67fe97edb96b6c60a0"
  disclosure_fix: "1ef6041c2f0d01712d9b038aa5bc5dd57d214b12"
  predecessor_candidate: "de066bc7663cab6d3cd5d896b0c8e0b8b7174bce"
---

## Issue code-w01-01

- owner: w01
- owner_role: code
- root: code-w01-01
- parent: none
- depth: 0
- status: resolved
- lifecycle: finished
- attempt: 6
- reassignments: 0
- dispositions: none
- uncertainties: legacy ordinal provenance is not recorded
- evidence: none
- assignments:
  - code-w01-01:0; repairs=1; failed=0; rounds=0
- summary: Linked config parents and linked lock files can bypass write-path refusal.

## Issue code-w01-02

- owner: w01
- owner_role: code
- root: code-w01-02
- parent: none
- depth: 0
- status: resolved
- lifecycle: finished
- attempt: 6
- reassignments: 0
- dispositions: none
- uncertainties: legacy ordinal provenance is not recorded; native Windows behavior unverified (stage-scoped acceptance deferral)
- evidence: none
- assignments:
  - code-w01-02:0; repairs=1; failed=1; rounds=1
- summary: Windows atomic replacement does not preserve existing ACL restrictions.

## Issue code-w01-03

- owner: w01
- owner_role: code
- root: code-w01-03
- parent: none
- depth: 0
- status: escalated
- lifecycle: escalated
- attempt: 9
- reassignments: 1
- dispositions: none
- uncertainties: legacy ordinal provenance is not recorded; missing original per-entry review decisions for the escalated batch (LIVE-REVIEW-003)
- evidence: none
- assignments:
  - code-w01-03:0; repairs=2; failed=2; rounds=2
  - code-w01-03:1; repairs=2; failed=2; rounds=2
- summary: Configuration validation errors can disclose input values.

## Issue code-w01-04

- owner: w01
- owner_role: code
- root: code-w01-04
- parent: none
- depth: 0
- status: resolved
- lifecycle: finished
- attempt: 6
- reassignments: 0
- dispositions: none
- uncertainties: legacy ordinal provenance is not recorded
- evidence: none
- assignments:
  - code-w01-04:0; repairs=1; failed=0; rounds=0
- summary: Fallback consent is requested before showing resolved preferences.

## Issue code-w01-05

- owner: w01
- owner_role: code
- root: code-w01-05
- parent: none
- depth: 0
- status: resolved
- lifecycle: finished
- attempt: 8
- reassignments: 0
- dispositions: none
- uncertainties: legacy ordinal provenance is not recorded
- evidence: none
- assignments:
  - code-w01-05:0; repairs=2; failed=1; rounds=2
- summary: --global-models is silently accepted on commands that do not select model preferences.

## Issue code-w01-06

- owner: w01
- owner_role: code
- root: code-w01-06
- parent: none
- depth: 0
- status: resolved
- lifecycle: finished
- attempt: 8
- reassignments: 0
- dispositions: none
- uncertainties: legacy ordinal provenance is not recorded
- evidence: none
- assignments:
  - code-w01-06:0; repairs=1; failed=0; rounds=1
- summary: Preference validation allows Unicode C1 control characters despite the requirement to reject Unicode control characters.

## Issue docs-w01-01

- owner: w01
- owner_role: docs
- root: docs-w01-01
- parent: none
- depth: 0
- status: resolved
- lifecycle: finished
- attempt: 8
- reassignments: 0
- dispositions: none
- uncertainties: legacy ordinal provenance is not recorded
- evidence: none
- assignments:
  - docs-w01-01:0; repairs=1; failed=0; rounds=1
- summary: The handover says the existing user guide was unchanged, but does not establish the required operator-entry-point link to the new command guide.
