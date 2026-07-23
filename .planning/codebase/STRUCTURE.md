# Codebase Structure

**Analysis Date:** 2026-07-23

## Directory Layout

```text
pc-switcher/
├── src/pcswitcher/          # The package (src layout)
│   ├── jobs/                # Job implementations + job base classes
│   ├── schemas/             # JSON schema for config.yaml
│   ├── *.filter             # rsync filter rule files shipped as package data
│   └── *.yaml               # default config + example machine-packages
├── tests/
│   ├── unit/                # Default pytest run
│   ├── unit_jobs/           # Extra unit tests kept outside tests/unit/jobs
│   ├── contract/            # Interface/contract tests
│   ├── local_rsync/         # Tests shelling out to a real local rsync
│   └── integration/         # VM/SSH-backed tests + provisioning shell scripts
├── docs/
│   ├── adr/                 # Architecture decision records (+ considerations/)
│   ├── system/              # Living specs (architecture, core, data-model, logging, testing)
│   ├── dev/                 # Agent-facing development and testing guides
│   ├── ops/                 # CI and test-infrastructure operations
│   ├── planning/            # High level requirements, feature breakdown
│   └── Premature analysis/  # Early exploration; inspiration only, not requirements
├── specs/                   # SpecKit feature specs
├── .planning/               # GSD planning state (roadmap, phases, codebase map)
├── .github/                 # CI workflows
├── install.sh               # Bootstrap installer (also used by InstallOnTargetJob)
├── pyproject.toml           # Package metadata, deps, pytest config
├── ruff.toml                # Lint/format config
└── pyrightconfig.json       # Type-check config
```

## Directory Purposes

**`src/pcswitcher/`:**
- Purpose: the whole application; flat module layout with one nested `jobs/` package
- Key files: `cli.py`, `orchestrator.py`, `config.py`, `executor.py`, `connection.py`, `events.py`, `logger.py`, `ui.py`, `models.py`, `lock.py`, `sync_history.py`, `btrfs_snapshots.py`, `version.py`, `confirmer.py`, `disk.py`, `sudoers.py`, `terminal.py`, `install.py`, `config_sync.py`

**`src/pcswitcher/jobs/`:**
- Purpose: one module per job, plus shared job infrastructure
- Infrastructure: `base.py` (`Job`/`SystemJob`/`SyncJob`/`BackgroundJob`), `context.py` (`JobContext`), `package_sync_core.py`, `package_phase.py`, `package_items.py`, `package_state.py`, `package_review.py`
- Jobs: `folder_sync.py`, `apt_sync.py`, `snap_sync.py`, `flatpak_sync.py`, `vscode_state_sync.py`, `btrfs.py`, `install_on_target.py`, `disk_space_monitor.py`, `dummy_success.py`, `dummy_fail.py`

**`src/pcswitcher/schemas/`:**
- Purpose: `config-schema.yaml`, the JSON schema validating user config

**`tests/`:**
- Purpose: layered test suite; default `pytest` run excludes `integration` markers
- Key files: `tests/conftest.py` (root fixtures, root-logger filtering), `tests/unit/conftest.py`, `tests/integration/conftest.py`, `tests/run-integration-tests.sh`

**`tests/integration/scripts/`:**
- Purpose: shell provisioning for the VM test fleet (`provision-test-infra.sh`, `reset-vm.sh`, `upgrade-vms.sh`, `internal/*.sh`)

**`docs/`:**
- Purpose: ADRs are the decision record of last resort; `docs/system/` holds the living specs that must be updated alongside code

## Key File Locations

**Entry Points:**
- `src/pcswitcher/cli.py`: Typer app; console script `pc-switcher`
- `install.sh`: bootstrap/remote installer

**Configuration:**
- `pyproject.toml`: deps, dynamic versioning, pytest options and markers
- `ruff.toml`, `pyrightconfig.json`: lint/format and type-check settings
- `src/pcswitcher/default-config.yaml`: shipped default user config
- `src/pcswitcher/schemas/config-schema.yaml`: config validation schema
- `src/pcswitcher/home.filter`, `src/pcswitcher/root.filter`: rsync filter rules (no inline comments allowed — a trailing `#` becomes part of the pattern)
- `src/pcswitcher/machine-packages.example.yaml`: example machine-specific package list

**Core Logic:**
- `src/pcswitcher/orchestrator.py`: session phases
- `src/pcswitcher/jobs/base.py`: the job contract
- `src/pcswitcher/executor.py`: local/remote command execution
- `src/pcswitcher/models.py`: shared dataclasses/enums/exceptions

**Testing:**
- `tests/unit/`, `tests/contract/`, `tests/local_rsync/`, `tests/integration/`
- `tests/manual-playbook.md`, `tests/self-update-test-playbook.md`: manual UAT scripts

## Naming Conventions

**Modules:**
- `snake_case.py`, one concept per module
- A sync job's module name MUST equal its `Job.name` ClassVar — discovery imports `pcswitcher.jobs.<job_name>` and scans for a matching `SyncJob` subclass

**Tests:**
- `test_<subject>.py`, mirroring the source module or the behavior area
- Directory mirrors the layer being tested (`tests/unit/jobs/`, `tests/unit/orchestrator/`, `tests/unit/cli/`, `tests/unit/ui/`)

**Docs:**
- ADRs: `docs/adr/adr-NNN-kebab-title.md`, indexed in `docs/adr/_index.md`
- Supporting analysis: `docs/adr/considerations/`

**Classes/functions:**
- `PascalCase` classes, `snake_case` functions, leading `_` for module- and class-private helpers

## Where to Add New Code

**New sync job:**
- Implementation: `src/pcswitcher/jobs/<job_name>.py` with a `SyncJob` subclass whose `name` equals `<job_name>`
- Export it from `src/pcswitcher/jobs/__init__.py`
- Config schema: `CONFIG_SCHEMA` ClassVar on the job; add the job's section to `src/pcswitcher/schemas/config-schema.yaml` and `src/pcswitcher/default-config.yaml`
- Tests: `tests/unit/jobs/test_<job_name>.py`; VM-backed coverage in `tests/integration/jobs/`

**New package manager support:**
- Extend `src/pcswitcher/jobs/package_sync_core.py` / `package_items.py` and register with `PackagePhaseCoordinator` in `src/pcswitcher/jobs/package_phase.py`

**New CLI command:**
- `src/pcswitcher/cli.py` with `@app.command()`; tests in `tests/unit/cli/test_commands.py`

**New orchestrator phase:**
- `src/pcswitcher/orchestrator.py` in `run()`, with a private `_phase_name()` helper; document the phase in `docs/system/architecture.md`

**Shared models/exceptions:**
- `src/pcswitcher/models.py`

**Architecture decision:**
- `docs/adr/adr-NNN-*.md` (read `docs/adr/adr-001-adr.md` first) and update `docs/adr/_index.md`

## Special Directories

**`.planning/`:**
- Purpose: GSD workflow state (roadmap, phase plans, this codebase map)
- Generated: partly, by GSD commands
- Committed: yes

**`specs/` and `.specify/`:**
- Purpose: SpecKit feature specs and templates
- Committed: yes

**`dist/`:**
- Purpose: build artifacts
- Generated: yes
- Committed: no (gitignored)

**`.entire/`, `.codex/`, `.claude/`, `.vscode/`:**
- Purpose: tooling/agent configuration and session metadata; not application code

**`docs/Premature analysis/`:**
- Purpose: early exploration that may conflict with the current requirements
- Treat as inspiration only; never read automatically or treat as requirements

**Runtime state (not in the repo):**
- `~/.local/share/pc-switcher/`: lock file, sync history, `sync-*.log` logs
- `/mnt/btrfs-snapshots/pc-switcher`: pre/post btrfs snapshots per host
