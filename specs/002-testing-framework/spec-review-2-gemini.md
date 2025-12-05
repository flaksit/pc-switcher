# Specification Review 2

## 1. Locking Mechanism & Provisioning Race Condition

**Observation**: FR-005 requires a locking mechanism to prevent concurrent runs. FR-006 requires automatic provisioning if VMs don't exist.
**Issue**: If the lock is stored *on* the VM (as implied by "Shared VM infrastructure"), there is a race condition during the provisioning phase. If two processes (e.g., CI and a developer) start simultaneously when no VMs exist, both will see "no VMs", both will try to provision, and there is no lock to check yet.
**Suggestion**: Clarify the locking strategy for the *provisioning* phase. Does the lock exist independently of the VMs (e.g., Cloud API tag, separate storage)? Or do we accept a race condition during the rare provisioning event?
**Recommendation**: Add a requirement or clarification that the locking mechanism must handle the "VM does not exist" state, possibly by using the Cloud Provider API as the source of truth for the lock (e.g., checking for a specific "lock" resource or tag).

## 2. Provider Agnosticism vs "Rescue Mode"

**Observation**: Resolution 1, Point 9 states "Hetzner is an implementation detail... Not here." However, FR-006 was updated to explicitly require "OS installation with btrfs filesystem via rescue mode".
**Issue**: "Rescue mode" is a specific terminology and mechanism often associated with providers like Hetzner. Other providers might use "User Data", "Custom Images", or "ISO mounting".
**Suggestion**: Generalize FR-006 to remove "via rescue mode" and replace it with "via custom OS installation mechanism" or similar, to maintain the provider-agnostic intent of the specification.

## 3. Resource Lifecycle & Deterministic Naming

**Observation**: FR-006 mandates automatic provisioning. SC-005 mandates low cost.
**Issue**: To ensure cost control and reuse of VMs (instead of creating new ones indefinitely), the VMs must have deterministic names or identifiers. The spec implies this but doesn't explicitly require it.
**Suggestion**: Add a requirement (e.g., FR-006a) that Test VMs must use deterministic naming (e.g., `pc-switcher-test-source`, `pc-switcher-test-target`) to ensure the framework reconnects to existing VMs instead of provisioning new ones, and to prevent resource leaks.

## 4. Test Data Generation Fixtures

**Observation**: The spec focuses heavily on infrastructure (VMs, Reset, Locking).
**Issue**: Integration tests for a sync tool will heavily rely on creating specific file patterns (large files, deep directories, symlinks, specific permissions) to verify sync behavior.
**Suggestion**: Consider adding a requirement (e.g., in the Developer Guide section FR-021 or a new Functional Requirement) for the framework to provide "Test Data Fixtures" or helpers. This would standardize how tests create "dirty" states to be synced, rather than every test writing its own `dd` or `touch` commands.

## 5. SSH Key Distribution during Auto-Provisioning

**Observation**: FR-006 requires auto-provisioning. FR-011 requires SSH communication.
**Issue**: When the framework auto-provisions a VM, it needs to inject SSH keys so that:
1. The Test Runner (Dev/CI) can access the VM.
2. The Source VM can access the Target VM (for sync).
**Suggestion**: Add a requirement or clarification on how SSH keys are handled during auto-provisioning. Does the framework generate ephemeral keys? Does it upload existing keys from the environment? This is critical for the "seamless" User Story 2.
