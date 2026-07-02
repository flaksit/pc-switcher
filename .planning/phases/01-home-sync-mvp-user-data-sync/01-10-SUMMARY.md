---
phase: 01-home-sync-mvp-user-data-sync
plan: 10
subsystem: docs/adr
tags: [adr, safety-model, divergence-detection, documentation]
requires: [01-09-SUMMARY.md]
provides: [ADR-015, adr-015-reference, context-supersession]
affects: [docs/adr, .planning/phases/01-home-sync-mvp-user-data-sync]
tech_stack:
  added: []
  patterns: [adr-process, decision-record, supersession-marker]
key_files:
  created:
    - docs/adr/adr-015-topology-based-sync-safety-model.md
  modified:
    - docs/adr/_index.md
    - .planning/phases/01-home-sync-mvp-user-data-sync/01-CONTEXT.md
decisions:
  - "ADR-015 accepted: topology-based sync-safety model replaces btrfs find-new content-detection"
  - "ADR-014 is not superseded — its divergence-detection step is realized by the topology check"
  - "D-06/D-07/D-08 in 01-CONTEXT.md marked superseded; original text preserved for history"
metrics:
  duration: 3min
  completed: 2026-07-02T11:51:51Z
  tasks_completed: 3
  files_changed: 3
status: complete
---

# Phase 01 Plan 10: Topology-Based Sync-Safety ADR Summary

## One-Liner

ADR-015 documenting the topology-based sync-safety pivot: btrfs find-new removed; safety now from snapshots backstop + rsync dry-run FULL deletion log + topology out-of-order warn+confirm.

## Tasks Completed

| Task | Description | Commit | Files |
|------|-------------|--------|-------|
| 1 | Write ADR-015 (topology-based sync-safety model) | 765ce9c | docs/adr/adr-015-topology-based-sync-safety-model.md |
| 2 | Index ADR-015 in _index.md | 1403bc5 | docs/adr/_index.md |
| 3 | Mark D-06/D-07/D-08 superseded in 01-CONTEXT.md | a01be42 | .planning/phases/01-home-sync-mvp-user-data-sync/01-CONTEXT.md |

## What Was Built

ADR-015 is the authoritative decision record for the divergence-safety design pivot introduced by gap-closure plans 01-10 through 01-14. Before any code is removed, this ADR locks the rationale so downstream executors and reviewers have a single citable decision.

The ADR records the three verified root causes that make btrfs `find-new` content-detection wrong for this use case (wrong question, desktop noise forcing an ever-growing exclusion denylist that widens the false-negative window per CR-01, subvolume granularity mismatch with /home=@home vs /root=@), and establishes the four-pillar safety model that replaces it.

01-CONTEXT.md decisions D-06, D-07, D-08, and D-18's enforcement clause are marked superseded (inline markers on headings); original text is preserved. A new "Current sync-safety model (ADR-015)" section summarizes the new pillars and links to the ADR.

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None — this plan is documentation only; no code stubs.

## Threat Surface Scan

No new network endpoints, auth paths, file access patterns, or schema changes introduced. Documentation-only plan.

## Self-Check: PASSED

- `docs/adr/adr-015-topology-based-sync-safety-model.md` exists, Status: Accepted, contains find-new rejection, snapshot reference, dry-run reference: FOUND
- Commit 765ce9c exists: FOUND
- `docs/adr/_index.md` references adr-015: FOUND
- Commit 1403bc5 exists: FOUND
- `01-CONTEXT.md` contains "supersed" and "adr-015": FOUND
- Commit a01be42 exists: FOUND
- No Supersedes header in ADR-015: CONFIRMED
- D-06 text preserved in 01-CONTEXT.md: CONFIRMED
