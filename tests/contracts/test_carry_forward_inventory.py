"""Approved carry-forward inventory participates in manifest approval inputs."""
import pytest
from pydantic import ValidationError

from orch.contracts import Manifest, approved_files


def manifest(**overrides):
    values = {
        "slug": "x",
        "branch": "feature/x",
        "workers": [{"id": "w1", "title": "t", "instruction": "workers/w1.md",
                     "allowed_paths": ["src/**"]}],
    }
    values.update(overrides)
    return Manifest(**values)


def test_carry_forward_inventory_participates_in_approved_files(tmp_path):
    path = tmp_path / "carry-forward-inventory.md"
    path.write_text("# Carry-forward inventory\n\nApproved.\n")
    value = manifest(carry_forward_inventory="carry-forward-inventory.md")
    assert path in approved_files(tmp_path, value)


def test_carry_forward_inventory_is_a_safe_relative_path():
    with pytest.raises(ValidationError):
        manifest(carry_forward_inventory="../outside.md")
    with pytest.raises(ValidationError):
        manifest(carry_forward_inventory="/absolute.md")
    with pytest.raises(ValidationError):
        manifest(carry_forward_inventory="nested\\windows.md")
