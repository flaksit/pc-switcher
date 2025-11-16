# Contributing to PC-switcher

Thank you for your interest in contributing to PC-switcher. This guide covers development setup, testing, code quality standards, and the pull request workflow.

## Development Setup

### Prerequisites

- Ubuntu 24.04 LTS
- [uv](https://github.com/astral-sh/uv) package manager
- Git

### Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Clone and Setup

```bash
# Clone repository (use SSH for write access)
git clone git@github.com:yourusername/pc-switcher.git
cd pc-switcher

# Sync all dependencies (uv automatically installs Python 3.13)
uv sync

# Verify setup
uv run pc-switcher --version
```

### Project Structure

```
pc-switcher/
├── src/pcswitcher/         # Main source code
│   ├── cli/                # Command-line interface
│   ├── core/               # Core orchestration logic
│   ├── modules/            # Sync modules (btrfs, user-data, etc.)
│   ├── remote/             # SSH remote execution
│   └── utils/              # Utility functions
├── tests/                  # Test suite
├── docs/                   # Documentation
│   ├── adr/               # Architecture Decision Records
│   └── Premature analysis/ # Early exploration (reference only)
├── specs/                  # Feature specifications
│   └── 001-foundation/    # Foundation feature specs
├── config/                # Default and example configuration files
└── pyproject.toml         # Project configuration
```

## Running Tests

### Full Test Suite

```bash
# Run all tests
uv run pytest

# Run with verbose output
uv run pytest -v

# Run and stop on first failure
uv run pytest -x

# Run with coverage report
uv run pytest --cov=src --cov-report=term-missing
```

### Specific Tests

```bash
# Run specific test file
uv run pytest tests/test_session.py

# Run specific test function
uv run pytest tests/test_session.py::test_generate_session_id

# Run tests matching pattern
uv run pytest -k "btrfs"
```

### Test Categories

- **Unit tests**: Fast, isolated tests with mocked dependencies
- **Integration tests**: Tests that use real SSH connections or btrfs operations
- **End-to-end tests**: Complete sync flow validation (requires test machines)

## Code Quality

### Type Checking

```bash
# Run type checker (basedpyright)
uv run basedpyright

# Check specific file
uv run basedpyright src/pcswitcher/core/orchestrator.py
```

Type hints are required for all function signatures. Use modern Python 3.13+ syntax:
- `str | None` instead of `Optional[str]`
- `list[str]` instead of `List[str]`
- Include `from __future__ import annotations` for forward references

### Linting

```bash
# Check for linting issues
uv run ruff check .

# Auto-fix issues
uv run ruff check --fix .

# Format code
uv run ruff format .

# Check formatting without changes
uv run ruff format --check .
```

### Spell Checking

```bash
# Check for common typos
uv run codespell
```

### Complete Quality Check

Run all checks before submitting a PR:

```bash
uv run ruff format --check .
uv run ruff check .
uv run basedpyright
uv run pytest
uv run codespell
```

## Code Style Guidelines

### Python Conventions

Follow PEP 8 with these specifics:
- **Line length**: 119 characters (configured in pyproject.toml)
- **Target version**: Python 3.13
- **Imports**: Use absolute imports, organize with isort rules
- **Type annotations**: Required for all public functions

### Docstrings

Use Google-style docstrings:

```python
def sync_to_target(
    source_path: Path,
    target_hostname: str,
    dry_run: bool = False,
) -> SyncResult:
    """Synchronize files from source to target machine.

    Transfers all files in source_path to the target machine using
    rsync over SSH. Progress is reported via callback.

    Args:
        source_path: Local directory to sync
        target_hostname: Target machine hostname or IP
        dry_run: If True, show what would be synced without making changes

    Returns:
        SyncResult containing statistics and any errors encountered

    Raises:
        ConnectionError: If SSH connection fails
        SyncError: If sync operation fails after retries
    """
```

### Comments

- **Do**: Explain the "why" for non-obvious logic
- **Do**: Document complex algorithms or workarounds
- **Don't**: Repeat what is obvious from code
- **Don't**: Add comments like "Added this function" or "Fixed bug"
- **Don't**: Use decorative comment headers

### Error Messages

Make error messages actionable:

```python
# Good: Specific and actionable
raise ConfigError(
    f"Subvolume '{subvol}' not found. "
    f"Run 'sudo btrfs subvolume list /' to see available subvolumes, "
    f"then update config to match."
)

# Bad: Vague
raise ConfigError(f"Invalid subvolume: {subvol}")
```

## Module Development

### Creating a New Module

1. Create module file in `src/pcswitcher/modules/`
2. Implement `SyncModule` interface from `core/module.py`
3. Add module to default config schema
4. Write unit tests
5. Update documentation

### Module Interface

```python
from pcswitcher.core.module import SyncModule

class MyModule(SyncModule):
    @property
    def name(self) -> str:
        return "my-module"

    @property
    def required(self) -> bool:
        return False  # Can be disabled

    def get_config_schema(self) -> dict[str, Any]:
        """Return JSON Schema for validation."""
        pass

    def validate(self) -> list[str]:
        """Check prerequisites without state changes."""
        pass

    def pre_sync(self) -> None:
        """Setup before main sync."""
        pass

    def sync(self) -> None:
        """Main sync logic."""
        pass

    def post_sync(self) -> None:
        """Cleanup after sync."""
        pass

    def abort(self, timeout: float) -> None:
        """Stop running processes (best-effort)."""
        pass
```

### Module Logging

```python
from pcswitcher.core.logging import LogLevel

# In module methods
self.log(LogLevel.INFO, "Starting operation", count=42)
self.log(LogLevel.FULL, "Processing file", path="/some/path")
self.log(LogLevel.ERROR, "File missing, skipping", path=missing_path)

# For critical failures, raise SyncError instead of logging CRITICAL
if critical_failure:
    raise SyncError("Detailed error message with remediation steps")
```

### Progress Reporting

```python
def sync(self) -> None:
    items = get_items_to_sync()
    for i, item in enumerate(items):
        # Progress as float 0.0-1.0
        percentage = (i + 1) / len(items)
        self.emit_progress(percentage, f"Syncing {item}")
        process_item(item)
```

## Git Workflow

### Branch Naming

- Feature branches: `feature/description`
- Bug fixes: `fix/description`
- Documentation: `docs/description`

### Commit Messages

Write clear, descriptive commit messages:

```
Add btrfs snapshot rollback functionality

Implement rollback_to_presync() method that restores system
state from pre-sync snapshots. This provides data safety when
sync operations fail.

Key changes:
- Add rollback method to BtrfsSnapshotsModule
- Verify snapshot existence before rollback
- Delete current subvolume and restore from snapshot
- Log all operations for auditability
```

### Pull Request Process

1. **Create feature branch**:
   ```bash
   git checkout -b feature/my-feature
   ```

2. **Make changes and test**:
   ```bash
   # Write code
   # Run quality checks
   uv run ruff format .
   uv run ruff check .
   uv run basedpyright
   uv run pytest
   ```

3. **Commit with descriptive message**:
   ```bash
   git add .
   git commit -m "Description of changes"
   ```

4. **Push and create PR**:
   ```bash
   git push -u origin feature/my-feature
   # Create PR on GitHub
   ```

5. **PR requirements**:
   - All quality checks pass
   - Tests cover new functionality
   - Documentation updated if needed
   - Descriptive PR title and description

### PR Template

```markdown
## Summary
- Brief description of changes

## Test Plan
- [ ] Unit tests added/updated
- [ ] Integration tests pass
- [ ] Manual testing performed

## Documentation
- [ ] README updated (if applicable)
- [ ] Docstrings added for new functions
- [ ] Architecture docs updated (if applicable)
```

## Architecture Decision Records (ADRs)

### When to Create ADR

Create an ADR when making significant architectural decisions:
- Choosing libraries or frameworks
- Designing module interfaces
- Selecting communication patterns
- Making trade-offs that affect future development

### ADR Format

ADRs are stored in `docs/adr/` and follow this structure:

```markdown
# ADR-NNN: Title

## TL;DR
Brief summary of the decision

## Implementation Rules
- Specific guidelines for implementing this decision
- What code must do/not do

## Context
Background and problem statement

## Decision
What was decided and why

## Consequences
- Positive outcomes
- Negative trade-offs
- Neutral impacts

## References
- Related ADRs
- External documentation
```

### ADR Immutability

ADRs are immutable once accepted. If a decision needs to change:
1. Create a new ADR that supersedes the old one
2. Reference the old ADR
3. Mark the old ADR as superseded

## Testing Best Practices

### Unit Test Example

```python
from unittest.mock import Mock
from pcswitcher.modules.dummy_success import DummySuccessModule

def test_module_validation():
    config = {"duration_seconds": 10}
    remote = Mock()
    module = DummySuccessModule(config, remote)

    # Inject mocked methods (normally done by orchestrator)
    module.log = Mock()
    module.emit_progress = Mock()

    errors = module.validate()
    assert errors == []
```

### Mocking Guidelines

- Mock external dependencies (SSH, filesystem, subprocess)
- Mock at the boundary, not internal implementation
- Use `unittest.mock.Mock` or `patch`
- Verify mock calls to ensure correct behavior

### Test Fixtures

```python
import pytest
from pathlib import Path

@pytest.fixture
def temp_config_file(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
sync_modules:
  btrfs_snapshots: true
btrfs_snapshots:
  subvolumes: ["@"]
  snapshot_dir: "/.snapshots"
  keep_recent: 3
  max_age_days: 7
""")
    return config_path
```

## Common Development Tasks

### Adding a Dependency

```bash
# Runtime dependency
uv add package-name

# Development dependency
uv add --dev package-name

# Sync after adding
uv sync
```

### Debugging

```bash
# Run with Python debugger
uv run python -m pdb -m pcswitcher.cli.main sync target

# Increase log verbosity
# Edit config: log_cli_level: DEBUG
```

### Local Installation

```bash
# Install as editable for development
uv tool install --editable .

# Now pc-switcher command uses local source
pc-switcher --version
```

## Getting Help

- Read existing code and tests for patterns
- Check ADRs for architectural decisions
- Review specs/ for feature specifications
- Open an issue for questions or bugs
