# ADR-003: Python Orchestrator with Task-Specific Languages

Status: Accepted
Date: 2025-11-14

## TL;DR
Use Python as the primary orchestration language for PC-switcher CLI, with task-specific languages (bash, etc.) for individual sync operations.

## Implementation Rules

**Required Patterns:**
- **Python version**: Target Python 3.13
- **Package structure**: Build as an installable Python package using `uv`, distributable to both source and target machines
- **Dependency management**: Use `uv` exclusively for all dependency management and running Python code (see `~/.claude/python.md` for details)
- **Modern Python practices**: Follow conventions in `~/.claude/python_conventions.md` including:
  - Modern type syntax (`str | None`, `dict[str]`)
  - Full type annotations
  - `from __future__ import annotations`
  - Use `Path` from pathlib for all file operations
  - Use `ruff` for linting/formatting, `basedpyright` for type checking, `pytest` for testing
- Python for main orchestrator, CLI, SSH coordination, state management, and conflict detection
- Use proven libraries: `asyncssh` for SSH, `rich` for TUI, `click`/`typer` for CLI, `structlog` for logging
- Delegate to bash scripts (or other languages) for specific sync operations when appropriate
- All orchestration logic (command sequencing, error handling, phase management) in Python
- Comprehensive error handling using Python's exception system
- Unit tests using `pytest` with mocking for SSH operations

**Forbidden Approaches:**
- Do not implement core orchestration logic in bash
- Do not use languages unfamiliar to the maintainer for core functionality
- Do not mix orchestration concerns into task-specific scripts
- Do not use `pip` or direct `python` commands; always use `uv` for dependency management and running code


## Context

PC-switcher requires orchestration of complex multi-step synchronization between two machines over SSH. The implementation must handle:
- Remote command execution and feedback collection
- State management and conflict detection
- Rich terminal UI for progress reporting
- Error handling and recovery
- Multiple heterogeneous sync operations (files, packages, Docker, VMs, k3s)

See `docs/adr/considerations/adr-003-implementation-language-analysis.md` for detailed analysis including:
- Evaluation of Bash, Python, Go, Ansible, and hybrid approaches
- Pros/cons analysis for each option
- Decision rationale based on project principles

## Decision

- **Python** as the primary implementation language for:
  - Main CLI entry point
  - Orchestration logic across source and target machines
  - SSH communication layer (`asyncssh`)
  - State management and conflict detection
  - Terminal UI (using `rich` or `textual`)
  - Logging and audit trails

- **Task-specific languages** (bash, or others) for:
  - Individual sync operations where appropriate (e.g., apt package sync, docker sync)
  - System-level operations where bash is more natural
  - Operations that benefit from direct shell integration

- **Architecture pattern:**
  ```plain
  pc-switcher/
  ├── pyproject.toml      # uv project configuration
  ├── uv.lock             # Locked dependencies
  ├── README.md
  ├── src/
  │   └── pcswitcher/     # Installable Python package
  │       ├── __init__.py
  │       ├── cli.py      # CLI entry point
  │       ├── orchestrator.py # Main sync orchestration
  │       ├── ssh.py      # Remote execution
  │       ├── conflict.py # Conflict detection
  │       └── ui.py       # Terminal UI
  ├── scripts/            # Task-specific scripts (bundled with package)
  │   ├── sync-apt.sh
  │   ├── sync-docker.sh
  │   └── ...
  └── tests/              # Test suite
      └── ...
  ```
  - CLI entry point configured in `pyproject.toml` as `pcsync` console script
  - Installable via `uv pip install .` or `uv tool install .`

- **Python version selection (as of 2025-11-14):**
  - Target: Python 3.13
  - Target: Python 3.13
  - Python 3.14 support status for selected libraries:
    - ✅ Rich 14.2.0+, Textual 6.3.0+, Typer 0.20.0+, Click 8.3.0+, structlog (latest)
    - ✅ AsyncSSH (supports latest Python versions)

**Rationale:**
- Aligns with **Reliability principle (#1)**: Python's exception handling, logging, and testability
- Aligns with **Maintainability principle (#6)**: Maintainer knows Python well; reduces future cognitive load
- Aligns with **UX principle (#2)**: Excellent TUI libraries (`rich`, `textual`) for smooth terminal experience
- Aligns with **Well-supported tools principle (#3)**: Mature ecosystem
- Hybrid approach leverages strengths of both: Python for structure, bash for system integration

## Consequences

**Positive:**
- Robust error handling and recovery using Python's exception system
- Rich terminal UI capabilities for progress reporting and user interaction
- Easy to test orchestration logic with mocking and fixtures
- Natural integration with SSH libraries (`asyncssh`)
- Can evolve individual sync operations independently (bash scripts callable from Python)
- Maintainable by someone who knows Python well
- Standard dependency (Python 3.13 available on Ubuntu 24.04)
- Modern dependency management with `uv` (fast, reliable, reproducible builds)
- Modern type system enables better IDE support and early error detection
- Clean distribution model: single installable package deployable to both source and target machines
- Proper package structure enables versioning, dependency management, and upgrade path

**Negative:**
- Not a single static binary (though could use `pyinstaller` if needed)
- Requires discipline to maintain clean separation between orchestration (Python) and task execution (scripts)

- Additional tooling dependency (`uv`) though it's becoming standard in modern Python development

## References
- High Level Requirements: UX requirements, project principles, workflow
- ADR-002: SSH as communication channel
- docs/adr/considerations/adr-003-implementation-language-analysis.md: Detailed technology analysis and alternatives
- ~/.claude/python.md: Python development conventions and tooling requirements
- [Python 3.14 Readiness Tracker](http://pyreadiness.org/3.14/) for monitoring library compatibility
