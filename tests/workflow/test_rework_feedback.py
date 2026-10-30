"""Dependency-expanded rework feedback must name usable upstream evidence."""
from types import SimpleNamespace

from orch.contracts import Finding, Manifest, Verdict, write_markdown
from orch.workflow import build_rework_feedback


def manifest():
    return Manifest(slug="feature", branch="feature/feature", workers=[
        {"id": "w1", "title": "one", "instruction": "workers/w1.md",
         "allowed_paths": ["src/w1/**"]},
        {"id": "w2", "title": "two", "instruction": "workers/w2.md",
         "allowed_paths": ["src/w2/**"], "depends_on": ["w1"]},
        {"id": "w3", "title": "three", "instruction": "workers/w3.md",
         "allowed_paths": ["src/w3/**"], "depends_on": ["w2"]},
    ])


def rework_result(tmp_path):
    path = tmp_path / "verdict-code-1.md"
    write_markdown(path, Verdict(
        reviewer="code", result="rework", candidate_sha="a" * 40,
        findings=[Finding(id="code-w1-1", worker="w1", severity="major",
                          action="rework_same_worker", root_issue_id="code-w1-1",
                          summary="upstream defect")],
    ), "## Body\n")
    return SimpleNamespace(verdict_path=str(path))


def test_two_worker_dependency_feedback_names_the_upstream_finding(tmp_path):
    result = rework_result(tmp_path)
    text = build_rework_feedback("w2", manifest(), {"w1", "w2"}, [result])
    assert "code-w1-1" in text
    assert "upstream defect" in text
    assert str(result.verdict_path) in text
    assert "w1 -> w2" in text
    assert "no longer covers" in text
    assert any(line.strip() and not line.startswith("#")
               for line in text.splitlines())


def test_transitive_dependency_feedback_includes_the_chain_and_root_finding(tmp_path):
    result = rework_result(tmp_path)
    text = build_rework_feedback("w3", manifest(), {"w1", "w2", "w3"}, [result])
    assert "code-w1-1" in text
    assert "upstream defect" in text
    assert "w1 -> w2 -> w3" in text
    assert "Dependency rework" in text


def test_directly_reviewed_worker_feedback_keeps_its_own_finding(tmp_path):
    result = rework_result(tmp_path)
    text = build_rework_feedback("w1", manifest(), {"w1", "w2", "w3"}, [result])
    assert "Findings for your work" in text
    assert "code-w1-1" in text
    assert "Dependency rework" not in text


def test_unreadable_upstream_verdict_still_produces_nonempty_feedback(tmp_path):
    result = SimpleNamespace(verdict_path=str(tmp_path / "missing.md"))
    text = build_rework_feedback("w2", manifest(), {"w1", "w2"}, [result])
    assert "Dependency rework" in text
    assert str(result.verdict_path) in text
    body = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert body
