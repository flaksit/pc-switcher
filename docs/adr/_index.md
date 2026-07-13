# Architecture Decisions Summary (Last updated: 2026-07-03)

## Active Decisions

- [ADR-001](adr-001-adr.md): Use Architecture Decision Records (ADR)
- [ADR-002](adr-002-ssh-communication-channel.md): SSH as communication channel between source and target
- [ADR-003](adr-003-implementation-language.md): Python orchestrator with task-specific languages
- [ADR-004](adr-004-dynamic-versioning-github-releases.md): Dynamic package versioning from GitHub Releases
- [ADR-005](adr-005-asyncio-concurrency.md): Use Asyncio for Concurrency
- [ADR-006](adr-006-testing-framework.md): VM-Based Testing Framework
- [ADR-007](adr-007-tdd-implementation.md): Test-Driven Development for Implementation
- [ADR-008](adr-008-ci-pipeline.md): CI Pipeline with Draft-Aware Integration Tests
- [ADR-009](adr-009-ai-readiness-labels.md): AI Readiness Labels for Issue Triage
- [ADR-010](adr-010-logging-infrastructure.md): Standard Library Logging Infrastructure
- [ADR-011](adr-011-sdd-with-living-specs.md): Specification-Driven Development with Living Specs
- [ADR-012](adr-012-documentation-structure.md): Documentation Structure and Strategy
- [ADR-013](adr-013-rsync-over-ssh-user-data-transport.md): rsync-over-SSH as user-data transport, running as root via sudo
- [ADR-014](adr-014-unified-dry-run-contract.md): Unified dry-run contract for all SyncJobs
- [ADR-015](adr-015-topology-based-sync-safety-model.md): Topology-based sync-safety model (btrfs find-new content-detection removed)
- [ADR-016](adr-016-hardcoded-runtime-file-excludes.md): Hardcoded exclusion of pc-switcher's own runtime files from folder sync

### Instructions for AI agents
Load specific ADRs only when relevant to current task: search workspace files to find relevant ADRs

## Superseded

(None)

Example:
- ADR-005: Direct IBKR Gateway → Superseded by ADR-017
