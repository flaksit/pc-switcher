# Phase 1: Home-Sync MVP (User Data Sync) - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents. Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-30

**Phase:** 1-Home-Sync MVP (User Data Sync)

**Areas discussed:** Scope boundary, Deletion/mirror semantics, Target-divergence guard, Preview & dry-run, Root access model, Integrity verification, TUI granularity, Logging verbosity, Tool-wide dry-run policy, Include/exclude config model, Job name, Preconditions

## Scope boundary

| Option | Description | Selected |
|--------|-------------|----------|
| /home + /root, system config stays P3 | Folder-data only; /etc, systemd, users/groups, dconf stay Phase 3 | ✓ |
| /home + /root + /etc now | Pull /etc in too | |
| Let me clarify | User describes boundary | |

**User's choice:** /home + /root, system config stays Phase 3.

**Notes:** /root pulled into Phase 1 because rsync must run as root anyway to sync all of /home (files owned by other users/root must come through with metadata). The framework's default-config.yaml already anticipated `user_data: # Sync /home and /root`. ROADMAP + REQUIREMENTS to be updated for the move.

## Deletion / mirror semantics

| Option | Description | Selected |
|--------|-------------|----------|
| Mirror (--delete) + mass-delete guard | Exact replica + ceiling on deletions | |
| Mirror (--delete), no guard | Exact replica, no ceiling | ✓ |
| Additive only | Never delete on target | |

**User's choice:** Mirror with --delete, no arbitrary delete cap.

**Notes:** A mass-delete cap gives a false sense of security and is case-dependent. Real safety = btrfs snapshots as backstop + robust "target wasn't changed since last sync" detection. If B is unchanged, A→B delete is safe. This detection must be implemented well.

## Target-divergence guard

| Option | Description | Selected |
|--------|-------------|----------|
| Warn + confirm if last role was SOURCE | Use sync_history role | ✓ (reframed) |
| Hard block | Refuse sync, require override | |
| No guard in Phase 1 | One-directional overwrite only | |

**User's choice:** Warn + confirm — but reframed: the determining check is not "last role." On A→B the check is whether the subvolume/files on B changed since the last B→A sync. Repeated A→B with continued work on A is fully acceptable (not a conflict).

**Notes:** sync_history.py (role-only) is insufficient; needs a per-target divergence marker. Leading candidate: compare target's current @/@home against last post-sync btrfs snapshot.

## Preview & dry-run (Phase 1 framing)

| Option | Description | Selected |
|--------|-------------|----------|
| Summary + confirm before applying | Auto dry-run preview every sync | |
| --dry-run flag only | Preview only when flag passed | ✓ |
| No preview at all | Rely on snapshots | |

**User's choice:** --dry-run flag only; no routine auto-preview (the divergence warning is the one interactive gate). Superseded in detail by the tool-wide dry-run policy below.

## Root access model

| Option | Description | Selected |
|--------|-------------|----------|
| sudo via --rsync-path, normal SSH login | Escalate only for rsync; no root SSH | ✓ |
| Direct root SSH login | Connect as root | |
| Let me think | Discuss further | |

**User's choice:** sudo via `--rsync-path="sudo rsync"` on a normal-user SSH connection; smaller attack surface than enabling root SSH.

## Integrity verification

| Option | Description | Selected |
|--------|-------------|----------|
| rsync default delta + optional verify pass | Default delta, opt-in --checksum | ✓ then dropped |
| Force --checksum every sync | Read+checksum all files each run | |
| rsync default only | Trust delta detection | ✓ (final) |

**User's choice:** Final decision — rely on rsync's built-in transfer verification (it already checksum-verifies every transferred file); no custom verify feature. User corrected that reads don't wear SSDs and noted rsync's millions-of-users track record.

**Notes:** The integration test independently checksums per success criteria 1 and 5 — that is the test's job, not the tool's runtime behavior.

## TUI granularity

| Option | Description | Selected |
|--------|-------------|----------|
| Overall progress + current file | rsync --info=progress2 style | ✓ |
| Per-file streaming | Print every file | |
| Phase-level only | Per-folder only | |

**User's choice:** Overall progress + current file.

## Logging verbosity

| Option | Description | Selected |
|--------|-------------|----------|
| Per-file at FULL, summary at INFO | File-level audit at FULL, summaries at INFO | ✓ |
| Per-file at INFO | Surface every file in terminal | |
| Summary only | No per-file audit trail | |

**User's choice:** Per-file at FULL, per-folder summary at INFO.

## Tool-wide dry-run policy

| Option | Description | Selected |
|--------|-------------|----------|
| Full preview, zero writes anywhere | Read-only preview across all jobs; every SyncJob implements a real preview | ✓ |
| Validate-only, no per-job preview | Validation + which-jobs-run only | |
| Let me specify | User defines | |

**User's choice:** Full preview, zero writes anywhere. User explicitly required a single consistent --dry-run policy across the entire tool / all SyncJobs, not per-job.

## Include/exclude config model

| Option | Description | Selected |
|--------|-------------|----------|
| List of folder entries, each with own rules | Per-folder include/exclude → rsync filter rules | ✓ |
| Global folder list + one shared exclude list | One shared exclude for all | |
| Raw rsync filter-file passthrough | Native rsync filter syntax | |

**User's choice:** List of folder entries, each with its own include/exclude rules. Reusable for arbitrary paths (e.g. /important-data). Exclusions configurable with sensible defaults in the example config; .ssh/id_*, tailscale config, GPU/fontconfig caches, VS Code cache dirs are defaults (overridable). Dev caches (uv/pip/cargo/npm) and VS Code User state are synced.

## Job name

| Option | Description | Selected |
|--------|-------------|----------|
| folder_sync (generic) | Reflects generic folder sync | ✓ |
| user_data (matches existing config) | Existing config comment | |
| Other name | User-supplied | |

**User's choice:** folder_sync / FolderSyncJob; update the default-config.yaml comment.

## Preconditions (consistency)

| Option | Description | Selected |
|--------|-------------|----------|
| Note as documented assumption | Don't enforce in Phase 1 | |
| Discuss now | Talk through enforcement | |
| Explore more | Other areas | ✓ |

**User's choice:** Defer to a following phase — e.g. enforce "no user logged in except the shell running pc-switcher", or lsof-based open-file checks. Out of scope for Phase 1.

## Claude's Discretion

- Exact rsync invocation details beyond `-aAXHS` (info flags, `--numeric-ids`, partial-transfer handling).
- Internal structure of the folder-entry config schema and its mapping to rsync filter syntax.

## Deferred Ideas

- Consistency preconditions (no-user-logged-in / lsof) — later phase.
- Per-file conflict detection & resolution — Phase 2.
- Reflink / shared-extent (CoW) preservation — relevant to VM images / snapshotted datasets, later phases.
- /etc, systemd, users/groups, GNOME dconf — system config, Phase 3.
