# Feature Specification: Testing Framework

**Feature Branch**: `002-testing-framework`  
**Created**: 2025-12-05  
**Status**: Draft  
**Input**: User description: "Implement testing framework with three-tier testing (unit, integration, manual) and create retroactive tests for all existing 001-foundation functionality"

## Navigation

Documentation Hierarchy:
- [High level requirements](../../High%20level%20requirements.md) - Project vision and scope
- [Architecture Decision Records](../../docs/adr/_index.md) - Cross-cutting architectural decisions
- [ADR-006: Testing Framework](../../docs/adr/adr-006-testing-framework.md) - Testing architecture decision
- Specification (this document) - Detailed requirements for this feature

Pre-analysis References:
- [Testing Framework Documentation](../../docs/testing-framework.md) - Architecture and Implementation details
- [Testing Implementation Plan](../../docs/adr/considerations/testing-implementation-plan.md) - Detailed implementation plan
- [Testing Report](../../docs/adr/considerations/testing-report.md) - Coverage analysis

## User Scenarios & Testing

### User Story 1 - Unit Test Suite for Fast Feedback (Priority: P1)

As a pc-switcher developer, I can run unit tests locally to verify my changes before pushing code. These tests execute quickly, require no external infrastructure, and are safe to run on any development machine. The tests cover all pure logic and business rules without requiring real external systems.

**Why this priority**: P1 because fast local testing is essential for development velocity. Unit tests catch logic errors early without requiring VM infrastructure.

**Independent Test**: Can be fully tested by running `uv run pytest tests/unit tests/contract -v` on any developer machine.

**Acceptance Scenarios**:

1. **Given** I have cloned the repository, **When** I run `uv run pytest tests/unit tests/contract -v`, **Then** all unit tests execute within 30 seconds without requiring any external services or configuration

2. **Given** I modify business logic code, **When** I run unit tests, **Then** any logic errors are detected through failing assertions before code is pushed

3. **Given** unit tests do not depend on external systems, **When** tests execute, **Then** they produce deterministic results regardless of the host environment

4. **Given** unit tests exist for a module, **When** I run them, **Then** both success paths and failure paths (error handling, edge cases) are tested

---

### User Story 2 - Integration Test Suite with VM Isolation (Priority: P1)

As a pc-switcher developer, I can run integration tests that exercise real system operations (btrfs snapshots, SSH connections, full workflows) on isolated test VMs. The VMs are automatically provisioned when needed and reset to a clean baseline before each test session, ensuring tests cannot damage my machine and each run starts fresh.

**Why this priority**: P1 because pc-switcher performs destructive system operations. Testing with mocks would hide real-world issues. VM isolation provides safety while enabling realistic testing.

**Independent Test**: Can be fully tested by running `uv run pytest tests/integration -v -m integration`.

**Acceptance Scenarios**:

1. **Given** I want to run integration tests, **When** I execute the integration test command, **Then** test VMs are automatically provisioned if not already available, and tests execute real system operations on the VMs

2. **Given** I start an integration test session, **When** the test framework initializes, **Then** all test VMs are reset to a clean baseline state, removing any artifacts from previous sessions

3. **Given** I start integration tests while another run is in progress, **When** I attempt to run, **Then** the framework either waits for the lock or fails with a clear message identifying the current holder

4. **Given** integration tests exist for a feature, **When** I run them, **Then** both success paths and failure paths (error handling, edge cases) are tested

---

### User Story 3 - CI/CD Integration (Priority: P2)

As a pc-switcher developer, I can rely on CI to automatically run tests on my code changes. Unit tests run on every push for immediate feedback. Integration tests run on PRs to main and can be manually triggered on feature branches when I need to verify integration behavior before merging.

**Why this priority**: P2 because manual test execution is acceptable during initial development. CI automation is essential for team collaboration and preventing regressions.

**Independent Test**: Can be fully tested by pushing code changes and observing CI workflows execute correctly.

**Acceptance Scenarios**:

1. **Given** I push to any branch, **When** CI triggers, **Then** type checks, lint checks and unit tests run automatically

2. **Given** I open a PR targeting main branch, **When** CI triggers, **Then** both unit tests and integration tests run

3. **Given** I'm working on a feature branch that requires integration testing, **When** I manually trigger the integration test workflow, **Then** integration tests run against my feature branch

4. **Given** multiple integration test runs could occur simultaneously, **When** concurrency is detected, **Then** the framework ensures only one integration test run executes at a time

---

### User Story 4 - Manual Testing Playbook (Priority: P3)

As a pc-switcher developer, I have a documented playbook that serves two purposes: (1) verifying visual elements that cannot be automated (terminal colors, progress bars, rich formatting), and (2) providing a guided tour of pc-switcher features so I understand how the system works in practice.

**Why this priority**: P3 because visual verification and feature familiarization are only needed before releases or when onboarding. Core functionality testing via automated tests is more important for daily development.

**Independent Test**: Can be verified by following the playbook document and confirming all visual elements render correctly and all features are demonstrated.

**Acceptance Scenarios**:

1. **Given** I follow the manual playbook, **When** I run the specified commands, **Then** I can verify progress bar appearance, color-coded log levels, and rich console formatting

2. **Given** I am new to pc-switcher or preparing a release, **When** I follow the playbook, **Then** I experience a guided tour of all major features with explanations of expected behavior

3. **Given** the playbook is documented, **When** preparing for a release, **Then** I can systematically verify all visual elements and feature behaviors without missing any major cases

---

### User Story 5 - Spec-Driven Test Coverage for 001-Foundation (Priority: P1)

As a pc-switcher developer, I have comprehensive tests that verify 100% of the specifications defined in specs/001-foundation/spec.md. Tests are written based on the spec (user stories, acceptance scenarios, functional requirements), not the implementation code. If any part of the spec was not implemented or implemented incorrectly, the tests fail.

**Why this priority**: P1 because the existing foundation code is critical infrastructure. Bugs could break entire systems. Spec-driven tests ensure the implementation matches the documented requirements and catch gaps or deviations.

**Independent Test**: Can be verified by running the full test suite and confirming tests exist for every user story, acceptance scenario, and functional requirement in the 001-foundation spec.

**Acceptance Scenarios**:

1. **Given** tests are implemented based on specs/001-foundation/spec.md, **When** I run the full test suite, **Then** 100% of user stories have corresponding test coverage

2. **Given** tests are implemented based on specs/001-foundation/spec.md, **When** I run the full test suite, **Then** 100% of acceptance scenarios have corresponding test cases

3. **Given** tests are implemented based on specs/001-foundation/spec.md, **When** I run the full test suite, **Then** 100% of functional requirements have corresponding test assertions

4. **Given** a part of the spec was not implemented or implemented incorrectly, **When** I run the tests, **Then** the relevant tests fail, exposing the gap or bug

5. **Given** tests cover both success and failure paths, **When** I run the full test suite, **Then** error handling, edge cases, and boundary conditions from the spec are all verified

---

### Edge Cases

- What happens when test VMs are not provisioned?
  - Framework automatically provisions VMs before running integration tests; developer does not need to manually set up infrastructure
- What happens when VMs are unreachable during integration tests?
  - Tests fail with clear SSH connection error; lock is released; framework suggests checking VM status or re-provisioning (framework provides instructions to destroy VMs; re-provisioning will be automatic)
- What happens when VM reset fails?
  - Reset process exits with error; tests do not proceed on dirty state; framework provides instructions to destroy VMs (re-provisioning will be automatic)
- What happens when lock cannot be acquired after timeout?
  - Test run fails with "Failed to acquire lock" error showing current holder identity
- What happens when CI secrets are misconfigured?
  - Integration test job fails early with authentication error; unit tests still pass
- What happens when tests find a gap between spec and implementation?
  - Tests fail with clear assertion messages indicating which spec requirement is not met

## Requirements

### Functional Requirements

#### Test Structure

- **FR-001**: System MUST provide three-tier test structure: unit tests (fast, no external dependencies), integration tests (require isolated VMs), and manual playbook (visual verification and feature tour)

- **FR-002**: Unit tests MUST be safe to run on any machine; they MUST NOT execute real system-modifying commands, require network access to external services, or depend on specific filesystem types

- **FR-003**: Integration tests MUST run on isolated test infrastructure that is separate from developer machines and CI runners, preventing any possibility of damaging production systems

- **FR-004**: Test VMs MUST be reset to a clean baseline state before each integration test session, ensuring test isolation and reproducibility

- **FR-005**: Concurrent test runs MUST be prevented via a locking mechanism to avoid interference between parallel executions

- **FR-006**: Test VMs MUST be automatically provisioned when a developer runs integration tests for the first time or when VMs do not exist

#### Test Coverage Requirements

- **FR-007**: Tests MUST cover 100% of user stories defined in specs/001-foundation/spec.md

- **FR-008**: Tests MUST cover 100% of acceptance scenarios defined in specs/001-foundation/spec.md

- **FR-009**: Tests MUST cover 100% of functional requirements defined in specs/001-foundation/spec.md

- **FR-010**: Tests MUST verify both success paths and failure paths (error handling, edge cases, boundary conditions) for each requirement

- **FR-011**: Unit tests MUST complete full suite execution in under 30 seconds

- **FR-012**: Unit tests MUST be runnable with single command `uv run pytest tests/unit tests/contract -v`

- **FR-013**: Integration tests MUST be selectable via pytest marker for running separately from unit tests

#### Test VM Requirements

- **FR-014**: Test VMs MUST be configured with the same OS and filesystem type that pc-switcher targets (Ubuntu 24.04 LTS, btrfs)

- **FR-015**: Test VMs MUST have a test user account with sudo access for running privileged operations during tests

- **FR-016**: Test VMs MUST be able to communicate with each other via SSH to simulate source-to-target sync scenarios

- **FR-017**: VMs and related test infrastructure costs MUST remain under EUR 10/month on continuous run basis

#### CI/CD Requirements

- **FR-018**: CI MUST run type checks, lint checks and unit tests on every push to any branch

- **FR-019**: CI MUST run integration tests on PRs to main branch

- **FR-020**: CI MUST support manual trigger for running integration tests on any branch

- **FR-021**: CI MUST prevent parallel integration test runs through concurrency control

- **FR-022**: CI MUST reset test VMs before running integration tests

#### Manual Playbook Requirements

- **FR-023**: Manual playbook MUST document steps to verify all visual UI elements (progress bars, colors, formatting)

- **FR-024**: Manual playbook MUST provide a guided tour of all major pc-switcher features with expected behavior explanations

- **FR-025**: Manual playbook MUST be usable for both release verification and developer onboarding

### Key Entities

- **TestVM**: Represents an isolated VM for integration testing; has network identity, SSH access, required filesystem and subvolume configuration, and baseline state for reset
- **TestLock**: Represents the mechanism preventing concurrent test runs; has holder identity and acquisition time
- **TestSession**: Represents a single test run; has session ID, lock holder, VM reset status, and test results
- **MockExecutor**: Represents a mocked executor for unit tests; provides predictable command responses without real execution
- **TestFixture**: Represents a pytest fixture providing test resources; includes VM connections, event buses, temporary files, and cleanup logic

## Success Criteria

### Measurable Outcomes

- **SC-001**: Unit test suite executes completely in under 30 seconds on a standard development machine

- **SC-002**: Integration tests complete full VM-based testing in under 15 minutes

- **SC-003**: VM reset to clean baseline completes in under 30 seconds for all VMs

- **SC-004**: 100% of user stories in specs/001-foundation/spec.md have corresponding test coverage

- **SC-005**: 100% of acceptance scenarios in specs/001-foundation/spec.md have corresponding test cases

- **SC-006**: 100% of functional requirements in specs/001-foundation/spec.md have corresponding test assertions

- **SC-007**: All tests verify both success and failure paths as specified in the requirements

- **SC-008**: CI pipeline executes unit tests on 100% of pushes and integration tests on 100% of PRs to main

- **SC-009**: Lock mechanism successfully prevents concurrent test runs in 100% of contention scenarios

- **SC-010**: Manual playbook covers all visual elements for release verification

- **SC-011**: Test infrastructure costs remain under EUR 10/month

## Assumptions

- Cloud provider account is available with sufficient credits for VM provisioning
- GitHub repository has GitHub Actions enabled
- Developers have SSH key pairs for VM access
- Network connectivity allows SSH to cloud provider IPs

## Out of Scope

- Testing of features beyond 001-foundation (those features don't exist yet)
- Automated visual testing (terminal colors, progress bars require manual verification)
- Load testing or performance benchmarking
- Security penetration testing
- Automated VM cost optimization (manual destruction when not needed)
- Test coverage for third-party libraries (only test project code)
