# Specification Review 4

**Reviewer**: Gemini 3 Pro (Preview)
**Date**: 2025-12-05
**Spec Version**: 2025-12-05 (Draft)

## General Feedback

The specification is comprehensive and well-structured. It clearly defines the three-tier testing strategy and addresses the critical need for isolated integration testing involving system-level operations (btrfs, etc.). The focus on safety (VM isolation) and developer experience (fast unit tests, guided manual tests) is excellent.

However, there are a few areas regarding the shared infrastructure model, locking mechanics, and local execution prerequisites that need clarification to ensure the implementation phase goes smoothly.

## Specific Feedback & Questions

### 1. Shared vs. Dedicated Infrastructure & Locking (FR-005, FR-012)
The spec implies a shared infrastructure model (implied by the low cost constraint in FR-012 and the locking requirement in FR-005).
* **Ambiguity**: If the VMs are shared between CI and multiple developers, the locking mechanism is the single point of failure/contention.
* **Question**: Where does the `TestLock` live?
    * If it resides on the VM (e.g., a lock file), we have a race condition during provisioning (if the VM doesn't exist yet).
    * If it resides in the cloud (e.g., a tag on the VM or a separate cloud resource), it requires cloud API access just to check the lock.
* **Recommendation**: Clarify the storage location of the lock. If it's on the VM, address how "concurrent provisioning" (FR-006) is handled between a Developer and CI (FR-006 says local concurrent provisioning is "not checked"). This seems risky if a Developer tries to provision while CI is doing the same.

### 2. Cloud Provider & Tooling (FR-006, FR-012)
* **Observation**: FR-012 sets a strict cost limit (< 10 EUR/month). This strongly suggests a specific provider (likely Hetzner or a low-cost VPS provider) rather than AWS/GCP/Azure which are typically more expensive for persistent VMs.
* **Recommendation**: Explicitly mention the target cloud provider (or the requirement to select one). This decision impacts the implementation of the provisioning logic significantly. If the choice is left to the implementation phase, it should be noted as an open decision, but given the cost constraint, the options are limited.

### 3. Contract Tests Definition (FR-007)
* **Observation**: FR-007 mentions `tests/contract` in the command, but "Contract Tests" are not defined in the "Functional Requirements > Test Structure" section.
* **Recommendation**: Add a definition for Contract Tests. Presumably, these ensure that `MockExecutor` and the real `LocalExecutor`/`RemoteExecutor` adhere to the same behavioral interface. Explicitly stating this ensures the mocks remain reliable.

### 4. Local Execution & Secrets (FR-006a, User Story 2)
* **Observation**: User Story 2 allows developers to run integration tests locally. FR-006a mentions injecting keys from "CI secrets".
* **Gap**: For local runs, developers need equivalent secrets (Cloud API tokens, SSH keys).
* **Recommendation**: Explicitly require a mechanism for local secret management (e.g., loading from a `.env` file or `~/.config/pc-switcher/test-secrets.yaml`) and ensure the Developer Guide (FR-025) covers setting this up.

### 5. VM Lifecycle (FR-012)
* **Observation**: FR-012 mentions "continuous run basis".
* **Question**: Is the intention to keep VMs running 24/7 to save startup time (and just reset snapshots), or to destroy them to save money?
    * If 24/7: The "Provisioning" step is rare.
    * If On-Demand: The "Provisioning" step is frequent, and startup time becomes a factor for User Story 2 (P1).
* **Recommendation**: Clarify the intended default lifecycle. "Continuous run basis" in FR-012 might just be a pricing benchmark, but it's ambiguous.

### 6. VMExecutor vs. RemoteExecutor
* **Observation**: `VMExecutor` is defined as providing a "RemoteExecutor-like interface".
* **Recommendation**: Clarify if `VMExecutor` is a wrapper around the actual `RemoteExecutor` (to test the production code) or a test-specific implementation. Ideally, integration tests should use the production `RemoteExecutor` as much as possible to verify it works correctly with the real SSH connection.

### 7. Python Version
* **Recommendation**: Explicitly state that the testing framework must run on Python 3.14, consistent with the project's `AGENTS.md` and `ADR-003`.

## Minor Suggestions
* **FR-017b**: "Integration tests on forked PRs are NOT supported". Consider adding that they should be marked as "Skipped" rather than "Failed" to keep the PR green (if that's the intent).
