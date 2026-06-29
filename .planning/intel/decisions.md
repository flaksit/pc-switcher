# Decisions (from ADRs)

Synthesized from `docs/adr/*.md`. Precedence tier: ADR (0) — highest. One entry per ADR, preserved separately. LOCKED = `Accepted` status (immutable per ADR-001). ADR-009 is `Proposed`, hence not locked.

## ADR-001: Use Architecture Decision Records (ADR)

- source: /home/janfr/dev/pc-switcher/docs/adr/adr-001-adr.md
- status: locked (Accepted)
- scope: documentation process, docs/adr folder structure, ADR immutability, granularity guidelines, AI agent integration
- decision: Use immutable ADRs to document key architectural decisions. ADRs follow a fixed folder/document structure, are never edited after acceptance (only superseded with two-way references), and `_index.md` lists all current ADRs. AI agents read `_index.md` first, load specific ADRs on demand.

## ADR-002: SSH as Communication Channel between Source and Target

- source: /home/janfr/dev/pc-switcher/docs/adr/adr-002-ssh-communication-channel.md
- status: locked (Accepted)
- scope: SSH communication channel, ControlMaster multiplexing, source-target orchestration, progress streaming, signal handling/cleanup, SSH config aliases
- decision: Use SSH as the orchestration channel between source and target. Single persistent multiplexed connection (ControlMaster). Orchestration logic lives on source; target exposes discrete, stateless scripts invoked via SSH that stream line-buffered progress to stdout/stderr. No custom protocols, daemons, or persistent target services. User specifies target via SSH hostname (respects `~/.ssh/config`). File-sync protocols (rsync, etc.) invoked through this channel. SIGINT triggers explicit target cleanup before connection close.

## ADR-003: Python Orchestrator with Task-Specific Languages

- source: /home/janfr/dev/pc-switcher/docs/adr/adr-003-implementation-language.md
- status: locked (Accepted)
- scope: Python 3.14, uv package management, orchestrator, CLI, asyncssh, rich/typer, task-specific bash, package structure, pytest
- decision: Python 3.14 (via `uv` exclusively, never pip/system python) is the primary orchestration language for CLI, SSH coordination, state management, conflict detection, and TUI. Built as an installable package distributable to source and target. Use proven libraries: asyncssh (SSH), rich (TUI), typer/click (CLI). Delegate individual sync operations to task-specific languages (bash) where appropriate; never implement core orchestration in bash. Modern type syntax, full annotations, `from __future__ import annotations`, ruff/basedpyright/pytest.

## ADR-004: Dynamic Package Versioning from GitHub Releases

- source: /home/janfr/dev/pc-switcher/docs/adr/adr-004-dynamic-versioning-github-releases.md
- status: locked (Accepted)
- scope: dynamic versioning, GitHub Release tags, uv-dynamic-versioning plugin, hatchling backend, pyproject.toml, SemVer pre-release format, GitHub Actions
- decision: GitHub Release tags are the single source of truth for version. Version is NOT stored in pyproject.toml (`dynamic = ["version"]`). Build backend is hatchling + uv-dynamic-versioning (`vcs = "git"`, `style = "pep440"`); CI checkout needs `fetch-depth: 0`. Use SemVer tag format including pre-releases (`v0.1.0-alpha.1`, `-beta.1`, `-rc.1`). No build during GitHub Release creation (not needed with current installation method). Version parsing code must support SemVer pre-release identifiers.

## ADR-005: Use Asyncio for Concurrency

- source: /home/janfr/dev/pc-switcher/docs/adr/adr-005-asyncio-concurrency.md
- status: locked (Accepted)
- scope: asyncio concurrency model, async Job interface, asyncssh, asyncio.TaskGroup, signal-handling cancellation, subprocess execution
- decision: Use asyncio as the core concurrency model for all I/O and job execution. All Job interface methods (validate, pre_sync, sync, post_sync) are `async def`. Use asyncssh for remote ops, `asyncio.create_subprocess_exec` for local subprocesses, `asyncio.TaskGroup` for structured concurrency. SIGINT/SIGTERM trigger cancellation of the active Task for immediate abort (satisfies FR-003, FR-027). Forbidden: blocking calls (`time.sleep`, `subprocess.run`, blocking socket I/O) in the event loop.

## ADR-006: Testing Framework

- source: /home/janfr/dev/pc-switcher/docs/adr/adr-006-testing-framework.md
- status: locked (Accepted)
- scope: three-tier testing, unit tests, VM-isolated integration tests, btrfs snapshot reset, lock-based concurrency control, manual playbook
- decision: Three-tier testing. Unit tests are fast, dependency-free, safe anywhere, run every commit. Integration tests run only inside dedicated test VMs (two VMs pc1/pc2 mirroring source→target, real btrfs + SSH), reset to a clean baseline via btrfs snapshot rollback (~30s) before each run, with lock-based concurrency control preventing simultaneous dev/CI runs. Manual playbook covers visual verification only (colors, progress bars).

## ADR-007: Test-Driven Development for Implementation

- source: /home/janfr/dev/pc-switcher/docs/adr/adr-007-tdd-implementation.md
- status: locked (Accepted)
- scope: TDD, red-green-refactor, SpecKit implementation phase, tasks.md derivation, verification evidence
- decision: Use TDD (red-green-refactor) for all SpecKit `/speckit.implement` phases. Write a failing test before implementing; tests derived from `tasks.md`; refactoring must keep tests green. Applies to implementation phase only. Skip TDD only for trivial changes (typos, comments). Tests serve as the constitution's required verification evidence.

## ADR-008: CI Pipeline with Draft-Aware Integration Tests

- source: /home/janfr/dev/pc-switcher/docs/adr/adr-008-ci-pipeline.md
- status: locked (Accepted)
- scope: CI pipeline, integration tests, draft PR handling, branch protection, merge queue removal
- decision: Lint and Unit Tests run on all branches/pushes. Integration tests trigger on `pull_request` events but skip draft PRs (`if: github.event.pull_request.draft == false`). Branch protection requires all three checks (Lint, Unit Tests, Integration Tests) before merge to main. No merge queue (removed to avoid chicken-and-egg with the queue).

## ADR-009: AI Readiness Labels for Issue Triage

- source: /home/janfr/dev/pc-switcher/docs/adr/adr-009-ai-readiness-labels.md
- status: NOT locked (Proposed)
- scope: GitHub issue labels, AI readiness triage, ai:ready / ai:needs-speckit / ai:unclear, issue delegation
- decision (proposed): Three mutually-exclusive GitHub labels classify issues by AI readiness — `ai:ready` (clear + small, implement directly), `ai:needs-speckit` (clear but too large, needs speckit first), `ai:unclear` (scope/outcome unclear, human clarifies then re-triage). Orthogonal to priority labels. As Proposed, this decision is overridable by any higher-or-equal source without a blocker.

## ADR-010: Standard Library Logging Infrastructure

- source: /home/janfr/dev/pc-switcher/docs/adr/adr-010-logging-infrastructure.md
- status: locked (Accepted)
- scope: logging infrastructure, stdlib logging, QueueHandler/QueueListener, JSON and Rich formatters, logger hierarchy filtering, structlog rejection
- decision: Use Python stdlib `logging` (NOT structlog) as the sole logging foundation. Register custom FULL level (15) via `addLevelName`. Use QueueHandler + QueueListener for non-blocking output. Custom Formatters for JSON-lines file output and Rich TUI output. Three-setting filtering model via logger hierarchy: `file` (default DEBUG), `tui` (default INFO), `external` (additional floor for non-pcswitcher loggers, default WARNING). Forbidden: structlog processors/formatters, custom async queue implementations, separate internal/external pipelines.

## ADR-011: Specification-Driven Development with Living Specs

- source: /home/janfr/dev/pc-switcher/docs/adr/adr-011-sdd-with-living-specs.md
- status: locked (Accepted)
- scope: SDD, immutable specs history, living Golden Copy docs/system, semantic ID strategy, lineage tracking, consolidation workflow
- decision: Hybrid SDD. `specs/00x-*` folders are immutable history (frozen after feature completion, never edited). `docs/system/*.md` is the living "Golden Copy" — authoritative current state, consolidated from spec.md files. Use semantic IDs (`<DOMAIN>-<TYPE>-<DESCRIPTOR>`, e.g. `LOG-FR-ROTATION`) in living docs and code, with lineage notes (e.g. `001-FR-002 → 005-FR-013`, GitHub issue refs). Workflow A (big feature): run SpecKit then consolidate. Workflow B (small fix): edit living doc first, then implement. NOTE: this decision establishes that `docs/system/` overrides overlapping `specs/` content for current-state truth (see INGEST-CONFLICTS.md auto-resolved entry).

## ADR-012: Documentation Structure and Strategy

- source: /home/janfr/dev/pc-switcher/docs/adr/adr-012-documentation-structure.md
- status: locked (Accepted)
- scope: documentation structure, audience-based folders, dev/ops/planning/system folders, CLAUDE.md and AGENTS.md, no-duplication, manual playbooks in tests
- decision: Organize docs by audience — `docs/dev/` (AI agent instructions), `docs/ops/` (setup/troubleshooting), `docs/planning/` (scope), `docs/system/` (Golden Copy per ADR-011), `docs/adr/` (decisions). Keep CLAUDE.md lean with pointers; AGENTS.md redirects to CLAUDE.md. Manual playbooks live in `tests/`, not `docs/`. Apply DRY: no duplication except clearly-marked summaries. README.md is self-contained and user-facing.
