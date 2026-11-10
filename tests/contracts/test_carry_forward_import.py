"""One-shot carry-forward import: inventory grammar and every kill boundary."""
from __future__ import annotations

import pytest

from orch.carry_forward import (
    CARRY_FORWARD_COMPLETED_EVENT,
    CARRY_FORWARD_IMPORT_OPERATION,
    CARRY_FORWARD_ISSUE_EVENT,
    CarryForwardRefused,
    audit_carry_forward_issue,
    begin_carry_forward_import,
    import_carry_forward,
    import_carry_forward_issue,
    load_inventory,
    record_carry_forward_completion,
)
from orch.contracts import (
    AssignmentState,
    ContractError,
    IssueState,
    RunState,
    migrate_v1_state,
)


def assignment(identity: str, *, repairs: int = 0, failed: int = 0,
               rounds: int = 0) -> dict:
    return {"id": identity, "repairs": repairs, "failed": failed, "rounds": rounds}


def issue(identity: str = "i1", *, owner: str = "w1", owner_role: str = "code",
          root: str | None = None, parent: str | None = None, depth: int = 0,
          status: str = "open", lifecycle: str | None = None, attempt: int = 1,
          summary: str = "", assignments: list[dict] | None = None,
          reassignments: int | None = None, dispositions: tuple[str, ...] = (),
          evidence: tuple[str, ...] = (),
          uncertainties: tuple[str, ...] = ()) -> dict:
    if root is None:
        root = identity
    if lifecycle is None:
        lifecycle = {"open": "pending", "resolved": "finished",
                     "escalated": "escalated"}[status]
    if assignments is None:
        assignments = [assignment(f"{identity}:0")]
    if reassignments is None:
        reassignments = len(assignments) - 1
    return {
        "id": identity, "owner": owner, "owner_role": owner_role, "root": root,
        "parent": parent, "depth": depth, "status": status, "lifecycle": lifecycle,
        "attempt": attempt, "summary": summary, "assignments": assignments,
        "reassignments": reassignments, "dispositions": dispositions,
        "evidence": evidence, "uncertainties": uncertainties,
    }


def joined(items: tuple[str, ...]) -> str:
    return "; ".join(items) if items else "none"


def render_inventory(*, issues: list[dict], digest: str = "0" * 64,
                     predecessor_run_id: str = "pred-1",
                     predecessor_candidate_sha: str = "a" * 40,
                     source_shas: dict[str, str] | None = None) -> str:
    source_shas = source_shas if source_shas is not None else {"s1": "b" * 40}
    lines = [
        "---",
        "version: 1",
        f"predecessor_run_id: {predecessor_run_id}",
        f"predecessor_candidate_sha: \"{predecessor_candidate_sha}\"",
        f"inventory_digest: \"{digest}\"",
        "source_shas:",
    ]
    for name, sha in source_shas.items():
        lines.append(f"  {name}: \"{sha}\"")
    lines.extend(["---", ""])
    for item in issues:
        lines.extend([
            f"## Issue {item['id']}",
            "",
            f"- owner: {item['owner']}",
            f"- owner_role: {item['owner_role']}",
            f"- root: {item['root']}",
            f"- parent: {item['parent'] or 'none'}",
            f"- depth: {item['depth']}",
            f"- status: {item['status']}",
            f"- lifecycle: {item['lifecycle']}",
            f"- attempt: {item['attempt']}",
            f"- reassignments: {item['reassignments']}",
            f"- dispositions: {joined(item['dispositions'])}",
            f"- uncertainties: {joined(item['uncertainties'])}",
            f"- evidence: {joined(item['evidence'])}",
            "- assignments:",
        ])
        for value in item["assignments"]:
            lines.append(
                f"  - {value['id']}; repairs={value['repairs']}; "
                f"failed={value['failed']}; rounds={value['rounds']}"
            )
        lines.append(f"- summary: {item['summary']}" if item["summary"] else "- summary:")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_inventory(path, **kwargs):
    path.write_text(render_inventory(digest="0" * 64, **kwargs), encoding="utf-8")
    unsigned = load_inventory(path, verify_digest=False)
    path.write_text(render_inventory(digest=unsigned.computed_digest, **kwargs),
                    encoding="utf-8")
    return load_inventory(path)


def predecessor_issue(item: dict, *, assignment_ids: list[str | None] | None = None):
    return IssueState(
        id=item["id"], worker=item["owner"], root_issue_id=item["root"],
        parent_issue_id=item["parent"], depth=item["depth"], status=item["status"],
        owner_role=item["owner_role"], attempt=item["attempt"],
        assignments=[
            AssignmentState(
                assignment_id=(assignment_ids[index] if assignment_ids is not None
                               else value["id"]),
                repairs=value["repairs"], failed_repairs=value["failed"],
                review_rounds=value["rounds"], assessed_repairs=None,
                failed_repair_ordinals=None,
            )
            for index, value in enumerate(item["assignments"])
        ],
        evidence=list(item["evidence"]), summary=item["summary"],
    )


def predecessor_state(store, items: list[dict], *, run_id: str = "pred-1",
                      assignment_ids: dict[str, list[str | None]] | None = None):
    assignment_ids = assignment_ids or {}
    return store.save(RunState(
        version=1, run_id=run_id, slug="pred",
        issues={
            item["id"]: predecessor_issue(
                item, assignment_ids=assignment_ids.get(item["id"]))
            for item in items
        },
    ))


def target_state(store, *, run_id: str = "target-1", version: int = 2):
    return store.save(RunState(version=version, run_id=run_id, slug="target"))


def state_bytes(store, run_id: str = "target-1") -> bytes:
    return (store.run_dir(run_id) / "state.json").read_bytes()


def audit_events(store, run_id: str = "target-1") -> list[dict]:
    return [event for event in store.events(run_id)
            if event.get("kind") == CARRY_FORWARD_ISSUE_EVENT]


def completion_events(store, run_id: str = "target-1") -> list[dict]:
    return [event for event in store.events(run_id)
            if event.get("kind") == CARRY_FORWARD_COMPLETED_EVENT]


# ---------------------------------------------------------------------------
# Inventory grammar and digest
# ---------------------------------------------------------------------------


def test_inventory_round_trip_and_canonical_digest(tmp_path):
    path = tmp_path / "inventory.md"
    items = [
        issue("i1"),
        issue(
            "i2", root="i1", parent="i1", depth=1, lifecycle="in_repair",
            assignments=[assignment("i2:0", repairs=1, failed=1, rounds=1)],
            evidence=("review/round-1/verdict-code.md",),
            dispositions=("continuation:approved repair scope",),
            uncertainties=("legacy ordinal provenance",),
            summary="fix the widget",
        ),
    ]
    inventory = write_inventory(path, issues=items)
    assert inventory.issue_ids == ["i1", "i2"]
    assert inventory.issue("i2").lifecycle == "in_repair"
    assert inventory.issue("i2").evidence == ("review/round-1/verdict-code.md",)
    assert inventory.issue("i2").dispositions == ("continuation:approved repair scope",)
    assert inventory.issue("i2").uncertainties == ("legacy ordinal provenance",)
    assert inventory.issue("i2").summary == "fix the widget"
    assert inventory.computed_digest == inventory.inventory_digest

    # Field order and blank lines are not part of the canonical digest.
    text = path.read_text(encoding="utf-8")
    reordered = text.replace(
        "- owner: w1\n- owner_role: code\n",
        "- owner_role: code\n- owner: w1\n",
    )
    path.write_text(reordered, encoding="utf-8")
    assert load_inventory(path).computed_digest == inventory.inventory_digest


def test_inventory_digest_covers_issue_order(tmp_path):
    path = tmp_path / "inventory.md"
    items = [issue("i1"), issue("i2", root="i1", parent="i1", depth=1)]
    inventory = write_inventory(path, issues=items)
    path.write_text(
        render_inventory(issues=list(reversed(items)),
                         digest=inventory.inventory_digest),
        encoding="utf-8",
    )
    with pytest.raises(CarryForwardRefused, match="digest mismatch"):
        load_inventory(path)


def test_inventory_refuses_declared_digest_mismatch(tmp_path):
    path = tmp_path / "inventory.md"
    path.write_text(render_inventory(issues=[issue("i1")]), encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="digest mismatch"):
        load_inventory(path)


def test_inventory_refuses_unknown_front_matter_field(tmp_path):
    path = tmp_path / "inventory.md"
    text = render_inventory(issues=[issue("i1")]).replace(
        "version: 1\n", "version: 1\napproved_files: []\n")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="unrecognized field"):
        load_inventory(path)


def test_inventory_refuses_duplicate_yaml_key(tmp_path):
    path = tmp_path / "inventory.md"
    text = render_inventory(issues=[issue("i1")]).replace(
        "version: 1\n", "version: 1\nversion: 1\n")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="Duplicate"):
        load_inventory(path)


def test_inventory_refuses_invalid_version(tmp_path):
    path = tmp_path / "inventory.md"
    text = render_inventory(issues=[issue("i1")]).replace("version: 1", "version: 2")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="version"):
        load_inventory(path)


def test_inventory_refuses_invalid_source_sha(tmp_path):
    path = tmp_path / "inventory.md"
    path.write_text(
        render_inventory(issues=[issue("i1")], source_shas={"s1": "not-a-sha"}),
        encoding="utf-8",
    )
    with pytest.raises(CarryForwardRefused, match="source_shas"):
        load_inventory(path)


def test_inventory_refuses_missing_explicit_lifecycle_for_open_activity(tmp_path):
    path = tmp_path / "inventory.md"
    item = issue("i1", assignments=[assignment("i1:0", repairs=1, failed=0, rounds=0)])
    text = render_inventory(issues=[item]).replace("- lifecycle: pending\n", "")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="missing required field.*lifecycle"):
        load_inventory(path)


def test_committed_migration_refuses_ambiguous_open_activity():
    legacy = IssueState(
        id="i1", worker="w1", root_issue_id="i1", status="open",
        assignments=[AssignmentState(repairs=1, failed_repairs=0,
                                     failed_repair_ordinals=[])],
    )
    state = RunState(version=1, run_id="pred-1", slug="pred", issues={"i1": legacy})
    with pytest.raises(ContractError, match="explicit pending/in_repair mapping"):
        migrate_v1_state(state)


def test_inventory_refuses_unknown_body_field(tmp_path):
    path = tmp_path / "inventory.md"
    text = render_inventory(issues=[issue("i1")]).replace(
        "- owner: w1\n", "- owner: w1\n- approved: yes\n")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="unknown field 'approved'"):
        load_inventory(path)


def test_inventory_refuses_duplicate_issue(tmp_path):
    path = tmp_path / "inventory.md"
    path.write_text(render_inventory(issues=[issue("i1"), issue("i1")]),
                    encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="duplicate issue"):
        load_inventory(path)


def test_inventory_refuses_assignment_counter_inconsistency(tmp_path):
    path = tmp_path / "inventory.md"
    item = issue("i1", assignments=[assignment("i1:0", repairs=1, failed=2)])
    path.write_text(render_inventory(issues=[item]), encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="more failed repairs than repairs"):
        load_inventory(path)


def test_inventory_refuses_reassignment_mismatch(tmp_path):
    path = tmp_path / "inventory.md"
    item = issue("i1", reassignments=2)
    path.write_text(render_inventory(issues=[item]), encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="reassignments is 2"):
        load_inventory(path)


def test_inventory_refuses_contradictory_lifecycle_mapping(tmp_path):
    path = tmp_path / "inventory.md"
    item = issue("i1", status="resolved", lifecycle="pending")
    path.write_text(render_inventory(issues=[item]), encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="requires lifecycle finished"):
        load_inventory(path)


def test_inventory_refuses_unknown_parent(tmp_path):
    path = tmp_path / "inventory.md"
    item = issue("i1", parent="missing", depth=1)
    path.write_text(render_inventory(issues=[item]), encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="unknown parent"):
        load_inventory(path)


def test_inventory_refuses_root_that_is_not_a_root(tmp_path):
    path = tmp_path / "inventory.md"
    item = issue("i1", root="missing")
    path.write_text(render_inventory(issues=[item]), encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="root issue must name itself"):
        load_inventory(path)


def test_inventory_refuses_depth_mismatch(tmp_path):
    path = tmp_path / "inventory.md"
    items = [issue("i1"),
             issue("i2", root="i1", parent="i1", depth=2)]
    path.write_text(render_inventory(issues=items), encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="does not follow parent depth"):
        load_inventory(path)


def test_inventory_refuses_invalid_evidence_path(tmp_path):
    path = tmp_path / "inventory.md"
    item = issue("i1", evidence=("../outside.md",))
    path.write_text(render_inventory(issues=[item]), encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="portable relative path"):
        load_inventory(path)


def test_inventory_refuses_duplicate_assignment_identity(tmp_path):
    path = tmp_path / "inventory.md"
    items = [
        issue("i1", assignments=[assignment("same:0")]),
        issue("i2", root="i1", parent="i1", depth=1,
              assignments=[assignment("same:0")]),
    ]
    path.write_text(render_inventory(issues=items), encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="duplicate assignment identity"):
        load_inventory(path)


def test_inventory_refuses_system_owner_outside_check_scope(tmp_path):
    path = tmp_path / "inventory.md"
    item = issue("i1", owner_role="system")
    path.write_text(render_inventory(issues=[item]), encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="workflow check scope"):
        load_inventory(path)


# ---------------------------------------------------------------------------
# Fresh import, intent ordering and idempotent rerun
# ---------------------------------------------------------------------------


def test_import_records_intent_issues_audit_and_completion(orch_store, tmp_path):
    target_state(orch_store)
    items = [
        issue(
            "i1", lifecycle="in_repair",
            assignments=[assignment("i1:0", repairs=1, failed=1, rounds=1)],
            evidence=("review/round-1/verdict-code.md",),
            dispositions=("continuation:approved repair scope",),
            uncertainties=("legacy ordinal provenance",),
            summary="first issue",
        ),
        issue(
            "i2", root="i1", parent="i1", depth=1, lifecycle="in_repair",
            assignments=[assignment("i2:0", repairs=2, failed=2, rounds=2)],
        ),
    ]
    path = tmp_path / "inventory.md"
    inventory = write_inventory(path, issues=items)
    report = import_carry_forward(orch_store, run_id="target-1", inventory_path=path)

    assert report.status == "imported"
    assert report.imported_issues == ("i1", "i2")
    assert report.already_imported_issues == ()
    state = orch_store.load("target-1")
    operation = state.operations[CARRY_FORWARD_IMPORT_OPERATION]
    assert operation["status"] == "completed"
    assert operation["inventory_digest"] == inventory.inventory_digest
    assert operation["predecessor_run_id"] == "pred-1"
    first = state.issues["i1"]
    assert first.status == "in_repair" and first.legacy_status == "open"
    assert first.summary == "first issue"
    assert first.assignments[0].repairs == 1
    assert first.assignments[0].failed_repairs == 1
    assert first.assignments[0].review_rounds == 1
    assert first.assignments[0].failed_repair_ordinals is None
    assert first.assignments[0].assessed_repairs is None
    assert state.issues["i2"].reassignments == 0

    events = orch_store.events("target-1")
    assert [event["kind"] for event in events] == [
        "operation", CARRY_FORWARD_ISSUE_EVENT, CARRY_FORWARD_ISSUE_EVENT,
        "operation", CARRY_FORWARD_COMPLETED_EVENT,
    ]
    assert events[0]["operation"] == CARRY_FORWARD_IMPORT_OPERATION
    assert events[0]["status"] == "intent"
    assert events[3]["status"] == "completed"
    audits = audit_events(orch_store)
    assert [event["issue_id"] for event in audits] == ["i1", "i2"]
    assert audits[0]["counters"] == {"i1:0": {"repairs": 1, "failed": 1, "rounds": 1}}
    assert audits[0]["dispositions"] == ["continuation:approved repair scope"]
    assert audits[0]["uncertainties"] == ["legacy ordinal provenance"]
    assert audits[0]["issue_digest"] == inventory.issue("i1").issue_digest
    assert completion_events(orch_store)[0]["inventory_digest"] == inventory.inventory_digest


def test_completed_import_reports_without_any_write(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=[issue("i1")])
    import_carry_forward(orch_store, run_id="target-1", inventory_path=path)
    before_state = state_bytes(orch_store)
    before_events = orch_store.events("target-1")
    before_revision = orch_store.load("target-1").revision

    report = import_carry_forward(orch_store, run_id="target-1", inventory_path=path)

    assert report.status == "completed"
    assert report.imported_issues == ()
    assert report.already_imported_issues == ("i1",)
    assert state_bytes(orch_store) == before_state
    assert orch_store.events("target-1") == before_events
    assert orch_store.load("target-1").revision == before_revision


def test_completed_import_refuses_a_different_digest(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=[issue("i1")])
    import_carry_forward(orch_store, run_id="target-1", inventory_path=path)
    before_state = state_bytes(orch_store)
    before_events = orch_store.events("target-1")

    changed = tmp_path / "changed.md"
    write_inventory(changed, issues=[issue("i1", summary="changed")])
    with pytest.raises(CarryForwardRefused, match="different inventory digest"):
        import_carry_forward(orch_store, run_id="target-1", inventory_path=changed)
    assert state_bytes(orch_store) == before_state
    assert orch_store.events("target-1") == before_events


def test_invalid_inventory_refuses_before_any_write(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    path.write_text(render_inventory(issues=[issue("i1")]), encoding="utf-8")
    with pytest.raises(CarryForwardRefused, match="digest mismatch"):
        import_carry_forward(orch_store, run_id="target-1", inventory_path=path)
    assert orch_store.events("target-1") == []
    assert orch_store.load("target-1").operations == {}
    assert orch_store.load("target-1").issues == {}


def test_missing_target_run_is_refused_and_never_created(orch_store, tmp_path):
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=[issue("i1")])
    with pytest.raises(CarryForwardRefused, match="never creates a run"):
        import_carry_forward(orch_store, run_id="target-1", inventory_path=path)
    assert not (orch_store.data_dir / "runs" / "target-1").exists()


def test_version_one_target_requires_explicit_migration(orch_store, tmp_path):
    target_state(orch_store, version=1)
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=[issue("i1")])
    with pytest.raises(CarryForwardRefused, match="migration"):
        import_carry_forward(orch_store, run_id="target-1", inventory_path=path)
    assert orch_store.events("target-1") == []


def test_target_cannot_be_the_predecessor_run(orch_store, tmp_path):
    target_state(orch_store, run_id="pred-1")
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=[issue("i1")], predecessor_run_id="pred-1")
    with pytest.raises(CarryForwardRefused, match="cannot be the predecessor run"):
        import_carry_forward(orch_store, run_id="pred-1", inventory_path=path)


def test_import_preserves_escalated_allowance(orch_store, tmp_path):
    target_state(orch_store)
    items = [issue(
        "code-w01-03", status="escalated", lifecycle="escalated",
        assignments=[
            assignment("code-w01-03:0", repairs=2, failed=2, rounds=2),
            assignment("code-w01-03:1", repairs=2, failed=2, rounds=2),
        ],
        reassignments=1,
    )]
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=items)
    import_carry_forward(orch_store, run_id="target-1", inventory_path=path)

    current = orch_store.load("target-1").issues["code-w01-03"]
    assert current.status == "escalated"
    assert current.legacy_status == "escalated"
    assert current.reassignments == 1
    assert [(value.repairs, value.failed_repairs, value.review_rounds)
            for value in current.assignments] == [(2, 2, 2), (2, 2, 2)]
    assert all(value.failed_repair_ordinals is None for value in current.assignments)
    assert all(value.assessed_repairs is None for value in current.assignments)


# ---------------------------------------------------------------------------
# Kill boundaries: intent, per-issue save, audit, completion, partial resume
# ---------------------------------------------------------------------------


def test_crash_before_intent_is_a_clean_rerun(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    inventory = write_inventory(path, issues=[issue("i1")])
    state = orch_store.load("target-1")
    with pytest.raises(CarryForwardRefused, match="no durable intent"):
        import_carry_forward_issue(orch_store, state, inventory, "i1")
    assert orch_store.events("target-1") == []

    report = import_carry_forward(orch_store, run_id="target-1", inventory_path=path)
    assert report.status == "imported"
    assert report.imported_issues == ("i1",)


def test_crash_after_intent_resumes_at_first_issue(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    inventory = write_inventory(path, issues=[issue("i1"), issue(
        "i2", root="i1", parent="i1", depth=1)])
    state = begin_carry_forward_import(orch_store, orch_store.load("target-1"), inventory)
    intent_events = orch_store.events("target-1")
    assert len(intent_events) == 1 and intent_events[0]["status"] == "intent"
    assert orch_store.load("target-1").issues == {}

    report = import_carry_forward(orch_store, run_id="target-1", inventory_path=path)

    assert report.status == "resumed"
    assert report.imported_issues == ("i1", "i2")
    events = orch_store.events("target-1")
    assert events[0] == intent_events[0]
    assert [event["kind"] for event in events].count("operation") == 2
    assert [event["status"] for event in events
            if event["kind"] == "operation"] == ["intent", "completed"]
    assert state.operations[CARRY_FORWARD_IMPORT_OPERATION]["status"] == "intent"


def test_crash_after_issue_save_recreates_missing_audit(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    inventory = write_inventory(path, issues=[issue("i1"), issue(
        "i2", root="i1", parent="i1", depth=1)])
    state = begin_carry_forward_import(orch_store, orch_store.load("target-1"), inventory)
    state = import_carry_forward_issue(orch_store, state, inventory, "i1")
    assert "i1" in orch_store.load("target-1").issues
    assert audit_events(orch_store) == []

    report = import_carry_forward(orch_store, run_id="target-1", inventory_path=path)

    assert report.status == "resumed"
    assert report.imported_issues == ("i2",)
    assert report.already_imported_issues == ("i1",)
    assert [event["issue_id"] for event in audit_events(orch_store)] == ["i1", "i2"]
    assert len(completion_events(orch_store)) == 1


def test_crash_after_audit_does_not_duplicate_it(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    inventory = write_inventory(path, issues=[issue("i1"), issue(
        "i2", root="i1", parent="i1", depth=1)])
    state = begin_carry_forward_import(orch_store, orch_store.load("target-1"), inventory)
    state = import_carry_forward_issue(orch_store, state, inventory, "i1")
    audit_carry_forward_issue(orch_store, state, inventory, "i1")
    assert [event["issue_id"] for event in audit_events(orch_store)] == ["i1"]

    report = import_carry_forward(orch_store, run_id="target-1", inventory_path=path)

    assert report.status == "resumed"
    assert [event["issue_id"] for event in audit_events(orch_store)] == ["i1", "i2"]
    assert len(completion_events(orch_store)) == 1


def test_crash_after_completion_save_emits_missing_completion_event(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    inventory = write_inventory(path, issues=[issue("i1")])
    state = begin_carry_forward_import(orch_store, orch_store.load("target-1"), inventory)
    state = import_carry_forward_issue(orch_store, state, inventory, "i1")
    state = audit_carry_forward_issue(orch_store, state, inventory, "i1")
    state = record_carry_forward_completion(orch_store, state, inventory)
    assert state.operations[CARRY_FORWARD_IMPORT_OPERATION]["status"] == "completed"
    assert completion_events(orch_store) == []

    report = import_carry_forward(orch_store, run_id="target-1", inventory_path=path)

    assert report.status == "completed"
    assert report.completion_event_id is not None
    assert len(completion_events(orch_store)) == 1
    assert [event["status"] for event in orch_store.events("target-1")
            if event["kind"] == "operation"] == ["intent", "completed"]


def expected_issue(item):
    legacy = IssueState(
        id=item.id, worker=item.worker, root_issue_id=item.root,
        parent_issue_id=item.parent, depth=item.depth, status=item.status,
        owner_role=item.owner_role, attempt=item.attempt,
        assignments=[
            AssignmentState(
                assignment_id=value.assignment_id, repairs=value.repairs,
                failed_repairs=value.failed, review_rounds=value.rounds,
                assessed_repairs=None, failed_repair_ordinals=None,
            )
            for value in item.assignments
        ],
        evidence=list(item.evidence), summary=item.summary,
    )
    state = RunState(version=1, run_id="target-1", slug="target",
                     issues={item.id: legacy})
    migrated = migrate_v1_state(
        state, issue_lifecycles={item.id: item.lifecycle},
        assignment_ids={item.id: item.assignment_ids()})
    return migrated.issues[item.id]


def test_partial_target_with_matching_issue_is_verified_and_skipped(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    inventory = write_inventory(path, issues=[issue("i1"), issue(
        "i2", root="i1", parent="i1", depth=1)])
    state = orch_store.load("target-1")
    state.issues["i1"] = expected_issue(inventory.issue("i1"))
    orch_store.save(state)

    report = import_carry_forward(orch_store, run_id="target-1", inventory_path=path)

    assert report.status == "imported"
    assert report.imported_issues == ("i2",)
    assert report.already_imported_issues == ("i1",)
    assert [event["issue_id"] for event in audit_events(orch_store)] == ["i1", "i2"]


def test_resume_refuses_a_contradictory_issue_record(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    inventory = write_inventory(path, issues=[issue("i1"), issue(
        "i2", root="i1", parent="i1", depth=1)])
    state = begin_carry_forward_import(orch_store, orch_store.load("target-1"), inventory)
    import_carry_forward_issue(orch_store, state, inventory, "i1")
    changed = orch_store.load("target-1")
    changed.issues["i1"].assignments[0].repairs = 99
    orch_store.save(changed)

    with pytest.raises(CarryForwardRefused, match="contradicts"):
        import_carry_forward(orch_store, run_id="target-1", inventory_path=path)
    assert completion_events(orch_store) == []


def test_audit_before_issue_save_is_refused(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    inventory = write_inventory(path, issues=[issue("i1")])
    state = begin_carry_forward_import(orch_store, orch_store.load("target-1"), inventory)
    with pytest.raises(CarryForwardRefused, match="before it is durably imported"):
        audit_carry_forward_issue(orch_store, state, inventory, "i1")


def test_audit_without_durable_issue_is_not_repaired(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    inventory = write_inventory(path, issues=[issue("i1")])
    begin_carry_forward_import(orch_store, orch_store.load("target-1"), inventory)
    orch_store.event(
        "target-1", CARRY_FORWARD_ISSUE_EVENT,
        inventory_digest=inventory.inventory_digest,
        issue_id="i1", issue_digest=inventory.issue("i1").issue_digest,
    )
    with pytest.raises(CarryForwardRefused, match="audit evidence but no durable issue"):
        import_carry_forward(orch_store, run_id="target-1", inventory_path=path)


def test_audit_event_with_a_different_digest_is_refused(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    inventory = write_inventory(path, issues=[issue("i1"), issue(
        "i2", root="i1", parent="i1", depth=1)])
    state = begin_carry_forward_import(orch_store, orch_store.load("target-1"), inventory)
    state = import_carry_forward_issue(orch_store, state, inventory, "i1")
    orch_store.event(
        "target-1", CARRY_FORWARD_ISSUE_EVENT,
        inventory_digest=inventory.inventory_digest,
        issue_id="i1", issue_digest="0" * 64,
    )
    with pytest.raises(CarryForwardRefused, match="disagrees with its durable record"):
        import_carry_forward(orch_store, run_id="target-1", inventory_path=path)


def test_multiple_audit_events_are_refused(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    inventory = write_inventory(path, issues=[issue("i1"), issue(
        "i2", root="i1", parent="i1", depth=1)])
    state = begin_carry_forward_import(orch_store, orch_store.load("target-1"), inventory)
    state = import_carry_forward_issue(orch_store, state, inventory, "i1")
    state = audit_carry_forward_issue(orch_store, state, inventory, "i1")
    orch_store.event(
        "target-1", CARRY_FORWARD_ISSUE_EVENT,
        inventory_digest=inventory.inventory_digest,
        issue_id="i1", issue_digest=inventory.issue("i1").issue_digest,
    )
    with pytest.raises(CarryForwardRefused, match="multiple audit events"):
        import_carry_forward(orch_store, run_id="target-1", inventory_path=path)


def test_multiple_completion_events_are_refused(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    inventory = write_inventory(path, issues=[issue("i1")])
    import_carry_forward(orch_store, run_id="target-1", inventory_path=path)
    orch_store.event(
        "target-1", CARRY_FORWARD_COMPLETED_EVENT,
        inventory_digest="0" * 64,
        predecessor_run_id=inventory.predecessor_run_id,
    )
    with pytest.raises(CarryForwardRefused, match="multiple completion events"):
        import_carry_forward(orch_store, run_id="target-1", inventory_path=path)


# ---------------------------------------------------------------------------
# Read-only predecessor verification
# ---------------------------------------------------------------------------


def test_predecessor_is_never_written(orch_store, tmp_path):
    items = [issue("i1", assignments=[assignment("i1:0", repairs=1, failed=0,
                                                  rounds=0)])]
    predecessor_state(orch_store, items)
    predecessor_dir = orch_store.run_dir("pred-1")
    before_state = (predecessor_dir / "state.json").read_bytes()
    before_events = orch_store.events("pred-1")
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=items)

    import_carry_forward(
        orch_store, run_id="target-1", inventory_path=path,
        predecessor_run_dir=predecessor_dir,
    )

    assert (predecessor_dir / "state.json").read_bytes() == before_state
    assert orch_store.events("pred-1") == before_events
    assert not (predecessor_dir / "events.jsonl").exists()


def test_predecessor_directory_must_be_disjoint_from_target(orch_store, tmp_path):
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=[issue("i1")])
    with pytest.raises(CarryForwardRefused, match="read-only"):
        import_carry_forward(
            orch_store, run_id="target-1", inventory_path=path,
            predecessor_run_dir=orch_store.run_dir("target-1"),
        )
    assert orch_store.events("target-1") == []


def test_predecessor_cross_check_refuses_incomplete_inventory(orch_store, tmp_path):
    items = [issue("i1"), issue("i2", root="i1", parent="i1", depth=1)]
    predecessor_state(orch_store, items)
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=[items[0]])
    with pytest.raises(CarryForwardRefused, match="missing predecessor issues: i2"):
        import_carry_forward(
            orch_store, run_id="target-1", inventory_path=path,
            predecessor_run_dir=orch_store.run_dir("pred-1"),
        )
    assert orch_store.events("target-1") == []


def test_predecessor_cross_check_refuses_unknown_inventory_issue(orch_store, tmp_path):
    items = [issue("i1")]
    predecessor_state(orch_store, items)
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=[*items, issue("i3")])
    with pytest.raises(CarryForwardRefused, match="unknown inventory issues: i3"):
        import_carry_forward(
            orch_store, run_id="target-1", inventory_path=path,
            predecessor_run_dir=orch_store.run_dir("pred-1"),
        )
    assert orch_store.events("target-1") == []


def test_predecessor_cross_check_refuses_counter_mismatch(orch_store, tmp_path):
    original = issue("i1", assignments=[assignment("i1:0", repairs=1, failed=0,
                                                    rounds=0)])
    predecessor_state(orch_store, [original])
    target_state(orch_store)
    changed = issue("i1", assignments=[assignment("i1:0", repairs=2, failed=0,
                                                  rounds=0)])
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=[changed])
    with pytest.raises(CarryForwardRefused, match="counters do not match"):
        import_carry_forward(
            orch_store, run_id="target-1", inventory_path=path,
            predecessor_run_dir=orch_store.run_dir("pred-1"),
        )
    assert orch_store.events("target-1") == []


def test_predecessor_cross_check_refuses_evidence_mismatch(orch_store, tmp_path):
    original = issue("i1", evidence=("review/round-1/verdict-code.md",))
    predecessor_state(orch_store, [original])
    target_state(orch_store)
    changed = issue("i1", evidence=("review/round-2/verdict-code.md",))
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=[changed])
    with pytest.raises(CarryForwardRefused, match="evidence does not match"):
        import_carry_forward(
            orch_store, run_id="target-1", inventory_path=path,
            predecessor_run_dir=orch_store.run_dir("pred-1"),
        )
    assert orch_store.events("target-1") == []


def test_predecessor_cross_check_refuses_legacy_status_mismatch(orch_store, tmp_path):
    original = issue("i1", status="open")
    predecessor_state(orch_store, [original])
    target_state(orch_store)
    changed = issue("i1", status="resolved", lifecycle="finished")
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=[changed])
    with pytest.raises(CarryForwardRefused, match="legacy status does not match"):
        import_carry_forward(
            orch_store, run_id="target-1", inventory_path=path,
            predecessor_run_dir=orch_store.run_dir("pred-1"),
        )
    assert orch_store.events("target-1") == []


def test_predecessor_cross_check_refuses_canonical_predecessor_status(orch_store, tmp_path):
    item = issue("i1")
    predecessor = predecessor_issue(item)
    predecessor.status = "pending"
    orch_store.save(RunState(version=1, run_id="pred-1", slug="pred",
                             issues={"i1": predecessor}))
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=[item])
    with pytest.raises(CarryForwardRefused, match="canonical status"):
        import_carry_forward(
            orch_store, run_id="target-1", inventory_path=path,
            predecessor_run_dir=orch_store.run_dir("pred-1"),
        )
    assert orch_store.events("target-1") == []


def test_assignment_identity_mapping_is_explicit(orch_store, tmp_path):
    items = [issue("i1", assignments=[assignment("i1:0", repairs=1, failed=0,
                                                  rounds=0)])]
    predecessor_state(orch_store, items,
                      assignment_ids={"i1": [None]})
    target_state(orch_store)
    path = tmp_path / "inventory.md"
    write_inventory(path, issues=items)

    report = import_carry_forward(
        orch_store, run_id="target-1", inventory_path=path,
        predecessor_run_dir=orch_store.run_dir("pred-1"),
    )

    assert report.status == "imported"
    current = orch_store.load("target-1").issues["i1"]
    assert current.assignments[0].assignment_id == "i1:0"
    assert current.assignments[0].repairs == 1
