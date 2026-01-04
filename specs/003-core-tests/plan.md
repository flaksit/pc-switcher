# Implementation Plan: Retroactive Tests for 001-Core

**Branch**: `003-core-tests` | **Date**: 2025-12-11 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/003-core-tests/spec.md`

**Note**: This template is filled in by the `/speckit.plan` command. See `.specify/templates/commands/plan.md` for the execution workflow.

## Summary

Create comprehensive spec-driven tests for all existing 001-core functionality. Tests validate 100% of user stories, acceptance scenarios, and functional requirements defined in specs/001-core/spec.md. Tests use the existing testing framework (002-testing-framework) with unit tests for logic verification and integration tests for real VM-based operations. Each test includes traceability to specific spec requirements through test naming and docstrings.

## Technical Context

**Language/Version**: Python 3.14 (per ADR-003)
**Primary Dependencies**: pytest, pytest-asyncio, asyncssh (existing testing framework from 002-testing-framework)
**Storage**: N/A (tests verify code behavior, not storage)
**Testing**: pytest with unit and integration markers; test VMs provisioned via existing framework
**Target Platform**: Ubuntu 24.04 LTS on btrfs (same as pc-switcher target platform)
**Project Type**: Single project (testing existing pc-switcher implementation)
**Performance Goals**: Unit test suite completes in <30 seconds; integration tests complete in <15 minutes
**Constraints**: Tests must be spec-driven (validate requirements, not implementation details); 100% traceability to spec requirements; unit tests must use mock executors to avoid real system operations
**Scale/Scope**: 9 user stories from 001-core spec, 44 active acceptance scenarios (3 removed), 44 active functional requirements (4 removed/skipped)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **Reliability Without Compromise**: ✓ Tests validate all reliability mechanisms defined in 001-core spec (snapshots, error handling, interrupt handling, conflict detection). Comprehensive test coverage ensures the core infrastructure behaves correctly and detects regressions early. No data integrity concerns for tests themselves (read-only verification).

- **Frictionless Command UX**: ✓ Tests validate UX requirements (single command sync, automated installation, graceful interrupts). Running tests remains simple: `uv run pytest tests/unit` (fast) and `uv run pytest tests/integration -m integration` (comprehensive). No additional manual steps required beyond what testing framework provides.

- **Well-supported tools and best practices**: ✓ No new tools introduced. Uses existing pytest + pytest-asyncio from 002-testing-framework. All dependencies already vetted and documented in ADR-006. Follows standard Python testing patterns and pytest conventions.

- **Minimize SSD Wear**: ✓ Tests execute on isolated VMs (integration) or in-memory (unit), not on development machines. No additional SSD wear on production systems. Integration tests verify btrfs snapshot COW behavior minimizes wear.

- **Throughput-Focused Syncing**: N/A for testing feature itself. Tests validate sync performance requirements (SC-004: version check <30s, SC-009: installation <2min). Performance regression detection through integration tests.

- **Deliberate Simplicity**: ✓ Test structure follows established pytest patterns. One test file per major component/job. Clear naming convention ties tests to spec requirements (test_fr001_*, test_us1_as1_*). No complex test infrastructure beyond existing framework.

- **Up-to-date Documentation**: ✓ Plan creates test coverage documentation (data-model.md maps requirements to tests). Quickstart.md provides developer onboarding for running/writing tests. No changes to main project docs needed (tests are internal verification, not user-facing feature).

## Project Structure

### Documentation (this feature)

```text
specs/003-core-tests/
├── spec.md              # Feature specification (already exists)
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output: test patterns and approaches
├── data-model.md        # Phase 1 output: requirement-to-test mapping
├── quickstart.md        # Phase 1 output: how to run and write tests
├── contracts/           # Phase 1 output: test coverage contracts
│   └── coverage-map.yaml  # Mapping of spec requirements to test files
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT by /speckit.plan)
```

### Test Code (repository root)

```text
tests/
├── unit/                        # Fast tests with no external dependencies
│   ├── orchestrator/
│   │   ├── test_job_lifecycle.py       # FR-001, FR-002: job interface and lifecycle
│   │   ├── test_config_system.py       # FR-028 to FR-033: config loading/validation
│   │   ├── test_interrupt_handling.py  # FR-024 to FR-027: SIGINT handling
│   │   └── test_logging_system.py      # FR-018 to FR-023: log levels and routing
│   ├── jobs/
│   │   ├── test_dummy_jobs.py          # FR-038 to FR-042: dummy job behaviors
│   │   ├── test_install_job.py         # FR-005 to FR-007c: version check and install
│   │   └── test_snapshot_job.py        # FR-008 to FR-017: btrfs snapshot logic
│   └── cli/
│       └── test_commands.py            # FR-046 to FR-048: CLI commands
│
└── integration/                 # Real VM-based system tests
    ├── test_end_to_end_sync.py          # US-1 to US-9: complete workflows
    ├── test_self_installation.py        # US-2: install/upgrade on target
    ├── test_snapshot_infrastructure.py  # US-3: btrfs snapshots creation/cleanup
    ├── test_logging_integration.py      # US-4: unified logging from source+target
    ├── test_interrupt_integration.py    # US-5: graceful Ctrl+C handling
    └── test_terminal_ui.py              # US-9: progress reporting (visual verification)
```

**Structure Decision**: Tests follow existing pytest structure from 002-testing-framework. Unit tests organized by component (orchestrator, jobs, cli) for clarity. Integration tests organized by user story for traceability. Each test file corresponds to a major component or user story from 001-core spec.

## Test Independence (FR-009)

All tests MUST be independent and not rely on execution order or shared mutable state:

- **No shared state**: Each test sets up its own fixtures/mocks; no global variables modified between tests
- **Isolation via fixtures**: Use pytest fixtures with appropriate scope (`function` by default) for setup/teardown
- **Integration test isolation**: Each integration test gets a clean VM state via btrfs snapshot reset
- **Verification**: Run tests in random order with `pytest --randomly-seed=<N>` to catch order dependencies

## Success and Failure Path Coverage (FR-004, SC-004)

Every requirement MUST have tests covering both success and failure paths:

- **Success path**: Normal operation completes as expected
- **Failure path**: Error conditions are handled correctly (invalid input, exceptions, edge cases)

Implementation approach is flexible per test:
- **Separate functions**: Use when success/failure scenarios are complex or have different setup
- **Combined function**: Use when both paths can be tested cleanly with multiple assertions

The edge cases listed in data-model.md cover failure paths for their respective requirements.

## Coverage Verification

After all tests are implemented, perform a manual verification pass to confirm:
1. All user stories, acceptance scenarios, and functional requirements from 001-core spec have corresponding tests
2. Tests follow the naming convention and include traceability docstrings
3. Each requirement has both success and failure path coverage (FR-004)

**Note**: `contracts/coverage-map.yaml` and `data-model.md` are planning artifacts for this feature implementation. They guide test creation but are not maintained long-term after implementation is complete.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., 4th project] | [current need] | [why 3 projects insufficient] |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |
