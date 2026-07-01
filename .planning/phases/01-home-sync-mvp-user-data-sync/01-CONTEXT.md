# Phase 1: Home-Sync MVP (User Data Sync) - Context

**Gathered:** 2026-06-30

**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 1 delivers the first real sync job: a single command replicates configured folders from the source machine to the target over rsync-over-SSH, with per-folder include/exclude rules and full file-metadata preservation, proven correct by a bidirectional round-trip integration test.

The job is **generic folder sync**, not home-specific. Default folders shipped enabled are `/home` and `/root`. The same mechanism syncs any configured path (e.g. a user-created `/important-data`).

**In scope:**

- Generic `folder_sync` job (`FolderSyncJob`) on the existing `SyncJob` base.
- Sync of `/home` and `/root` by default; arbitrary folders via config.
- rsync-over-SSH transport, running as root on both ends.
- Per-folder include/exclude rules (machine-specific exclusions).
- File-metadata preservation: owner, group, permissions, POSIX ACLs, mtimes, soft links, hard links.
- Mirror semantics with `--delete` (target becomes an exact replica).
- Target-divergence detection (the reliability linchpin — see D-06).
- Tool-wide `--dry-run` contract (see D-12).
- Specifying the consistency preconditions (D-17–D-19) and enforcing the target-divergence one; the session-quiescence preconditions are documented assumptions until a later phase.

**Out of scope (deferred):**

- `/etc`, systemd units, users/groups, GNOME dconf and other **system config** — stays Phase 3. `/root` (root's home directory) is the only root-owned path pulled forward into Phase 1.
- Per-file conflict detection (richer reporting) — not needed for safety given the preconditions + divergence guard; deferred (see D-09).
- Reflink / shared-extent (CoW) topology preservation — not achievable with rsync; only relevant to VM images / snapshotted datasets handled in later phases.
- Automated enforcement of session-quiescence preconditions (login-session / `lsof` open-file checks) — later phase. The preconditions themselves ARE specified in Phase 1 (D-17–D-19); the target-divergence precondition is enforced now (D-06).
- Packages, Docker, VMs, k3s, rollback — Phases 2–7.

**Roadmap impact:** moving `/root` into Phase 1 reverses the recent "defer `/root` to Phase 3" decision. ROADMAP.md and REQUIREMENTS.md must be updated so traceability stays honest (REQ-sync-scope-app-and-system-config keeps only `/etc` + system config in Phase 3; `/root`-as-folder-data moves to Phase 1 under REQ-sync-scope-user-data / the generic folder-sync mechanism).
</domain>

<decisions>
## Implementation Decisions

### Job shape & scope

- **D-01:** Build a generic `folder_sync` job (class `FolderSyncJob`), not a home-specific job. Update `default-config.yaml` (currently comments `user_data: true # Sync /home and /root`) to the `folder_sync` name.
- **D-02:** Default folders: `/home` and `/root`. The mechanism must work for any configured path.
- **D-03:** `/root` is in Phase 1 scope; `/etc` and other system config remain Phase 3.

### Transport & privileges

- **D-04:** Transport is rsync-over-SSH (already chosen over btrfs send/receive; ADR still to be formalized — see canonical refs).
- **D-05:** rsync runs **as root on both ends** — required to read/write files of all owners across `/home` and `/root` and to preserve owner/group. Obtain root via the existing normal-user SSH connection plus `--rsync-path="sudo rsync"` on the target, and local `sudo` on the source. Do **not** enable root SSH login (smaller attack surface). Assumes passwordless sudo for rsync; the researcher should confirm the exact mechanism and how it fits ADR-002's connection model.
- **D-13:** rsync flag baseline `-aAXHS` (archive, ACLs, xattrs, hard links, sparse). Preserves soft links, hard links, owner/group/perms/ACLs/xattrs/mtimes. Reflinks / shared-extent topology are NOT preserved (accepted tradeoff).

### Mirror & deletion safety

- **D-06 (linchpin):** Mirror with `--delete` so the target becomes an exact replica and removals propagate (satisfies success criterion 4). **No arbitrary mass-delete cap** — that is false security and case-dependent. Safety comes from (a) btrfs pre/post snapshots as backstop, and (b) robust detection that the **target has not diverged since the last sync between this machine pair**. If the target is unchanged → mirror+delete is provably safe and runs. If the target changed independently → warn and require explicit confirmation. This divergence detection+enforcement MUST be implemented well; it is the core reliability mechanism of Phase 1.
- **D-07:** The guard is about *target divergence since last sync*, NOT about "last role." Doing A→B, continuing work on A, then A→B again is **not** a conflict (A stays the source of truth; B was untouched). The conflict case is B being modified independently after it last participated in a sync.
- **D-08:** `sync_history.py` today records only last *role* (source/target) and is insufficient for D-06/D-07. A per-target sync-state marker is needed. Leading candidate: compare the target's current `@`/`@home` against the last post-sync btrfs snapshot (subvolume generation / `btrfs subvolume find-new`), reusing existing snapshot infrastructure. The researcher decides the exact mechanism.
- **D-09:** Per-file conflict detection is NOT required for safety. Given the consistency preconditions (D-17–D-19) plus the target-divergence guard (D-06/D-07), no conflict can arise unsupervised: the only dangerous case (the target diverged since last sync) is already caught at folder/subvolume granularity and stops the sync for manual resolution. Per-file detection would add only richer *reporting* of which files diverged — a nicety, not a correctness need — so it stays deferred (was tagged Phase 2 under REQ-conflict-detection-no-resolution). No auto-resolution in any case.

### Consistency preconditions

These preconditions make the one-directional mirror safe and the captured state consistent. They are **specified now**; Phase 1 *enforces* only the divergence precondition (D-18, via D-06). The session-quiescence preconditions (D-17, D-19) are **documented assumptions the user is responsible for** in Phase 1; a later phase adds automated checking + enforcement with a user override.

- **D-17 (single active machine):** Only one machine is active at a time; each sync is one-directional source→target and the user does not use the target concurrently. Project premise; assumed, not enforced in Phase 1.
- **D-18 (target not independently modified):** The target's synced folders must not have changed since their last sync with the source. This is the divergence precondition and IS checked + enforced in Phase 1 via D-06/D-07 (warn + require confirmation on divergence).
- **D-19 (quiescent source during capture):** For a consistent copy, the source's synced folders should not be written by other sessions/apps during the sync — ideally no user logged in except the shell running `pc-switcher` (or apps closed). Open files / live app state can yield an inconsistent snapshot. Assumed in Phase 1; a later phase adds automated detection (e.g. login-session / `lsof` checks) + enforcement with `--override`.

### Include/exclude configuration

- **D-10:** Config is a **list of folder entries, each with its own include/exclude rules**, mapped to rsync filter rules. Supports folder-specific exclusions and arbitrary paths.
- **D-11:** Exclusions are **never hardcoded** — they are configurable with sensible defaults shipped in the example config, all user-overridable. Default exclusions: `.ssh/id_*`, `.config/tailscale`, GPU/shader caches, fontconfig cache, and VS Code *cache* dirs (`Cache`, `CachedData`, `GPUCache`, `Code Cache`). Explicitly **synced** (not excluded): dev-tool caches (uv, pip, cargo, npm) and VS Code `~/.config/Code/User/` state (settings, `state.vscdb`, workspaceStorage, History) — per the #46 research, syncing `/home` captures the full VS Code state that Settings Sync misses, which is desired.

### Dry-run, integrity, observability

- **D-12:** **Tool-wide `--dry-run` contract** (applies to ALL SyncJobs, not just folder_sync): a full read-only preview — connect, lock, validate, run divergence detection, and every job reports exactly what it WOULD do (folder_sync uses `rsync --dry-run` to list transfers + deletions). No btrfs snapshots taken, no files changed, no sync-history update. Every SyncJob MUST implement a real preview. This is a cross-cutting policy worth documenting (possibly an ADR / base-Job contract).
- **D-14:** Integrity relies on rsync's built-in, always-on transfer verification (rsync checksum-verifies every file it transfers). **No custom verify / `--checksum` feature** — trust the tool. (The integration test independently checksums files per success criteria 1 and 5; that is the test's responsibility, not the tool's runtime behavior.)
- **D-15:** TUI shows overall progress + current file (rsync `--info=progress2` style), fitting the existing Rich progress UI.
- **D-16:** Logging — per-file transfers/deletions at FULL (level 15; in the file log by default, off the TUI), per-folder summaries (counts, bytes, deletions) at INFO (level 20). Matches the existing six-level logging model.

### Claude's Discretion

- Exact rsync invocation details beyond `-aAXHS` (e.g. `--info` flags, `--numeric-ids` for cross-machine owner fidelity, partial-transfer handling) — planner/researcher decide, consistent with the decisions above.
- Internal structure of the folder-entry config schema and how it maps to rsync filter syntax.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project scope & requirements

- `.planning/PROJECT.md` — project vision, locked decisions, reliability priority order.
- `.planning/REQUIREMENTS.md` — REQ-sync-scope-user-data, REQ-machine-specific-exclusions, REQ-sync-scope-file-metadata, REQ-manual-sync-workflow, REQ-terminal-ux (Phase 1 requirements). NOTE: needs update to move `/root` into Phase 1.
- `.planning/ROADMAP.md` §"Phase 1" — goal and the 5 success criteria. NOTE: needs update for `/root` scope move.
- `docs/planning/High level requirements.md` — complete project vision, acceptable-constraints clause.

### Architecture decisions (locked)

- `docs/adr/adr-002-ssh-communication-channel.md` — SSH channel: multiplexed ControlMaster, source orchestrates, target runs stateless scripts, hostname via `~/.ssh/config`. Constrains how rsync-as-root-over-SSH is wired.
- `docs/adr/adr-005-asyncio-concurrency.md` — all Job methods `async def`; no blocking calls in the event loop (rsync must be driven via async subprocess).
- `docs/adr/adr-010-logging-infrastructure.md` — six-level logging (FULL=15), file/TUI/external floors, JSON Lines + Rich.
- `docs/adr/_index.md` — ADR index / supersession tracking.
- **Pending ADR:** rsync-over-SSH as the user-data transport (chosen over btrfs send/receive) — to be formalized before/during Phase 1. The unified `--dry-run` contract (D-12) and root-via-sudo model (D-05) are also ADR-worthy.

### Existing code to build on

- `src/pcswitcher/jobs/base.py` — `Job` / `SyncJob` base; implement `validate()` + `execute()`, own `CONFIG_SCHEMA`.
- `src/pcswitcher/jobs/context.py` — `JobContext` (executors, config, event bus).
- `src/pcswitcher/jobs/btrfs.py` + `src/pcswitcher/btrfs_snapshots.py` — pre/post snapshot infra; basis for target-divergence detection (D-08).
- `src/pcswitcher/sync_history.py` — records last role; must be extended for per-target divergence marker (D-08).
- `src/pcswitcher/config.py` + `src/pcswitcher/default-config.yaml` — config parsing/schema; add the `folder_sync` section and default exclusions.
- `src/pcswitcher/config_sync.py` — existing source↔target file fetch/diff pattern.
- `src/pcswitcher/executor.py` — Local/Remote executor protocol used to drive rsync.
- `.planning/codebase/ARCHITECTURE.md`, `.planning/codebase/STRUCTURE.md` — orchestrator 10-phase flow and where to add a job.

### External research

- GitHub issue #46 (`gh issue view 46`) — VS Code sync research: syncing `/home` captures full `~/.config/Code/User/` state (settings, `state.vscdb`, workspaceStorage, History); only regenerable caches should be excluded.
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets

- `SyncJob` base (`jobs/base.py`): the folder_sync job slots straight in with `validate()`/`execute()` + `CONFIG_SCHEMA`; no new orchestration plumbing.
- btrfs snapshot infra (`jobs/btrfs.py`, `btrfs_snapshots.py`): pre/post snapshots already wrap every sync — reuse for both the rollback backstop and the target-divergence detection (compare current subvol vs last post-sync snap).
- Executor protocol (`executor.py`): `LocalExecutor`/`RemoteExecutor` already abstract local-vs-remote command execution — drive rsync through it (async, non-interactive, no stdin).
- Rich progress UI (`ui.py`) + EventBus (`events.py`): `_report_progress()` feeds the existing live progress display for the overall-progress + current-file TUI.
- Six-level logger (`logger.py`): FULL/INFO floors already configured in `default-config.yaml`.

### Established Patterns

- Jobs are discovered by name from config under `sync_jobs:`; module name matches job name (`jobs/folder_sync.py` → `FolderSyncJob`).
- Three-phase validation: config schema → job config schema → system-state checks (folder_sync should validate folder existence, sudo-rsync availability, btrfs layout in `validate()`).
- Jobs must respect dry-run and make no changes (extend to the D-12 contract).
- No blocking I/O in the event loop (ADR-005): rsync runs via async subprocess.

### Integration Points

- New module `src/pcswitcher/jobs/folder_sync.py`.
- New `folder_sync:` config section in `default-config.yaml` + schema in `config.py`.
- Divergence-marker logic extends `sync_history.py` and/or reads btrfs snapshots.
- Orchestrator already runs enabled jobs in Phase 9; no orchestrator change needed beyond the tool-wide dry-run contract (D-12) touching the base Job interface.
</code_context>

<specifics>
## Specific Ideas

- "rsync -aAXHS gets everything except shared-extent topology" — user's own prep note; the chosen flag baseline (D-13).
- VS Code: the goal is the *complete* environment replication that Settings Sync fails to provide (open files, layout, per-workspace extension state via `state.vscdb`) — achieved automatically by syncing `/home`, minus caches.
- Reusability is a stated goal: the same mechanism should sync an ad-hoc `/important-data` folder, not just home dirs.
</specifics>

<deferred>
## Deferred Ideas

- **Automated precondition checking/enforcement** — the consistency preconditions (D-17–D-19) are *specified* in Phase 1, but only the divergence one (D-18) is enforced now. Automated checking + enforcement of the session-quiescence preconditions (login-session / `lsof` open-file detection) with a user `--override` is deferred to a later phase.
- **Per-file conflict detection (richer reporting)** — not a safety requirement given preconditions + divergence guard (D-09); only adds finer reporting of which files diverged. Deferred (was Phase 2 under REQ-conflict-detection-no-resolution). No auto-resolution in any case.
- **Reflink / shared-extent (CoW) preservation** — only matters for VM images / snapshotted datasets inside synced folders; handled where those scopes live (VMs = Phase 5).
- **`/etc`, systemd, users/groups, GNOME dconf** — system config, Phase 3.

None of these expand Phase 1 scope; they are explicitly held for later phases.
</deferred>

---

*Phase: 1-Home-Sync MVP (User Data Sync)*

*Context gathered: 2026-06-30*
