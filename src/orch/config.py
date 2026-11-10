"""Validated TOML configuration with global < repository < feature precedence.

This module owns parsing, layer validation, merging and provenance for every
reader and writer. The model-preference commands reuse these helpers instead of
introducing a second precedence engine.
"""
from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from platformdirs import user_config_path, user_state_path
from pydantic import Field, ValidationError

from orch.contracts import (
    ContractError,
    FeatureSpec,
    Limits,
    RoleModels,
    StrictModel,
    validation_detail,
)

ROLE_NAMES = (
    "planner",
    "worker",
    "replacement",
    "code_reviewer",
    "docs_reviewer",
    "conflict_resolver",
)
ORDINARY_ROLES = tuple(role for role in ROLE_NAMES if role != "replacement")
REPLACEMENT_FIELDS = frozenset({"inherit", "reasoning"})
ORDINARY_FIELDS = frozenset({"model", "reasoning"})


class Config(StrictModel):
    data_dir: Path = user_state_path("orch", appauthor=False)
    models: RoleModels = Field(default_factory=RoleModels)
    limits: Limits = Field(default_factory=Limits)
    viewer: bool = False
    webhook_env: str | None = None
    redaction_env: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class FallbackFinding:
    """One identifiable repository role override that cannot be used."""

    role: str
    reason: str
    file: Path
    resolved_source: str


@dataclass(frozen=True)
class RoleProvenance:
    """Where the effective model and reasoning of one role came from."""

    model_source: str
    reasoning_source: str
    model_path: Path | None
    reasoning_path: Path | None


@dataclass(frozen=True)
class ConfigResolution:
    """A validated, immutable configuration snapshot for one invocation."""

    config: Config
    global_path: Path
    repo_path: Path | None
    provenance: Mapping[str, RoleProvenance]
    findings: tuple[FallbackFinding, ...] = ()
    global_models: bool = False
    global_layer: Mapping[str, object] = field(default_factory=dict)
    repository_layer: Mapping[str, object] = field(default_factory=dict)


def global_config_path() -> Path:
    """Absolute platform configuration file reused by every reader and writer.

    The path is intentionally not resolved: writers need the original path
    components to detect a linked configuration directory before any lock or
    staging file is created. Read-only callers resolve it themselves.
    """
    return user_config_path("orch", appauthor=False) / "config.toml"


def toml_error_detail(exc: tomllib.TOMLDecodeError) -> str:
    """Sanitized TOML syntax diagnostic without echoing document content."""
    line = getattr(exc, "lineno", None)
    column = getattr(exc, "colno", None)
    if line is not None and column is not None:
        return f"invalid TOML (line {line}, column {column})"
    return "invalid TOML"


def merge(base: dict, overrides: dict) -> dict:
    """Recursive mapping merge; lists and scalars replace, mappings combine."""
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ContractError(f"{path}: {toml_error_detail(exc)}") from exc
    except UnicodeDecodeError as exc:
        raise ContractError(f"{path}: invalid UTF-8 encoding") from exc


def _resolve_data_dir(layer: dict, path: Path) -> dict:
    if "data_dir" in layer:
        if not isinstance(layer["data_dir"], str) or not layer["data_dir"]:
            raise ContractError(f"{path}: data_dir must be a nonempty path string")
        # Relative configured paths resolve against the containing file,
        # never the caller's working directory.
        layer["data_dir"] = str((path.parent / layer["data_dir"]).resolve())
    return layer


def _validate_mapping(mapping: dict, path: Path) -> dict:
    """Validate one complete layer (including models) and normalize its paths."""
    try:
        Config.model_validate(mapping)
    except ValidationError as exc:
        raise ContractError(f"{path}: invalid configuration: {validation_detail(exc, Config)}") from exc
    return _resolve_data_dir(mapping, path)


def _known_roles(models: object, path: Path) -> None:
    """Reject unknown role keys without echoing the untrusted table key."""
    if not isinstance(models, dict):
        raise ContractError(f"{path}: models must be a table")
    for role in models:
        if role not in ROLE_NAMES:
            raise ContractError(
                f"{path}: unknown model role in the models table; "
                f"expected one of: {', '.join(ROLE_NAMES)}")


def _read_layer(path: Path) -> dict:
    layer = _read_toml(path)
    _known_roles(layer.get("models", {}), path)
    try:
        # Validate each layer on its own so an unknown key names its file.
        # The original partial mapping is kept: materializing defaults here
        # would let a later layer overwrite an earlier layer's values.
        Config.model_validate(layer)
    except ValidationError as exc:
        raise ContractError(f"{path}: invalid configuration: {validation_detail(exc, Config)}") from exc
    return _resolve_data_dir(layer, path)


def _validate_non_model(mapping: dict, path: Path) -> dict:
    """Validate unrelated repository settings while allowing role fallback."""
    try:
        Config.model_validate(mapping)
    except ValidationError as exc:
        raise ContractError(f"{path}: invalid configuration: {validation_detail(exc, Config)}") from exc
    return _resolve_data_dir(mapping, path)


def _ordinary_complete(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != ORDINARY_FIELDS:
        return False
    try:
        Config.model_validate({"models": {"worker": dict(value)}})
    except ValidationError:
        return False
    return True


def _replacement_complete(value: object) -> bool:
    if not isinstance(value, dict) or set(value) - REPLACEMENT_FIELDS:
        return False
    if value.get("inherit") != "worker" or "reasoning" not in value:
        return False
    try:
        Config.model_validate({"models": {"replacement": dict(value)}})
    except ValidationError:
        return False
    return True


def repository_role_findings(layer: dict, path: Path) -> tuple[dict, list[FallbackFinding]]:
    """Classify repository role tables as complete units or fallback findings.

    Returns the accepted model mapping and findings for identifiable invalid
    roles. Unknown roles and malformed model containers are configuration
    errors because there is no role to fall back for.
    """
    models = layer.get("models")
    if models is None:
        return {}, []
    _known_roles(models, path)
    accepted: dict = {}
    findings: list[FallbackFinding] = []
    for role, value in models.items():
        if role == "replacement":
            if isinstance(value, dict) and "model" in value:
                findings.append(FallbackFinding(
                    role, "replacement must not declare its own model", path, "global"))
            elif not _replacement_complete(value):
                findings.append(FallbackFinding(
                    role, "replacement needs inherit = worker and a reasoning value",
                    path, "global"))
            else:
                accepted[role] = value
            continue
        if not _ordinary_complete(value):
            findings.append(FallbackFinding(
                role, "repository role overrides need both model and reasoning",
                path, "global"))
        else:
            accepted[role] = value
    return accepted, findings


def validate_repository_layer(layer: dict, path: Path) -> dict:
    """Strict repository validation used before replacing a file."""
    accepted, findings = repository_role_findings(layer, path)
    if findings:
        detail = "; ".join(f"{item.role}: {item.reason}" for item in findings)
        raise ContractError(f"{path}: invalid repository configuration: {detail}")
    _validate_non_model({k: v for k, v in layer.items() if k != "models"}, path)
    if "models" in layer:
        _validate_mapping(layer, path)
    return accepted


def _provenance(
    global_models: dict, accepted: dict, global_path: Path, repo_path: Path | None,
    selected_global_only: bool,
) -> dict[str, RoleProvenance]:
    result: dict[str, RoleProvenance] = {}

    def source(role: str) -> tuple[str, str, Path | None, Path | None]:
        value = global_models.get(role)
        model_source = "global" if role in global_models else "unset"
        model_path = global_path if role in global_models else None
        reasoning_source = "unset"
        reasoning_path = None
        if isinstance(value, dict) and value.get("reasoning") is not None:
            reasoning_source = "global"
            reasoning_path = global_path
        if not selected_global_only and role in accepted:
            model_source = "repository"
            model_path = repo_path
            reasoning_source = "repository"
            reasoning_path = repo_path
        return model_source, reasoning_source, model_path, reasoning_path

    for role in ORDINARY_ROLES:
        model_source, reasoning_source, model_path, reasoning_path = source(role)
        result[role] = RoleProvenance(model_source, reasoning_source, model_path, reasoning_path)

    worker = result["worker"]
    worker_value = global_models.get("worker")
    worker_reasoning_set = (
        isinstance(worker_value, dict) and worker_value.get("reasoning") is not None
    ) or (not selected_global_only and "worker" in accepted)
    model_source = "worker_inheritance" if worker.model_source != "unset" else "unset"
    model_path = worker.model_path
    if not selected_global_only and "replacement" in accepted:
        reasoning_source, reasoning_path = "repository", repo_path
    elif isinstance(global_models.get("replacement"), dict) and (
            global_models["replacement"].get("reasoning") is not None):
        reasoning_source, reasoning_path = "global", global_path
    elif worker_reasoning_set:
        reasoning_source, reasoning_path = "worker_inheritance", worker.reasoning_path
    else:
        reasoning_source, reasoning_path = "unset", None
    result["replacement"] = RoleProvenance(
        model_source, reasoning_source, model_path, reasoning_path)
    return result


def resolve_config(repo: Path | None = None, global_path: Path | None = None, *,
                   global_models: bool = False) -> ConfigResolution:
    """Pure read: parse, validate and resolve without prompting or writing.

    A broken or invalid selected file is fatal. Identifiable invalid repository
    role overrides are returned as findings so the caller can request consent;
    complete valid overrides are kept and all unrelated settings are preserved.
    """
    global_file = (global_path or global_config_path()).resolve()
    global_layer: dict = {}
    if global_file.exists():
        global_layer = _read_layer(global_file)
    global_models_layer = global_layer.get("models", {})
    if not isinstance(global_models_layer, dict):
        raise ContractError(f"{global_file}: models must be a table")

    repo_file = (repo / ".orch.toml").resolve() if repo else None
    repository_layer: dict = {}
    accepted: dict = {}
    findings: list[FallbackFinding] = []
    active_repo_file = None
    if repo_file and repo_file.exists():
        active_repo_file = repo_file
        repository_layer = _read_toml(repo_file)
        if not isinstance(repository_layer, dict):
            raise ContractError(f"{repo_file}: configuration must be a table")
        _validate_non_model(
            {k: v for k, v in repository_layer.items() if k != "models"}, repo_file)
        if not global_models:
            accepted, findings = repository_role_findings(repository_layer, repo_file)

    values = dict(global_layer)
    repository_settings = {k: v for k, v in repository_layer.items() if k != "models"}
    values = merge(values, _resolve_data_dir(repository_settings, active_repo_file)
                   if active_repo_file else repository_settings)
    if not global_models and accepted:
        merged_models = merge(global_models_layer, accepted)
        values["models"] = merged_models
    try:
        config = Config.model_validate(values)
    except ValidationError as exc:
        raise ContractError(
            f"{repo_file or global_file}: invalid configuration: "
            f"{validation_detail(exc, Config)}") from exc

    provenance = _provenance(
        global_models_layer, accepted, global_file, active_repo_file, global_models)
    return ConfigResolution(
        config=config,
        global_path=global_file,
        repo_path=active_repo_file,
        provenance=MappingProxyType(provenance),
        findings=tuple(findings),
        global_models=global_models,
        global_layer=MappingProxyType(global_layer),
        repository_layer=MappingProxyType(repository_layer),
    )


def load_config(repo: Path | None = None, global_path: Path | None = None) -> Config:
    """Merge global TOML then repository `.orch.toml` into validated settings.

    Retained strict signature and behaviour for noninteractive library callers:
    repository role tables are validated as complete layers, without per-role
    fallback. The CLI resolver reuses the same parsing and merging helpers.
    """
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
        raise ContractError(
            f"combined configuration: {validation_detail(exc, Config)}") from exc


def effective_feature(feature: FeatureSpec, config: Config, source: Path) -> FeatureSpec:
    """Apply configuration defaults under explicit feature overrides.

    Role model overrides merge field by field, so a feature that pins
    ``models.worker.model`` keeps the configured reasoning setting for that
    role, and replacement always inherits the final worker model. A replacement
    may additionally override only its reasoning; without that override it
    inherits the worker's reasoning as well.
    """
    values = feature.model_dump(mode="json", exclude_unset=True)
    defaults = {"models": config.models.model_dump(exclude_none=True),
                "limits": config.limits.model_dump()}
    try:
        result = FeatureSpec.model_validate(merge(defaults, values))
    except ValidationError as exc:
        raise ContractError(
            f"{source}: invalid effective feature settings: "
            f"{validation_detail(exc, FeatureSpec)}") from exc
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
