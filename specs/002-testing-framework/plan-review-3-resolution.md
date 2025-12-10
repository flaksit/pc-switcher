# Plan Review 3 Resolution

**Feature**: 002-testing-framework
**Review File**: plan-review-3-architecture-gemini3.md
**Date**: 2025-12-06

## Arguments Passed to Command

```
For 1: A
For 2: we already use a good method in the provision.sh script outlined testing-implementation-plan.md
For 3: Ignore
```

## Resolution Log

### Point 1: Lock Persistence vs. VM Reset Strategy (Critical)

**Issue**: The plan proposes storing the lock file at `/tmp/pc-switcher-integration-test.lock` on the `pc1` VM. However:
1. Reboot clears `/tmp` (it's typically a tmpfs)
2. Snapshot rollback restores filesystem to baseline state which doesn't contain the lock file
3. Result: Lock is destroyed during reset, allowing concurrent CI jobs to collide

**User Decision**: Use Option A - Hetzner Server Labels

**Resolution**: Update lock.sh to use `hcloud server add-label` and `hcloud server remove-label` instead of file-based locking. The lock state will be stored on the Hetzner control plane, which survives VM reboots and snapshot rollbacks.

**Changes to plan.md**:
- Updated `lock.sh` script to use Hetzner Server Labels
- Updated Complexity Tracking table to reflect this decision
- Updated `tests/integration/conftest.py` fixture to use new lock mechanism

---

### Point 2: Flaky Reset Wait Mechanism (Moderate)

**Issue**: Reviewer stated that `reset-vm.sh` uses `sleep 20` to wait for VM to come back online, which is flaky.

**User Response**: "we already use a good method in the provision.sh script outlined testing-implementation-plan.md"

**Resolution**: The reviewer was looking at `research.md` line 186, which had a bare `sleep 20`. The actual implementation in `testing-implementation-plan.md` already uses the correct polling loop approach. Updated `research.md` to match:

```bash
# Wait for VM to come back online (polling loop, not fixed sleep)
sleep 15
until ssh -o ConnectTimeout=5 -o BatchMode=yes root@$VM true 2>/dev/null; do
    sleep 5
done
```

This is the same pattern used in `provision.sh` as the user noted.

---

### Point 3: Instance Type Availability (Minor)

**Issue**: `cx23` instance type may not exist or be a typo. Standard types are `cx22`, `cx32`, etc.

**User Decision**: Ignore: cx23 exists.

**Resolution**: No changes made. The user has decided to keep `cx23` as-is. This can be corrected during actual implementation if the instance type doesn't exist.

---

## Summary of Changes Made

### plan.md
- Updated Complexity Tracking table: Lock mechanism now "Hetzner Server Labels"
- Updated "Reliability Without Compromise" section: references external lock storage
- Updated "Deliberate Simplicity" section: references Hetzner Labels instead of lock file

### pre-analysis/testing-implementation-plan.md
- Rewrote `lock.sh` script to use `hcloud server add-label` / `hcloud server remove-label`
- Added `status` action for checking current lock holder
- Updated integration conftest.py fixture comments

### data-model.md
- Updated TestLock entity storage from file path to Hetzner Server Labels
- Updated state diagram (removed "VM reboot auto-cleanup", added "manual cleanup")
- Updated validation rules for stuck lock cleanup

### quickstart.md
- Updated troubleshooting section for "Failed to acquire lock" with new commands

### contracts/README.md
- Updated Lock Contract section with new label-based format and operations

### research.md
- Rewrote Section 5 (Lock Mechanism) with new decision and rationale
- Added explanation of why file-based lock was rejected
- Updated lock operations examples
- Fixed Section 4 reset script: replaced `sleep 20` with polling loop
