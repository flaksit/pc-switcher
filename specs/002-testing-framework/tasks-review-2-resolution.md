# Tasks Review 2 Resolution

**Review file**: tasks-review-2-gemini3.md
**Resolved by**: Claude
**Date**: 2025-12-06

## Arguments

Gemini gave feedback on tasks.md as well. They reviewed at the same time as the previous review we resolved, so it is possible they bring up similar feedback that we already addressed.

## Conversation Log

No clarifying questions were needed. All issues were low-severity clarifications that I addressed directly.

## Analysis

The Gemini review identified 4 low-severity issues. Here's my analysis:

### E1 (LOW) - Local secrets configuration in Dev Guide (FR-025)
**Status**: Already addressed in Codex review (tasks-review-1-resolution.md)

We added T025a during the previous resolution: "Add local development setup section documenting how to configure secrets for local integration test runs (environment variables mirroring CI secrets per FR-025)"

**Action**: None needed.

### B1 (LOW) - SSH key responsibilities ambiguity between T005 and T006
**Status**: Addressed

T005 was updated to clearly say "CI/user SSH public key injection" (keys for external access).
T006 was updated to clearly say "generate/exchange inter-VM SSH keys for pc1↔pc2 communication" (keys for VM-to-VM communication).

**Action**: Updated T005 and T006 descriptions.

### C1 (LOW) - "Baseline services" undefined in T005
**Status**: Addressed

Added "(sshd enabled)" to clarify what baseline services means.

**Action**: Updated T005 description.

### C2 (LOW) - T004 doesn't mention orchestrating existing provision.sh
**Status**: Addressed

Added "orchestrates existing provision.sh for OS installation with btrfs" to T004.

**Action**: Updated T004 description.

## Resolutions Applied

### tasks.md Changes

| Issue | Task | Change |
|-------|------|--------|
| C2 | T004 | Added "orchestrates existing provision.sh for OS installation with btrfs" |
| C1 | T005 | Changed "baseline services" to "baseline services (sshd enabled)" |
| B1 | T005 | Changed "SSH keys (including CI-provided public key injection per FR-006a)" to "CI/user SSH public key injection (per FR-006a)" |
| B1 | T006 | Changed "inter-VM SSH access" to "generate/exchange inter-VM SSH keys for pc1↔pc2 communication" |
| E1 | - | Already addressed by T025a from Codex review |

## Additional Decision: Script Architecture Refactoring

After the initial resolution, user questioned the script structure through several iterations:

### First Question: Two vs One Script

**User**: "T004 suggests that we should have two distinct scripts. Is there a reason why not to bundle this in a single script?"

**Initial Resolution**: Agreed to merge into single unified `provision-vms.sh`.

### Second Question: T005 and T006 Separation

**User**: "Shouldn't T005 and T006 write to the same script as well?"

**Resolution**: Clarified the difference:
- `configure-vm.sh` operates on a single host (called twice, in parallel)
- `configure-hosts.sh` operates on both hosts together (needs both to be configured first)

### Third Question: Proper Separation of Concerns

**User**: "Actually, shouldn't we have the same split for T004? A script that provisions a single VM. And then a second orchestrator script that calls this first script twice, in parallel... Which of all these scripts makes the baseline btrfs snapshots?"

**User**: "For consistency, we should create a separate script for the baseline snapshots. The orchestrator only calls other scripts."

**User**: "Script names: provision-vm.sh and provision-vms.sh are too similar and do different things. Come up with clearly distinct names."

**Final Resolution**: Complete refactoring to single-responsibility scripts:

| Script | Responsibility | Called by |
|--------|---------------|-----------|
| `create-vm.sh` | Creates single VM via hcloud + installs OS with btrfs | Orchestrator (2x parallel) |
| `configure-vm.sh` | Configures single VM: testuser, SSH keys, services | Orchestrator (2x parallel) |
| `configure-hosts.sh` | Configures both VMs: /etc/hosts, inter-VM SSH keys | Orchestrator (1x) |
| `create-baseline-snapshots.sh` | Creates baseline btrfs snapshots on both VMs | Orchestrator (1x) |
| `provision-test-infra.sh` | Orchestrator: calls all above in correct order | Developer/CI |
| `reset-vm.sh` | Resets single VM to baseline | Fixture |
| `lock.sh` | Lock management via Hetzner Server Labels | Fixture |

**User**: "Do we need to update pre-analysis/testing-implementation-plan.md as well to avoid any confusion?"

**Resolution**: Yes, updated the pre-analysis document with a note explaining the refactoring and updated the directory structure.

**User**: "Don't renumber tasks. Otherwise, all references that already exist (e.g. in review docs) are not valid anymore."

**Resolution**: Used T006a and T006b for new tasks instead of renumbering existing tasks.

### Changes Made

**tasks.md**:
- T004: Now creates single VM (`create-vm.sh`)
- T005: Configures single VM (`configure-vm.sh`) - description updated
- T006: Configures both VMs (`configure-hosts.sh`) - description updated
- T006a: NEW - Creates baseline snapshots (`create-baseline-snapshots.sh`)
- T006b: NEW - Orchestrator (`provision-test-infra.sh`)
- T007: Resets single VM (`reset-vm.sh`) - unchanged
- T008: Lock management (`lock.sh`) - unchanged
- Summary: Updated task counts (41 total, 7 in Phase 2)
- Notes: Updated to explain script architecture

**plan.md**:
- Updated project structure with all 7 scripts

**pre-analysis/testing-implementation-plan.md**:
- Updated directory structure
- Added note at top explaining refactoring and that script implementations need adaptation

## Summary

All 4 feedback points from tasks-review-2-gemini3.md have been addressed:
- E1: Already resolved in previous review (T025a exists)
- B1: Clarified SSH key responsibilities in T005 and T006
- C1: Specified baseline services in T005
- C2: Initially added provision.sh orchestration, then refactored to single-responsibility scripts

Additional extensive refactoring to script architecture based on user feedback.
