# External Integrations

**Analysis Date:** 2026-06-29

## APIs & External Services

**GitHub:**
- Release fetching - What it's used for: Checking pc-switcher updates and version availability
  - SDK/Client: PyGithub 2.5.0
  - Auth: GITHUB_TOKEN environment variable (optional)
  - Implementation: `src/pcswitcher/version.py`
  - Methods: `get_releases()` queries flaksit/pc-switcher repository via GitHub API v3
  - Rate limits: 5000 req/hr (authenticated), 60 req/hr (unauthenticated)

## Data Storage

**Databases:**
- None - this is a local-first application

**File Storage:**
- Local filesystem only (no cloud storage)
  - Log files: `~/.local/share/pc-switcher/logs/` (JSON lines format, per ADR-004)
  - Sync history: `~/.local/share/pc-switcher/sync-history.json`
  - Configuration: User-specified location, typically `~/.config/pc-switcher/config.yaml`
  - Client: Standard Python pathlib.Path for all filesystem operations (`src/pcswitcher/`)

**Caching:**
- None - no explicit caching layer

## Authentication & Identity

**SSH Authentication:**
- Method: SSH key-based authentication (no password auth)
- Implementation: asyncssh 2.21.1 (`src/pcswitcher/connection.py`)
- Key management: Assumes SSH keys already configured on source machine (~/.ssh)
- Session multiplexing: Semaphore-controlled (default max 10 concurrent sessions)
- Keepalive: TCP keepalive enabled (15s interval, 3 lost packets to disconnect)

**GitHub API Authentication:**
- Method: GitHub Personal Access Token (PAT) via GITHUB_TOKEN env var (optional)
- Fallback: Unauthenticated access (lower rate limits)
- Scope: Read-only access to public repository (flaksit/pc-switcher)

## Monitoring & Observability

**Error Tracking:**
- None - errors logged to local files and console, no external service

**Logs:**
- File: JSON lines format in `~/.local/share/pc-switcher/logs/`
  - Formatter: `JsonFormatter` in `src/pcswitcher/logger.py`
  - Fields: timestamp, level, message, filename, lineno, and context-specific data
  - Controlled by: LogConfig (file level, TUI level, external logger level)
- Console: Rich-formatted output to terminal (ANSI colors, tables, progress bars)
  - Formatter: `RichFormatter` in `src/pcswitcher/logger.py`
- Custom log level: FULL (level 15, between DEBUG and INFO) for operational details

## CI/CD & Deployment

**Hosting:**
- GitHub repository: flaksit/pc-switcher (public)
- Installation via: `curl | bash` from `install.sh` or local git checkout

**CI Pipeline:**
- GitHub Actions (`.github/workflows/ci.yml`)
  - Lint job: ruff check, ruff format, basedpyright, codespell
  - Unit tests: pytest on tests/unit and tests/contract
  - Conditional: Only runs on code changes (uses dorny/paths-filter)
  - Integration tests: Separate workflow, requires VM infrastructure (not in main CI)

## Environment Configuration

**Required env vars:**
- None (all features work without environment variables)

**Optional env vars:**
- GITHUB_TOKEN - For GitHub API authentication (avoids rate limits)

**Secrets location:**
- SSH keys: Standard ~/.ssh/ location (managed by user's SSH setup)
- GitHub token: Environment variable (set by user before running pc-switcher)
- No secrets stored in version control

## Webhooks & Callbacks

**Incoming:**
- None - pc-switcher does not expose a webhook server

**Outgoing:**
- None - pc-switcher does not send webhooks

## System Integration Points

**SSH Communication:**
- Protocol: SSH v2 (via asyncssh)
- Channels: Multiple concurrent channels per connection (semaphore-limited)
- Commands executed on target: Custom scripts, shell commands for btrfs, system utilities
- File transfer: None (uses SSH commands for all operations, not SCP/SFTP)

**Filesystem Integration:**
- Technology: btrfs (Btrfs filesystem)
- Snapshots: Read-only btrfs snapshots created pre/post sync
- Subvolumes: Defaults to @ (root) and @home (home subvolume)
- Commands: btrfs subvolume snapshot, btrfs subvolume delete, btrfs filesystem usage
- Executor: Local (SystemdExecution, LocalExecutor) or Remote (RemoteExecutor via SSH)

**System Utilities:**
- df - Disk space checking (parsed from `df -B1` output)
- btrfs - Snapshot management (via `sudo btrfs ...` commands)
- Standard Linux tools: grep, sed, awk, etc. (via command execution)

---

*Integration audit: 2026-06-29*
