# CLAUDE.md

This file provides guidance to AI agents when working with code in this repository.


## Project Overview

**PC-switcher** is a synchronization system for seamless switching between Linux desktop machines (laptops, workstations). The goal is near-complete system-state replication rather than simple user data sync.

**Project Stage**: Core infrastructure complete. Core sync functionality in development.

**Target Environment**: Ubuntu 24.04 LTS, btrfs filesystem, machines connected via SSH (LAN, VPN, etc.).

## Critical Context Files

**ALWAYS READ FIRST**:
- `~/.claude/CLAUDE.md` - General agent instructions for all projects
- `docs/planning/High level requirements.md` - Complete project vision, scope, workflow, and constraints
- `docs/adr/_index.md` - Summary of all architectural decisions

**For development work**: See `docs/dev/` for AI agent instructions:
- `docs/dev/development-guide.md` - Development expectations and workflow
- `docs/dev/testing-guide.md` - How to write tests

**Premature Analysis Warning**: Files in `docs/Premature analysis/` are early exploration work that may conflict with the High level requirements. These are **inspiration only** and MUST NOT be read automatically or treated as requirements. Only reference them when explicitly requested for specific feature planning.

**When creating or updating an ADR**: Read ADR-001 first for instructions.

## Current Project Structure

```text
src/pcswitcher/       # Core implementation (orchestrator, CLI, jobs, config)
tests/                # Unit, contract, integration tests, and manual playbook
docs/
├── dev/              # AI agent instructions (development-guide.md, testing-guide.md)
├── ops/              # Operational guides (testing-architecture.md, testing-ops.md, ci-setup.md)
├── planning/         # Project scope (High level requirements.md, Feature breakdown.md)
├── system/           # Golden Copy specs (per ADR-011)
└── adr/              # Architectural decisions
specs/                # SpecKit feature specifications (immutable history)
```

## CLI Commands

```bash
pc-switcher sync <target>       # Sync to target machine
pc-switcher init                # Create default config
pc-switcher logs                # Show logs directory
pc-switcher cleanup-snapshots   # Clean up old btrfs snapshots
```

## Development Commands

```bash
uv run ruff check . && uv run ruff format .   # Lint and format
uv run basedpyright                           # Type check
uv run pytest                                 # Unit tests
tests/run-integration-tests.sh                # Integration tests
tests/run-integration-tests.sh tests/integration/test_end_to_end_sync.py::TestInstallOnTargetIntegration::test_install_on_target_fresh_machine                # Specific integration test
```

## Active Technologies
- Python 3.14 (per ADR-003) via uv env + pytest, pytest-asyncio, asyncssh
- **Typer** - CLI framework
- **Rich** - Terminal UI with progress bars
- **asyncssh** - SSH communication (per ADR-002)
- **pytest + pytest-asyncio** - Testing framework
- JSON lines log files in `~/.local/share/pc-switcher/logs/` (004-python-logging)

## REMEMBER
- You MUST ALWAYS use `uv run` for running Python or python packages: `uv run python`, `uv run ruff`, `uv run basedpyright`, etc.
- You MUST NEVER use the system Python directly. So DO NOT run `python3`, `python`, `pip`, etc. directly.
- When creating a PR on GitHub, ALWAYS set it as draft so that the integration tests don't run prematurely.

## Python Standards

This project uses Python 3.14 via uv. NEVER use system Python directly.

### Critical Rules
- `from __future__ import annotations` in all files
- Modern type syntax: `str | None`, `dict[str, int]`, `list[T]` (not `Optional`, `Dict`, `List`)
- `Path` from pathlib for all file paths
- Full type annotations on all function signatures
- Import from `collections.abc` not `typing` for collection types
- Use `@override` for method overrides
- Prefer `StrEnum` for string-based enums

### Quality
- Single responsibility, focused functions
- Context managers for resources
- Specific exception types with meaningful messages
- Minimal inline comments - code should be self-documenting
- Docstrings explain "why", not obvious "what"

### Tooling
- `uv run ruff check . && uv run ruff format .` - lint/format
- `uv run basedpyright` - type check
- `uv run pytest` - tests
- `uv run codespell` - typo check

### Testing
- Test edge cases and error conditions
- Mock external I/O, but create integration tests against real deps
- Avoid trivial tests that just test the language
