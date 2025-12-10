# Architecture Review: Testing Framework Plan

**Reviewer**: GitHub Copilot (Architecture)
**Date**: 2025-12-06
**Feature**: 002-testing-framework

## Summary

The proposed architecture is generally sound, pragmatic, and well-aligned with the project's "Deliberate Simplicity" and "Reliability" principles. The choice of btrfs snapshots for fast resets and the three-tier testing strategy are excellent design decisions.

However, there is one **critical architectural flaw** regarding the locking mechanism that renders the concurrency control ineffective.

## Critical Issues

### 1. Lock Persistence vs. VM Reset Strategy
**Severity**: Critical (Breaks FR-005 and SC-003)

The plan proposes storing the lock file at `/tmp/pc-switcher-integration-test.lock` on the `pc1` VM. It also proposes resetting `pc1` by rolling back the root filesystem to a baseline snapshot and rebooting.

**The Problem**:
1. **Reboot clears `/tmp`**: Since `/tmp` is typically a `tmpfs` (RAM-based), the lock file will be lost immediately upon reboot.
2. **Snapshot Rollback reverts state**: Even if the lock were stored on persistent disk (e.g., `/root/lock`), the snapshot rollback restores the filesystem to the "Baseline" state. The baseline state (created at provisioning) does **not** contain the lock file.
3. **Result**: As soon as the test session resets the VMs (which happens *after* acquiring the lock but *before* running the tests), the lock is destroyed. A second CI job could then acquire the "missing" lock and start its own reset/test cycle, causing a collision.

**Recommendation**:
The lock state must be stored **externally** to the state being reset.
* **Option A (Recommended)**: Use **Hetzner Server Labels**. Use the `hcloud` CLI to add a label (e.g., `lock_holder=<job_id>`) to the `pc1` server object. This state is stored in the Hetzner control plane, survives reboots/rollbacks, and is accessible via the existing `hcloud` CLI.
* **Option B**: Use a separate, non-reset btrfs subvolume or partition on `pc1` specifically for lock state. This adds complexity to the provisioning and reset scripts.

## Robustness & Reliability

### 2. Flaky Reset Wait Mechanism
**Severity**: Moderate (Affects Reliability)

The `reset-vm.sh` script uses `sleep 20` to wait for the VM to come back online after reboot. Fixed sleeps are a common source of CI flakiness (too short = failure, too long = waste).

**Recommendation**:
Replace `sleep 20` with a polling loop that attempts to connect via SSH until successful (with a timeout). This ensures the tests start exactly when the VM is ready.

### 3. Instance Type Availability
**Severity**: Minor

The plan mentions `cx23` instance type. As of late 2024/2025, the standard Hetzner Cloud Intel types are `cx22`, `cx32`, etc., and AMD are `cpx11`, `cpx21`. `cx23` may not exist or might be a typo.

**Recommendation**:
Verify the instance type. `cx22` or `cax11` (Arm64, if supported by dependencies) are likely the intended targets for the <€10 budget.

## Validation

### 4. Architecture Soundness
**Status**: Approved (with fixes)

Apart from the locking issue, the architecture is excellent:
* **Isolation**: Using real VMs with btrfs snapshots provides the perfect balance of realism and speed.
* **Simplicity**: Avoiding Terraform and complex orchestration for a 2-VM setup is the right call.
* **Interface**: The `RemoteExecutor` fixture design correctly decouples tests from the transport, enabling the Contract Tests (FR-003a) to be meaningful.

Once the locking mechanism is moved to an external store (like Hetzner Labels), this plan is ready for implementation.
