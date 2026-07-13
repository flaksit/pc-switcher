# Ingest Synthesis Summary

Single entry point for `gsd-roadmapper`. Mode: new (net-new bootstrap). Source: 24 classified docs from `.planning/intel/classifications/`. No existing `.planning/` context.

## Doc counts by type

- ADR: 12 (docs/adr/adr-001 … adr-012)
- SPEC: 5 (docs/system/architecture.md, core.md, data-model.md, logging.md, testing.md)
- PRD: 6 (docs/planning/High level requirements.md, docs/planning/Feature breakdown.md, specs/001-core/spec.md, specs/002-testing-framework/spec.md, specs/003-core-tests/spec.md, specs/004-python-logging/spec.md)
- DOC: 1 (README.md)
- Total: 24. All high-confidence, manifest_override=true. No UNKNOWN/low-confidence docs.

## Decisions

- 11 locked (Accepted): ADR-001 (ADR process), ADR-002 (SSH channel), ADR-003 (Python 3.14 + uv orchestrator), ADR-004 (dynamic versioning from GitHub releases), ADR-005 (asyncio concurrency), ADR-006 (three-tier testing), ADR-007 (TDD), ADR-008 (draft-aware CI), ADR-010 (stdlib logging), ADR-011 (SDD living specs), ADR-012 (doc structure).
- 1 not locked (Proposed): ADR-009 (AI readiness labels).
- Detail: intel/decisions.md

## Requirements

- 21 requirement entries extracted. IDs: REQ-near-full-state-replication, REQ-sync-scope-user-data, REQ-sync-scope-packages, REQ-sync-scope-app-and-system-config, REQ-sync-scope-file-metadata, REQ-sync-scope-vms, REQ-sync-scope-docker, REQ-sync-scope-k3s, REQ-machine-specific-exclusions, REQ-environment-constraints, REQ-manual-sync-workflow, REQ-conflict-detection-no-resolution, REQ-terminal-ux, REQ-reliability-principles, REQ-feature-modular-architecture, REQ-core-001-infrastructure, REQ-testing-framework-002, REQ-core-tests-003, REQ-python-logging-004 (plus product-level vision and feature decomposition framing).
- Detail: intel/requirements.md

## Constraints

- 5 constraint entries (SPEC tier, docs/system/ living Golden Copy):
  - protocol/component-architecture: 1 (architecture.md)
  - protocol/contract: 1 (core.md)
  - schema: 1 (data-model.md)
  - protocol/nfr: 1 (logging.md)
  - nfr/process: 1 (testing.md)
- Detail: intel/constraints.md

## Context topics

- 1 (README.md — user-facing project overview, DOC tier, informational only)
- Detail: intel/context.md

## Conflicts

- 0 blockers
- 0 competing-variants
- 3 auto-resolved (INFO): living Golden Copy supersedes specs/00x on overlap (per ADR-011 + precedence); benign architecture.md<->data-model.md navigation cross-ref cycle (fully synthesized, not gated); ADR-009 Proposed/not-locked.
- Detail: /home/janfr/dev/pc-switcher/.planning/INGEST-CONFLICTS.md

## Per-type intel files

- /home/janfr/dev/pc-switcher/.planning/intel/decisions.md
- /home/janfr/dev/pc-switcher/.planning/intel/requirements.md
- /home/janfr/dev/pc-switcher/.planning/intel/constraints.md
- /home/janfr/dev/pc-switcher/.planning/intel/context.md

## Routing status

READY — no blockers, no competing variants. Safe for gsd-roadmapper to route. Note the ADR-011 living-spec model: roadmapper should treat docs/system/*.md as current-state authority and specs/00x as frozen history. Project stage (per CLAUDE.md): core infrastructure complete; core sync functionality (user features 5-10) in development.
