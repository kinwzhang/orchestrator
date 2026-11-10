"""Malformed YAML/TOML diagnostics never echo untrusted source content."""
from __future__ import annotations

import tomllib

import pytest

from orch.config import toml_error_detail
from orch.contracts import ContractError, FeatureSpec, read_markdown

SECRET = "https://hooks.invalid/very-secret-token"


@pytest.mark.parametrize("front_matter, expected", [
    (f"slug: demo\n{SECRET}: [broken", "line 2, column"),
    (f"slug: demo\nworker: !{SECRET} value", "line 2, column"),
    (f"slug: demo\n{SECRET}: a\n{SECRET}: b",
     "Duplicate or non-string YAML key"),
    (f"slug: demo\nmodels: {{worker: [{SECRET}}}", "line 2, column"),
])
def test_malformed_feature_yaml_never_echoes_source(tmp_path, front_matter, expected):
    path = tmp_path / "feature.md"
    path.write_text("---\n" + front_matter + "\n---\n")
    before = path.read_bytes()
    with pytest.raises(ContractError) as caught:
        read_markdown(path, FeatureSpec)
    message = str(caught.value)
    assert SECRET not in message
    assert str(path) in message
    assert expected in message
    assert path.read_bytes() == before


def test_invalid_contract_locations_never_echo_input_values(tmp_path):
    path = tmp_path / "feature.md"
    path.write_text(
        "---\n"
        "slug: demo\n"
        "target_project: .\n"
        "requirement: requirement.md\n"
        f"{SECRET}: leak\n"
        "---\n"
    )
    with pytest.raises(ContractError) as caught:
        read_markdown(path, FeatureSpec)
    message = str(caught.value)
    assert SECRET not in message
    assert "extra_forbidden" in message
    assert str(path) in message


def test_toml_error_detail_never_echoes_document_content():
    with pytest.raises(tomllib.TOMLDecodeError) as caught:
        tomllib.loads(f'{SECRET} = [broken')
    detail = toml_error_detail(caught.value)
    assert SECRET not in detail
    assert detail.startswith("invalid TOML (line 1, column")
