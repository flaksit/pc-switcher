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

## References

- ADR-013: rsync-over-SSH as user-data transport (transport layer this safety model sits above)
- ADR-014: Unified dry-run contract — its "run divergence/conflict detection during dry-run" step is now realized by the topology out-of-order check; ADR-014 is immutable and not superseded by this decision
- CR-01, CR-02: `.planning/phases/01-home-sync-mvp-user-data-sync/01-VERIFICATION.md` — concrete failure evidence motivating the removal of `find-new`
- GitHub #159: the A→B / work-on-A / A→B pattern that the topology check must not block as a hard abort
