# orch

A local, Markdown-driven delivery orchestrator. Describe a feature, discuss its plan, approve execution, then let workers, checks and independent reviewers prepare a candidate for human acceptance.

## Start here

The canonical development checkout is now:

```bash
cd /home/zwj808/PycharmProjects/orc2610_orchestrator
uv sync --group dev
uv run orch --help
```

The project uses Python 3.14+ and uv. Authenticate Codex and OpenCode separately; do not put credentials in requirements or model preferences.

## Model preferences

```bash
uv run orch models show
```

Global preferences are stored at `~/.config/orch/config.toml`. Repository `.orch.toml` and feature settings can override them. An effective settings change requires renewed approval before subsequent dispatch.

## Delivery workflow

```text
new -> plan/discussion -> approve -> prepare -> work -> check -> review
    -> human testing -> done
```

Use step mode for controlled progression. Status and watch can run in another terminal while a stage executes. Main merges, remote pushes and deployments require separate operator direction.

## Documentation

- [Current local user guide](documentation/20261110_user_manual.md)
- [Detailed feature workflow](documentation/20261026_user_guide.md)
- [Model-preference commands](documentation/20261030_model_preferences_cli.md)
- [Lifecycle and review application](documentation/20261109_review_lifecycle_application.md)
- [Lifecycle operations and audit recovery](documentation/20261109_review_lifecycle_operations.md)
- [First-cycle demo journey](documentation/20261110_first_orch_cycle_user_journey.md)
- [Enhancement backlog](documentation/20261110_enhancement_backlog.md)
- [First-cycle archive and restoration](documentation/20261110_first_cycle_archive.md)

## Verification

```bash
uv run pytest -q
uv run ruff check .
uv lock --check
```

Tests use temporary fixtures and stub agents, not paid/live model calls. The first delivery was accepted under Fedora-stage scope. Native Windows verification remains deferred; YAML diagnostic-location caveats remain documented.
