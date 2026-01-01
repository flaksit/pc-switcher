# Testing Framework Specification

**Domain**: Testing Infrastructure
**Source**: `specs/002-testing-framework`

## Overview

This document specifies the testing framework for pc-switcher, including the three-tier test structure (unit, integration, manual), VM infrastructure, and CI/CD integration.

## Requirements

### Test Structure

- **TST-FR-001**: System MUST provide three-tier test structure: unit tests, integration tests, and manual playbook.
- **TST-FR-002**: Unit tests MUST be safe to run on any machine without external dependencies.
- **TST-FR-003**: Integration tests MUST run on isolated test infrastructure (VMs).
- **TST-FR-003a**: Contract tests MUST verify that MockExecutor and real executor implementations adhere to the same interface.
- **TST-FR-004**: Test VMs MUST be reset to a clean baseline state before each integration test session.
- **TST-FR-005**: Concurrent test runs MUST be prevented via a locking mechanism.
- **TST-FR-006**: Test VMs MUST be automatically provisioned when integration tests are run and VMs do not exist.
- **TST-FR-006a**: During auto-provisioning, SSH keys MUST be injected from CI secrets.

### Unit Test Requirements

- **TST-FR-007**: Unit tests MUST be runnable with single command `uv run pytest tests/unit tests/contract -v`.
- **TST-FR-008**: Integration tests MUST be selectable via pytest marker (`-m integration`).
- **TST-FR-008a**: Integration tests MUST NOT run by default.
- **TST-FR-008b**: When integration tests are explicitly requested but VM environment variables are not configured, tests MUST be skipped.

### Test VM Requirements

- **TST-FR-009**: Test VMs MUST be configured with Ubuntu 24.04 LTS and btrfs.
- **TST-FR-010**: Test VMs MUST have a test user account with sudo access.
- **TST-FR-011**: Test VMs MUST be able to communicate with each other via SSH.
- **TST-FR-011a**: Test infrastructure MUST consist of exactly two VMs (pc1 and pc2).
- **TST-FR-012**: VMs and related test infrastructure costs MUST remain under EUR 10/month.

### CI/CD Requirements

- **TST-FR-013**: CI MUST run type checks, lint checks, and unit tests on every push.
- **TST-FR-014**: CI MUST run integration tests on PRs to main branch.
- **TST-FR-015**: CI MUST support manual trigger for running integration tests.
- **TST-FR-016**: CI MUST prevent parallel integration test runs.
- **TST-FR-017**: CI MUST reset test VMs before running integration tests.
- **TST-FR-017a**: CI MUST skip integration tests when secrets are unavailable.
- **TST-FR-017b**: Integration tests on forked PRs are NOT supported.
- **TST-FR-017c**: CI MUST preserve test logs and artifacts.

### Manual Playbook Requirements

- **TST-FR-018**: Manual playbook MUST document steps to verify all visual UI elements.
- **TST-FR-019**: Manual playbook MUST provide a guided tour of all major features.
- **TST-FR-020**: Manual playbook MUST be usable for release verification and onboarding.

### Documentation Requirements

- **TST-FR-021**: Developer guide MUST document how to write integration tests.
- **TST-FR-022**: Developer guide MUST document VM interaction patterns.
- **TST-FR-023**: Developer guide MUST document test organization.
- **TST-FR-024**: Developer guide MUST include troubleshooting section.
- **TST-FR-025**: Operational guide MUST document all required CI secrets.
- **TST-FR-026**: Operational guide MUST provide VM provisioning instructions.
- **TST-FR-027**: Operational guide MUST document all environment variables.
- **TST-FR-028**: Operational guide MUST document cost monitoring procedures.
- **TST-FR-029**: Operational guide MUST include runbooks for failure scenarios.
- **TST-FR-030**: Architecture documentation MUST include diagrams of test structure.
- **TST-FR-031**: Architecture documentation MUST explain rationale for key design decisions.
- **TST-FR-032**: Architecture documentation MUST provide links to ADR-006.
- **TST-FR-033**: Documentation MUST be organized as separate files.

### Test Fixture Requirements

- **TST-FR-034**: Testing framework MUST provide minimal pytest fixtures for VM command execution.

## Data Model

### TestVM

Represents a Hetzner Cloud VM used for integration testing.

| Field | Type | Description |
|-------|------|-------------|
| name | string | VM name (e.g., "pc1") |
| ipv4_address | string | Public IPv4 address |
| server_type | string | Hetzner server type (e.g., "cx23") |
| location | string | Datacenter location (e.g., "fsn1") |
| ssh_user | string | SSH user for test access ("testuser") |
| baseline_snapshot_root | string | Path to baseline @ snapshot |
| baseline_snapshot_home | string | Path to baseline @home snapshot |

### TestLock

Represents the concurrency control mechanism.

| Field | Type | Description |
|-------|------|-------------|
| holder | string | Lock holder identity |
| acquired | datetime | Timestamp when lock was acquired |
| hostname | string | Machine that acquired the lock |

### TestSession

Represents a single integration test run.

| Field | Type | Description |
|-------|------|-------------|
| session_id | string | Unique identifier |
| holder | string | Same as TestLock.holder |
| vm_reset_status | enum | pending, in_progress, completed, failed |
| lock_acquired | boolean | Whether lock was successfully acquired |
| test_results | object | pytest exit code and summary |
