# Plan Review 1 Resolution

**Feature**: 002-testing-framework
**Review File**: plan-review-1-architecture-codex.md
**Date**: 2025-12-05

## Arguments

No overriding arguments were provided.

## Conversation Log

### Initial Analysis (Claude)

I read the review file which raised 4 points:

1. **Terraform/OpenTofu state strategy is inconsistent** - plan.md and research.md assume local state (ephemeral in CI), while pre-analysis documents S3 backend. With local state, CI cannot safely reuse or tear down long-lived Hetzner VMs.

2. **Scope overshoots the feature** - pre-analysis enumerates 40+ concrete tests for 001-foundation, but this feature is only about the testing framework. The plan should focus on framework architecture and leave test backlogs to features they validate.

3. **Provisioning/reset flow described in conflicting ways** - Snapshot naming differs between research.md (`baseline-@`/`active-@`) and pre-analysis (`baseline-root`/`baseline-home`). No single authoritative lifecycle.

4. **Concurrency control not wired through all destructive operations** - Lock model documented but provisioning/reset flows don't mandate acquiring lock. CI concurrency groups don't protect against developer runs.

### Clarifying Questions (Claude)

1. On Point 1 (State Strategy): The pre-analysis uses Hetzner Object Storage S3-compatible backend. Is this the approach you want, or do you have a preference for a different remote backend?

2. On Point 3 (Snapshot Naming): Which convention do you prefer?
   - `baseline-@` / `active-@` (matches btrfs subvolume naming)
   - `baseline-root` / `baseline-home` (more descriptive)

3. On Point 4 (Lock Scope): Should the lock protect:
   - Option A: Only integration test execution (provisioning assumed to be rare manual operation)
   - Option B: All destructive operations including provisioning and reset

### User Answers

**Q1**: Hetzner S3 is expensive. Can't we use a folder (sub-account) on a Hetzner Storage Box that I have already? This is accessible through ssh/scp/sftp or if-needed smb

**Q2**: `/.snapshots/baseline/@`, `/.snapshots/baseline/@home`, as described in docs/testing-framework.md lines 108-117. Bring the rest of this doc and the other docs in line with this naming and this procedure.

**Q3**: Option A (lock only protects test execution)

**On Point 2 (scope)**: Split the document in two documents:
- a) `testing-implementation-plan.md` (containing only the testing framework)
- b) add a new document `specs/003-foundation-tests/pre-analysis/implementation-plan.md` to branch `003-foundation-tests` (containing only the retroactive tests for 001-foundation)

## Resolutions Applied

### Resolution 1: State Backend - Hetzner Storage Box

Changed from local state / S3 to Hetzner Storage Box with SSH/SCP sync:

- OpenTofu uses local backend
- Wrapper script syncs state to/from Storage Box before/after tofu operations
- State file location on Storage Box: `pc-switcher/test-infrastructure/terraform.tfstate`
- Simple, cost-effective, single source of truth

Files updated:
- research.md §3 (OpenTofu/Hetzner Cloud Configuration)
- quickstart.md (First-Time Setup section)
- plan.md (Technical Context)

### Resolution 2: Snapshot Naming Convention

Standardized on `/.snapshots/baseline/@` and `/.snapshots/baseline/@home` per docs/testing-framework.md:

- Baseline snapshots: `/.snapshots/baseline/@`, `/.snapshots/baseline/@home` (read-only)
- Reset procedure: mv active to `@_old`, snapshot from baseline, reboot, delete old
- Test artifacts: `/.snapshots/pc-switcher/test-*` (deleted on reset)

Files updated:
- research.md §4 (btrfs Snapshot Management)
- data-model.md (BtrfsSnapshot entity, snapshot paths)
- quickstart.md (reset procedures)

### Resolution 3: Lock Scope - Test Execution Only

Lock protects only integration test execution, not provisioning:

- Provisioning is rare, manual, protected by CI concurrency groups for CI runs
- Local concurrent provisioning is explicitly unsupported (documented constraint)
- Reset is part of test session, thus covered by lock

Files updated:
- research.md §5 (Lock Mechanism rationale)
- data-model.md (TestSession state diagram notes)
- plan.md (Complexity Tracking table)

### Resolution 4: Scope Split

Removed detailed test specifications from this feature's scope:

- `pre-analysis/testing-implementation-plan.md` trimmed to framework-only content
- Test specifications for 001-foundation will be moved to `specs/003-foundation-tests/pre-analysis/implementation-plan.md` on branch `003-foundation-tests`
- This feature focuses on: VM provisioning/reset, locking, CI hooks, fixtures, documentation structure

Files updated:
- pre-analysis/testing-implementation-plan.md (removed Unit Test Specifications, Integration Test Specifications, kept only framework infrastructure)
- plan.md (scope clarification in Summary)

## Files Modified

| File | Changes |
|------|---------|
| `specs/002-testing-framework/research.md` | §3: Changed to Storage Box backend; §4: Updated snapshot naming to `/.snapshots/baseline/@`; §5: Clarified lock scope |
| `specs/002-testing-framework/data-model.md` | Updated TestVM and BtrfsSnapshot entities with correct paths; Added Storage Box env vars |
| `specs/002-testing-framework/quickstart.md` | Updated first-time setup with tofu-wrapper.sh and Storage Box env vars; Updated reset commands; Added Storage Box secrets |
| `specs/002-testing-framework/plan.md` | Added scope note in Summary; Updated Storage section; Added tofu-wrapper.sh to project structure; Updated Complexity Tracking table |
| `specs/002-testing-framework/pre-analysis/testing-implementation-plan.md` | Removed all test specifications; Kept only framework infrastructure (scripts, OpenTofu config, fixtures, CI workflow) |

## Outstanding Action Items

1. **Create branch `003-foundation-tests`**: Move the test specifications (Unit Test Specifications, Integration Test Specifications) to `specs/003-foundation-tests/pre-analysis/implementation-plan.md`. This is outside the scope of this review resolution and should be done as a separate task.

---

## Follow-up Change: Replace OpenTofu with hcloud CLI

**Date**: 2025-12-06

### Conversation Log

**User prompt:**
> Is tf really the way to go? We're not using it for very much else than spinning up VMs. And we have a lot of scripting around it to setup and configure and reinstall ... Maybe just use bash/python scripts with hcloud cli? Tofu state not really needed then. Just checking if the VMs exist or not should be sufficient. What do you think?

**Claude response:**

You raise an excellent point. Let me analyze the trade-offs:

## Current OpenTofu Approach

**What it does:**
- Creates 2 VMs
- Creates 1 SSH key

**What it requires:**
- OpenTofu installed
- State file synced to/from Storage Box (tofu-wrapper.sh)
- Storage Box credentials in CI secrets

**Everything else** (OS reinstall with btrfs, user setup, SSH hardening, baseline snapshots, reset) is already done via bash scripts with `hcloud` CLI.

## Alternative: Pure hcloud CLI

```bash
# Create VM if it doesn't exist
if ! hcloud server describe pc-switcher-pc1 &>/dev/null; then
    hcloud server create \
        --name pc-switcher-pc1 \
        --type cx23 \
        --image ubuntu-24.04 \
        --location fsn1 \
        --ssh-key pc-switcher-test-key
fi
```

**Pros:**
- No state file needed - just check if VM exists
- hcloud CLI already required for provision.sh (rescue mode)
- Eliminates Storage Box complexity for state
- Fewer CI secrets (no STORAGE_BOX_*)
- Simpler to understand and maintain

**Cons:**
- Less "standard" IaC pattern
- Slightly more imperative code

## My Assessment

I agree with your thinking. OpenTofu is overkill here:
- We're only creating 2 VMs with trivial config
- The real work (btrfs install, configuration) happens in scripts anyway
- State management adds complexity that outweighs benefits
- Idempotency is trivial: "if VM exists, skip create"

**Recommendation:** Switch to pure hcloud CLI scripts. This eliminates:
- tofu-wrapper.sh
- Storage Box for state
- 3 CI secrets (STORAGE_BOX_*)
- OpenTofu installation step in CI

Would you like me to update the plan documents to use hcloud CLI instead of OpenTofu?

---

**User prompt:**
> Yes. Update @docs/testing-framework.md as well. And check if @specs/002-testing-framework/spec.md needs updating (it shouldn't, but you never know).

---

### Decision

Replace OpenTofu with pure hcloud CLI scripts.

### Rationale
- OpenTofu only creates 2 VMs and 1 SSH key - overkill for this use case
- No state management needed - simply check if VM exists: `hcloud server describe <name>`
- Eliminates Storage Box complexity and 3 CI secrets
- hcloud CLI is already required for other operations (rescue mode, SSH key management)

### Files Updated
- `specs/002-testing-framework/research.md` - Section 3 rewritten for hcloud CLI
- `specs/002-testing-framework/data-model.md` - Removed Storage Box env vars and terraform artifacts
- `specs/002-testing-framework/quickstart.md` - Updated setup instructions
- `specs/002-testing-framework/plan.md` - Updated dependencies and project structure
- `specs/002-testing-framework/pre-analysis/testing-implementation-plan.md` - Replaced infrastructure config
- `docs/testing-framework.md` - Updated provisioning section
- `specs/002-testing-framework/spec.md` - Verified no changes needed (no tooling references)

### Removed from Plan
- `tests/infrastructure/main.tf`
- `tests/infrastructure/variables.tf`
- `tests/infrastructure/outputs.tf`
- `tests/infrastructure/versions.tf`
- `tests/infrastructure/cloud-config.yaml`
- `tests/infrastructure/scripts/tofu-wrapper.sh`
- STORAGE_BOX_HOST, STORAGE_BOX_USER, STORAGE_BOX_SSH_KEY secrets

### Added to Plan
- `tests/infrastructure/scripts/provision-vms.sh` - Creates VMs via hcloud CLI if they don't exist
