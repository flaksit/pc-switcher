# ADR-006: Testing Framework

Status: Accepted
Date: 2025-12-05

## TL;DR
Three-tier testing: unit tests (fast, safe anywhere), integration tests (VM-isolated for destructive operations), manual playbook (visual verification).

## Implementation Rules
- Unit tests must be safe to run on any machine without isolation
- Integration tests must only run inside dedicated test VMs
- Test VMs must be reset to a clean baseline before each test run
- Concurrent test runs (dev vs CI) must be prevented via locking

## Context
PC-switcher executes system commands as root, modifies filesystems, creates/deletes btrfs snapshots, and syncs files between machines. Bugs in the implementation or in the tests themselves could damage or destroy the systems involved in testing. Safe testing requires isolation from the developer's machine.

## Decision

**VM isolation for integration tests:**
- Dedicated test VMs provide a safe environment for destructive operations
- Two VMs (pc1 + pc2) mirror the real source→target architecture
- Real btrfs filesystem and SSH connections instead of mocks

**Persistent VMs with snapshot-based reset:**
- VMs remain running between test runs (not created/destroyed each time)
- Btrfs snapshot rollback resets VMs to clean baseline state before each run
- Fast reset (~30s) enables rapid iteration

**Lock-based isolation:**
- Shared VM infrastructure requires concurrency control
- Lock prevents simultaneous dev and CI test runs

**Three-tier test structure:**
- **Unit tests**: Fast, no external dependencies, safe to run anywhere, every commit
- **Integration tests**: Require VM isolation, run on-demand and on PRs to main
- **Manual playbook**: Visual verification only (terminal colors, progress bars)

## Consequences

**Positive:**
- Tests cannot damage developer machines or CI infrastructure
- Real btrfs and SSH testing catches issues that mocks would hide
- Fast snapshot reset enables quick iteration despite using full VMs

**Negative:**
- Integration tests require access to running VMs
- Initial VM setup is a manual provisioning step
- Small ongoing infrastructure cost

## References
- [Testing Framework Documentation](../testing-framework.md)
- [Architecture Discussion](considerations/testing-framework-architecture-conversation.txt)
