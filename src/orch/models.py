"""Read and change model preferences without a second precedence engine.

Every command here is a pure configuration operation: it never loads run state,
constructs an Engine, approves a plan, dispatches an agent or contacts a
provider. Writes are validated, locked, format-preserving and atomic.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import portalocker
import tomlkit
from pydantic import ValidationError

from orch.config import (
    ROLE_NAMES,
    Config,
    ConfigResolution,
    FallbackFinding,
    global_config_path,
    merge,
    repository_role_findings,
    resolve_config,
    toml_error_detail,
    validate_repository_layer,
    validation_detail,
)
from orch.contracts import (
    ConflictError,
    ContractError,
    FeatureSpec,
    InfrastructureError,
    preference_value,
    read_markdown,
)

LOCK_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class PreferenceRow:
    role: str
    model: str | None
    reasoning: str | None
    model_source: str
    reasoning_source: str
    model_path: Path | None
    reasoning_path: Path | None


@dataclass(frozen=True)
class PreferenceView:
    rows: tuple[PreferenceRow, ...]
    global_path: Path
    repo_path: Path | None
    global_models: bool = False
    feature_path: Path | None = None
    findings: tuple[FallbackFinding, ...] = ()


@dataclass(frozen=True)
class UpdateResult:
    file: Path
    role: str
    updates: dict[str, str] = field(default_factory=dict)
    removed: tuple[str, ...] = ()
    view: PreferenceView | None = None
    shadowed: dict[str, str] = field(default_factory=dict)


def inspect_preferences(resolution: ConfigResolution, *,
                        feature_path: Path | None = None) -> PreferenceView:
    """Build the six-row view without requiring every role to be dispatchable."""
    provenance = dict(resolution.provenance)
    config = resolution.config
    values: dict[str, tuple[str | None, str | None]] = {}
    for role in ROLE_NAMES:
        if role == "replacement":
            worker = config.models.worker
            model = worker.model if worker is not None else None
            override = config.models.replacement.reasoning
            reasoning = override if override is not None else (worker.reasoning if worker else None)
        else:
            spec = getattr(config.models, role)
            model = spec.model if spec is not None else None
            reasoning = spec.reasoning if spec is not None else None
        values[role] = (model, reasoning)

    effective = None
    if feature_path is not None:
        feature, _ = read_markdown(feature_path, FeatureSpec)
        from orch.config import effective_feature
        effective = effective_feature(feature, config, feature_path)
        explicit_roles = feature.models.model_fields_set
        for role in explicit_roles:
            if role not in ROLE_NAMES:
                continue
            if role == "replacement":
                replacement = feature.models.replacement
                if "reasoning" in replacement.model_fields_set:
                    provenance[role] = type(provenance[role])(
                        provenance[role].model_source, "feature",
                        provenance[role].model_path, feature_path)
            else:
                spec = getattr(feature.models, role)
                if spec is None:
                    continue
                current = provenance[role]
                model_source, model_path = current.model_source, current.model_path
                reasoning_source = current.reasoning_source
                reasoning_path = current.reasoning_path
                if "model" in spec.model_fields_set:
                    model_source, model_path = "feature", feature_path
                if "reasoning" in spec.model_fields_set:
                    reasoning_source, reasoning_path = "feature", feature_path
                provenance[role] = type(current)(
                    model_source, reasoning_source, model_path, reasoning_path)
        for role in ROLE_NAMES:
            if role == "replacement":
                worker = effective.models.worker
                model = worker.model if worker is not None else None
                override = effective.models.replacement.reasoning
                reasoning = override if override is not None else (worker.reasoning if worker else None)
            else:
                spec = getattr(effective.models, role)
                model = spec.model if spec is not None else None
                reasoning = spec.reasoning if spec is not None else None
            values[role] = (model, reasoning)

    # A replacement model always follows the final effective worker model, and
    # its inherited reasoning follows the worker when no override exists.
    worker_row = provenance["worker"]
    replacement = provenance["replacement"]
    replacement_model_source = (
        "worker_inheritance" if worker_row.model_source != "unset" else "unset")
    replacement_reasoning_source = replacement.reasoning_source
    replacement_reasoning_path = replacement.reasoning_path
    worker_reasoning = values["worker"][1]
    if replacement_reasoning_source == "unset" and worker_reasoning is not None:
        replacement_reasoning_source = "worker_inheritance"
        replacement_reasoning_path = worker_row.reasoning_path
    provenance["replacement"] = type(replacement)(
        replacement_model_source, replacement_reasoning_source,
        worker_row.model_path, replacement_reasoning_path)

    rows = []
    for role in ROLE_NAMES:
        model, reasoning = values[role]
        source = provenance[role]
        rows.append(PreferenceRow(
            role=role,
            model=model,
            reasoning=reasoning,
            model_source=source.model_source,
            reasoning_source=source.reasoning_source,
            model_path=source.model_path,
            reasoning_path=source.reasoning_path,
        ))
    return PreferenceView(
        rows=tuple(rows),
        global_path=resolution.global_path,
        repo_path=resolution.repo_path,
        global_models=resolution.global_models,
        feature_path=feature_path,
        findings=resolution.findings,
    )


# ---------------------------------------------------------------------------
# Safe, format-preserving writes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Snapshot:
    exists: bool
    data: bytes = b""
    mode: int | None = None
    device: int | None = None
    inode: int | None = None
    modified_ns: int | None = None
    dacl: bytes | None = None


def _linked(path: Path) -> bool:
    """Refuse symlinks, junctions and reparse points on every path component."""
    candidate = path.absolute()
    while True:
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            pass
        except OSError:
            return True
        else:
            if stat.S_ISLNK(info.st_mode):
                return True
            if os.name == "nt" and (
                    getattr(info, "st_file_attributes", 0) & 0x400):
                return True
        if candidate.parent == candidate:
            return False
        candidate = candidate.parent


def _windows_dacl(path: Path) -> bytes:
    """Return the raw discretionary ACL bytes of a Windows file."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p)]
    result = advapi32.GetNamedSecurityInfoW(
        str(path), 1, 0x00000004, None, None, ctypes.byref(dacl), None,
        ctypes.byref(descriptor))
    if result:
        raise OSError(result, "cannot read Windows file access restrictions")
    try:
        if not dacl.value:
            return b""

        class AclSize(ctypes.Structure):
            _fields_ = [("AceCount", wintypes.DWORD),
                        ("AclBytesInUse", wintypes.DWORD),
                        ("AclBytesFree", wintypes.DWORD)]

        size = AclSize()
        advapi32.GetAclInformation.restype = wintypes.BOOL
        advapi32.GetAclInformation.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
        if not advapi32.GetAclInformation(
                dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(dacl, size.AclBytesInUse)
    finally:
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(descriptor)


def _copy_windows_dacl(source: Path, destination: Path) -> None:
    """Apply the source file's ACL to a staging file, without inheritance."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p)]
    result = advapi32.GetNamedSecurityInfoW(
        str(source), 1, 0x00000004, None, None, ctypes.byref(dacl), None,
        ctypes.byref(descriptor))
    if result:
        raise OSError(result, "cannot read Windows file access restrictions")
    try:
        advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION:
        # install exactly the original ACL and stop broader parent inheritance.
        result = advapi32.SetNamedSecurityInfoW(
            str(destination), 1, 0x00000004 | 0x80000000, None, None,
            dacl.value, None)
        if result:
            raise OSError(result, "cannot preserve Windows file access restrictions")
    finally:
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(descriptor)


def _replace_windows(replacement: Path, replaced: Path) -> None:
    """Replace a file while retaining the replaced file's ACL."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ReplaceFileW.restype = wintypes.BOOL
    kernel32.ReplaceFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p]
    if not kernel32.ReplaceFileW(
            str(replaced), str(replacement), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def _snapshot(path: Path) -> _Snapshot:
    if not path.exists():
        return _Snapshot(exists=False)
    info = path.stat()
    return _Snapshot(
        exists=True,
        data=path.read_bytes(),
        mode=stat.S_IMODE(info.st_mode),
        device=getattr(info, "st_dev", None),
        inode=getattr(info, "st_ino", None),
        modified_ns=getattr(info, "st_mtime_ns", None),
        dacl=_windows_dacl(path) if os.name == "nt" else None,
    )


def _unchanged(path: Path, snapshot: _Snapshot) -> bool:
    if _linked(path):
        return False
    current = _snapshot(path)
    if current.exists != snapshot.exists or current.data != snapshot.data:
        return False
    if (current.device, current.inode) != (snapshot.device, snapshot.inode):
        return False
    if current.modified_ns != snapshot.modified_ns:
        return False
    return not (os.name == "nt" and current.dacl != snapshot.dacl)


def _load_existing(path: Path, scope: str) -> dict:
    snapshot = _snapshot(path)
    if not snapshot.exists:
        return {}
    try:
        text = snapshot.data.decode("utf-8")
        layer = tomllib.loads(text)
    except UnicodeDecodeError as exc:
        raise ContractError(f"{path}: invalid UTF-8 encoding") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ContractError(f"{path}: {toml_error_detail(exc)}") from exc
    if scope == "global":
        _validate_global_layer(layer, path)
    else:
        validate_repository_layer(layer, path)
    return layer


def _validate_global_layer(layer: dict, path: Path) -> None:
    try:
        Config.model_validate(layer)
    except ValidationError as exc:
        raise ContractError(f"{path}: invalid configuration: {validation_detail(exc, Config)}") from exc
    if "data_dir" in layer and (
            not isinstance(layer["data_dir"], str) or not layer["data_dir"]):
        raise ContractError(f"{path}: data_dir must be a nonempty path string")


def _proposed_config(layer: dict, scope: str, path: Path, repo: Path | None) -> None:
    """Validate the proposed file plus the selected effective context."""
    global_path = global_config_path()
    if scope == "global":
        _validate_global_layer(layer, path)
        global_layer = layer
        repo_layer = {}
        if repo is not None:
            repo_file = repo / ".orch.toml"
            if repo_file.exists():
                repo_layer = _load_existing(repo_file, "repo")
    else:
        global_layer = {}
        if global_path.exists():
            global_layer = _load_existing(global_path, "global")
        repo_layer = layer
        validate_repository_layer(repo_layer, path)
    values = merge(global_layer, {k: v for k, v in repo_layer.items() if k != "models"})
    accepted, findings = repository_role_findings(repo_layer, path)
    if findings:
        raise ContractError(f"{path}: invalid repository configuration")
    if accepted:
        values["models"] = merge(global_layer.get("models", {}), accepted)
    try:
        Config.model_validate(values)
    except ValidationError as exc:
        raise ContractError(
            f"{path}: effective configuration is invalid: "
            f"{validation_detail(exc, Config)}") from exc


def _role_table(doc, role: str):
    if "models" not in doc:
        doc["models"] = tomlkit.table()
    models = doc["models"]
    if role not in models:
        models[role] = tomlkit.table()
    return models[role]


def _drop_role(doc, role: str) -> None:
    if "models" not in doc:
        return
    models = doc["models"]
    if role in models:
        del models[role]
    if not models:
        del doc["models"]


def _edit_document(doc, *, role: str, scope: str,
                   model: str | None, reasoning: str | None) -> tuple[dict, tuple[str, ...]]:
    updates: dict[str, str] = {}
    removed: list[str] = []
    if role == "replacement":
        if model is not None:
            raise ContractError(
                "replacement always inherits the worker model; only --reasoning is accepted")
        table = doc.get("models", {}).get(role)
        if reasoning == "inherit":
            if scope == "repo":
                if table is None:
                    raise ContractError("No repository replacement override to clear")
                _drop_role(doc, role)
                removed.append("reasoning")
            else:
                if table is None or "reasoning" not in table:
                    raise ContractError("No global replacement reasoning override to clear")
                del table["reasoning"]
                removed.append("reasoning")
            return updates, tuple(removed)
        table = _role_table(doc, role)
        table["inherit"] = "worker"
        table["reasoning"] = reasoning
        updates["reasoning"] = reasoning
        return updates, tuple(removed)

    table = doc.get("models", {}).get(role)
    if scope == "repo":
        if table is None and (model is None or reasoning is None):
            raise ContractError(
                f"New repository override for {role} needs both --model and --reasoning")
        table = _role_table(doc, role)
        if model is not None:
            table["model"] = model
            updates["model"] = model
        if reasoning is not None:
            table["reasoning"] = reasoning
            updates["reasoning"] = reasoning
        if model is None and reasoning is not None and "model" not in table:
            raise ContractError(
                f"Configure an explicit model and reasoning for repository role {role}")
        return updates, tuple(removed)

    table = _role_table(doc, role)
    if model is not None:
        table["model"] = model
        updates["model"] = model
    if reasoning is not None:
        table["reasoning"] = reasoning
        updates["reasoning"] = reasoning
    if "model" not in table:
        raise ContractError(f"Configure models.{role}.model before setting its reasoning")
    return updates, tuple(removed)


def _acquire_configuration_lock(lock_path: Path, target: Path):
    """Open and exclusively lock the sibling configuration lock file."""
    if _linked(lock_path):
        raise ContractError(f"Refusing a linked configuration lock path: {lock_path}")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | nofollow, 0o600)
    except OSError as exc:
        raise InfrastructureError(
            f"{target}: configuration lock is unavailable: {exc}") from exc
    stream = os.fdopen(fd, "a+")
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    try:
        while True:
            try:
                portalocker.lock(
                    stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
                return stream
            except portalocker.exceptions.LockException as exc:
                if time.monotonic() >= deadline:
                    raise InfrastructureError(
                        f"{target}: configuration lock is unavailable: {exc}") from exc
                time.sleep(0.1)
    except BaseException:
        stream.close()
        raise


def _write_candidate(path: Path, text: str, snapshot: _Snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staging = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            # Protect staging with the original restrictions before writing
            # the new content, then verify before committing.
            if os.name == "nt" and snapshot.exists:
                _copy_windows_dacl(path, staging)
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(staging, snapshot.mode if snapshot.mode is not None else 0o600)
        elif snapshot.exists and _windows_dacl(staging) != snapshot.dacl:
            raise InfrastructureError(
                f"{path}: cannot preserve existing Windows access restrictions; "
                "no replacement was made")
        for attempt in range(3):
            try:
                if os.name == "nt" and snapshot.exists:
                    _replace_windows(staging, path)
                else:
                    os.replace(staging, path)
                break
            except (PermissionError, OSError) as exc:
                retryable = isinstance(exc, PermissionError) or (
                    os.name == "nt" and getattr(exc, "winerror", None) in {5, 32, 33})
                if attempt == 2 or not retryable:
                    raise
                time.sleep(0.05)
        if os.name == "nt" and snapshot.exists and _windows_dacl(path) != snapshot.dacl:
            raise InfrastructureError(
                f"{path}: settings were committed but Windows access "
                "restrictions could not be verified")
        if os.name != "nt":
            try:
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError as exc:
                raise InfrastructureError(
                    f"{path}: settings were committed but directory sync failed: {exc}") from exc
    finally:
        staging.unlink(missing_ok=True)


def _shadowed_fields(view: PreferenceView, role: str, scope: str,
                     fields: tuple[str, ...]) -> dict[str, str]:
    row = next((item for item in view.rows if item.role == role), None)
    if row is None:
        return {}
    expected = "global" if scope == "global" else "repository"
    shadowed: dict[str, str] = {}
    for name in fields:
        source = getattr(row, f"{name}_source")
        if source not in {expected, "unset"}:
            shadowed[name] = source
        elif source == "unset":
            shadowed[name] = "unset"
    return shadowed


def update_preferences(*, path: Path, role: str, model: str | None = None,
                       reasoning: str | None = None, repo: Path | None = None,
                       _barrier: Callable[[], None] | None = None) -> UpdateResult:
    """Strictly validated, locked, format-preserving preference update."""
    if role not in ROLE_NAMES:
        raise ContractError(
            f"Unknown role: {role}. Choose one of: {', '.join(ROLE_NAMES)}")
    if model is None and reasoning is None:
        raise ContractError("Specify at least one of --model or --reasoning")
    clearing = role == "replacement" and reasoning == "inherit"
    try:
        model = preference_value(model, "model")
        if not clearing:
            reasoning = preference_value(reasoning, "reasoning")
    except ValueError as exc:
        raise ContractError(str(exc)) from exc
    if role == "replacement" and model is not None:
        raise ContractError(
            "replacement always inherits the worker model; only --reasoning is accepted")

    requested = Path(path)
    if _linked(requested):
        raise ContractError(f"Refusing to write through a linked configuration path: {requested}")
    scope = "global" if requested.resolve() == global_config_path().resolve() else "repo"
    if scope == "repo":
        if repo is None:
            raise ContractError("Repository scope requires a selected repository")
        requested = repo / ".orch.toml"
        if _linked(requested):
            raise ContractError(
                f"Refusing to write through a linked configuration path: {requested}")
    target = requested.resolve()

    lock_path = target.with_name(f"{target.name}.models.lock")
    lock_stream = _acquire_configuration_lock(lock_path, target)
    try:
        if _linked(requested) or _linked(target) or _linked(lock_path):
            raise ContractError(
                f"Refusing to write through a linked configuration path: {requested}")
        existing = _load_existing(target, scope)
        snapshot = _snapshot(target)
        if snapshot.exists:
            text = snapshot.data.decode("utf-8")
            doc = tomlkit.parse(text)
        else:
            doc = tomlkit.document()
        updates, removed = _edit_document(
            doc, role=role, scope=scope, model=model, reasoning=reasoning)
        candidate_text = tomlkit.dumps(doc)
        try:
            candidate = tomllib.loads(candidate_text)
        except tomllib.TOMLDecodeError as exc:  # pragma: no cover - defensive
            raise ContractError(f"{target}: {toml_error_detail(exc)}") from exc
        _proposed_config(candidate, scope, target, repo)
        if _barrier is not None:
            _barrier()
        if not _unchanged(target, snapshot):
            raise ConflictError(
                f"{target}: configuration changed while editing; no replacement was made")
        if not snapshot.exists and existing:
            raise ConflictError(f"{target}: configuration appeared while editing")
        try:
            _write_candidate(target, candidate_text, snapshot)
        except OSError as exc:
            raise InfrastructureError(
                f"{target}: cannot replace configuration atomically: {exc}") from exc
        resolution = resolve_config(repo)
        view = inspect_preferences(resolution)
        shadowed = _shadowed_fields(view, role, scope, tuple(updates) + removed)
        return UpdateResult(file=target, role=role, updates=updates,
                            removed=removed, view=view, shadowed=shadowed)
    finally:
        try:
            portalocker.unlock(lock_stream)
        finally:
            lock_stream.close()


# ---------------------------------------------------------------------------
# Command coordination
# ---------------------------------------------------------------------------

def _interactive() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, OSError):
        return False


def _print_findings(resolution: ConfigResolution) -> None:
    print("Invalid repository model preferences; global preferences will be used:")
    for finding in resolution.findings:
        print(f"  - {finding.role}: {finding.reason} ({finding.file})")
    print(f"Update {resolution.repo_path} or fix it before relying on these roles.")


def consent_resolution(repo: Path | None, *, global_models: bool = False,
                       feature_path: Path | None = None) -> ConfigResolution:
    """Resolve selected context and require explicit consent for fallback.

    The explanation and the resolved effective values are printed before the
    default-no question, so the operator can see what would be used.
    """
    resolution = resolve_config(repo, global_models=global_models)
    if resolution.findings and not global_models:
        _print_findings(resolution)
        _print_view(inspect_preferences(resolution, feature_path=feature_path))
        if not _interactive():
            raise ContractError(
                "Noninteractive input cannot consent to global model fallback; "
                "fix the repository configuration or pass --global-models")
        try:
            answer = input("Proceed using global preferences for these roles? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            raise ContractError(
                "Declined global model fallback; fix the repository configuration or "
                "pass --global-models")
    return resolution


def _git_root(start: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return Path(root).resolve() if root else None


def _selected_repo(target: Path | None) -> Path | None:
    if target is None:
        return _git_root(Path.cwd())
    candidate = target.resolve()
    if not candidate.is_dir():
        raise ContractError(f"Target repository does not exist: {candidate}")
    root = _git_root(candidate)
    if root is None:
        raise ContractError(f"Not inside a Git repository: {candidate}")
    return root


def _registry(data_dir: Path) -> list[dict]:
    directory = data_dir / "features"
    if not directory.is_dir():
        return []
    entries = []
    for path in sorted(directory.glob("*.json")):
        try:
            entries.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return entries


def _feature_context(slug: str, data_dir: Path | None) -> tuple[Path, Path]:
    if data_dir is None:
        data_dir = resolve_config(None).config.data_dir
    matches = [entry for entry in _registry(data_dir)
               if entry.get("slug") == slug or entry.get("run_id") == slug]
    if len(matches) != 1:
        raise ContractError(f"Unknown or ambiguous feature: {slug}")
    entry = matches[0]
    plan = Path(entry["plan"])
    feature_path = plan / "feature.md"
    if not feature_path.is_file():
        raise ContractError(f"Registered feature has no feature.md: {feature_path}")
    return Path(entry["repo"]).resolve(), feature_path


def _print_view(view: PreferenceView) -> None:
    print(f"Global configuration: {view.global_path}")
    print(f"Repository configuration: {view.repo_path or 'none (global only)'}")
    if view.feature_path is not None:
        print(f"Feature override: {view.feature_path}")
    if view.global_models:
        print("Model selection: global preferences (--global-models)")
    print()
    print("| Role | Model | Reasoning | Model source | Reasoning source |")
    print("|---|---|---|---|---|")
    for row in view.rows:
        model = _escape(row.model) if row.model is not None else "_unset_"
        reasoning = _escape(row.reasoning) if row.reasoning is not None else "_unset_"
        print(f"| {row.role} | {model} | {reasoning} | "
              f"{row.model_source} | {row.reasoning_source} |")
    print()
    print("Replacement inherits the final worker model; its reasoning override is separate.")
    if view.findings:
        print("Global preferences are being used for the roles listed above.")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _show(args) -> int:
    if args.target is not None and args.feature is not None:
        raise ContractError("--target and --feature are mutually exclusive")
    feature_path = None
    if args.feature is not None:
        repo, feature_path = _feature_context(args.feature, args.data_dir)
    else:
        repo = _selected_repo(args.target)
    resolution = consent_resolution(
        repo, global_models=bool(getattr(args, "global_models", False)),
        feature_path=feature_path)
    view = inspect_preferences(resolution, feature_path=feature_path)
    _print_view(view)
    return 0


def _set(args) -> int:
    if getattr(args, "global_models", False):
        raise ContractError("--global-models cannot be combined with models set")
    if args.scope == "repo":
        repo = _selected_repo(args.target)
        if repo is None:
            raise ContractError("Repository scope requires a selected repository")
        path = repo / ".orch.toml"
    else:
        if args.target is not None:
            raise ContractError("--target is only valid with --scope repo")
        repo = _git_root(Path.cwd())
        path = global_config_path()
    if repo is not None:
        resolution = resolve_config(repo)
        if resolution.findings:
            _print_findings(resolution)
            raise ContractError(
                "Fix the invalid repository configuration before changing preferences")
    result = update_preferences(
        path=path, role=args.role, model=args.model, reasoning=args.reasoning, repo=repo)
    print(f"Updated {result.file} ({result.role}).")
    if result.updates:
        details = ", ".join(f"{name} = {_escape(value)}" for name, value in result.updates.items())
        print(f"Explicit updates: {details}")
    if result.removed:
        print("Removed: " + ", ".join(result.removed))
    if result.view is not None:
        _print_view(result.view)
    if result.shadowed:
        names = ", ".join(f"{field} ({source})" for field, source in result.shadowed.items())
        print(f"Shadowed for this context: {names}")
        print("A higher-priority layer supplies the effective value for the selected context.")
    print("Changed effective settings require renewed approval before dispatch.")
    return 0


def run_models(args) -> int:
    """Route models show/set without loading run state or any provider."""
    if getattr(args, "models_command", None) == "show":
        return _show(args)
    if getattr(args, "models_command", None) == "set":
        return _set(args)
    raise ContractError("Choose a models subcommand: show or set")
