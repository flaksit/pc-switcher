# User AGENTS.md

This file provides guidance to AI agents when working with code in this repository.


## General assistant rules

## General Coding Guidelines about using comments

- Keep all comments concise and clear and suitable for inclusion in final production.
- DO use comments whenever the intent of a given piece of code is subtle or confusing or avoids a bug or is not obvious from the code itself.
- DO NOT repeat in comments what is obvious from the names of functions or variables or types.
- DO NOT include comments that reflect what you did, such as “Added this function” as this is meaningless to anyone reading the code later.  
  Instead, describe in your message to the user any other contextual information.
- DO NOT use fancy or needlessly decorated headings like “===== MIGRATION TOOLS =====” in comments

## Git Workflow

- Always git clone with ssh. Not https.
- When merging a git branch, don't use fast-forward and don't squash commits.

## Working Principles

- When you have Open questions and some part of the remainder of your task depends on the answer, then stop, ask the question and wait for the answer before continuing. Try to group your open question and ask them together, so that your work is interupted less number of times.

## Tools and commands

- When connecting to a PostgreSQL database through kubectl port-forwarding, you need to connect without SSL. Use the following command:

  ```bash
  psql -h localhost -p [port] -U [user] -d [database] --set=sslmode=disable
  ```

## Documentation

- Write diagrams in Mermaid syntax

---

# Project AGENTS.md

## Project Overview

**PC-switcher** is a synchronization system for seamless switching between Linux desktop machines (laptops, workstations). The goal is near-complete system-state replication rather than simple user data sync.

**Project Stage**: Early planning/design phase. No implementation code exists yet.

**Target Environment**: Ubuntu 24.04 LTS, btrfs filesystem, machines connected via 1Gb LAN during sync.

## Critical Context Files

**ALWAYS READ FIRST**:
- `High level requirements.md` - Complete project vision, scope, workflow, and constraints
- `docs/adr/_index.md` - Summary of all architectural decisions

**Premature Analysis Warning**: Files in `docs/Premature analysis/` are early exploration work that may conflict with `High level requirements.md`. These are **inspiration only** and MUST NOT be read automatically or treated as requirements. Only reference them when explicitly requested for specific feature planning.

## Documentation-First Workflow

This project uses **SpecKit** - a structured workflow for specification-driven development via custom slash commands:

### SpecKit Workflow

1. **Constitution**:
   - `/speckit.constitution` - Create/update project constitution (coding principles, patterns, constraints)

2. **Specification Phase**:
   - `/speckit.specify` - Create feature spec from natural language description
   - `/speckit.clarify` - Identify underspecified areas with targeted questions

3. **Planning Phase**:
   - `/speckit.plan` - Generate design artifacts from spec
   - `/speckit.tasks` - Create dependency-ordered actionable tasks

4. **Quality Assurance**:
   - `/speckit.analyze` - Cross-artifact consistency analysis after task generation
   - `/speckit.checklist` - Generate custom validation checklist

5. **Implementation**:
   - `/speckit.implement` - Execute implementation plan from tasks.md

### Feature Development Pattern

```bash
# Start a feature
/speckit.specify "feature description"
/speckit.clarify  # if needed
/speckit.plan
/speckit.tasks
/speckit.analyze  # verify consistency
/speckit.implement
```

## Architecture Decision Records (ADRs)

Follow ADR-001 guidelines:

- **Location**: `docs/adr/`
- **Immutable**: ADRs can be superseded but never edited after acceptance
- **Structure**: TL;DR, Implementation Rules, Context, Decision, Consequences, References
- Keep ADRs concise; place detailed background in `docs/adr/considerations/`
- Always update `docs/adr/_index.md` when adding new ADRs

**Granularity**: Combine tightly-coupled decisions; split when decisions can evolve independently or cross major boundaries (infrastructure vs application vs domain logic).

## Sync Scope Requirements

### What MUST Sync
1. User data (`/home`, `/root`) - all documents, code, configs, selective caches
2. Installed packages (apt, snap, flatpak, manual .debs, custom PPAs)
3. Application configurations (GNOME, cloud mounts, systemd services)
4. System configurations (machine-independent `/etc` files, users/groups)
5. File metadata (owner, permissions, ACLs, timestamps)
6. VMs (KVM/virt-manager)
7. Docker (images, containers, volumes, cache)
8. k3s (local single-node cluster state, including PVCs)

### NEVER Sync (Machine-Specific)
- SSH keys (`.ssh/id_*`)
- Tailscale config (`.config/tailscale`)
- Machine hardware cache (GPU shaders, fontconfig)
- Machine-specific packages and configuration

## Development Principles

Priority order (trade-offs possible):
1. **Reliability**: No data loss, conflict detection, consistent state after sync
2. **Smooth UX**: Minimal manual intervention
3. **Use well-supported tools** and best practices
4. **Minimize disk wear** (NVMe SSDs)
5. Sync speed
6. **Maintainability/simplicity**: Simple architecture, easy to understand and modify

## Documentation Requirements

All documentation must be:
- Always up-to-date with current state
- Multi-level (rationale → architecture → implementation → scripts → user docs)
- Easily navigable between levels

## Project Structure

This repository will contain:
- Scripts and programs implementing sync logic
- Installation and upgrade scripts
- Complete documentation (architecture, implementation, user docs, development guidelines)

This repository will NOT contain:
- Configuration management (must be usable by multiple users with their own configs)

## Key Constraints

- Only one machine actively used at a time (uni-directional sync)
- Manual trigger for sync operations
- Acceptable to require constraints (documented clearly):
  - VMs suspended/powered off before sync
  - User logout from desktop before sync
  - Certain applications not running during sync
  - Specific directory subvolume structure
- Everything automatable should be automated
- Must detect conflicts from unsupported concurrent use (resolution is manual)

## Workflow

1. Work on source machine
2. Start/awaken target machine
3. Manual trigger sync from source to target
4. Wait for sync completion
5. Resume work on target
6. (Optional) Suspend/shutdown source machine

## Notes

- UX: Terminal-based UI, single command to launch entire sync process
- No implementation code exists yet - focus on specification and planning first using SpecKit workflow

## Active Technologies
- Python 3.13 (per ADR-003; Paramiko not yet supporting 3.14) (001-foundation)
- IMPORTANT: Read ~/.claude/python.md and refered files therein for python conventions. Follow them strictly.

## Recent Changes
- 001-foundation: Added Python 3.13 (per ADR-003; Paramiko not yet supporting 3.14)


---

# Python Development Conventions

These conventions apply to all Python code and projects, regardless of the agent or persona being used.

## Python Version and Modern Practices

- Target Python >=3.12 exclusively
- Use modern type syntax: `str | None` instead of `Optional[str]`, `dict[str]` instead of `Dict[str]`
- Always include `from __future__ import annotations` for forward references
- Use `@override` decorator for method overrides (from typing in 3.12+)
- Prefer `StrEnum` for string-based enums
- Use `Path` from pathlib for all file operations

## Project Setup and Developer Workflows

- ALWAYS use uv for running all code and managing dependencies. Never use direct `pip` or `python` commands.
- Use modern uv commands: `uv sync`, `uv run ...`, etc. Use `uv add`. NEVER use `uv pip install`.
- Use `codespell` for checking common typos in code and comments
- Use `ruff` for linting and formatting
- Use `basedpyright` for type checking
- Use `pytest` for testing

## Code Quality Standards

- Full type annotations using modern syntax for all function signatures
- Import types from correct modules (e.g., use `collections.abc` not `typing` for collection types)
- Implement proper error handling with specific exception types and meaningful messages
- Follow single responsibility principle and keep functions focused
- Use context managers for resource management
- Prefer composition over inheritance
- Write self-documenting code with meaningful names
- Follow PEP 8 (max line length: 119 characters), use Python's strengths, avoid anti-patterns

## Documentation Philosophy

- Document architectural decisions and design patterns used
- Write concise, explanatory docstrings that focus on the "why" not the obvious "what"
- Explain complex logic rationales inline where needed
- Use backticks for variable/parameter names in docstrings
- Avoid repetitive or obvious comments

## Testing Strategy

- Prefer inline tests when appropriate for better code locality
- Avoid trivial tests that just test the language itself
- Use simple assertions rather than complex pytest fixtures when possible
- Test edge cases, error conditions, and boundary values
- Mock external dependencies and I/O operations appropriately
- For each test with mocks, create an integration test that runs against real dependencies
- Never use `assert False` - raise `AssertionError` with explanation instead

## Performance Optimization Techniques

- Use appropriate data structures (sets for membership, deque for queues, etc.)
- Implement generators for memory-efficient iteration over large datasets
- Apply caching strategies (functools.lru_cache, custom caching) where beneficial
- Utilize built-in functions and libraries (itertools, collections, functools)
- Consider algorithmic complexity and choose efficient algorithms
- Profile code using cProfile, line_profiler, or memory_profiler before optimizing

## Error Handling and Type Checking

- Use `# pyright: ignore` sparingly and always with explanation
- Raise `AssertionError` with descriptive messages for invariant violations
- Handle exceptions at appropriate levels, avoid bare except clauses
- Use specific exception types that convey meaning

## File Operations Best Practices

- Always use `Path` objects for file paths
- Implement atomic file writing (write to temp, then rename)
- Use context managers for file operations
- Handle encoding explicitly (default to UTF-8)

## Core Principles

- Be pragmatic: avoid over-engineering and premature optimization
- Leverage Python's standard library first. Use third-party packages judiciously
- Prioritize code clarity and explainability over clever tricks
- Always strive for code that is not just functional, but clean, maintainable, and performant


---


# Python Development Commands

## Package Management with uv

```bash
# Initialize new project
uv init

# Add dependencies
uv add package_name
uv add --dev ipykernel pytest ruff basedpyright  # dev dependencies

# Sync dependencies
uv sync

# Show installed packages
uv tree
uv show package_name

# Run commands in virtual environment
uv run python script.py
uv run pytest
```

## Testing with pytest

```bash
# Run all tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=src --cov-report=term-missing

# Run specific test file or function
uv run pytest tests/test_module.py
uv run pytest tests/test_module.py::test_function

# Run tests matching pattern
uv run pytest -k "pattern"

# Run tests with verbose output
uv run pytest -v

# Run tests and stop on first failure
uv run pytest -x
```

## Linting and Formatting with ruff

```bash
# Format code
uv run ruff format .

# Check linting issues
uv run ruff check .

# Fix auto-fixable issues
uv run ruff check --fix .

# Check specific file
uv run ruff check path/to/file.py
```

## Type Checking with basedpyright

```bash
# Type check entire project
uv run basedpyright

# Type check specific file
uv run basedpyright path/to/file.py
```

## Code Quality Checks

```bash
# Run all quality checks (typical CI pipeline)
uv run ruff format --check .
uv run ruff check .
uv run basedpyright
uv run pytest

# Check for common typos
uv run codespell
```

## Profiling and Performance

```bash
# Profile with cProfile
uv run python -m cProfile -s cumtime script.py

# Profile with line_profiler
uv run kernprof -l -v script.py

# Memory profiling
uv run python -m memory_profiler script.py
```

## Common Development Patterns

```bash
# Create requirements.txt from uv (if needed for compatibility)
uv pip freeze > requirements.txt

# Check outdated packages
uv pip list --outdated

# Clean pyc files and caches
find . -type d -name "__pycache__" -exec rm -rf {} +
find . -type d -name ".pytest_cache" -exec rm -rf {} +
find . -type d -name ".ruff_cache" -exec rm -rf {} +
find . -type f -name "*.pyc" -delete
```
