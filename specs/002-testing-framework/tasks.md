# Tasks: Testing Framework

**Input**: Design documents from `/specs/002-testing-framework/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, quickstart.md ✓

**Tests**: Not explicitly requested in spec - test tasks NOT included. Framework infrastructure only.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and pytest configuration

- [ ] T001 Configure pytest with asyncio_mode="auto" and integration marker in pyproject.toml
- [ ] T002 [P] Add `-m "not integration"` to pytest addopts to exclude integration tests by default in pyproject.toml
- [ ] T003 [P] Create tests/integration/conftest.py with environment variable check and skip logic per research.md §2

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: VM infrastructure scripts that MUST be complete before ANY user story can proceed

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

> **Reference**: Complete script implementations are available in `specs/002-testing-framework/pre-analysis/testing-implementation-plan.md`. Use these as starting point and verify they work correctly with the current environment.

- [ ] T004 Create tests/infrastructure/scripts/provision-vms.sh to create VMs via hcloud CLI per research.md §3; use CX23 instances (2 shared vCPUs, 4GB RAM) to stay within EUR 10/month cost cap (FR-012)
- [ ] T005 [P] Create tests/infrastructure/scripts/configure-vm.sh for testuser setup, SSH keys (including CI-provided public key injection per FR-006a), and baseline services
- [ ] T006 [P] Create tests/infrastructure/scripts/configure-hosts.sh to configure /etc/hosts and inter-VM SSH access
- [ ] T007 Create tests/infrastructure/scripts/reset-vm.sh implementing btrfs snapshot reset flow per research.md §4
- [ ] T008 Create tests/infrastructure/scripts/lock.sh for Hetzner Server Labels lock operations per research.md §5

**Checkpoint**: Foundation ready - VM infrastructure scripts complete

---

## Phase 3: User Story 1 - Unit Test Suite for Fast Feedback (Priority: P1) 🎯 MVP

**Goal**: Developers can run unit tests locally with `uv run pytest tests/unit tests/contract -v` completing in < 30 seconds with no external dependencies

**Independent Test**: Run `uv run pytest tests/unit tests/contract -v` on any developer machine and verify all tests pass without configuration

### Implementation for User Story 1

> **Note**: US1 tasks are verification-only because unit test infrastructure already exists. The framework feature ensures existing tests work with new pytest configuration; no new test code is written here.

- [ ] T009 [US1] Verify existing tests in tests/unit/ and tests/contract/ work with new pytest configuration; confirm tests contain no real btrfs operations, no network calls to external services, and no filesystem-type dependencies (FR-002 safety)
- [ ] T010 [US1] Verify existing fixtures in tests/conftest.py are sufficient for unit test isolation; confirm mocks are used for all external system interactions (FR-002 safety)
- [ ] T011 [US1] Implement contract tests for MockExecutor vs RemoteExecutor interface parity in tests/contract/test_executor_contract.py per FR-003a

**Checkpoint**: User Story 1 complete - unit tests run fast and safe on any machine

---

## Phase 4: User Story 2 - Integration Test Suite with VM Isolation (Priority: P1)

**Goal**: Developers can run integration tests on isolated Hetzner VMs with automatic provisioning and baseline reset

**Independent Test**: Run `uv run pytest -m integration -v` with VM environment variables configured

### Implementation for User Story 2

> **Note**: All US2 fixtures go in tests/integration/conftest.py only. The root tests/conftest.py remains unchanged (unit test fixtures only).

- [ ] T012 [P] [US2] Create VM connection fixtures (pc1_connection, pc2_connection) in tests/integration/conftest.py per research.md §7
- [ ] T013 [P] [US2] Create VM executor fixtures (pc1_executor, pc2_executor) wrapping RemoteExecutor in tests/integration/conftest.py
- [ ] T014 [US2] Add session-scoped fixture for lock acquisition and release in tests/integration/conftest.py
- [ ] T015 [US2] Add session-scoped fixture that handles: (1) lock acquisition, (2) VM existence check with auto-provisioning if VMs don't exist (or skip with clear message if secrets unavailable), (3) VM reset to baseline before test session in tests/integration/conftest.py

**Checkpoint**: User Story 2 complete - integration test fixtures ready for use

---

## Phase 5: User Story 3 - CI/CD Integration (Priority: P2)

**Goal**: CI runs unit tests on every push and integration tests on PRs to main with concurrency control

**Independent Test**: Push code changes and verify CI workflows execute correctly

### Implementation for User Story 3

- [ ] T016 [US3] Create .github/workflows/test.yml with lint-and-unit job running on every push per research.md §6
- [ ] T017 [US3] Add integration job to .github/workflows/test.yml with: fork detection, secret availability check (skip with clear notice per FR-017a when HCLOUD_TOKEN or SSH key unavailable), and concurrency group per research.md §6; job runs `uv run pytest -m integration` (fixtures handle provisioning/reset automatically)
- [ ] T018 [US3] Configure artifact upload for pytest output and provisioning logs in .github/workflows/test.yml
- [ ] T019 [US3] Add workflow_dispatch trigger for manual integration test runs in .github/workflows/test.yml

**Checkpoint**: User Story 3 complete - CI/CD pipeline operational

---

## Phase 6: User Story 4 - Manual Testing Playbook (Priority: P3)

**Goal**: Documented playbook for visual verification and feature tour

**Independent Test**: Follow the playbook document and confirm all visual elements render correctly

### Implementation for User Story 4

- [ ] T020 [US4] Create docs/testing-playbook.md with visual verification steps (progress bars, colors, formatting)
- [ ] T021 [US4] Add feature tour section to docs/testing-playbook.md covering all major pc-switcher features

**Checkpoint**: User Story 4 complete - manual playbook ready for release verification

---

## Phase 7: User Story 5 - Developer Guide for Integration Testing (Priority: P2)

**Goal**: Documentation enabling developers to write integration tests without reverse-engineering

**Independent Test**: A new developer can write a working integration test using only the guide

### Implementation for User Story 5

- [ ] T022 [P] [US5] Create docs/testing-developer-guide.md with test file creation and VM fixture usage patterns
- [ ] T023 [P] [US5] Add VM interaction patterns section (SSH, file transfer, btrfs, snapshots) to docs/testing-developer-guide.md
- [ ] T024 [US5] Add test organization section (directory structure, naming, markers) to docs/testing-developer-guide.md
- [ ] T025 [US5] Add troubleshooting section for common integration test failures to docs/testing-developer-guide.md
- [ ] T025a [US5] Add local development setup section documenting how to configure secrets for local integration test runs (environment variables mirroring CI secrets per FR-025) to docs/testing-developer-guide.md

**Checkpoint**: User Story 5 complete - developer documentation ready

---

## Phase 8: User Story 6 - Operational Guide for Test Infrastructure (Priority: P2)

**Goal**: Documentation for sysadmins to configure and maintain test infrastructure

**Independent Test**: Someone can set up test infrastructure from scratch using only the documentation

### Implementation for User Story 6

- [ ] T026 [P] [US6] Create docs/testing-ops-guide.md with CI secrets configuration (HCLOUD_TOKEN, HETZNER_SSH_PRIVATE_KEY)
- [ ] T027 [P] [US6] Add VM provisioning instructions (hcloud CLI setup, SSH key management) to docs/testing-ops-guide.md
- [ ] T028 [US6] Add environment variables documentation to docs/testing-ops-guide.md per data-model.md
- [ ] T029 [US6] Add cost monitoring section (VM costs, destruction/reprovisioning) to docs/testing-ops-guide.md
- [ ] T030 [US6] Add runbooks for common failure scenarios to docs/testing-ops-guide.md per spec.md edge cases

**Checkpoint**: User Story 6 complete - operational documentation ready

---

## Phase 9: User Story 7 - Architecture Documentation (Priority: P2)

**Goal**: Architecture documentation explaining design decisions and component interactions

**Independent Test**: Someone unfamiliar with the framework can explain its structure after reading

### Implementation for User Story 7

- [ ] T031 [US7] Update docs/testing-framework.md with three-tier test structure diagram (Mermaid)
- [ ] T032 [US7] Add component interaction diagram (Mermaid format per FR-030) showing VM, lock, and CI relationships in docs/testing-framework.md
- [ ] T033 [US7] Add design decision rationale (VM isolation, Hetzner labels lock, btrfs reset) to docs/testing-framework.md
- [ ] T034 [US7] Add links to ADR-006 and related decision records in docs/testing-framework.md

**Checkpoint**: User Story 7 complete - architecture documentation ready

---

## Phase 10: Polish & Cross-Cutting Concerns

**Purpose**: Final validation and cleanup

- [ ] T035 Validate all scripts are executable and have proper error handling
- [ ] T036 Run quickstart.md validation - verify all copy-paste commands work
- [ ] T037 Update docs/testing-framework.md navigation links to new documentation files
- [ ] T038 Verify pytest configuration with `uv run pytest --collect-only` to confirm marker behavior

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **User Stories (Phase 3+)**: All depend on Foundational phase completion
  - US1 and US2 are both P1 and can proceed in parallel
  - US3-US7 can proceed in priority order or in parallel if staffed
- **Polish (Phase 10)**: Depends on all user stories being complete

### User Story Dependencies

- **User Story 1 (P1)**: Can start after Foundational - independent
- **User Story 2 (P1)**: Can start after Foundational - depends on scripts from Phase 2
- **User Story 3 (P2)**: Can start after Phase 2 scripts exist - uses provision/reset scripts
- **User Story 4 (P3)**: Independent - documentation only
- **User Story 5 (P2)**: Can start after US2 fixtures exist (references them)
- **User Story 6 (P2)**: Independent - documentation referencing Phase 2 scripts
- **User Story 7 (P2)**: Independent - documentation only

### Within Each User Story

- Tasks marked [P] can run in parallel (different files)
- Non-[P] tasks have implicit ordering within the story

### Parallel Opportunities

- All Setup tasks marked [P] can run in parallel
- T004-T008 (Foundational): T005+T006 can run in parallel after T004 starts
- T012+T013 (US2): VM fixtures can be created in parallel
- T022+T023 (US5): Developer guide sections can be written in parallel
- T026+T027 (US6): Ops guide sections can be written in parallel

---

## Parallel Example: Foundational Phase

```bash
# After T004 starts (provision-vms.sh structure exists):
Task: "Create tests/infrastructure/scripts/configure-vm.sh"
Task: "Create tests/infrastructure/scripts/configure-hosts.sh"

# Sequential after both complete:
Task: "Create tests/infrastructure/scripts/reset-vm.sh"
Task: "Create tests/infrastructure/scripts/lock.sh"
```

---

## Implementation Strategy

### MVP First (User Stories 1 + 2 Only)

1. Complete Phase 1: Setup (pytest configuration)
2. Complete Phase 2: Foundational (VM infrastructure scripts)
3. Complete Phase 3: User Story 1 (unit test verification)
4. Complete Phase 4: User Story 2 (integration fixtures)
5. **STOP and VALIDATE**: Run `uv run pytest tests/unit tests/contract -v` and `uv run pytest -m integration -v`

### Incremental Delivery

1. Setup + Foundational → Infrastructure ready
2. Add US1 + US2 → Test framework functional (MVP!)
3. Add US3 → CI/CD pipeline operational
4. Add US4-US7 → Documentation complete
5. Polish → Production ready

### Parallel Team Strategy

With multiple developers:

1. Team completes Setup + Foundational together
2. Once Foundational is done:
   - Developer A: User Story 1 + User Story 2 (test infrastructure)
   - Developer B: User Story 3 (CI/CD)
   - Developer C: User Stories 4-7 (documentation)
3. All work integrates independently

---

## Summary

| Metric | Value |
|--------|-------|
| Total tasks | 39 |
| Phase 1 (Setup) | 3 tasks |
| Phase 2 (Foundational) | 5 tasks |
| User Story 1 (P1) | 3 tasks |
| User Story 2 (P1) | 4 tasks |
| User Story 3 (P2) | 4 tasks |
| User Story 4 (P3) | 2 tasks |
| User Story 5 (P2) | 5 tasks |
| User Story 6 (P2) | 5 tasks |
| User Story 7 (P2) | 4 tasks |
| Phase 10 (Polish) | 4 tasks |
| Parallel opportunities | 14 tasks marked [P] |
| MVP scope | Phases 1-4 (15 tasks) |

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- Tests NOT included as not explicitly requested in spec.md
- Existing provision.sh script preserved; new scripts complement it
- All infrastructure scripts go in tests/infrastructure/scripts/
- All documentation goes in docs/
