"""Shared fixtures frozen at the interface milestone (owned by W01).

Other workers may reuse these fixtures; renaming or changing their semantics
requires coordinator approval because tests across the repository depend on
this contract. Fixtures are additive only: locally defined fixtures with the
same name take precedence in their own module.
"""
from __future__ import annotations

import pytest

from orch.contracts import (
    Discussion,
    FeatureSpec,
    Manifest,
    WorkerSpec,
    write_markdown,
)
from orch.store import StateStore, atomic_write


@pytest.fixture
def orch_store(tmp_path) -> StateStore:
    """A private state store rooted in the test's temporary directory."""
    return StateStore(tmp_path / "state")


@pytest.fixture
def worker_factory():
    """Build a WorkerSpec with portable, disjoint default scopes."""

    def make(worker_id: str = "w1", paths=None, depends_on=(), title: str | None = None) -> WorkerSpec:
        return WorkerSpec(
            id=worker_id,
            title=title or worker_id,
            instruction=f"workers/{worker_id}.md",
            allowed_paths=list(paths or [f"src/{worker_id}/**"]),
            depends_on=list(depends_on),
        )

    return make


@pytest.fixture
def plan_factory(tmp_path, worker_factory):
    """Create a canonical plan folder whose artifacts all validate together.

    Returns ``(plan_path, manifest)``. Repeated calls with the same slug
    overwrite the same folder; use distinct slugs for distinct plans.
    """

    def make(slug: str = "demo", workers=None, checks=None,
             base_branch: str = "main", branch: str | None = None):
        plan = tmp_path / "plans" / slug
        plan.mkdir(parents=True, exist_ok=True)
        specs = list(workers) if workers is not None else [worker_factory("w1")]
        manifest = Manifest(
            slug=slug,
            branch=branch or f"feature/{slug}",
            base_branch=base_branch,
            workers=specs,
            checks=list(checks or []),
        )
        write_markdown(plan / "feature.md", FeatureSpec(
            slug=slug,
            target_project=tmp_path,
            requirement=plan / "requirement.md",
            base_branch=base_branch,
        ))
        write_markdown(plan / "discussion.md", Discussion(ready=True))
        write_markdown(plan / "manifest.md", manifest)
        for name, text in {
            "requirement.md": "# Requirement\n\nAcceptance: the feature works.\n",
            "plan.md": "# Plan\n\n## Interfaces\n\nShared contracts freeze first.\n",
            "reviewer.md": "# Code reviewer\n\nCheck acceptance criteria.\n",
            "doc-reviewer.md": "# Docs reviewer\n\nCheck documentation relevance only.\n",
        }.items():
            atomic_write(plan / name, text)
        for spec in specs:
            atomic_write(plan / spec.instruction, f"# {spec.title}\n\nComplete the assigned task.\n")
        return plan, manifest

    return make
