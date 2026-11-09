"""Guarded four-status issue lifecycle and separate durable dimensions."""
import pytest
from pydantic import ValidationError

from orch.contracts import (
    TRANSITION_GUARDS,
    ActivityRecord,
    AssignmentState,
    ContractError,
    DependencyRecord,
    FindingDependency,
    HumanDisposition,
    IssueState,
    LifecycleTransitionError,
    RunState,
    TransitionGuard,
    apply_human_disposition,
    apply_lifecycle_transition,
    create_system_issue,
    finish_system_issue,
    migrate_v1_state,
    validate_dependency_record,
)


def pending_issue(identity="i1", **updates):
    values = {"id": identity, "worker": "w1", "root_issue_id": identity,
              "status": "pending", "owner_role": "code",
              "assignments": [AssignmentState(failed_repair_ordinals=[])]}
    values.update(updates)
    return IssueState(**values)


def satisfying_guard(source, target):
    """Build a guard that satisfies the first listed alternative for a pair."""
    fields = {name: True for name in TRANSITION_GUARDS[(source, target)][0]}
    return TransitionGuard(**fields)


@pytest.mark.parametrize("pair", sorted(TRANSITION_GUARDS))
def test_every_listed_transition_is_guarded_and_applied(pair):
    source, target = pair
    issue = pending_issue(status=source)
    if source == "finished":
        issue.status = "finished"
    elif source == "escalated":
        issue.status = "escalated"
    else:
        issue.status = source
    before = issue.model_dump(mode="json")
    record = apply_lifecycle_transition(issue, target, satisfying_guard(source, target),
                                        evidence=["review:one"])
    assert issue.status == target
    assert record.from_status == source and record.to_status == target
    assert record.evidence == ["review:one"]
    assert issue.lifecycle_history[-1] == record
    # No assignment counter is invented by a lifecycle change.
    assert issue.assignments == issue.assignments.copy()
    assert issue.model_dump(mode="json")["assignments"] == before["assignments"]


def test_unlisted_transition_is_refused_before_mutation():
    issue = pending_issue(status="finished")
    before = issue.model_dump(mode="json")
    with pytest.raises(LifecycleTransitionError, match="not listed"):
        apply_lifecycle_transition(
            issue, "in_repair", TransitionGuard(dispatch_intent=True))
    assert issue.model_dump(mode="json") == before
    assert issue.lifecycle_history == []


def test_missing_guard_is_refused_before_mutation():
    issue = pending_issue()
    before = issue.model_dump(mode="json")
    with pytest.raises(LifecycleTransitionError, match="requires"):
        apply_lifecycle_transition(issue, "in_repair",
                                   TransitionGuard(dispatch_intent=True))
    assert issue.model_dump(mode="json") == before


def test_same_status_entry_requires_validated_no_change_evidence():
    issue = pending_issue()
    with pytest.raises(LifecycleTransitionError, match="same-status"):
        apply_lifecycle_transition(issue, "pending", TransitionGuard())
    record = apply_lifecycle_transition(
        issue, "pending", TransitionGuard(no_change_evidence=True), evidence=["applied:1"])
    assert record.guard == "same_status_no_change"


def test_legacy_status_is_mapped_without_inferring_open_activity():
    quiet = IssueState(id="i1", worker="w1", root_issue_id="i1", status="open")
    assert quiet.lifecycle == "pending" and quiet.current_lifecycle() == "pending"
    active = IssueState(id="i2", worker="w1", root_issue_id="i2", status="open",
                        assignments=[AssignmentState(repairs=1)])
    assert active.lifecycle == "ambiguous"
    with pytest.raises(ContractError, match="ambiguous"):
        active.current_lifecycle()
    resolved = IssueState(id="i3", worker="w1", root_issue_id="i3", status="resolved")
    assert resolved.lifecycle == "finished"


def test_human_disposition_continuation_and_scoped_acceptance():
    escalated = pending_issue(status="escalated", evidence=["LIVE-REVIEW-003"])
    continuation = HumanDisposition(
        kind="continuation", issue_id="i1", scope="approved repair scope",
        evidence=["human:continue"], allowance_disposition="unchanged",
        dependencies=["i0"])
    record = apply_human_disposition(escalated, continuation)
    assert escalated.status == "pending"
    assert "dependency:i0" in record.evidence
    assert any(item.startswith("allowance:") for item in record.evidence)

    acceptance = HumanDisposition(
        kind="deferral", issue_id="i1", scope="stage one", evidence=["human:accept"],
        allowance_disposition="exhausted", deferred_stage="Fedora stage one")
    record = apply_human_disposition(escalated, acceptance)
    assert escalated.status == "finished"
    assert "deferred-stage:Fedora stage one" in record.evidence
    with pytest.raises(ValidationError, match="approved stage"):
        HumanDisposition(kind="deferral", issue_id="i1", scope="s",
                         evidence=["e"], allowance_disposition="unchanged")


def test_new_issue_creation_is_pending_with_valid_lineage():
    from orch.contracts import create_pending_issue

    issue = create_pending_issue("f1", "w1", root_issue_id="f1", owner_role="code",
                                 summary="finding", evidence=["verdict.md"])
    assert issue.status == "pending" and issue.depth == 0
    derived = create_pending_issue("f1-1", "w1", root_issue_id="f1", parent_issue_id="f1",
                                   depth=1, owner_role="code")
    assert derived.status == "pending" and derived.parent_issue_id == "f1"
    with pytest.raises(ContractError, match="stable identity"):
        create_pending_issue("", "w1", root_issue_id="f1", owner_role="code")
    with pytest.raises(ContractError, match="own parent"):
        create_pending_issue("f1", "w1", root_issue_id="f1", parent_issue_id="f1",
                             owner_role="code")
    with pytest.raises(ContractError, match="name itself as its root"):
        create_pending_issue("f1", "w1", root_issue_id="f2", owner_role="code")


def test_system_check_issues_have_a_separate_owner_path():
    issue = create_system_issue("check-w1", "w1", root_issue_id="check-w1",
                                summary="deterministic check", evidence=["check.log"])
    assert issue.owner_role == "system" and issue.status == "pending"
    with pytest.raises(LifecycleTransitionError, match="check evidence"):
        finish_system_issue(issue, check_evidence=[])
    with pytest.raises(LifecycleTransitionError, match="system_check"):
        apply_lifecycle_transition(issue, "finished", TransitionGuard(reviewer_acceptance=True))
    record = finish_system_issue(issue, check_evidence=["check.log"])
    assert issue.status == "finished" and record.guard.startswith("system_check")
    with pytest.raises(ContractError, match="workflow-owned"):
        create_system_issue("code-1", "w1", root_issue_id="code-1",
                            summary="not a check", evidence=["x"])
    with pytest.raises(ContractError, match="check evidence"):
        create_system_issue("post-check-w1", "w1", root_issue_id="post-check-w1",
                            summary="missing", evidence=[])


def test_activity_is_separate_from_counters_and_lifecycle():
    state = RunState(run_id="r1", slug="demo")
    state.activity.append(ActivityRecord(
        activity_id="dispatch-1", kind="dispatch", issue_id="i1",
        assignment_id="i1:0", attempt=1, process_ownership="uncertain",
        evidence=["intent:1"]))
    state.issues["i1"] = pending_issue()
    assert state.activity[0].process_ownership == "uncertain"
    assert state.issues["i1"].assignments[-1].repairs == 0
    assert state.issues["i1"].status == "pending"


def dependency_record(**overrides):
    values = {
        "plan_version": "plan-1",
        "source_verdicts": ["review-result:one"],
        "dependencies": [FindingDependency(
            dependent_issue_id="i1", prerequisite_issue_ids=["i2"],
            rationale="needs interface", shared_resource_paths=["src/shared.py"],
            verification_prerequisites=["assembled-evidence"])],
    }
    values.update(overrides)
    return DependencyRecord(**values)


def test_dependency_record_validates_known_ownership_scopes_and_cycles():
    record = dependency_record()
    validate_dependency_record(
        record, known_issue_ids={"i1", "i2"}, issue_owners={"i1": "w1", "i2": "w2"},
        known_worker_ids={"w1", "w2"}, approved_scopes={"w1": ["src/**"],
                                                        "w2": ["src/**"]})
    with pytest.raises(ContractError, match="unknown issue"):
        validate_dependency_record(
            record, known_issue_ids={"i2"}, issue_owners={"i2": "w2"},
            known_worker_ids={"w2"}, approved_scopes={"w2": ["src/**"]})
    with pytest.raises(ContractError, match="no known owner"):
        validate_dependency_record(
            record, known_issue_ids={"i1", "i2"}, issue_owners={"i2": "w2"},
            known_worker_ids={"w2"}, approved_scopes={"w2": ["src/**"]})
    with pytest.raises(ContractError, match="outside every approved scope"):
        validate_dependency_record(
            record, known_issue_ids={"i1", "i2"}, issue_owners={"i1": "w1",
                                                                "i2": "w2"},
            known_worker_ids={"w1", "w2"}, approved_scopes={"w2": ["tests/**"]})
    cyclic = dependency_record(dependencies=[
        FindingDependency(dependent_issue_id="i1", prerequisite_issue_ids=["i2"],
                          rationale="a"),
        FindingDependency(dependent_issue_id="i2", prerequisite_issue_ids=["i1"],
                          rationale="b"),
    ])
    with pytest.raises(ContractError, match="cycle"):
        validate_dependency_record(
            cyclic, known_issue_ids={"i1", "i2"}, issue_owners={"i1": "w1", "i2": "w2"},
            known_worker_ids={"w1", "w2"},
            approved_scopes={"w1": ["src/**"], "w2": ["src/**"]})


def test_dependency_record_participates_in_approval_and_parsing(tmp_path):
    from orch.contracts import Manifest, approved_files, load_dependency_record, write_markdown

    record = dependency_record()
    path = tmp_path / "dependencies.md"
    write_markdown(path, record, "## Dependencies\n\nApproved.\n")
    parsed, body = load_dependency_record(path)
    assert parsed.plan_version == "plan-1" and "Approved" in body
    manifest = Manifest(slug="x", branch="feature/x", dependency_record="dependencies.md",
                        workers=[{"id": "w1", "title": "t", "instruction": "workers/w1.md",
                                  "allowed_paths": ["src/**"]}])
    assert path in approved_files(tmp_path, manifest)


def test_migration_requires_explicit_ambiguous_mapping():
    issue = IssueState(id="i1", worker="w1", root_issue_id="i1", status="open",
                       assignments=[AssignmentState(repairs=1, failed_repairs=0,
                                                    failed_repair_ordinals=[])])
    state = RunState(run_id="r1", slug="demo", version=1, issues={"i1": issue})
    with pytest.raises(ContractError, match="explicit pending/in_repair mapping"):
        migrate_v1_state(state)
    migrated = migrate_v1_state(state, issue_lifecycles={"i1": "in_repair"})
    assert migrated.version == 2
    assert migrated.issues["i1"].status == "in_repair"
    assert migrated.issues["i1"].legacy_status == "open"
    assert migrated.issues["i1"].assignments[0].repairs == 1
