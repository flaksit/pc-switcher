# ADR-015: Topology-Based Sync-Safety Model

Status: Accepted

Date: 2026-07-01

## TL;DR
Sync safety comes from btrfs pre/post snapshots (rollback backstop) + rsync `--dry-run` deletion log at FULL + a topology-based out-of-order warn+confirm step that reads sync-history `last_role`/`last_peer`; the btrfs `find-new` content-detection guard is removed.

## Implementation Rules

### Required
- Topology check reads `last_role` and `last_peer` from both the local and target sync-history to detect out-of-order sync patterns; on detection warn and require confirmation — never a hard abort
- Provide `--allow-out-of-order` to bypass the topology confirmation step
- Persist rsync deletion items at FULL log level for both `--dry-run` and real runs
- btrfs pre/post snapshots remain required as the rollback backstop

### Forbidden
- No `btrfs subvolume find-new` guard in the sync path
- No per-target btrfs generation markers

## Context

The previous divergence guard used `btrfs subvolume find-new` to detect whether the target had changed since the last sync. Failures CR-01 and CR-02 (see `01-VERIFICATION.md`) exposed three root causes that make content-detection wrong for this use case.

First, `find-new` answers the wrong question: it measures byte-changes since a given generation, not whether the target was independently modified by the user, which is the safety-relevant signal.

Second, normal desktop activity — boot, login, cache writes, unattended upgrades — continuously bumps btrfs generations. Every path added to suppress this noise widens the false-negative window: CR-01 showed an unanchored substring filter silently masking nested user files, allowing `rsync --delete` to proceed against genuinely diverged data.

Third, `/home` lives on `@home` and `/root` on `@`; a single `find-new` call cannot span the sync scope without reading two separate subvolumes, and the granularity of what changed does not align with per-folder sync semantics.

rsync `--dry-run --delete` already provides the exact, scope-correct, noise-free preview of what would be destroyed, making content-detection redundant with the dry-run + snapshots combination and a permanent maintenance liability.

## Decision

- Sync safety rests on four pillars: (1) btrfs pre/post snapshots as the rollback backstop; (2) rsync `--dry-run` whose deletions are itemized in the FULL-level log; (3) a topology-based out-of-order warn+confirm step that reads `last_role` and `last_peer` from both the local and target sync-history; (4) trusting the user — no destructive-preview confirmation is required in the normal (in-order) case.
- `btrfs subvolume find-new` content-detection is removed. No per-target generation markers are maintained.
- The topology check is warn+confirm, never a hard abort: the pattern A→B / continue work on A / A→B again is a legitimate workflow (GitHub #159) and must not be blocked.
- ADR-014's "run divergence/conflict detection during dry-run" step is now realized by the topology out-of-order check, not by btrfs `find-new`; ADR-014 remains immutable and is not superseded by this decision.

## Consequences

**Positive:**
- Eliminates CR-01 (false-negative data loss from unanchored filter) and CR-02 (false-positive block on every upgrade+sync cycle)
- No per-machine exclusion denylist to maintain; the deletion preview is exact, scope-correct, and self-updating as the sync scope changes
- Topology check handles the safety-relevant case (unexpected source machine) without false positives from normal desktop activity

**Negative:**
- Topology check requires populated sync-history on both machines; first-ever sync has no prior `last_role`/`last_peer` record (treat absent history as in-order)
- btrfs snapshots remain a mandatory prerequisite for the rollback backstop; the topology check alone is not a sufficient rollback mechanism

## Refinement: first-sync is distinct from out-of-order

This section refines the Decision above; where they differ, this section takes precedence.

The original model folded three situations into a single orchestrator pre-flight gated by `--allow-out-of-order`:

- W1: the target has no readable sync-history
- W2: the target last synced with a different peer (machine-C)
- W3: consecutive push — the same source pushes again without a back-sync (GitHub #159)

W1 is not an out-of-order condition. A target with no readable sync-history has never been synced by pc-switcher, so there is no prior topology to be "out of order" with. It is a first-ever sync whose risk is simply that `rsync --delete` will overwrite every configured folder wholesale. That is a different question ("do you want to overwrite this untracked target?") from the out-of-order question ("this target's last peer isn't who you expect"), and it warrants its own confirmation with its own bypass flag.

Decisions:

- First-sync detection and its overwrite confirmation move out of the orchestrator and into `FolderSyncJob`, which owns the destructive `rsync --delete` transfer. The job reads the target's sync-history; unreadable, empty, or unparsable content is treated as a first sync. Before transferring, it confirms an explicit "this overwrites the target" prompt that lists the in-scope folders. Non-first syncs proceed silently.
- The first-sync confirmation is gated by a new `--allow-first-sync` flag (auto-approve in non-interactive runs). `--allow-out-of-order` now affects only W2/W3.
- The orchestrator's `_check_out_of_order` handles only W2/W3. When the target has no readable sync-history it does not treat this as out-of-order and does not abort — it proceeds and defers the first-sync confirmation to `FolderSyncJob`.
- Under `--dry-run`, both gates log their warning and proceed without prompting (ADR-014: dry-run is a read-only rehearsal).
- A reusable `Confirmer` abstraction (`pcswitcher.confirmer.TerminalUIConfirmer`) now backs both gates. Interactive runs pause the live TUI, show a yellow Rich panel, and prompt `Continue anyway? [y/n]`; non-interactive runs fall back to the caller's `--allow-*` flag. It is injected via `JobContext.confirmer` so future interactive jobs reuse the same mechanism instead of reaching into the console/TUI directly.

This supersedes the Consequences note "first-ever sync … treat absent history as in-order": absent history is now handled by the first-sync confirmation, not silently treated as in-order.

## References

- ADR-013: rsync-over-SSH as user-data transport (transport layer this safety model sits above)
- ADR-014: Unified dry-run contract — its "run divergence/conflict detection during dry-run" step is now realized by the topology out-of-order check; ADR-014 is immutable and not superseded by this decision
- CR-01, CR-02: `.planning/phases/01-home-sync-mvp-user-data-sync/01-VERIFICATION.md` — concrete failure evidence motivating the removal of `find-new`
- GitHub #159: the A→B / work-on-A / A→B pattern that the topology check must not block as a hard abort
