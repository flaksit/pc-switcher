---
phase: 01-home-sync-mvp-user-data-sync
plan: "01"
subsystem: docs
tags: [adr, rsync, ssh, dry-run, architecture]

requires: []
provides:
  - ADR-013 locking rsync-over-SSH as the user-data transport with root-via-sudo model
  - ADR-014 locking the tool-wide dry-run contract for all SyncJobs
affects:
  - 01-02-folder-sync-implementation
  - 01-03-dry-run-enforcement
  - 01-04-validation
  - 01-05-integration-tests

tech-stack:
  added: []
  patterns:
    - "ADR structure: Status and Date as separate single-line paragraphs (hook-compliant)"

key-files:
  created:
    - docs/adr/adr-013-rsync-over-ssh-user-data-transport.md
    - docs/adr/adr-014-unified-dry-run-contract.md
  modified:
    - docs/adr/_index.md

key-decisions:
  - "rsync-over-SSH chosen over btrfs send/receive as user-data transport (D-04)"
  - "rsync runs as root on both ends via sudo; root SSH login explicitly forbidden (D-05)"
  - "--dry-run is a tool-wide contract binding all SyncJobs and the orchestrator (D-12)"

patterns-established:
  - "ADR pattern: Implementation Rules split into Required and Forbidden subsections for clarity"

requirements-completed:
  - REQ-sync-scope-user-data
  - REQ-manual-sync-workflow

coverage:
  - id: D1
    description: "ADR-013 records rsync-over-SSH as the user-data transport (root via sudo) per D-04 and D-05"
    requirement: REQ-sync-scope-user-data
    verification:
      - kind: manual_procedural
        ref: "docs/adr/adr-013-rsync-over-ssh-user-data-transport.md — Status: Accepted, contains --rsync-path and D-04/D-05 references"
        status: pass
    human_judgment: false
  - id: D2
    description: "ADR-014 records the unified dry-run contract for all SyncJobs per D-12"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: manual_procedural
        ref: "docs/adr/adr-014-unified-dry-run-contract.md — Status: Accepted, lists forbidden dry-run operations and cites D-12"
        status: pass
    human_judgment: false
  - id: D3
    description: "docs/adr/_index.md lists ADR-013 and ADR-014 under Active Decisions with correct links"
    verification:
      - kind: manual_procedural
        ref: "grep ADR-013 docs/adr/_index.md && grep ADR-014 docs/adr/_index.md"
        status: pass
    human_judgment: false

duration: 4min
completed: 2026-06-30
status: complete
---

# Phase 01 Plan 01: ADR Authoring — rsync Transport and Dry-Run Contract Summary

**Two immutable ADRs lock the Phase 1 transport and dry-run decisions: ADR-013 formalizes rsync-over-SSH with root-via-sudo (D-04/D-05); ADR-014 formalizes the tool-wide --dry-run contract (D-12).**

## Performance

- **Duration:** 4 min
- **Started:** 2026-06-30T13:16:34Z
- **Completed:** 2026-06-30T13:20:19Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- ADR-013 closes the STATE.md blocker: rsync-over-SSH transport is now captured as an immutable record with root-via-sudo model, forbidden root SSH login, required flag baseline (-aAXHS --numeric-ids), and async subprocess mandate
- ADR-014 formalizes the tool-wide --dry-run contract: all SyncJobs must run full read-only previews; forbidden operations enumerated; orchestrator enforcement point referenced (plan 01-03)
- ADR index updated with both new ADRs in numeric order and date bumped to 2026-06-30

## Task Commits

Each task was committed atomically:

1. **Task 1: Author ADR-013 — rsync-over-SSH user-data transport (root via sudo)** - `6c87c25` (docs)
2. **Task 2: Author ADR-014 (unified dry-run contract) and update the ADR index** - `729ec3e` (docs)

## Files Created/Modified
- `docs/adr/adr-013-rsync-over-ssh-user-data-transport.md` - Immutable ADR locking rsync-over-SSH transport with Implementation Rules (required: --numeric-ids, -aAXHS, async subprocess, SSH identity under sudo; forbidden: root SSH login, blocking calls)
- `docs/adr/adr-014-unified-dry-run-contract.md` - Immutable ADR locking tool-wide --dry-run contract with forbidden operations and orchestrator enforcement reference
- `docs/adr/_index.md` - Added ADR-013 and ADR-014 entries, bumped last-updated date

## Decisions Made
- ADR format deviation: Status and Date placed as separate one-line paragraphs (blank line between each) rather than adjacent lines — required to pass the hard-wrap hook (existing ADRs predate the hook and use the adjacent-line format)

## Deviations from Plan
None — plan executed exactly as written, modulo the formatting adjustment above which does not affect ADR content or immutability.

## Issues Encountered
- The markdown hard-wrap hook blocked the initial write because Status and Date were consecutive lines (treated as a wrapped paragraph). Fixed by separating each metadata field into its own paragraph. This differs from the existing ADR-001 through ADR-012 format but satisfies the project's CLAUDE.md rule ("one paragraph = one line"). Future ADR authors should follow this pattern.

## User Setup Required
None — no external service configuration required.

## Next Phase Readiness
- ADR-013 and ADR-014 are locked; later plan executors can implement against them without re-litigating transport or dry-run decisions
- STATE.md blocker ("rsync-over-SSH transport choice is not yet captured in an ADR") is resolved
- Plan 01-02 (FolderSyncJob implementation) and plan 01-03 (orchestrator dry-run enforcement) can proceed

## Self-Check: PASSED
- `docs/adr/adr-013-rsync-over-ssh-user-data-transport.md` — FOUND
- `docs/adr/adr-014-unified-dry-run-contract.md` — FOUND
- Commit `6c87c25` (ADR-013) — FOUND
- Commit `729ec3e` (ADR-014 + index) — FOUND
- `grep ADR-013 docs/adr/_index.md` — FOUND
- `grep ADR-014 docs/adr/_index.md` — FOUND

---
*Phase: 01-home-sync-mvp-user-data-sync — Completed: 2026-06-30*
