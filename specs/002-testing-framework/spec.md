# Feature Specification: Testing Framework

**Feature Branch**: `002-testing-framework`  
**Created**: 2025-12-05  
**Status**: Draft  
**Input**: User description: "Implement testing framework with three-tier testing (unit, integration, manual)"

## Navigation

Documentation Hierarchy:
- [High level requirements](../../High%20level%20requirements.md) - Project vision and scope
- [Architecture Decision Records](../../docs/adr/_index.md) - Cross-cutting architectural decisions
- [ADR-006: Testing Framework](../../docs/adr/adr-006-testing-framework.md) - Testing architecture decision
- Specification (this document) - Detailed requirements for this feature

Pre-analysis References:
- [Testing Framework Documentation](../../docs/testing-framework.md) - Architecture and Implementation details
- [Testing Implementation Plan](pre-analysis/testing-implementation-plan.md) - Detailed implementation plan  
  This document covers both the testing framework and retroactive tests for existing functionality. This feature is only about the framework itself.

## Clarifications

### Session 2025-12-05

- Q: Which documentation deliverables should be in scope? → A: Developer guide + operational guide + architecture/design documentation
- Q: Where should documentation files live? → A: Separate files: `docs/testing-framework.md` (architecture), `docs/testing-developer-guide.md`, `docs/testing-ops-guide.md`

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

### User Story 5 - Developer Guide for Integration Testing (Priority: P2)

As a pc-switcher developer writing tests, I have comprehensive documentation that explains how to write integration tests, work with test VMs, use fixtures, handle test isolation, and follow established patterns. This enables me to contribute tests without reverse-engineering existing code.

**Why this priority**: P2 because developers can initially learn from existing test examples, but formal documentation prevents knowledge silos and reduces onboarding friction.

**Independent Test**: Can be verified by having a new developer follow the guide to write a new integration test without additional guidance.

**Acceptance Scenarios**:

1. **Given** I want to write an integration test, **When** I read the developer guide, **Then** I find step-by-step instructions for creating a new test file, using VM fixtures, and asserting outcomes

2. **Given** I need to understand VM interaction patterns, **When** I consult the guide, **Then** I find documented patterns for SSH execution, file transfers, btrfs operations, and snapshot management on test VMs

3. **Given** I want to understand test organization, **When** I read the guide, **Then** I find clear explanations of the test directory structure, naming conventions, and pytest markers

4. **Given** I need to debug a failing integration test, **When** I consult the troubleshooting section, **Then** I find guidance on common failure modes, how to inspect VM state, and how to manually reproduce issues

---

### User Story 6 - Operational Guide for Test Infrastructure (Priority: P2)

As a sysadmin/maintainer/devops engineer, I have comprehensive documentation for configuring and maintaining the test framework infrastructure. This includes secrets management, environment variables, VM provisioning configuration, and CI pipeline setup, and cost monitoring.

**Why this priority**: P2 because initial setup can be done with direct guidance, but documented procedures ensure maintainability and disaster recovery capability.

**Independent Test**: Can be verified by having someone set up the test infrastructure from scratch using only the documentation.

**Acceptance Scenarios**:

1. **Given** I need to configure CI secrets, **When** I read the operational guide, **Then** I find a complete list of required secrets, their purposes, and where to obtain/generate them

2. **Given** I need to set up test VM infrastructure, **When** I follow the guide, **Then** I find step-by-step provisioning instructions including cloud provider configuration, network setup, and SSH key management

3. **Given** I need to understand environment variables, **When** I consult the guide, **Then** I find documentation of all environment variables, their defaults, and their effects on test behavior

4. **Given** I need to monitor infrastructure costs, **When** I read the guide, **Then** I find instructions for tracking VM costs and procedures for destroying/reprovisioning infrastructure

5. **Given** test infrastructure fails, **When** I consult the troubleshooting section, **Then** I find runbooks for common failure scenarios (VM unreachable, provisioning failures, lock stuck)

---

### User Story 7 - Architecture Documentation for Testing Framework (Priority: P2)

As a pc-switcher developer or maintainer, I have architecture documentation that explains the testing framework design decisions, component interactions, and rationale. This enables me to understand why the system is structured as it is and make informed decisions when extending or modifying it.

**Why this priority**: P2 because the codebase can initially be understood through reading, but architecture documentation accelerates comprehension and ensures design intent is preserved across changes.

**Independent Test**: Can be verified by having someone unfamiliar with the testing framework explain its architecture after reading only the documentation.

**Acceptance Scenarios**:

1. **Given** I want to understand the testing architecture, **When** I read the architecture documentation, **Then** I find diagrams and descriptions of the three-tier test structure and how components interact

2. **Given** I want to understand design decisions, **When** I consult the documentation, **Then** I find rationale for key choices (VM isolation, locking mechanism, baseline reset strategy)

3. **Given** I want to extend the testing framework, **When** I read the documentation, **Then** I find guidance on extension points and architectural constraints to respect

4. **Given** the testing framework references ADR-006, **When** I navigate from architecture docs, **Then** I find clear links to the ADR and related decision records

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

## Requirements

### Functional Requirements

#### Test Structure

- **FR-001**: System MUST provide three-tier test structure: unit tests (fast, no external dependencies), integration tests (require isolated VMs), and manual playbook (visual verification and feature tour)

- **FR-002**: Unit tests MUST be safe to run on any machine; they MUST NOT execute real system-modifying commands, require network access to external services, or depend on specific filesystem types

- **FR-003**: Integration tests MUST run on isolated test infrastructure that is separate from developer machines and CI runners, preventing any possibility of damaging production systems

- **FR-004**: Test VMs MUST be reset to a clean baseline state before each integration test session, ensuring test isolation and reproducibility

- **FR-005**: Concurrent test runs MUST be prevented via a locking mechanism to avoid interference between parallel executions

- **FR-006**: Test VMs MUST be automatically provisioned when a developer runs integration tests for the first time or when VMs do not exist

#### Unit Test Requirements

- **FR-007**: Unit tests MUST be runnable with single command `uv run pytest tests/unit tests/contract -v`

- **FR-008**: Integration tests MUST be selectable via pytest marker for running separately from unit tests

#### Test VM Requirements

- **FR-009**: Test VMs MUST be configured with the same OS and filesystem type that pc-switcher targets (Ubuntu 24.04 LTS, btrfs)

- **FR-010**: Test VMs MUST have a test user account with sudo access for running privileged operations during tests

- **FR-011**: Test VMs MUST be able to communicate with each other via SSH to simulate source-to-target sync scenarios

- **FR-012**: VMs and related test infrastructure costs MUST remain under EUR 10/month on continuous run basis

#### CI/CD Requirements

- **FR-013**: CI MUST run type checks, lint checks and unit tests on every push to any branch

- **FR-014**: CI MUST run integration tests on PRs to main branch

- **FR-015**: CI MUST support manual trigger for running integration tests on any branch

- **FR-016**: CI MUST prevent parallel integration test runs through concurrency control

- **FR-017**: CI MUST reset test VMs before running integration tests

#### Manual Playbook Requirements

- **FR-018**: Manual playbook MUST document steps to verify all visual UI elements (progress bars, colors, formatting)

- **FR-019**: Manual playbook MUST provide a guided tour of all major pc-switcher features with expected behavior explanations

- **FR-020**: Manual playbook MUST be usable for both release verification and developer onboarding

#### Documentation Requirements

- **FR-021**: Developer guide MUST document how to write integration tests including test file creation, VM fixture usage, and assertion patterns

- **FR-022**: Developer guide MUST document VM interaction patterns for SSH execution, file transfers, btrfs operations, and snapshot management

- **FR-023**: Developer guide MUST document test organization including directory structure, naming conventions, and pytest markers

- **FR-024**: Developer guide MUST include troubleshooting section for common integration test failures

- **FR-025**: Operational guide MUST document all required CI secrets, their purposes, and how to obtain/generate them

- **FR-026**: Operational guide MUST provide step-by-step VM provisioning instructions including cloud provider configuration and SSH key management

- **FR-027**: Operational guide MUST document all environment variables, their defaults, and effects on test behavior

- **FR-028**: Operational guide MUST document cost monitoring procedures and infrastructure destruction/reprovisioning

- **FR-029**: Operational guide MUST include runbooks for common infrastructure failure scenarios

- **FR-030**: Architecture documentation MUST include diagrams describing the three-tier test structure and component interactions

- **FR-031**: Architecture documentation MUST explain rationale for key design decisions (VM isolation, locking, baseline reset)

- **FR-032**: Architecture documentation MUST provide links to ADR-006 and related decision records

- **FR-033**: Documentation MUST be organized as separate files: `docs/testing-framework.md` (architecture), `docs/testing-developer-guide.md` (developer guide), `docs/testing-ops-guide.md` (operational guide)

### Key Entities

- **TestVM**: Represents an isolated VM for integration testing; has network identity, SSH access, required filesystem and subvolume configuration, and baseline state for reset
- **TestLock**: Represents the mechanism preventing concurrent test runs; has holder identity and acquisition time
- **TestSession**: Represents a single test run; has session ID, lock holder, VM reset status, and test results
- **MockExecutor**: Represents a mocked executor for unit tests; provides predictable command responses without real execution
- **TestFixture**: Represents a pytest fixture providing test resources; includes VM connections, event buses, temporary files, and cleanup logic

## Success Criteria

### Measurable Outcomes

- **SC-001**: VM reset to clean baseline completes in under 30 seconds for all VMs

- **SC-002**: CI pipeline executes unit tests on 100% of pushes and integration tests on 100% of PRs to main

- **SC-003**: Lock mechanism successfully prevents concurrent test runs in 100% of contention scenarios

- **SC-004**: Manual playbook covers all visual elements for release verification

- **SC-005**: Test infrastructure costs remain under EUR 10/month

- **SC-006**: Developer guide enables a new developer to write a working integration test without additional guidance

- **SC-007**: Operational guide enables infrastructure setup from scratch without additional guidance

- **SC-008**: Architecture documentation enables someone unfamiliar with the testing framework to explain its structure after reading

## Assumptions

- Cloud provider account is available with sufficient credits for VM provisioning
- GitHub repository has GitHub Actions enabled
- Developers have SSH key pairs for VM access
- Network connectivity allows SSH to cloud provider IPs

## Out of Scope

- Writing actual tests for specific features
- Automated visual testing (terminal colors, progress bars require manual verification)
- Load testing or performance benchmarking
- Security penetration testing
- Automated VM cost optimization (manual destruction when not needed)
- Test coverage for third-party libraries (only test project code)
