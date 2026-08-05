# External Integrations

**Analysis Date:** 2026-07-23

## APIs & External Services

**Source control / release distribution:**
- GitHub Releases API - used to resolve the highest release at or below a version ("release floor") and to list stable releases for self-upgrade
  - SDK/Client: `pygithub` (`from github import Auth, BadCredentialsException, Github` in `src/pcswitcher/version.py:25`)
  - Auth: `GITHUB_TOKEN` env var, optional. Unauthenticated falls back to 60 req/hr; token gives 5000 req/hr (`src/pcswitcher/version.py:154-166`). A rejected token logs a warning and degrades to anonymous rather than failing.
  - Repo constant: `GITHUB_REPO_URL = "https://github.com/flaksit/pc-switcher"` (`src/pcswitcher/cli.py:528`)
- raw.githubusercontent.com - installer bootstrap; `src/pcswitcher/install.py:33` builds `https://raw.githubusercontent.com/flaksit/pc-switcher/{ref}/install.sh`, and `install.sh` itself is designed for `curl -sSL ... | bash`
- astral.sh - `install.sh:111` bootstraps `uv` via `curl -LsSf https://astral.sh/uv/install.sh | sh`

**Remote machines (the core integration):**
- SSH to the peer desktop, via `asyncssh` (`src/pcswitcher/connection.py`)
  - Auth: user's own SSH agent/keys; `~/.ssh/config` is honoured automatically by `asyncssh.connect` — no credentials handled by the tool
  - Capabilities used: `run()` for one-shot commands, `create_process()` for streamed long-running jobs, `start_sftp_client()` for file transfer, keepalive for health monitoring
  - See `docs/adr/adr-002-ssh-communication-channel.md`

**Package managers on both hosts:**
- APT (`apt`, `apt-get`, `apt-mark`, `dpkg`) - `src/pcswitcher/jobs/apt_sync.py`
- Snap (`snap`) - `src/pcswitcher/jobs/snap_sync.py`
- Flatpak (`flatpak`) - `src/pcswitcher/jobs/flatpak_sync.py`
- Declarative convergence model per `docs/adr/adr-020-declarative-package-convergence.md`

**Cloud provider (test infrastructure only):**
- Hetzner Cloud, via the `hcloud` CLI in `tests/run-integration-tests.sh`
  - Auth: `HCLOUD_TOKEN` env var
  - Used to look up the IPs of the `pc1`/`pc2` test VMs and reset them to baseline

## Data Storage

**Databases:**
- SQLite - not a datastore of the app, but read/written as *synced content*: VS Code `state.vscdb` files under `~/.config/<editor>/User/globalStorage/` are selectively merged (`src/pcswitcher/jobs/vscode_state_sync.py`, `docs/adr/adr-018-selective-vscode-state-sync.md`). The remote side is driven through the target's system `python3` + stdlib `sqlite3`, not the `sqlite3` CLI (which Ubuntu does not ship by default).

**File storage:**
- Local filesystem only. Bulk user data moves host-to-host with `rsync` over SSH (`src/pcswitcher/jobs/folder_sync.py`, `docs/adr/adr-013-rsync-over-ssh-user-data-transport.md`), filtered by `src/pcswitcher/home.filter` and `src/pcswitcher/root.filter`.
- btrfs subvolume snapshots taken pre/post sync on each host under `/mnt/btrfs-snapshots/pc-switcher` (`src/pcswitcher/btrfs_snapshots.py`, `src/pcswitcher/jobs/btrfs.py`)

**State files:**
- `~/.local/share/pc-switcher/sync-history.json` (`src/pcswitcher/sync_history.py`)
- `~/.local/share/pc-switcher/logs/` (`src/pcswitcher/logger.py`)
- Lock file in the same directory (`src/pcswitcher/lock.py`, `fcntl`-based)

**Caching:**
- None.

## Authentication & Identity

**Remote access:**
- Delegated entirely to the user's SSH setup. No password, key material, or token is stored by the tool.
- `src/pcswitcher/jobs/folder_sync.py:543` deliberately protects `~/.ssh` (notably `authorized_keys`) from being overwritten by a sync, which would otherwise lock the tool out of its own target.

**Local privilege escalation:**
- `sudo` via a managed drop-in at `/etc/sudoers.d/pc-switcher` (`src/pcswitcher/sudoers.py`), scoped to the package-manager and `/etc/apt` operations the jobs need. The distro's `/etc/sudoers` is never edited.

**GitHub:**
- Optional `GITHUB_TOKEN` for API rate limits only. No OAuth flow, no user identity.

## Monitoring & Observability

**Error tracking:**
- None (no Sentry/equivalent).

**Logs:**
- Python `logging` with rotating file handlers into `~/.local/share/pc-switcher/logs/`, surfaced by `pc-switcher logs` (`src/pcswitcher/logger.py`, `docs/adr/adr-010-logging-infrastructure.md`)
- Live terminal output through `rich` (`src/pcswitcher/ui.py`); untrusted log content must be wrapped in `rich.text.Text` to avoid markup parsing errors

**Disk monitoring:**
- `src/pcswitcher/jobs/disk_space_monitor.py` / `src/pcswitcher/disk.py` watch free space on both hosts during sync.

## CI/CD & Deployment

**Hosting:**
- None. Distributed as a CLI installed per-machine with `uv tool install` from a GitHub release.

**CI pipeline (GitHub Actions, `.github/workflows/`):**
- `ci.yml` - lint (`basedpyright`, `ruff check`, `ruff format --check`, `codespell`) and unit tests on every branch push; gated by `dorny/paths-filter` so doc-only pushes skip
- `integration-tests.yml` - runs only on non-draft PRs targeting `main`; waits for `ci.yml` then drives the Hetzner VMs. Stacked PRs based on a non-`main` branch skip integration entirely.
- `vm-updates.yml` - maintains the test VM baseline
- `pr-requires-issue-closing.yml` - policy check that a PR references a closing issue
- `claude.yml` - AI assistant workflow

**Release:**
- Annotated git tags only; the `uv-dynamic-versioning` backend derives the version from them, so lightweight tags produce a wrong dev-suffixed version (`docs/adr/adr-004-dynamic-versioning-github-releases.md`).

## Environment Configuration

**Required env vars:**
- None for normal operation.

**Optional:**
- `GITHUB_TOKEN` - GitHub API rate limit
- `PCSWITCHER_SKIP_VERSION_CHECK` - bypass the version-compatibility gate
- `VERSION` - pin the release installed by `install.sh`
- `HCLOUD_TOKEN`, `PC_SWITCHER_TEST_PC1_HOST`, `PC_SWITCHER_TEST_PC2_HOST`, `PC_SWITCHER_TEST_USER` - integration tests only

**Secrets location:**
- No secret files in the repo. SSH keys come from the user's agent/`~/.ssh`; CI tokens live in GitHub Actions secrets.

## Webhooks & Callbacks

**Incoming:**
- None. There is no server component.

**Outgoing:**
- None.
