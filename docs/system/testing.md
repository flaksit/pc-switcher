# Testing Framework

This document defines the current testing framework requirements and architecture for pc-switcher.

## User Scenarios & Testing

### User Story 1 - Unit Test Suite for Fast Feedback (TST-US-UNIT)

As a pc-switcher developer, I can run unit tests locally to verify my changes before pushing code. These tests execute quickly, require no external infrastructure, and are safe to run on any development machine. The tests cover all pure logic and business rules without requiring real external systems.

**Why this priority**: P1 because fast local testing is essential for development velocity. Unit tests catch logic errors early without requiring VM infrastructure.

**Independent Test**: Can be fully tested by running `uv run pytest tests/unit tests/contract -v` on any developer machine.

**Acceptance Scenarios**:

1. **Given** I have cloned the repository, **When** I run `uv run pytest tests/unit tests/contract -v`, **Then** all unit tests execute within 30 seconds without requiring any external services or configuration

2. **Given** I modify business logic code, **When** I run unit tests, **Then** any logic errors are detected through failing assertions before code is pushed

3. **Given** unit tests do not depend on external systems, **When** tests execute, **Then** they produce deterministic results regardless of the host environment

4. **Given** unit tests exist for a module, **When** I run them, **Then** both success paths and failure paths (error handling, edge cases) are tested

Lineage: 002-US-1

### User Story 2 - Integration Test Suite with VM Isolation (TST-US-INTEGRATION)

As a pc-switcher developer, I can run integration tests that exercise real system operations (btrfs snapshots, SSH connections, full workflows) on isolated test VMs. The VMs are automatically provisioned when needed (including OS installation with btrfs filesystem) and reset to a clean baseline before each test session, ensuring tests cannot damage my machine and each run starts fresh.

**Why this priority**: P1 because pc-switcher performs destructive system operations. Testing with mocks would hide real-world issues. VM isolation provides safety while enabling realistic testing.

**Independent Test**: Can be fully tested by running `uv run pytest tests/integration -v -m integration`.

**Acceptance Scenarios**:

1. **Given** I want to run integration tests, **When** I execute the integration test command, **Then** test VMs are automatically provisioned if not already available (including OS installation with btrfs filesystem), and tests execute real system operations on the VMs

2. **Given** I start an integration test session, **When** the test framework initializes, **Then** all test VMs are reset to a clean baseline state, removing any artifacts from previous sessions; if baseline snapshots are missing or invalid, the reset fails with an actionable error message

3. **Given** I start integration tests while another run is in progress, **When** I attempt to run, **Then** the framework either waits for the lock or fails with a clear message identifying the current holder

4. **Given** integration tests exist for a feature, **When** I run them, **Then** both success paths and failure paths (error handling, edge cases) are tested

Lineage: 002-US-2

### User Story 3 - CI/CD Integration (TST-US-CI)

As a pc-switcher developer, I can rely on CI to automatically run tests on my code changes. Unit tests run on every push for immediate feedback. Integration tests run on PRs to main and can be manually triggered on feature branches when I need to verify integration behavior before merging.

**Why this priority**: P2 because manual test execution is acceptable during initial development. CI automation is essential for team collaboration and preventing regressions.

**Independent Test**: Can be fully tested by pushing code changes and observing CI workflows execute correctly.

**Acceptance Scenarios**:

1. **Given** I push to any branch, **When** CI triggers, **Then** type checks, lint checks and unit tests run automatically

2. **Given** I open a PR targeting main branch, **When** CI triggers, **Then** both unit tests and integration tests run

3. **Given** I'm working on a feature branch that requires integration testing, **When** I manually trigger the integration test workflow, **Then** integration tests run against my feature branch

4. **Given** multiple integration test runs could occur simultaneously, **When** concurrency is detected, **Then** the framework ensures only one integration test run executes at a time

Lineage: 002-US-3

### User Story 4 - Manual Testing Playbook (TST-US-PLAYBOOK)

As a pc-switcher developer, I have a documented playbook that serves two purposes: (1) verifying visual elements that cannot be automated (terminal colors, progress bars, rich formatting), and (2) providing a guided tour of pc-switcher features so I understand how the system works in practice.

**Why this priority**: P3 because visual verification and feature familiarization are only needed before releases or when onboarding. Core functionality testing via automated tests is more important for daily development.

**Independent Test**: Can be verified by following the playbook document and confirming all visual elements render correctly and all features are demonstrated.

**Acceptance Scenarios**:

1. **Given** I follow the manual playbook, **When** I run the specified commands, **Then** I can verify progress bar appearance, color-coded log levels, and rich console formatting

2. **Given** I am new to pc-switcher or preparing a release, **When** I follow the playbook, **Then** I experience a guided tour of all major features with explanations of expected behavior

3. **Given** the playbook is documented, **When** preparing for a release, **Then** I can systematically verify all visual elements and feature behaviors without missing any major cases

Lineage: 002-US-4

### User Story 5 - Developer Guide for Integration Testing (TST-US-DEV-GUIDE)

As a pc-switcher developer writing tests, I have comprehensive documentation that explains how to write integration tests, work with test VMs, use fixtures, handle test isolation, and follow established patterns. This enables me to contribute tests without reverse-engineering existing code.

**Why this priority**: P2 because developers can initially learn from existing test examples, but formal documentation prevents knowledge silos and reduces onboarding friction.

**Independent Test**: Can be verified by having a new developer follow the guide to write a new integration test without additional guidance.

**Acceptance Scenarios**:

1. **Given** I want to write an integration test, **When** I read the developer guide, **Then** I find step-by-step instructions for creating a new test file, using VM fixtures, and asserting outcomes

2. **Given** I need to understand VM interaction patterns, **When** I consult the guide, **Then** I find documented patterns for SSH execution, file transfers, btrfs operations, and snapshot management on test VMs

3. **Given** I want to understand test organization, **When** I read the guide, **Then** I find clear explanations of the test directory structure, naming conventions, and pytest markers

4. **Given** I need to debug a failing integration test, **When** I consult the troubleshooting section, **Then** I find guidance on common failure modes, how to inspect VM state, and how to manually reproduce issues

Lineage: 002-US-5

### User Story 6 - Operational Guide for Test Infrastructure (TST-US-OPS-GUIDE)

As a sysadmin/maintainer/devops engineer, I have comprehensive documentation for configuring and maintaining the test framework infrastructure. This includes secrets management, environment variables, VM provisioning configuration, and CI pipeline setup, and cost monitoring.

**Why this priority**: P2 because initial setup can be done with direct guidance, but documented procedures ensure maintainability and disaster recovery capability.

**Independent Test**: Can be verified by having someone set up the test infrastructure from scratch using only the documentation.

**Acceptance Scenarios**:

1. **Given** I need to configure CI secrets, **When** I read the operational guide, **Then** I find a complete list of required secrets, their purposes, and where to obtain/generate them

2. **Given** I need to set up test VM infrastructure, **When** I follow the guide, **Then** I find step-by-step provisioning instructions including cloud provider configuration, network setup, and SSH key management

3. **Given** I need to understand environment variables, **When** I consult the guide, **Then** I find documentation of all environment variables, their defaults, and their effects on test behavior

4. **Given** I need to monitor infrastructure costs, **When** I read the guide, **Then** I find instructions for tracking VM costs and procedures for destroying/reprovisioning infrastructure

5. **Given** test infrastructure fails, **When** I consult the troubleshooting section, **Then** I find runbooks for common failure scenarios (VM unreachable, provisioning failures, lock stuck)

Lineage: 002-US-6

### User Story 7 - Architecture Documentation (TST-US-ARCH-DOC)

As a pc-switcher developer or maintainer, I have architecture documentation that explains the testing framework design decisions, component interactions, and rationale. This enables me to understand why the system is structured as it is and make informed decisions when extending or modifying it.

**Why this priority**: P2 because the codebase can initially be understood through reading, but architecture documentation accelerates comprehension and ensures design intent is preserved across changes.

**Independent Test**: Can be verified by having someone unfamiliar with the testing framework explain its architecture after reading only the documentation.

**Acceptance Scenarios**:

1. **Given** I want to understand the testing architecture, **When** I read the architecture documentation, **Then** I find diagrams and descriptions of the three-tier test structure and how components interact

2. **Given** I want to understand design decisions, **When** I consult the documentation, **Then** I find rationale for key choices (VM isolation, locking mechanism, baseline reset strategy)

3. **Given** I want to extend the testing framework, **When** I read the documentation, **Then** I find guidance on extension points and architectural constraints to respect

4. **Given** the testing framework references ADR-006, **When** I navigate from architecture docs, **Then** I find clear links to the ADR and related decision records

Lineage: 002-US-7

## Requirements

### Functional Requirements

#### Test Structure

- **TST-FR-THREE-TIER**: System MUST provide three-tier test structure: unit tests (fast, no external dependencies), integration tests (require isolated VMs), and manual playbook (visual verification and feature tour)
  Lineage: 002-FR-001

- **TST-FR-UNIT-SAFE**: Unit tests MUST be safe to run on any machine; they MUST NOT execute real system-modifying commands, require network access to external services, or depend on specific filesystem types
  Lineage: 002-FR-002

- **TST-FR-INT-ISOLATED**: Integration tests MUST run on isolated test infrastructure that is separate from developer machines and CI runners, preventing any possibility of damaging production systems
  Lineage: 002-FR-003

- **TST-FR-CONTRACT**: Contract tests MUST verify that MockExecutor and real executor implementations (LocalExecutor, RemoteExecutor) adhere to the same behavioral interface, ensuring mocks remain reliable representations of production behavior
  Lineage: 002-FR-003a

- **TST-FR-RESET**: Test VMs MUST be reset to a clean baseline state before each integration test session, ensuring test isolation and reproducibility; reset MUST fail with an actionable error if baseline snapshots are missing or invalid
  Lineage: 002-FR-004

- **TST-FR-LOCK**: Concurrent test runs MUST be prevented via a locking mechanism that stores holder identity and acquisition time; stuck locks require manual cleanup (documented in operational guide)
  Lineage: 002-FR-005

- **TST-FR-PROVISION**: Test VMs MUST be automatically provisioned when integration tests are run and VMs do not exist; provisioning includes cloud VM creation and OS installation with btrfs filesystem; baseline snapshots MUST be created at provisioning time; concurrent provisioning is prevented by CI concurrency controls (local concurrent provisioning is not supported and not checked)
  Lineage: 002-FR-006

- **TST-FR-SSH-INJECT**: During auto-provisioning, SSH keys MUST be injected from CI secrets; the test user accounts on both VMs MUST have the public key in authorized_keys to enable CI access. Keys for inter-VM communication can be generated during provisioning because not needed by anyone.
  Lineage: 002-FR-006a

#### Unit Test Requirements

- **TST-FR-UNIT-CMD**: Unit tests MUST be runnable with single command `uv run pytest tests/unit tests/contract -v`
  Lineage: 002-FR-007

- **TST-FR-INT-MARKER**: Integration tests MUST be selectable via pytest marker (`-m integration`) for running separately from unit tests
  Lineage: 002-FR-008

- **TST-FR-INT-DEFAULT**: Integration tests MUST NOT run by default; `uv run pytest` (without explicit `-m integration` marker) MUST NOT run integration tests even if VM environment variables are configured
  Lineage: 002-FR-008a

- **TST-FR-INT-SKIP**: When integration tests are explicitly requested (`-m integration`) but VM environment variables are not configured, tests MUST be skipped (not failed) with a clear message
  Lineage: 002-FR-008b

#### Test VM Requirements

- **TST-FR-VM-OS**: Test VMs MUST be configured with the same OS and filesystem type that pc-switcher targets (Ubuntu 24.04 LTS, btrfs)
  Lineage: 002-FR-009

- **TST-FR-VM-SUDO**: Test VMs MUST have a test user account with sudo access for running privileged operations during tests
  Lineage: 002-FR-010

- **TST-FR-VM-SSH**: Test VMs MUST be able to communicate with each other via SSH to simulate source-to-target sync scenarios
  Lineage: 002-FR-011

- **TST-FR-VM-TWO**: Test infrastructure MUST consist of exactly two VMs (pc1 and pc2) to mirror the real pc-switcher sync architecture; each VM MUST have btrfs root filesystem with `@` and `@home` subvolumes
  Lineage: 002-FR-011a

- **TST-FR-COST**: VMs and related test infrastructure costs MUST remain under EUR 10/month on continuous run basis; VMs are expected to remain running persistently (reset via btrfs snapshots, not reprovisioning); manual destruction is acceptable when extended downtime is expected
  Lineage: 002-FR-012

#### CI/CD Requirements

- **TST-FR-CI-PUSH**: CI MUST run type checks (basedpyright), lint checks (ruff), and unit tests (pytest) on every push to any branch
  Lineage: 002-FR-013

- **TST-FR-CI-PR**: CI MUST run integration tests on PRs to main branch (from the main repository only)
  Lineage: 002-FR-014

- **TST-FR-CI-MANUAL**: CI MUST support manual trigger for running integration tests on any branch
  Lineage: 002-FR-015

- **TST-FR-CI-CONCUR**: CI MUST prevent parallel integration test runs through concurrency control
  Lineage: 002-FR-016

- **TST-FR-CI-RESET**: CI MUST reset test VMs before running integration tests
  Lineage: 002-FR-017

- **TST-FR-CI-SKIP-FORK**: CI MUST skip integration tests with a clear notice when secrets are unavailable (e.g., forked PRs); unit tests MUST still run in this case
  Lineage: 002-FR-017a

- **TST-FR-FORK-NOTICE**: Integration tests on forked PRs are NOT supported; CI MUST skip (not fail) integration tests and clearly indicate this when a fork PR is detected
  Lineage: 002-FR-017b

- **TST-FR-CI-ARTIFACTS**: CI MUST preserve test logs and artifacts (pytest output, provisioning logs, reset logs) to enable debugging of failed runs
  Lineage: 002-FR-017c

#### Manual Playbook Requirements

- **TST-FR-PLAY-VISUAL**: Manual playbook MUST document steps to verify all visual UI elements (progress bars, colors, formatting)
  Lineage: 002-FR-018

- **TST-FR-PLAY-TOUR**: Manual playbook MUST provide a guided tour of all major pc-switcher features with expected behavior explanations
  Lineage: 002-FR-019

- **TST-FR-PLAY-DUAL**: Manual playbook MUST be usable for both release verification and developer onboarding
  Lineage: 002-FR-020

#### Documentation Requirements

- **TST-FR-DOC-WRITE**: Developer guide MUST document how to write integration tests including test file creation, VM fixture usage, and assertion patterns
  Lineage: 002-FR-021

- **TST-FR-DOC-VM**: Developer guide MUST document VM interaction patterns for SSH execution, file transfers, btrfs operations, and snapshot management
  Lineage: 002-FR-022

- **TST-FR-DOC-ORG**: Developer guide MUST document test organization including directory structure, naming conventions, and pytest markers
  Lineage: 002-FR-023

- **TST-FR-DOC-DEBUG**: Developer guide MUST include troubleshooting section for common integration test failures
  Lineage: 002-FR-024

- **TST-FR-OPS-SECRETS**: Operational guide MUST document all required CI secrets, their purposes, and how to obtain/generate them; Developer guide MUST document how to configure equivalent secrets for local integration test runs (e.g., environment variables or config file)
  Lineage: 002-FR-025

- **TST-FR-OPS-PROVISION**: Operational guide MUST provide step-by-step VM provisioning instructions including cloud provider configuration and SSH key management
  Lineage: 002-FR-026

- **TST-FR-OPS-ENV**: Operational guide MUST document all environment variables, their defaults, and effects on test behavior
  Lineage: 002-FR-027

- **TST-FR-OPS-COST**: Operational guide MUST document cost monitoring procedures and infrastructure destruction/reprovisioning
  Lineage: 002-FR-028

- **TST-FR-OPS-RUNBOOK**: Operational guide MUST include runbooks for common infrastructure failure scenarios
  Lineage: 002-FR-029

- **TST-FR-ARCH-DIAGRAM**: Architecture documentation MUST include diagrams describing the three-tier test structure and component interactions; all diagrams MUST be in Mermaid format (per repository documentation standards)
  Lineage: 002-FR-030

- **TST-FR-ARCH-RATIONALE**: Architecture documentation MUST explain rationale for key design decisions (VM isolation, locking, baseline reset)
  Lineage: 002-FR-031

- **TST-FR-ARCH-ADR**: Architecture documentation MUST provide links to ADR-006 and related decision records
  Lineage: 002-FR-032

- **TST-FR-DOC-FILES**: Documentation MUST be organized as separate files: `docs/testing-framework.md` (architecture), `docs/testing-developer-guide.md` (developer guide), `docs/testing-ops-guide.md` (operational guide), `docs/testing-playbook.md` (manual playbook)
  Lineage: 002-FR-033

#### Test Fixture Requirements

- **TST-FR-FIXTURES**: Testing framework MUST provide minimal pytest fixtures for VM command execution, enabling integration tests to run commands on test VMs via a RemoteExecutor-like interface
  Lineage: 002-FR-034

### Key Entities

- **TestVM**: Represents an isolated VM for integration testing; has network identity, SSH access, required filesystem and subvolume configuration, and baseline state for reset
- **TestLock**: Represents the mechanism preventing concurrent test runs; has holder identity and acquisition time
- **TestSession**: Represents a single test run; has session ID, lock holder, VM reset status, and test results
- **MockExecutor**: Represents a mocked executor for unit tests; provides predictable command responses without real execution
- **TestFixture**: Represents a pytest fixture providing test resources; includes VM connections, event buses, temporary files, and cleanup logic
- **VMExecutor**: Represents a fixture for executing commands on test VMs; provides a RemoteExecutor-like interface for integration tests to run commands on source/target VMs

Lineage: 002-Key-Entities

## Success Criteria

- **TST-SC-FAST-RESET**: VM reset to clean baseline is fast due to btrfs snapshot rollback (no cloud VM snapshot restore or reprovisioning required)
  Lineage: 002-SC-001

- **TST-SC-CI-100**: CI pipeline executes unit tests on 100% of pushes and integration tests on 100% of PRs to main (from main repository)
  Lineage: 002-SC-002

- **TST-SC-LOCK**: Lock mechanism successfully prevents concurrent test runs in 100% of contention scenarios
  Lineage: 002-SC-003

- **TST-SC-PLAYBOOK**: Manual playbook covers all visual elements for release verification
  Lineage: 002-SC-004

- **TST-SC-COST**: Test infrastructure costs remain under EUR 10/month (see TST-FR-COST for details)
  Lineage: 002-SC-005

- **TST-SC-DEV-GUIDE**: Developer guide enables a new developer to write a working integration test without additional guidance
  Lineage: 002-SC-006

- **TST-SC-OPS-GUIDE**: Operational guide enables infrastructure setup from scratch without additional guidance
  Lineage: 002-SC-007

- **TST-SC-ARCH-EXPLAIN**: Architecture documentation enables someone unfamiliar with the testing framework to explain its structure after reading
  Lineage: 002-SC-008

## Edge Cases

- **Test VMs not provisioned**: Framework automatically provisions VMs before running integration tests (including OS installation with btrfs filesystem); developer does not need to manually set up infrastructure

- **Baseline snapshots missing or invalid**: Reset process fails immediately with actionable error: "Baseline snapshot missing. Run provisioning to create baseline snapshots."; tests do not proceed

- **VMs unreachable during integration tests**: Tests fail with clear SSH connection error; lock is released; framework suggests checking VM status or re-provisioning (framework provides instructions to destroy VMs; re-provisioning will be automatic)

- **VM reset fails**: Reset process exits with error; tests do not proceed on dirty state; framework provides instructions to destroy VMs (re-provisioning will be automatic)

- **Lock cannot be acquired after timeout**: Test run fails with "Failed to acquire lock" error showing current holder identity; stuck locks require manual cleanup

- **Test run crashes without releasing the lock**: Lock remains held; subsequent runs fail after timeout; manual cleanup is required (documented in operational guide)

- **CI secrets misconfigured or missing**: Integration tests are skipped with clear notice; unit tests still run and pass

- **PR from a forked repository**: Integration tests are skipped with clear notice (secrets are not available to forks); unit tests still run

- **VM environment variables not set locally**:
  - If integration tests are explicitly requested (`-m integration`), they are skipped with clear message: "Skipping integration tests: VM environment not configured"
  - If running `uv run pytest` without markers, integration tests are never run regardless of env var state

Lineage: 002-Edge-Cases

## Assumptions

- Cloud provider account is available with sufficient credits for VM provisioning
- GitHub repository has GitHub Actions enabled
- Developers have SSH key pairs for VM access
- Network connectivity allows SSH to cloud provider IPs
- Python 3.14 is used (per ADR-003)

Lineage: 002-Assumptions

## Out of Scope

- Writing actual tests for specific features
- Automated visual testing (terminal colors, progress bars require manual verification)
- Load testing or performance benchmarking
- Security penetration testing
- Automated VM cost optimization (manual destruction when not needed)
- Test coverage for third-party libraries (only test project code)
- Test data generation fixtures (helpers for creating specific file patterns, permissions, etc. for sync tests) - to be added when actual tests are written; note: minimal fixtures for VM command execution ARE in scope (see TST-FR-FIXTURES)

Lineage: 002-Out-of-Scope
