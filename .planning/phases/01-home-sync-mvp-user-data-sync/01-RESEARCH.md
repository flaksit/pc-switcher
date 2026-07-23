# Phase 1: Home-Sync MVP (User Data Sync) - Research

**Researched:** 2026-06-30

**Domain:** rsync-over-SSH, btrfs divergence detection, asyncio subprocess streaming, Python job integration

**Confidence:** MEDIUM (rsync man page verified via fetch; btrfs API via official docs; codebase verified by direct read)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Job shape & scope**
- D-01: Build a generic `folder_sync` job (`FolderSyncJob`), not a home-specific job. Update `default-config.yaml` (currently comments `user_data: true`) to the `folder_sync` name.
- D-02: Default folders: `/home` and `/root`. Mechanism must work for any configured path.
- D-03: `/root` is in Phase 1 scope; `/etc` and other system config remain Phase 3.

**Transport & privileges**
- D-04: Transport is rsync-over-SSH.
- D-05: rsync runs as root on both ends — via local `sudo rsync` on source, and `--rsync-path="sudo rsync"` on target over the normal-user SSH connection. Do NOT enable root SSH login.
- D-13: rsync flag baseline `-aAXHS` (archive, ACLs, xattrs, hard links, sparse).

**Mirror & deletion safety**
- D-06 (linchpin): Mirror with `--delete`; no arbitrary mass-delete cap. Safety comes from (a) btrfs pre/post snapshots and (b) divergence detection. If target unchanged → mirror+delete runs. If target diverged → warn and require explicit confirmation.
- D-07: Guard is about *target divergence since last sync*, not about "last role." A→B, work on A, A→B again is NOT a conflict.
- D-08: Leading candidate for divergence detection: compare target's current subvolume generation against the last post-sync btrfs snapshot generation. Researcher decides exact mechanism.
- D-09: Per-file conflict detection is NOT required — deferred. Divergence guard catches the only dangerous case.

**Consistency preconditions**
- D-17: Single active machine assumed (not enforced in Phase 1).
- D-18: Target not independently modified — IS checked and enforced via D-06.
- D-19: Source quiescence during capture — assumed, not enforced in Phase 1.

**Include/exclude configuration**
- D-10: Config is a list of folder entries, each with its own include/exclude rules, mapped to rsync filter rules.
- D-11: Exclusions are never hardcoded — configurable with sensible defaults. Defaults exclude: `.ssh/id_*`, `.config/tailscale`, GPU/shader caches, fontconfig cache, VS Code cache dirs. Explicitly synced: dev-tool caches (uv, pip, cargo, npm) and VS Code `~/.config/Code/User/` state.

**Dry-run, integrity, observability**
- D-12: Tool-wide `--dry-run` contract (all SyncJobs): full read-only preview — connect, lock, validate, divergence detection, and every job reports what it WOULD do. No snapshots, no file changes, no history update.
- D-14: Integrity relies on rsync's built-in transfer verification. No custom `--checksum` feature.
- D-15: TUI shows overall progress + current file (rsync `--info=progress2` style).
- D-16: Per-file transfers/deletions at FULL (level 15); per-folder summaries at INFO (level 20).

### Claude's Discretion

- Exact rsync invocation details beyond `-aAXHS` (e.g. `--info` flags, `--numeric-ids`, partial-transfer handling).
- Internal structure of the folder-entry config schema and how it maps to rsync filter syntax.

### Deferred Ideas (OUT OF SCOPE)

- Automated precondition checking/enforcement for session-quiescence (D-17, D-19).
- Per-file conflict detection (richer reporting) — deferred Phase 2.
- Reflink/shared-extent (CoW) preservation.
- `/etc`, systemd, users/groups, GNOME dconf — Phase 3.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
| -- | ----------- | ---------------- |
| REQ-sync-scope-user-data | Sync `/home` and `/root` via generic per-folder include/exclude mechanism | rsync -aAXHS with configurable folder entries; default-config.yaml update |
| REQ-machine-specific-exclusions | Never sync `.ssh/id_*`, `.config/tailscale`, GPU caches, fontconfig, VS Code cache dirs | rsync filter rules with first-match-wins ordering; default exclude list in config |
| REQ-sync-scope-file-metadata | Preserve owner, group, permissions, POSIX ACLs, timestamps | `-aAXH` flags; `--numeric-ids` for cross-machine uid/gid; `acl` package on both ends |
| REQ-manual-sync-workflow | Single command triggers full sync; user waits for completion | `pc-switcher sync <target>` invokes orchestrator which runs FolderSyncJob |
| REQ-terminal-ux | Terminal UI with progress; simple experience for sync and errors | Rich progress bar via existing TerminalUI; rsync --out-format for per-file FULL logs |
</phase_requirements>

## Summary

Phase 1 delivers the first real sync job: `FolderSyncJob`, a generic folder-sync mechanism that copies configured folders (`/home` and `/root` by default) from the source machine to a target over rsync-over-SSH, running as root on both ends to preserve cross-owner metadata. The job implements full metadata preservation (-aAXHS + --numeric-ids), mirror semantics with --delete, and a target-divergence guard that stops the sync if the target has been independently modified since the last sync.

The three hardest technical problems are: (1) wiring rsync-as-root over the existing asyncssh connection without enabling root SSH login; (2) implementing the divergence guard using btrfs subvolume generation numbers, extending `sync_history.py` to store per-target per-subvolume post-sync generation markers; and (3) streaming rsync's progress output (which uses carriage returns, not newlines) into the existing asyncio pipeline and Rich TUI.

Everything builds on proven existing infrastructure: BtrfsSnapshotJob already creates pre/post snapshots, LocalExecutor already drives async subprocesses, the six-level logger and TerminalUI are in place, and the JobContext already carries `dry_run`. The new job is a SyncJob extension with a `validate()`/`execute()` pair and its own CONFIG_SCHEMA.

**Primary recommendation:** Implement FolderSyncJob as a thin orchestration layer over `sudo rsync` subprocess; use btrfs subvolume generation comparison for divergence detection; use `--out-format` for per-file logging and `xfr#N` parsing from --info=progress2 for TUI progress.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
| ---------- | ----------- | -------------- | --------- |
| Folder sync (rsync invocation) | Source machine (local subprocess) | Target machine (rsync server via sudo) | rsync forks its own SSH subprocess; source orchestrates the transfer |
| SSH connection for orchestration | Source machine (asyncssh) | Target machine (stateless SSH commands) | ADR-002: orchestration over asyncssh; rsync uses its own SSH transport |
| Divergence detection | Target machine (btrfs query) | Source machine (generation storage) | Generation numbers live on the target; marker stored on source |
| Divergence marker persistence | Source machine (`sync_history.py`) | — | Extended JSON file per existing pattern |
| Filter rule computation | Source machine (FolderSyncJob) | — | Rules assembled from config before rsync invocation |
| POSIX ACL + xattr preservation | rsync transport | Both machines (acl package) | rsync natively handles -A/-X; acl package required on both ends |
| Progress streaming | Source machine (asyncio subprocess) | FolderSyncJob parser | stdout stream from local rsync subprocess |
| TUI progress update | FolderSyncJob → EventBus → TerminalUI | — | Existing _report_progress() pattern |
| Pre/post snapshots | BtrfsSnapshotJob (existing) | — | Existing SystemJob, runs before/after FolderSyncJob |
| Dry-run enforcement | FolderSyncJob (JobContext.dry_run) | rsync --dry-run | JobContext.dry_run already established; job passes it to rsync |

## Standard Stack

### Core

| Library/Tool | Version | Purpose | Why Standard |
| ------- | ------- | ------- | ------------ |
| rsync | 3.2.7 (on Ubuntu 24.04) [VERIFIED: local] | File sync engine | Industry standard; handles metadata, ACLs, xattrs, hard links, filter rules, --delete, and transfer verification natively |
| Python asyncio | stdlib (Python 3.14) [VERIFIED: codebase] | Async subprocess for rsync | ADR-005 mandates asyncio; LocalExecutor already uses create_subprocess_shell |
| acl (Linux package) | pre-installed on Ubuntu 24.04 | Required for POSIX ACL support with rsync -A | Without this package, -A is silently degraded on some filesystems |
| btrfs-progs | pre-installed on Ubuntu 24.04 | `btrfs subvolume show` for generation querying | Already required by BtrfsSnapshotJob infrastructure |

### Supporting

| Library/Tool | Version | Purpose | When to Use |
| ------- | ------- | ------- | ----------- |
| sudoers (system config) | N/A | Allow sync user to run rsync without password | Required on both source and target; entry: `username ALL=(ALL) NOPASSWD: /usr/bin/rsync` |
| jsonschema | already in pyproject.toml [VERIFIED: codebase] | Validate folder_sync CONFIG_SCHEMA | Used by base Job.validate_config() |
| PyYAML | already in pyproject.toml [VERIFIED: codebase] | Load default-config.yaml and config-schema.yaml | Used by existing Configuration loader |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
| ---------- | --------- | -------- |
| rsync subprocess | asyncssh native SFTP | rsync has filter rules, --delete, metadata preservation, transfer verification, hard-link detection built in; SFTP would require hand-rolling all of this |
| btrfs generation comparison | `find -newer` or file hash | btrfs generation is O(1) and filesystem-level; `find -newer` is O(N files) |
| `--out-format` for per-file logging | `--itemize-changes` | `--out-format` gives structured output; `--itemize-changes` is similar but less configurable |

**Installation:** No new Python packages. System packages `rsync` and `acl` must be present on both machines (validated in `FolderSyncJob.validate()`).

## Package Legitimacy Audit

No new Python packages are introduced in this phase. rsync and acl are standard Ubuntu 24.04 system packages.

| Package | Source | Verdict | Disposition |
| ------- | ------ | ------- | ----------- |
| rsync | Ubuntu 24.04 apt | OK | System package, already installed |
| acl | Ubuntu 24.04 apt | OK | System package, validate presence in job |

**Packages removed due to SLOP verdict:** none

**Packages flagged as suspicious SUS:** none

## Architecture Patterns

### System Architecture Diagram

```
pc-switcher sync <target>
        │
        ▼
[Orchestrator]
  Phase 1-4: lock, SSH, validate, pre-snapshot (existing)
        │
        ▼
[FolderSyncJob.validate()]
  ├── check sudo rsync available locally
  ├── check sudo rsync available on target (via RemoteExecutor)
  ├── check acl package installed (both ends)
  ├── check btrfs subvolumes exist for configured folders
  └── check divergence: query target subvolume generation
             │
             └── if current_gen > stored_gen: WARN → prompt confirm
                                              (dry_run: log only)
        │
        ▼
[FolderSyncJob.execute()]
  for each enabled folder:
    ├── build rsync command (flags + filter rules)
    ├── LocalExecutor.start_process("sudo rsync ... -e ssh ...")
    ├── stream stdout chunks:
    │     ├── \r-terminated → parse progress2 → _report_progress()
    │     └── \n-terminated → parse --out-format → _log(FULL)
    ├── log folder summary at INFO (files, bytes, deletions)
    └── raise on non-zero exit
        │
        ▼
[Update divergence marker on both machines]
  ├── query source subvolume generation (local btrfs subvolume show)
  ├── query target subvolume generation (via RemoteExecutor)
  └── write to sync_history.json (extended format)
        │
        ▼
  Phase post-snapshot, unlock (existing)
```

### Recommended Project Structure

```
src/pcswitcher/
├── jobs/
│   └── folder_sync.py          # new: FolderSyncJob
├── sync_history.py             # extend: add per-target divergence markers
├── schemas/
│   └── config-schema.yaml      # extend: add folder_sync section
└── default-config.yaml         # extend: add folder_sync section with defaults

tests/
├── unit/
│   └── jobs/
│       └── test_folder_sync.py # new: unit tests for FolderSyncJob
├── contract/
│   └── test_job_interface.py   # extend: add FolderSyncJob to contract suite
└── integration/
    └── test_folder_sync.py     # new: A→B, mutate, B→A round-trip integration test
```

### Pattern 1: rsync Invocation (Root on Both Ends)

**What:** Spawn rsync as a local root subprocess; rsync SSH's to target using normal-user credentials; target runs rsync as root via sudo.

**When to use:** Any folder sync invocation.

**Key insight:** The asyncssh connection is used for orchestration (validation, btrfs commands, history updates). rsync spawns its own independent SSH transport connection to the target. This is correct: ADR-002 says "File syncing protocols (rsync, rclone, custom) are invoked through this SSH communication channel" — meaning the orchestrator triggers rsync over the channel, not that rsync reuses the asyncssh TCP socket.

```python
# Source: codebase analysis [VERIFIED: codebase] + rsync man page [VERIFIED: webfetch]
# Build rsync command for a single folder transfer

def _build_rsync_cmd(
    self,
    src_path: str,
    target_host: str,
    dst_path: str,
    filter_rules: list[str],
    dry_run: bool,
) -> str:
    # Flags: archive + ACLs + xattrs + hard links + sparse + numeric IDs + mirror
    flags = "-aAXHS --numeric-ids --delete"
    if dry_run:
        flags += " --dry-run"
    # Progress and per-file logging
    flags += " --info=progress2 --out-format='%i %n%L'"
    # Partial transfers (resume interrupted)
    flags += " --partial"
    # Create missing destination path components
    flags += " --mkpath"

    # SSH transport (no TTY needed; -T avoids pseudo-tty allocation overhead)
    ssh_opts = "-T -q"
    e_arg = f"ssh {ssh_opts}"

    # Assemble filter rules
    filter_args = " ".join(f"--filter='- {rule}'" for rule in filter_rules)

    # src_path must end with / for directory contents (not the directory itself)
    src = f"{src_path.rstrip('/')}/"
    dst = f"{target_host}:{dst_path.rstrip('/')}/"

    return (
        f"sudo rsync {flags} {filter_args} "
        f"-e '{e_arg}' --rsync-path='sudo rsync' {src} {dst}"
    )
```

**Sudoers configuration on BOTH machines (source and target):**
```
# /etc/sudoers.d/pcswitcher-rsync
username ALL=(ALL) NOPASSWD: /usr/bin/rsync
```

### Pattern 2: rsync Progress Streaming (asyncio)

**What:** Stream rsync output from an asyncio subprocess, handling `\r`-terminated progress2 updates and `\n`-terminated per-file output simultaneously.

**When to use:** During FolderSyncJob.execute() for each folder transfer.

**Key insight:** `--info=progress2` uses carriage returns (`\r`) for in-place line updates. The existing `LocalProcess.stdout()` iterator is newline-based and will hang waiting for `\n`. Need chunk-based reading that splits on both `\r` and `\n`. [VERIFIED: webfetch man7.org + websearch]

```python
# Source: asyncio docs [ASSUMED] + codebase analysis [VERIFIED: codebase]
import re
import asyncio

# Progress2 pattern: "9.53G 21% 317.26MB/s 0:00:28 (xfr#83063, to-chk=443926/538653)"
_PROGRESS2_RE = re.compile(
    r"(\d+[\d.]*[KMGT]?)\s+(\d+)%\s+(\S+)\s+(\S+)\s+\(xfr#(\d+),\s*to-chk=(\d+)/(\d+)\)"
)
# Out-format pattern for per-file: "%i %n%L" -> "<flags> <filename>"
# Example: ">f..t...... path/to/file.txt"

async def _stream_rsync(
    self,
    process: asyncio.subprocess.Process,
    folder_path: str,
) -> tuple[int, int, int]:
    """Stream rsync output, updating TUI and logging per-file events.

    Returns:
        Tuple of (files_transferred, bytes_transferred, files_deleted)
    """
    assert process.stdout is not None
    files_xfr = 0
    bytes_xfr = 0
    files_deleted = 0
    buf = ""

    while True:
        chunk = await process.stdout.read(4096)
        if not chunk:
            break
        buf += chunk.decode(errors="replace")
        # Split on both \r and \n
        parts = re.split(r"[\r\n]", buf)
        buf = parts[-1]  # Keep incomplete last fragment
        for part in parts[:-1]:
            part = part.strip()
            if not part:
                continue
            m = _PROGRESS2_RE.search(part)
            if m:
                pct = int(m.group(2))
                files_xfr = int(m.group(5))
                total_check = int(m.group(7))
                self._report_progress(ProgressUpdate(
                    percent=pct,
                    current=files_xfr,
                    total=total_check,
                ))
            elif part.startswith(">") or part.startswith("<") or part.startswith("*"):
                # Per-file --out-format line
                if part.startswith("*deleting"):
                    files_deleted += 1
                self._log(Host.SOURCE, LogLevel.FULL, f"{folder_path}: {part}")

    return files_xfr, bytes_xfr, files_deleted
```

### Pattern 3: Target Divergence Detection

**What:** Before each sync, verify that the target's synced subvolumes have not been modified since the last sync from this source machine.

**When to use:** At the start of `FolderSyncJob.validate()`, before snapshots and rsync.

**Mechanism:** [VERIFIED: btrfs.readthedocs.io via webfetch]
1. Post-sync: record the target's btrfs subvolume generation per synced path.
2. Pre-next-sync: query current generation; compare to stored value.
3. If current > stored: target was written after last sync → divergence.

```python
# Source: btrfs official docs [VERIFIED: webfetch]

async def _get_subvolume_generation(
    self,
    executor: Executor,
    mount_point: str,
) -> int:
    """Get current btrfs subvolume generation for a mount point.

    Args:
        executor: LocalExecutor or RemoteExecutor
        mount_point: e.g. "/home"

    Returns:
        Generation number (increases with every write transaction)
    """
    result = await executor.run_command(
        f"sudo btrfs subvolume show {mount_point}"
    )
    if result.exit_code != 0:
        raise RuntimeError(f"Cannot read subvolume generation for {mount_point}: {result.stderr}")
    # Parse "Generation:          12345" from output
    for line in result.stdout.splitlines():
        if "Generation:" in line:
            return int(line.split()[-1])
    raise RuntimeError(f"Generation not found in: {result.stdout[:200]}")

async def _check_target_divergence(self, folder: FolderEntry) -> bool:
    """Returns True if target has diverged since last sync.

    Uses btrfs subvolume generation comparison. If no stored marker
    exists (first sync to this target), returns False (no divergence).
    """
    stored_gen = self._divergence_store.get_generation(
        target=self.context.target_hostname,
        path=folder.path,
    )
    if stored_gen is None:
        return False  # First sync — no baseline to compare

    current_gen = await _get_subvolume_generation(self.target, folder.path)
    return current_gen > stored_gen

async def _record_divergence_markers(self) -> None:
    """Record post-sync target generation numbers for all synced folders."""
    for folder in self._active_folders:
        gen = await _get_subvolume_generation(self.target, folder.path)
        self._divergence_store.set_generation(
            target=self.context.target_hostname,
            path=folder.path,
            generation=gen,
        )
    self._divergence_store.save()
```

**Divergence marker storage extension to `sync_history.py`:**

The existing file stores `{"last_role": "source"}`. Extend to:
```json
{
  "last_role": "source",
  "target_generations": {
    "laptop-b": {
      "/home": 54321,
      "/root": 1234
    }
  }
}
```
This is additive and backward-compatible: old clients that don't know about `target_generations` continue to work.

### Pattern 4: Config Schema Shape

**What:** `folder_sync:` top-level YAML key with a list of folder entries.

**When to use:** In `default-config.yaml` and `config-schema.yaml`.

```yaml
# In default-config.yaml
folder_sync:
  folders:
    - path: /home
      enabled: true
      excludes:
        # Machine-specific SSH keys — never synced
        - .ssh/id_*
        # Tailscale machine-specific config
        - .config/tailscale
        # GPU shader and fontconfig caches (regenerable per GPU/hardware)
        - .cache/nvidia
        - .cache/mesa_shader_cache
        - .nv
        - .cache/fontconfig
        # VS Code regenerable caches (NOT the Code/User state — that IS synced)
        - .config/Code/Cache
        - .config/Code/CachedData
        - .config/Code/GPUCache
        - .config/Code/Code Cache
        - .config/Code/CachedExtensionVSIXs
    - path: /root
      enabled: true
      excludes:
        - .ssh/id_*
        - .config/tailscale
```

The `excludes` list maps to `--filter='- PATTERN'` arguments in the rsync invocation (one `--filter` arg per exclude, in order). The planner decides whether to also support global-level excludes that apply to all folders.

### Anti-Patterns to Avoid

- **`--delete-excluded` with machine-specific items:** Machine-specific files left on the target (SSH keys, Tailscale config) must NOT be deleted by syncing from another machine. Default `--delete` without `--delete-excluded` protects them. [VERIFIED: webfetch rsync man page]
- **Root SSH login:** ADR-002 forbids it. Use `--rsync-path='sudo rsync'` over the normal-user SSH connection instead.
- **Blocking rsync call:** Must use `asyncio.create_subprocess_shell` (or `start_process`), never `subprocess.run()`. ADR-005 bans blocking calls in the event loop.
- **Line-based stdout reader for --info=progress2:** Progress2 uses `\r`, not `\n`. The existing `LocalProcess.stdout()` iterator will block until `\n` arrives. Use chunk-based reading instead.
- **Hardcoded exclusions in Python code:** All exclusions must be configurable (D-11). Default list lives in `default-config.yaml`, not in `folder_sync.py`.
- **UID/GID name mapping across machines:** Without `--numeric-ids`, rsync maps ownership by *name* — it rewrites each file's UID/GID to whatever the target's account table assigns that name. For exact machine-state replication that defeats the goal: ownership becomes target-table-dependent and non-deterministic instead of matching the source's numeric layout. Always use `--numeric-ids`. [VERIFIED: webfetch rsync man page]

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
| ------- | ----------- | ----------- | --- |
| File transfer with metadata | Custom SFTP loop | rsync | Transfer verification, hard-link detection, sparse file handling, ACLs, xattrs, filter rules, --delete — all built in |
| Checksum verification | Custom hash walk after sync | Trust rsync's built-in (D-14) | rsync checksums every block during transfer; a post-hoc walk would double the I/O |
| Filter rule engine | Custom pattern matching | rsync --filter/--exclude/--include | First-match-wins rule engine with directory matching already implemented |
| SSH tunnel management | Custom asyncssh proxying for rsync | rsync's -e option with system ssh | rsync already knows how to use SSH; passing -e 'ssh -T -q' is sufficient |
| Progress parsing | Custom rsync protocol parser | Read rsync's --info=progress2 and --out-format stdout | rsync reports progress in a documented, stable format |
| Snapshot generation query | Custom inode-scan divergence check | `btrfs subvolume show` → parse Generation: field | One command, O(1), no filesystem scan needed |
| Divergence data model | New database or file format | Extend existing `sync_history.json` | Keeps state in one place; existing atomic-write pattern reusable |

**Key insight:** rsync handles the entire data-plane complexity. The job's responsibility is: (1) configuration → filter rules mapping, (2) divergence guard, (3) subprocess lifecycle, (4) progress parsing → TUI/log bridge, (5) history update. Everything else is rsync's domain.

## Common Pitfalls

### Pitfall 1: rsync `-e` Option Breaks with Sudo

**What goes wrong:** `sudo rsync ... -e 'ssh ...'` fails because sudo may not preserve the user's SSH agent or `~/.ssh/config`. The SSH subprocess spawned by root rsync can't find the right key or config.

**Why it happens:** rsync running under sudo runs as root; root's SSH agent is different from the user's. If SSH keys or `~/.ssh/config` are only for the normal user, the sudo-elevated rsync can't find them.

**How to avoid:** Explicitly pass SSH identity file and known_hosts to the `-e` option. Or use `sudo -E rsync ...` to inherit environment (including `SSH_AUTH_SOCK`). The validate() step should test `sudo rsync --version` and a dry-run `-e` connection to catch this before execution.

**Warning signs:** rsync exits with "Host key verification failed" or "Permission denied (publickey)" when run as root.

### Pitfall 2: `--info=progress2` Blocks the Line Reader

**What goes wrong:** Using `async for line in proc.stdout` blocks until rsync flushes a `\n`. Progress2 output uses `\r` and only prints `\n` when rsync finishes or when a file transfer completes. The TUI appears frozen during large file transfers.

**Why it happens:** Python's asyncio StreamReader `readline()` blocks until `\n`. Progress2's in-place update uses `\r` without ever writing `\n` mid-transfer.

**How to avoid:** Use `proc.stdout.read(N)` in a loop and split on both `\r` and `\n`. Alternatively, use `--out-format` only for per-file lines (which use `\n`) and parse `xfr#N` for count-based progress — avoiding progress2 entirely.

**Warning signs:** TUI progress bar stuck at 0% for minutes despite transfer happening.

### Pitfall 3: `--numeric-ids` Missing → Ownership Remapped by Name

**What goes wrong:** rsync maps UID 1000 on source to the username "alice", then looks up "alice" on the target and chowns the file to the target's UID for that name. If the target's "alice" is UID 1001, the file lands on 1001 — a different number than the source. Where a source name has no match on the target, rsync silently falls back to the raw source number, so a single tree ends up with a mix of name-mapped and number-preserved ownership.

**Why it happens:** Default rsync behavior maps ownership by name, not number. Name-mapping is designed to preserve *logical* per-user ownership across differing account tables — the opposite of what exact machine-state replication wants, which is the source's numeric layout reproduced verbatim.

**How to avoid:** Always pass `--numeric-ids`. This preserves the raw UID/GID numbers from the source and makes ownership a pure function of the source rather than of the target's account table.

**Warning signs:** Files on target have a different numeric owner than the source, varying by target, even though the sync reported success.

### Pitfall 4: Divergence Marker Not Updated on Target Sync Role

**What goes wrong:** The divergence marker is stored on the source per-target. But when B syncs to A (B→A), the divergence marker for A's copy needs to be updated on B (the new source). If the marker update is asymmetric, future A→B syncs falsely flag divergence.

**Why it happens:** Marker update logic only runs from the source's perspective. After a B→A sync, B is the new source and A is the target; the marker on B for A needs to be current.

**How to avoid:** Always update the divergence marker at the end of a successful sync, using the current sync's source/target roles. The marker records target_generations[target_host][path] on the source machine. This is always correct regardless of direction.

**Warning signs:** After every successful bidirectional sync, the next sync in either direction always warns "target may have diverged."

### Pitfall 5: acl Package Missing Silently Degrades -A

**What goes wrong:** rsync with `-A` on a system without the `acl` package doesn't fail — it silently ignores ACLs. ACLs are not preserved and the success criteria fail.

**Why it happens:** rsync checks for ACL support at runtime; if the support library is absent, ACL operations are no-ops.

**How to avoid:** In `FolderSyncJob.validate()`, verify the `acl` package is installed on both source and target using `dpkg -l acl | grep -q '^ii'`.

**Warning signs:** Files with custom ACLs on source have only basic permissions on target; integration test ACL checks fail.

### Pitfall 6: `config-schema.yaml` Uses `additionalProperties: false`

**What goes wrong:** Adding `folder_sync:` as a new top-level key to `config.yaml` causes schema validation to fail with "Additional properties are not allowed."

**Why it happens:** The existing `config-schema.yaml` has `additionalProperties: false` at the top level (line 200).

**How to avoid:** When adding the `folder_sync:` section to `config-schema.yaml`, also add `folder_sync:` to the `properties:` section at the top level. Additionally, update `sync_jobs` properties to include `folder_sync: true/false`.

**Warning signs:** Any `config.yaml` with a `folder_sync:` key fails validation with "Additional properties" error.

## Code Examples

### Folder Entry Dataclass

```python
# Source: codebase analysis [VERIFIED: codebase]
from dataclasses import dataclass, field

@dataclass
class FolderEntry:
    """A single folder to sync with its include/exclude configuration."""
    path: str                              # Absolute path, e.g. "/home"
    enabled: bool = True
    excludes: list[str] = field(default_factory=list)  # rsync filter patterns

    def to_rsync_filter_args(self) -> list[str]:
        """Convert excludes to rsync --filter arguments.

        Returns list of strings like ['--filter=- .ssh/id_*', ...]
        First-match-wins: order of excludes in config is preserved.
        """
        return [f"--filter=- {pattern}" for pattern in self.excludes]
```

### FolderSyncJob Skeleton

```python
# Source: codebase analysis [VERIFIED: codebase] (mirrors existing job pattern)
from __future__ import annotations
from typing import ClassVar, Any
from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.models import Host, LogLevel, ValidationError

class FolderSyncJob(SyncJob):
    """Generic folder sync via rsync-over-SSH.

    Syncs configured folders from source to target, running rsync as root
    on both ends to preserve cross-owner file metadata. Enforces target
    divergence check before mirror+delete to prevent data loss.
    """
    name: ClassVar[str] = "folder_sync"

    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "folders": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "enabled": {"type": "boolean", "default": True},
                        "excludes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "minItems": 1,
            }
        },
        "required": ["folders"],
        "additionalProperties": False,
    }

    async def validate(self) -> list[ValidationError]:
        errors: list[ValidationError] = []
        # 1. Check sudo rsync available locally
        # 2. Check sudo rsync available on target (via RemoteExecutor)
        # 3. Check acl package installed on both ends
        # 4. Check each folder path exists on source
        # 5. Check divergence for each enabled folder
        #    — if diverged and not dry_run: warn and prompt; abort if not confirmed
        #    — if dry_run: log warning only
        return errors

    async def execute(self) -> None:
        for folder in self._active_folders():
            await self._sync_folder(folder)
        if not self.context.dry_run:
            await self._record_divergence_markers()
```

### btrfs Generation Query

```bash
# Source: btrfs.readthedocs.io [VERIFIED: webfetch]
# Run on target via RemoteExecutor:
sudo btrfs subvolume show /home
# Relevant output line:
#   Generation:          54321

# Parse with Python:
# for line in result.stdout.splitlines():
#     if "Generation:" in line:
#         gen = int(line.split()[-1])
```

### rsync Dry-Run Preview Mode

```python
# Source: rsync man page [VERIFIED: webfetch], D-12 contract
# In FolderSyncJob.execute():
if self.context.dry_run:
    # Add --dry-run to rsync flags — shows what WOULD be transferred
    # Log each file that would be transferred/deleted at FULL level
    # Do NOT update divergence markers
    # Do NOT take btrfs snapshots (BtrfsSnapshotJob already handles this)
    pass
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
| ------------ | ---------------- | ------------ | ------ |
| rsync `-a` only | `-aAXHS` | When POSIX ACLs became relevant | Without -A and -X, extended permissions and custom namespace attributes are lost |
| `--progress` (per-file) | `--info=progress2` (overall) | rsync 3.1.0 | Overall progress is more useful for large trees; per-file still available via --out-format |
| UID/GID name mapping | `--numeric-ids` | Always recommended for cross-machine | Prevents ownership corruption when UID allocations differ between machines |
| `--delete` with no guards | `--delete` with divergence guard | Project design | Prevents mass deletion when target has independent changes |
| Post-sync manual verification | Trust rsync's built-in transfer verification | Always | rsync checksums every block transferred; no separate verify step needed |

**Deprecated/outdated:**
- `rsync -e ssh` without `-T`: wastes a pseudo-TTY allocation; use `-T -q`.
- `--checksum` flag for post-transfer verification: forces re-read of all files on both ends; unnecessary when trust rsync's built-in verification (D-14).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
| - | ----- | ------- | ------------- |
| A1 | `acl` package is pre-installed on Ubuntu 24.04 LTS systems, or at least available via apt | Standard Stack | If absent and not validated, -A silently fails; validate() must check explicitly |
| A2 | asyncssh does not create an OpenSSH ControlMaster socket that rsync could reuse; rsync spawns a separate SSH connection | Pattern 1 | If asyncssh does support ControlMaster socket export, rsync could reuse the existing connection — a minor optimization but not a correctness issue |
| A3 | The target's btrfs subvolume at the folder path (e.g. `/home`) has a `@home` subvolume that the `btrfs subvolume show` command can query | Divergence detection | If `/home` is not a btrfs subvolume (e.g. a plain directory on the root subvolume), generation tracking doesn't work; validate() must check explicitly |
| A4 | Sudoers entry for rsync (`NOPASSWD: /usr/bin/rsync`) is present on both source and target before first sync | Pattern 1 | rsync will fail at execution time with "sudo: a password is required"; validate() must test with a harmless sudo rsync --version check |
| A5 | rsync 3.2.x is available on Ubuntu 24.04 (verified locally: 3.2.7) | Standard Stack | Earlier versions may lack `--mkpath` flag (added in 3.2.3) |
| A6 | `--info=progress2` combined with `--out-format` produces both types of output on stdout; stderr carries errors only | Pattern 2 | If progress output goes to stderr, the streaming reader needs to merge stdout+stderr |

## Open Questions

All three are RESOLVED in the Phase 1 plans (closing plan refs noted per item); none blocks execution.

1. **ControlMaster socket for rsync to reuse the asyncssh connection** — RESOLVED
   - What we know: asyncssh is a pure-Python SSH implementation; it does NOT create an OpenSSH ControlMaster socket by default.
   - What's unclear: Whether asyncssh can be configured to expose a ControlMaster-compatible Unix socket that rsync's `-e ssh` option can use.
   - Recommendation: Treat rsync's SSH as an independent connection (simplest approach). If performance is a concern (second TCP handshake), this can be revisited using OpenSSH ControlMaster configured in `~/.ssh/config`.
   - Resolution: Plan 01-05 wires rsync over an independent system-`ssh` transport per ADR-002 (honoring `~/.ssh/config`/known_hosts; threat T-05-05), exactly the recommended simplest approach. ControlMaster reuse is explicitly deferred as a future optimization, not a Phase 1 requirement.

2. **Source-side sudo rsync: sudo prompts in non-interactive context** — RESOLVED
   - What we know: `sudo rsync` locally must be passwordless. If it's not, the asyncio subprocess will hang waiting for a password.
   - What's unclear: Whether the same sudoers entry (`NOPASSWD: /usr/bin/rsync`) is already present on developer machines or needs to be part of the installation guide.
   - Recommendation: Add to `validate()`: test `sudo rsync --version` locally and surface a clear error if it fails (not a runtime hang).
   - Resolution: Plan 01-04 Task 1 `validate()` runs `sudo rsync --version` on both source and target and returns a `ValidationError` (no runtime hang) when it fails; ADR-013 (plan 01-01) documents the scoped `NOPASSWD` sudoers entry as an install-guide prerequisite.

3. **Integration test btrfs subvolume for /root** — RESOLVED
   - What we know: The test VMs have `@home` as a separate subvolume. `/root` may not be a separate subvolume — it could be part of the root `@` subvolume.
   - What's unclear: Whether the divergence detection mechanism (subvolume generation) works for `/root` if `/root` is not its own subvolume. If `/root` is on `@`, any write anywhere on `@` would increment the generation.
   - Recommendation: In `validate()`, check whether the folder path is a btrfs subvolume root (not just any directory). If it's not a subvolume, generation tracking is not available for that path; warn the user and fall back to no divergence check (conservative: always allow, or always warn).
   - Resolution: Plan 01-04 Task 2 `_resolve_subvolume` checks whether the folder is a btrfs subvolume root; when it is not (e.g. `/root` on `@`), it logs a WARNING that divergence tracking is unavailable for that path and skips the check rather than crashing — the conservative documented fallback.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
| ---------- | ---------- | --------- | ------- | -------- |
| rsync | FolderSyncJob transport | ✓ | 3.2.7 [VERIFIED: local] | None — must be installed |
| sudo | Root escalation | ✓ | (system) | None — must have NOPASSWD entry |
| btrfs-progs (btrfs subvolume show) | Divergence detection | ✓ (assumed, same requirement as BtrfsSnapshotJob) | (system) | None — btrfs is project requirement |
| acl package | POSIX ACL preservation (-A) | [ASSUMED] pre-installed on Ubuntu 24.04 | — | Without: -A silently degrades; validate() must check |
| Python asyncio subprocess | rsync streaming | ✓ stdlib | Python 3.14 | N/A |
| Hetzner Cloud VMs (pc1/pc2) | Integration tests | ✓ (existing infra) | Ubuntu 24.04 | — |

**Missing dependencies with no fallback:** None at the project level. Target machines must have rsync and acl installed; validate() must check and fail fast with a clear error message.

## Validation Architecture

### Test Framework

| Property | Value |
| -------- | ----- |
| Framework | pytest + pytest-asyncio [VERIFIED: codebase] |
| Config file | pyproject.toml `[tool.pytest]` asyncio_mode = "auto" (pytest 9 canonical table) [VERIFIED: codebase] |
| Quick run command | `uv run pytest tests/unit/jobs/test_folder_sync.py tests/contract/ -x` |
| Full suite command | `uv run pytest tests/unit/ tests/contract/` |
| Integration tests | `tests/run-integration-tests.sh tests/integration/test_folder_sync.py` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
| ------ | -------- | --------- | ----------------- | ----------- |
| REQ-sync-scope-user-data | FolderSyncJob syncs /home contents to target; files byte-identical | Integration | `run-integration-tests.sh tests/integration/test_folder_sync.py::TestFolderSyncRoundTrip::test_a_to_b` | ❌ Wave 0 |
| REQ-sync-scope-user-data | FolderSyncJob default config includes /home and /root | Unit | `pytest tests/unit/jobs/test_folder_sync.py::test_default_folders` | ❌ Wave 0 |
| REQ-machine-specific-exclusions | .ssh/id_* never appears on target after sync | Integration | `run-integration-tests.sh tests/integration/test_folder_sync.py::TestExclusions::test_ssh_keys_excluded` | ❌ Wave 0 |
| REQ-machine-specific-exclusions | VS Code Cache excluded; User/ state included | Integration | `run-integration-tests.sh tests/integration/test_folder_sync.py::TestExclusions::test_vscode_cache_excluded` | ❌ Wave 0 |
| REQ-sync-scope-file-metadata | Owner/group/permissions/ACLs/timestamps match source after sync | Integration | `run-integration-tests.sh tests/integration/test_folder_sync.py::TestMetadata::test_metadata_preserved` | ❌ Wave 0 |
| REQ-manual-sync-workflow (D-06) | Divergence guard warns when target modified since last sync | Unit | `pytest tests/unit/jobs/test_folder_sync.py::TestDivergenceGuard::test_diverged_target_raises_warning` | ❌ Wave 0 |
| REQ-manual-sync-workflow (D-06) | Divergence guard allows sync when target unmodified | Unit | `pytest tests/unit/jobs/test_folder_sync.py::TestDivergenceGuard::test_unmodified_target_proceeds` | ❌ Wave 0 |
| REQ-manual-sync-workflow (D-07) | No false divergence when source synced twice without target changes | Unit | `pytest tests/unit/jobs/test_folder_sync.py::TestDivergenceGuard::test_consecutive_source_syncs_ok` | ❌ Wave 0 |
| REQ-terminal-ux (D-15) | Progress events emitted during rsync | Unit | `pytest tests/unit/jobs/test_folder_sync.py::TestProgress::test_progress_events_emitted` | ❌ Wave 0 |
| D-12 dry-run | rsync not executed in dry-run; what-would-happen is logged | Unit | `pytest tests/unit/jobs/test_folder_sync.py::TestDryRun::test_dry_run_no_files_changed` | ❌ Wave 0 |
| D-12 dry-run | Divergence check runs in dry-run; snapshot NOT taken | Unit | `pytest tests/unit/jobs/test_folder_sync.py::TestDryRun::test_dry_run_no_snapshot` | ❌ Wave 0 |
| Success criterion 4 | Full A→B→mutate→B→A round-trip; both directions byte-identical | Integration | `run-integration-tests.sh tests/integration/test_folder_sync.py::TestFolderSyncRoundTrip::test_bidirectional_round_trip` | ❌ Wave 0 |
| Success criterion 5 (checksums) | Round-trip integration asserts checksum match + metadata | Integration | (part of above) | ❌ Wave 0 |
| Job interface (contract) | FolderSyncJob satisfies SyncJob contract | Contract | `pytest tests/contract/test_job_interface.py` (extend existing) | ✅ extend |

### Sampling Rate

- Per task commit: `uv run pytest tests/unit/jobs/test_folder_sync.py tests/contract/ -x`
- Per wave merge: `uv run pytest tests/unit/ tests/contract/`
- Phase gate: Full unit + contract suite green, then integration round-trip test green, before `/gsd-verify-work`

### Wave 0 Gaps

- [ ] `tests/unit/jobs/test_folder_sync.py` — unit tests for FolderSyncJob (divergence, dry-run, progress, config schema, filter rules)
- [ ] `tests/integration/test_folder_sync.py` — A→B sync, metadata check, exclusions check, B→A round-trip, divergence guard integration
- [ ] `tests/unit/test_sync_history.py` — extend existing file with tests for new `target_generations` structure
- [ ] FolderSyncJob to be added to `tests/contract/test_job_interface.py` once implemented

### Integration Test: Success Criterion 5 (A→B / Mutate / B→A Round-Trip)

The integration test must automate the following against real Hetzner VMs:

1. **Setup:** Reset pc1 and pc2 to baseline. Create test files on pc1 with varying owners, permissions, ACLs, and hard links.
2. **A→B sync:** Run `pc-switcher sync pc2` from pc1. Assert: all test files exist on pc2, byte-identical (checksum), metadata matches (stat, getfacl). Assert: excluded files (`.ssh/id_*`) absent on pc2.
3. **Mutate on B:** Add, modify, and delete files on pc2's synced folders. Record changes.
4. **B→A sync:** Run `pc-switcher sync pc1` from pc2. Assert: pc1 reflects all mutations from step 3. Assert: same exclusion rules hold. Assert: metadata preserved.
5. **Divergence detection:** Modify pc2 independently without syncing. Then attempt A→B sync from pc1. Assert: divergence warning is emitted. Assert: sync blocked until user confirms.
6. **Dry-run:** Run `pc-switcher sync pc2 --dry-run` with divergence present. Assert: no files changed on pc2, divergence warning logged, rsync --dry-run output shows what would transfer.

Use `md5sum` for checksum comparison, `stat -c "%a %U %G"` for permissions/owner, `getfacl` for ACLs.

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
| ------------- | ------- | --------------- |
| V2 Authentication | no | SSH key auth is handled by asyncssh and OpenSSH config |
| V3 Session Management | no | Sync session uses existing lock mechanism |
| V4 Access Control | yes | rsync must only run with root privs; sudoers entry must be scoped to `/usr/bin/rsync` only |
| V5 Input Validation | yes | Folder paths and exclude patterns from config must not allow shell injection |
| V6 Cryptography | no | SSH encryption handled by OpenSSH/asyncssh |

### Known Threat Patterns for This Stack

| Pattern | STRIDE | Standard Mitigation |
| ------- | ------ | ------------------- |
| Shell injection via config (folder path or exclude pattern) | Tampering | Use `shlex.quote()` or explicit argument arrays (not shell interpolation) for all rsync arguments derived from config values |
| Overly broad sudoers entry | Elevation of privilege | Scope to `/usr/bin/rsync` only; do NOT use `ALL` or wildcards |
| Divergence guard bypass (user force-accepts) | Integrity | Log all explicit user confirmations at WARNING level; audit trail in JSON log file |
| rsync deleting machine-specific target files | Tampering/Data Loss | Never use `--delete-excluded`; verify excluded files are absent from rsync's delete candidates |
| SSH key disclosure via accidental sync | Information Disclosure | Validate exclusion rules in integration test: assert `.ssh/id_*` never appears on the target after sync |

## Sources

### Primary (HIGH confidence)

None — no Context7 docs available for rsync or btrfs (offline tools, not library SDKs).

### Secondary (MEDIUM confidence)

- rsync man page via man7.org [VERIFIED: webfetch man7.org/linux/man-pages/man1/rsync.1.html] — flag definitions (-a, -A, -X, -H, -S, --numeric-ids, --delete, --partial, --out-format, --filter)
- btrfs subvolume documentation via btrfs.readthedocs.io [VERIFIED: webfetch btrfs.readthedocs.io/en/latest/btrfs-subvolume.html] — find-new command, generation numbers, show command
- Dzombak blog 2025-06-30 [webfetch] — exact sudo rsync both-ends invocation pattern
- 0ink.net rsync filter rules 2023 [webfetch] — filter rule syntax, first-match-wins, protect modifier
- Codebase analysis [VERIFIED: direct read] — executor.py, jobs/base.py, btrfs.py, sync_history.py, ui.py, events.py, models.py, context.py, orchestrator.py, config.py, config-schema.yaml, default-config.yaml

### Tertiary (LOW confidence)

- WebSearch results on rsync progress2, filter rules, btrfs find-new, asyncio subprocess streaming — cross-checked with man page fetch

## Metadata

**Confidence breakdown:**
- Standard stack (rsync flags, btrfs commands): MEDIUM — verified via man page fetch and official docs
- Architecture (how job fits existing codebase): HIGH — direct codebase read
- Divergence detection mechanism: MEDIUM — official btrfs docs confirm generation numbers; exact integration with sync_history.py is architectural design
- rsync-as-root wiring: MEDIUM — pattern confirmed by dzombak blog (2025-06-30) and rsync man page; asyncssh ControlMaster limitation is inferred (A2 in assumptions log)
- Progress streaming: LOW — asyncio pattern from stdlib docs, rsync \r behavior from websearch

**Research date:** 2026-06-30

**Valid until:** 2026-09-30 (rsync 3.2.7 stable; btrfs subvolume show API stable)
