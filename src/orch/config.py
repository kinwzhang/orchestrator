"""Validated TOML configuration with global < repository < feature precedence."""
from __future__ import annotations

import tomllib
from pathlib import Path

from platformdirs import user_config_path, user_state_path
from pydantic import Field, ValidationError

from orch.contracts import ContractError, FeatureSpec, Limits, RoleModels, StrictModel


class Config(StrictModel):
    data_dir: Path = user_state_path("orch", appauthor=False)
    models: RoleModels = Field(default_factory=RoleModels)
    limits: Limits = Field(default_factory=Limits)
    viewer: bool = False
    webhook_env: str | None = None
    redaction_env: list[str] = Field(default_factory=list)


def merge(base: dict, overrides: dict) -> dict:
    """Recursive mapping merge; lists and scalars replace, mappings combine."""
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_layer(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            layer = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ContractError(f"{path}: invalid TOML: {exc}") from exc
    try:
        # Validate each layer on its own so an unknown key names its file.
        # The original partial mapping is kept: materializing defaults here
        # would let a later layer overwrite an earlier layer's values.
        Config.model_validate(layer)
    except ValidationError as exc:
        raise ContractError(f"{path}: invalid configuration: {exc}") from exc
    if "data_dir" in layer:
        if not isinstance(layer["data_dir"], str) or not layer["data_dir"]:
            raise ContractError(f"{path}: data_dir must be a nonempty path string")
        # Relative configured paths resolve against the containing file,
        # never the caller's working directory.
        layer["data_dir"] = str((path.parent / layer["data_dir"]).resolve())
    return layer


def load_config(repo: Path | None = None, global_path: Path | None = None) -> Config:
    """Merge global TOML then repository `.orch.toml` into validated settings."""
    paths = [global_path or user_config_path("orch", appauthor=False) / "config.toml"]
    if repo:
        paths.append(repo / ".orch.toml")
    values: dict = {}
    for path in paths:
        if path.exists():
            values = merge(values, _read_layer(path))
    try:
        return Config.model_validate(values)
    except ValidationError as exc:
        raise ContractError(f"combined configuration: {exc}") from exc


def effective_feature(feature: FeatureSpec, config: Config, source: Path) -> FeatureSpec:
    """Apply configuration defaults under explicit feature overrides.

    Role model overrides merge field by field, so a feature that pins
    ``models.worker.model`` keeps the configured reasoning setting for that
    role, and replacement always inherits the final worker model.
    """
    values = feature.model_dump(mode="json", exclude_unset=True)
    defaults = {"models": config.models.model_dump(exclude_none=True),
                "limits": config.limits.model_dump()}
    try:
        result = FeatureSpec.model_validate(merge(defaults, values))
    except ValidationError as exc:
        raise ContractError(f"{source}: invalid effective feature settings: {exc}") from exc
    result.target_project = (source.parent / result.target_project).resolve()
    result.requirement = (source.parent / result.requirement).resolve()
    # Runtime policy changes can affect notification destinations and whether
    # secrets are redacted before persistence. Keep only setting names/values,
    # never environment credentials, in the approved configuration digest.
    result._runtime_settings = {
        "viewer": config.viewer,
        "webhook_env": config.webhook_env,
        "redaction_env": list(config.redaction_env),
    }
    return result
