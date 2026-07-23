# Technology Stack

**Analysis Date:** 2026-07-23

## Languages

**Primary:**
- Python 3.14+ (`requires-python = ">=3.14"` in `pyproject.toml`, pinned by `.python-version`) - all application code under `src/pcswitcher/`

**Secondary:**
- Bash - installer `install.sh`, integration test driver `tests/run-integration-tests.sh`, VM helper scripts under `tests/integration/`
- YAML - config schema `src/pcswitcher/schemas/config-schema.yaml`, defaults `src/pcswitcher/default-config.yaml`, CI workflows in `.github/workflows/`
- rsync filter syntax - `src/pcswitcher/home.filter`, `src/pcswitcher/root.filter`

## Runtime

**Environment:**
- CPython 3.14 on Linux (Ubuntu 24.04 LTS target), `pythonPlatform = "Linux"` in `pyrightconfig.json`
- asyncio event loop drives all remote work (`src/pcswitcher/orchestrator.py`, `src/pcswitcher/executor.py`); see `docs/adr/adr-005-asyncio-concurrency.md`

**Package Manager:**
- `uv` (Astral) - used for dev (`uv run`, `uv sync`) and for end-user installation (`uv tool install` in `install.sh`)
- Lockfile: `uv.lock` present and committed
- Auto-activation via `.envrc` (direnv): `uv sync` then activate `.venv`

**Build backend:**
- `hatchling` with `uv-dynamic-versioning` (`[tool.hatch.version] source = "uv-dynamic-versioning"`)
- Version is derived from git tags, PEP 440 style. Annotated tags required; see `docs/adr/adr-004-dynamic-versioning-github-releases.md`

## Frameworks

**Core:**
- `typer >=0.27.0` - CLI framework; entry point `pc-switcher = "pcswitcher.cli:app"` (`src/pcswitcher/cli.py`)
- `rich >=15.0.0` - terminal UI (Console, Live, Panel, Table, Progress, Syntax, Text) in `src/pcswitcher/ui.py`, `src/pcswitcher/terminal.py`
- `asyncssh >=2.24.0` - SSH transport, process spawning, SFTP (`src/pcswitcher/connection.py`)
- `questionary >=2.1.1` - interactive prompts (`src/pcswitcher/confirmer.py`, package review flows)

**Testing:**
- `pytest >=9.1.1` with `pytest-asyncio >=1.4.0`, `pytest-randomly >=4.1.0`, `freezegun >=1.5.5`
- Markers declared in `pyproject.toml`: `integration`, `local_rsync`, `slow`, `benchmark`; default run excludes `integration`

**Build/Dev:**
- `ruff >=0.15.22` - lint + format, config in `ruff.toml` (target `py314`, line length 119)
- `basedpyright >=1.39.9` - type checking in `strict` mode, config in `pyrightconfig.json`
- `codespell >=2.4.3` - spelling gate in CI

## Key Dependencies

**Critical:**
- `asyncssh` - the entire remote execution model depends on it; resolves `~/.ssh/config` automatically
- `pygithub >=2.9.1` - GitHub Releases lookup for version floors and self-upgrade (`src/pcswitcher/version.py`)
- `pyyaml >=6.0.3` + `jsonschema >=4.26.0` - config load and validation (`src/pcswitcher/config.py`)
- `semver >=3.0.4` and `packaging >=26.2` - release/version comparison in `src/pcswitcher/version.py`, `src/pcswitcher/jobs/install_on_target.py`

**Infrastructure:**
- `pytimeparse2 >=1.7.1` - human-readable duration parsing for snapshot retention (`src/pcswitcher/btrfs_snapshots.py`)
- stdlib `sqlite3` - selective VS Code state sync (`src/pcswitcher/jobs/vscode_state_sync.py`)
- stdlib `fcntl` - single-instance lock (`src/pcswitcher/lock.py`)

**External binaries invoked (not Python packages):** `rsync`, `ssh`, `sudo`, `apt`/`apt-get`/`apt-mark`/`dpkg`, `snap`, `flatpak`, `btrfs`, `uv`, `python3` (on target), `pkill`.

## Configuration

**User config:**
- `~/.config/pc-switcher/config.yaml` (`src/pcswitcher/config.py:214`), created by `pc-switcher init` from `src/pcswitcher/default-config.yaml`
- Validated against `src/pcswitcher/schemas/config-schema.yaml`
- Per-machine package declarations follow `src/pcswitcher/machine-packages.example.yaml`

**Runtime state:**
- `~/.local/share/pc-switcher/logs/` (`src/pcswitcher/logger.py:336`)
- `~/.local/share/pc-switcher/sync-history.json` (`src/pcswitcher/sync_history.py:61`)
- Lock file in `~/.local/share/pc-switcher/` (`src/pcswitcher/lock.py:40`)
- Btrfs snapshots under `/mnt/btrfs-snapshots/pc-switcher`

**Privilege escalation:**
- sudoers drop-in `/etc/sudoers.d/pc-switcher` (`src/pcswitcher/sudoers.py:18`), never edits the distro `/etc/sudoers`

**Environment variables:**
- `GITHUB_TOKEN` (optional, raises GitHub API rate limit from 60 to 5000 req/hr)
- `PCSWITCHER_SKIP_VERSION_CHECK` (test/dev escape hatch)
- `VERSION` (consumed by `install.sh` for pinned installs)

**Tooling configs:**
- `pyproject.toml`, `ruff.toml`, `pyrightconfig.json`, `.markdownlint.jsonc`, `.envrc`

## Platform Requirements

**Development:**
- Linux, Python 3.14, `uv`, `direnv` (optional), `rsync` binary for `local_rsync`-marked tests
- Integration tests need two Hetzner Cloud VMs plus `hcloud` CLI and `HCLOUD_TOKEN`

**Production:**
- Ubuntu 24.04 LTS on btrfs, two or more machines reachable over SSH
- Installed as a `uv tool` via `curl -sSL .../install.sh | bash`; self-upgrade path uses `uv tool install --force`
