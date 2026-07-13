# Codebase Structure

**Analysis Date:** 2026-06-29

## Directory Layout

```
pc-switcher/
├── src/pcswitcher/              # Core implementation
│   ├── __init__.py
│   ├── cli.py                   # CLI entry point (Typer)
│   ├── orchestrator.py          # Main workflow orchestrator (10 phases)
│   ├── connection.py            # SSH connection management
│   ├── executor.py              # Local/remote command execution
│   ├── config.py                # Configuration loading and validation
│   ├── config_sync.py           # Sync config between source and target
│   ├── models.py                # Core dataclasses (Host, SyncSession, JobResult, etc.)
│   ├── events.py                # Event bus (pub/sub)
│   ├── logger.py                # Logging infrastructure (JSON output)
│   ├── ui.py                    # Rich terminal UI
│   ├── lock.py                  # File-based process locking (fcntl)
│   ├── disk.py                  # Disk space checking utilities
│   ├── btrfs_snapshots.py       # Snapshot naming and cleanup utilities
│   ├── sync_history.py          # Track last sync role (source/target)
│   ├── install.py               # Installation utilities
│   ├── version.py               # Version management
│   ├── default-config.yaml      # Default config template
│   └── jobs/                    # Pluggable sync jobs
│       ├── __init__.py
│       ├── base.py              # Job abstract base class
│       ├── context.py           # JobContext (executors, config, event bus)
│       ├── btrfs.py             # BtrfsSnapshotJob
│       ├── install_on_target.py # InstallOnTargetJob
│       ├── disk_space_monitor.py # DiskSpaceMonitorJob
│       ├── dummy_success.py     # Test job (always succeeds)
│       └── dummy_fail.py        # Test job (always fails)
│
├── tests/                       # Test suite
│   ├── __init__.py
│   ├── conftest.py             # Pytest configuration and fixtures
│   ├── unit/                   # Unit tests (no VM infrastructure)
│   │   ├── test_executor_login_shell.py
│   │   ├── test_version.py
│   │   ├── test_dry_run.py
│   │   ├── test_sync_history.py
│   │   ├── test_lock.py
│   │   ├── test_logging.py
│   │   └── conftest.py         # Unit-specific fixtures
│   ├── integration/            # Integration tests (require VM infrastructure)
│   │   ├── test_end_to_end_sync.py
│   │   ├── test_config_sync.py
│   │   ├── test_btrfs_operations.py
│   │   ├── test_init_command.py
│   │   ├── test_snapshot_infrastructure.py
│   │   ├── test_vm_connectivity.py
│   │   ├── test_installation_script.py
│   │   ├── test_interrupt_integration.py
│   │   ├── test_self_update.py
│   │   ├── test_logging_integration.py
│   │   └── conftest.py         # Integration-specific fixtures
│   └── contract/               # Contract tests (TBD)
│
├── docs/                       # Documentation
│   ├── adr/                    # Architecture Decision Records
│   │   ├── _index.md           # ADR index
│   │   ├── adr-001-adr.md      # ADR process
│   │   ├── adr-002-ssh-communication-channel.md
│   │   ├── adr-003-implementation-language.md
│   │   ├── adr-004-dynamic-versioning-github-releases.md
│   │   ├── adr-005-asyncio-concurrency.md
│   │   ├── adr-006-testing-framework.md
│   │   ├── adr-007-tdd-implementation.md
│   │   ├── adr-008-ci-pipeline.md
│   │   ├── adr-009-ai-readiness-labels.md
│   │   ├── adr-010-logging-infrastructure.md
│   │   ├── adr-011-sdd-with-living-specs.md
│   │   ├── adr-012-documentation-structure.md
│   │   └── considerations/     # ADR considerations and analysis
│   ├── dev/                    # Developer guides
│   │   ├── development-guide.md
│   │   └── testing-guide.md
│   ├── ops/                    # Operational guides
│   │   ├── testing-architecture.md
│   │   ├── testing-ops.md
│   │   └── ci-setup.md
│   ├── planning/               # Project planning
│   │   ├── High level requirements.md
│   │   └── Feature breakdown.md
│   ├── system/                 # Golden Copy specs (ADR-011)
│   └── Premature analysis/     # Early exploration (reference only, not binding)
│
├── specs/                      # SpecKit feature specifications (immutable history)
│   ├── 001-core/               # Core sync engine
│   │   ├── SPEC.md
│   │   ├── PLAN.md
│   │   ├── checklists/
│   │   └── contracts/
│   ├── 002-testing-framework/
│   ├── 003-core-tests/
│   └── [additional specs]/
│
├── .planning/                  # GSD planning system
│   ├── codebase/              # Codebase maps (ARCHITECTURE.md, STRUCTURE.md, etc.)
│   ├── intel/                 # Issue classifications and metadata
│   └── [GSD artifacts]/
│
├── .github/                    # GitHub configuration
│   ├── workflows/             # CI/CD pipelines
│   │   ├── ci.yml             # Code quality and test CI
│   │   └── integration-tests.yml # VM-based integration tests
│   ├── agents/                # GitHub-based agents
│   └── prompts/               # AI prompts for agents
│
├── .claude/                    # Claude Code configuration
│   ├── skills/                # Project-specific skills
│   └── commands/              # Custom commands
│
├── pyproject.toml             # Python project configuration (uv, pytest, ruff, etc.)
├── pyrightconfig.json         # Type checking config
├── install.sh                 # Installation script
├── .envrc                      # Direnv configuration
├── .gitignore
├── CLAUDE.md                  # Project-specific AI instructions
├── AGENTS.md                  # Agent documentation summary
└── README.md                  # Project README
```

## Directory Purposes

**src/pcswitcher/:**
- Purpose: All core implementation code
- Contains: CLI, orchestrator, executors, jobs, config parsing, logging, UI, utilities
- Key files: `cli.py` (entry point), `orchestrator.py` (main logic), `jobs/` (pluggable operations)

**src/pcswitcher/jobs/:**
- Purpose: Pluggable sync operation implementations
- Contains: Job base class, concrete job implementations (Btrfs, Install, DiskMonitor)
- Convention: Job name matches module name (e.g., `home_sync.py` defines HomeSyncJob)

**tests/unit/:**
- Purpose: Fast tests without VM infrastructure
- Contains: Tests for CLI parsing, config loading, lock behavior, version management, logging
- Markers: No `@pytest.mark.integration`

**tests/integration/:**
- Purpose: Tests requiring actual VM infrastructure (two machines, SSH, btrfs)
- Contains: End-to-end sync tests, snapshot operations, machine connectivity, self-update
- Markers: All use `@pytest.mark.integration`; skipped unless VMs available

**docs/adr/:**
- Purpose: Record all architectural decisions with rationale
- Convention: One ADR per decision; immutable once merged; supersessions tracked in _index.md
- When to read: When implementing features affecting architecture or understanding rationale

**docs/dev/:**
- Purpose: AI agent instructions for development
- Key files: `development-guide.md` (conventions, workflow), `testing-guide.md` (how to test)

**docs/planning/:**
- Purpose: Project scope, requirements, and roadmap
- Key files: `High level requirements.md` (complete vision), `Feature breakdown.md` (MVP and later phases)

**specs/:**
- Purpose: SpecKit feature specifications (immutable versioned history)
- Convention: Each feature spec in `NNN-name/` subdirectory; contains SPEC.md (contract), PLAN.md (implementation), checklists
- When to read: When implementing or understanding detailed feature requirements

## Key File Locations

**Entry Points:**
- `src/pcswitcher/cli.py`: Typer CLI app; commands: sync, init, logs, cleanup-snapshots, self update

**Main Logic:**
- `src/pcswitcher/orchestrator.py`: 10-phase sync workflow orchestration

**Core Modules:**
- `src/pcswitcher/models.py`: Host, SyncSession, JobResult, CommandResult, ValidationError, etc.
- `src/pcswitcher/executor.py`: Executor protocol, LocalExecutor, RemoteExecutor, Process protocol
- `src/pcswitcher/connection.py`: SSH connection management with asyncssh
- `src/pcswitcher/config.py`: YAML config parsing with jsonschema validation
- `src/pcswitcher/logger.py`: Stdlib logging with JSON file output and custom FULL level
- `src/pcswitcher/events.py`: EventBus pub/sub system
- `src/pcswitcher/ui.py`: Rich terminal UI with progress and logs
- `src/pcswitcher/lock.py`: File-based process locking (fcntl)

**Job Infrastructure:**
- `src/pcswitcher/jobs/base.py`: Abstract Job class with validation and execution interface
- `src/pcswitcher/jobs/context.py`: JobContext passed to all jobs (executors, config, event bus)
- `src/pcswitcher/jobs/btrfs.py`: Create pre/post btrfs snapshots
- `src/pcswitcher/jobs/install_on_target.py`: Install/upgrade pc-switcher on target
- `src/pcswitcher/jobs/disk_space_monitor.py`: Monitor disk space during sync

**Configuration:**
- `src/pcswitcher/default-config.yaml`: Default config template created by `pc-switcher init`

**Testing Fixtures:**
- `tests/conftest.py`: Global pytest configuration and fixtures
- `tests/unit/conftest.py`: Unit-specific fixtures
- `tests/integration/conftest.py`: Integration-specific fixtures (VM management)

**Documentation:**
- `docs/adr/_index.md`: Summary of all architectural decisions
- `docs/planning/High level requirements.md`: Complete project scope and constraints
- `CLAUDE.md`: Project-specific AI agent instructions

## Naming Conventions

**Files:**
- `*.py`: Python source code
- `*_test.py` or `test_*.py`: Test files (pytest discovers both patterns)
- `conftest.py`: Pytest configuration and fixtures (in each test directory)
- `*.yaml` or `*.yml`: Configuration files
- `adr-NNN-name.md`: ADR files (3-digit number, kebab-case)
- `SPEC.md`, `PLAN.md`: Feature specification and plan (SpecKit convention)

**Directories:**
- `src/pcswitcher/`: Main package (setuptools convention)
- `tests/unit/`, `tests/integration/`: Test organization by type
- `docs/adr/`, `docs/dev/`, `docs/ops/`, `docs/planning/`: Documentation by purpose
- `specs/NNN-name/`: Feature specs by version and name
- `jobs/`: Job implementations (convention: name matches class name)

**Classes and Functions:**
- `PascalCase` for classes (e.g., `Orchestrator`, `BtrfsSnapshotJob`, `Configuration`)
- `snake_case` for functions, variables, methods
- `UPPER_CASE` for constants
- `Abstract` prefix for abstract base classes (though `Job` is used, not `AbstractJob`)
- Protocol interfaces end with optional `Protocol` suffix (Executor, LocalExecutor, RemoteExecutor all explicit)

**Modules:**
- `snake_case` for module names (e.g., `executor.py`, `connection.py`, `btrfs_snapshots.py`)
- Jobs stored in `jobs/` with names matching what's enabled in config (e.g., `home_sync` job → `jobs/home_sync.py`)

## Where to Add New Code

**New Sync Job:**
- Create: `src/pcswitcher/jobs/your_job_name.py`
- Implement: Class inheriting from `Job`; set `name = "your_job_name"`
- Define: `CONFIG_SCHEMA` (jsonschema) for job config
- Implement: `async validate()` (check preconditions) and `async execute()` (do sync)
- Use: `self.source` and `self.target` executors, `self._report_progress()`, `self._log()`
- Enable: Add `your_job_name: true` to config.yaml under `sync_jobs`

**New CLI Command:**
- Edit: `src/pcswitcher/cli.py`
- Add: Function with `@app.command()` decorator or `@self_app.command()` for `self` subcommand
- Use: Typer options/arguments for CLI parsing
- Return: Exit code (0=success, 1=error)

**New Infrastructure Module:**
- Create: `src/pcswitcher/your_module.py`
- Export: Add to `__all__` at top
- Use: Import and inject into Orchestrator or JobContext
- Test: Add tests in `tests/unit/test_your_module.py`

**New Utility Function:**
- Location: `src/pcswitcher/utilities.py` (or existing module if related, e.g., `disk.py` for disk utilities)
- Pattern: Pure functions preferred; use Path for file operations
- Logging: Use stdlib logging if needed

**New Test:**
- Unit test: `tests/unit/test_*.py` (no VM infrastructure, fast)
- Integration test: `tests/integration/test_*.py` (requires VMs, uses `@pytest.mark.integration`)
- Fixture: Add to `tests/conftest.py` (global) or `tests/unit/conftest.py` (unit-specific)

**New ADR:**
- Create: `docs/adr/adr-NNN-kebab-case-title.md`
- Follow: ADR-001 template for structure
- Update: `docs/adr/_index.md` with new entry
- Note: Immutable once merged; supersessions recorded in _index.md

**New Feature Spec (SpecKit):**
- Create: `specs/NNN-feature-name/` directory
- Files: `SPEC.md` (contract), `PLAN.md` (implementation), `checklists/`, `contracts/`
- Convention: Versioned history; immutable once in specs/

## Special Directories

**dist/:**
- Purpose: Build artifacts (wheel, egg)
- Generated: Yes (by build system)
- Committed: No (in .gitignore)

**__pycache__/, .pytest_cache/, .ruff_cache/:**
- Purpose: Python and tool caches
- Generated: Yes
- Committed: No (in .gitignore)

**.planning/codebase/:**
- Purpose: GSD codebase maps (ARCHITECTURE.md, STRUCTURE.md, CONVENTIONS.md, TESTING.md, STACK.md, INTEGRATIONS.md, CONCERNS.md)
- Generated: By gsd-map-codebase agent
- Committed: Yes (tracked in git)

**.planning/intel/:**
- Purpose: Issue classifications and metadata from GSD
- Generated: By GSD commands
- Committed: Yes (tracked in git)

**.github/workflows/:**
- Purpose: CI/CD pipelines (GitHub Actions)
- Files: `ci.yml` (unit tests, lint, type check), `integration-tests.yml` (VM-based tests)
- Committed: Yes (tracked in git)

**.claude/, .codex/, .specify/:**
- Purpose: Claude Code and IDE configuration
- Committed: Selectively (.claude may be committed, .codex/.specify typically not)

---

*Structure analysis: 2026-06-29*
