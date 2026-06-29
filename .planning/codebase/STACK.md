# Technology Stack

**Analysis Date:** 2026-06-29

## Languages

**Primary:**
- Python 3.14 - Core implementation (per ADR-003), async-first design using `asyncio`

**Secondary:**
- Bash - Installation and utility scripts (`install.sh`)

## Runtime

**Environment:**
- Python 3.14 via uv virtual environment

**Package Manager:**
- uv (unified Python package/environment manager)
- Lockfile: `uv.lock` (present, reproducible builds)

## Frameworks

**Core:**
- Typer 0.20.0 - CLI framework with decorators (`src/pcswitcher/cli.py`)
- Rich 14.2.0 - Terminal UI with progress bars, tables, panels (`src/pcswitcher/ui.py`)
- asyncssh 2.21.1 - SSH client for remote machine communication (per ADR-002) (`src/pcswitcher/connection.py`)

**Testing:**
- pytest 9.0.1 - Unit and contract test runner
- pytest-asyncio 1.3.0 - Async test support with auto mode
- pytest-randomly 4.0.1 - Randomized test execution
- freezegun 1.5.5 - Time mocking for deterministic tests

**Build/Dev:**
- hatchling - Build backend (PEP 517 compliant)
- uv-dynamic-versioning - Version extraction from git tags/metadata
- ruff 0.14.7 - Linting and formatting
- basedpyright 1.34.0 - Static type checking (strict mode)
- codespell 2.4.1 - Spell checking

## Key Dependencies

**Critical:**
- asyncssh 2.21.1 - SSH communication with target machines (requires cryptography, typing-extensions)
- PyGithub 2.5.0 - GitHub API access for release information
- jsonschema 4.25.1 - Schema validation for YAML configuration
- pyyaml 6.0.3 - YAML configuration file parsing

**Infrastructure:**
- packaging 25.0 - Version number parsing and comparison
- pytimeparse2 1.7.1 - Duration string parsing (e.g., "7d", "1h30m")
- semver 3.0.4 - Semantic versioning utilities
- Rich 14.2.0 - Terminal UI with Rich formatting

**Build Dependencies:**
- cryptography (transitive via asyncssh) - SSH key cryptography
- typing-extensions (transitive via asyncssh) - Type hints backports

## Configuration

**Environment:**
- GITHUB_TOKEN (optional) - Environment variable for GitHub API authentication
  - If set: 5000 requests/hour rate limit
  - If not set: 60 requests/hour rate limit
  - Used by `src/pcswitcher/version.py` for fetching releases

**Build:**
- `ruff.toml` - Linting and formatting rules (target: Python 3.14, line length: 119)
- `pyrightconfig.json` - Type checking configuration (strict mode, Linux platform)
- `pyproject.toml` - Project metadata, dependencies, test configuration
- `.python-version` - Python version specification (3.14)

## Platform Requirements

**Development:**
- Python 3.14+ with uv
- Linux with btrfs filesystem (for testing btrfs features)
- SSH client (for testing SSH integration)

**Production:**
- Ubuntu 24.04 LTS (verified in `install.sh` by `id=/ubuntu` and `VERSION_ID=/24.04`)
- btrfs filesystem for snapshots (subvolumes @ and @home by default)
- SSH connectivity to target machines
- Standard Linux utilities: df, btrfs-progs, grep, sed, awk

---

*Stack analysis: 2026-06-29*
