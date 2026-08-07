## Project Overview

**PC-switcher** is a synchronization system for seamless switching between Linux desktop machines (laptops, workstations). The goal is near-complete system-state replication rather than simple user data sync.

**Project Stage**: Core infrastructure complete. Core sync functionality in development.

**Target Environment**: Ubuntu 24.04 LTS, btrfs filesystem, machines connected via SSH (LAN, VPN, etc.).

## Critical Context Files

**ALWAYS READ FIRST**:
- `~/.claude/CLAUDE.md` - General agent instructions for all projects
- `docs/planning/high-level-requirements.md` - Complete project vision, scope, workflow, and constraints
- `docs/adr/_index.md` - Summary of all architectural decisions

**For development work**: See `docs/dev/` for AI agent instructions:
- `docs/dev/development-guide.md` - Development expectations and workflow
- `docs/dev/testing-guide.md` - How to write tests

**Premature Analysis Warning**: Files in `docs/premature-analysis/` are early exploration work that may conflict with the High level requirements. These are **inspiration only** and MUST NOT be read automatically or treated as requirements. Only reference them when explicitly requested for specific feature planning.

**When creating or updating an ADR**: Read ADR-001 first for instructions.

## CLI Commands

```bash
pc-switcher sync <hostname>     # Sync to the named machine
pc-switcher init                # Create default config
pc-switcher logs                # Show logs directory
pc-switcher cleanup-snapshots   # Clean up old btrfs snapshots
```

## Development Commands

```bash
uv run ruff check . && uv run ruff format .
uv run codespell
uv run basedpyright
uv run pytest                                 # Unit tests
tests/run-integration-tests.sh                # Integration tests
tests/run-integration-tests.sh tests/integration/jobs/test_install_on_target_job.py::TestInstallOnTargetIntegration::test_install_on_target_fresh_machine   # Specific integration test
```

## Executor calls: `mutates=` unless purely read-only

Everything reaching source/target goes through `Executor` (`executor.py`), which drives `--confirm-each-command` and the verbatim DEBUG trace. `run_command`/`start_process`/`send_file`/`get_file` MUST pass `mutates="<phrase>"` unless the call is PURELY read-only — it may be omitted only when the call can change no state at all: no file content, no process state, no lock or advisory state, no package-manager database. "Changes no file content" is NOT sufficient grounds to leave a call ungated. Running a read under `sudo` does NOT make it a write: `sudo <read-only command>` is read-only. In-process changes (no shell command): `executor.declare_modification(...)`. Omitting it ships a change the user is never shown; `tests/unit/test_mutates_audit.py` enforces this.

## REMEMBER
- When creating a PR on GitHub, ALWAYS set it as draft so that the integration tests don't run prematurely.
