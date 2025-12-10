# Plan Review 2 Resolution

**Feature**: 002-testing-framework
**Date**: 2025-12-06
**Review file**: `plan-review-2-detail-codex.md`

## Arguments

No overriding arguments were provided.

## Conversation Log

### Initial Analysis

After reading the review, I identified all 11 feedback items and analyzed each one. The issues were:

1. Provisioning tooling inconsistency (OpenTofu references in data-model.md and plan.md)
2. Integration test provisioning not truly automatic for local runs
3. FR-003a contract coverage missing for MockExecutor
4. Lock contract divergence from the spec (directory vs JSON file)
5. Env var skip logic incomplete (only checks PC1, not PC2)
6. Reset flow lacks actionable failure for missing baselines
7. CI key injection for auto-provisioning unclear
8. Fork/missing-secret handling absent
9. CI artifact retention missing
10. Manual playbook in wrong location
11. Ops/Dev guide content requirements not called out

### Clarifying Questions

**Q1 (Auto-provisioning for local runs):**
The spec says integration tests should auto-provision VMs when they don't exist. For local developer runs, should:
- (A) The pytest hook/fixture check for VM existence and run `provision-vms.sh` if they don't exist (fully automatic)?
- (B) The pytest hook/fixture fail with a clear message instructing the developer to run provisioning manually (semi-automatic)?

**User answer:** A. Can't we use the exact same mechanism as in CI? Of course, secrets/envvars need to be in place.

**Q2 (Contract tests for MockExecutor):**
FR-003a requires contract tests that verify MockExecutor adheres to the same interface as LocalExecutor/RemoteExecutor. Should I:
- (A) Add tasks to create these contract tests as part of this feature?
- (B) Note this as out of scope?

**User answer:** A. If creating MockExecutor is in scope, then testing it is in scope as well. Out of scope is testing stuff that was already implemented.

**Additional note from user:** plan.md also contains references to tofu. Do a full search and fix any mentions.

## Resolutions

### Issue 1: Provisioning tooling inconsistency

**Resolution:** Removed all OpenTofu/tofu references from plan.md and data-model.md.

**Changes:**
- `plan.md`: Changed "OpenTofu (actively maintained Terraform fork)" to "hcloud CLI (Hetzner's official command-line tool)"
- `data-model.md`: Updated state diagram to use "provision-vms.sh" and "hcloud server delete" instead of "tofu apply/destroy"

### Issue 2: Auto-provisioning for local runs

**Resolution:** Updated the CI workflow to use dynamic VM discovery via `hcloud server ip` after auto-provisioning. Local runs use the same mechanism - if `HCLOUD_TOKEN` and `SSH_PUBLIC_KEY` env vars are set, provisioning happens automatically via `provision-vms.sh`. The flow is:
1. CI or local developer sets `HCLOUD_TOKEN` and has SSH key available
2. `provision-vms.sh` checks if VMs exist via `hcloud server describe`
3. Creates VMs if missing, then runs full provisioning
4. VM IPs are retrieved dynamically via `hcloud server ip`

**Changes:**
- Updated CI workflow to derive public key from private key (`ssh-keygen -y`)
- Updated CI workflow to pass `SSH_PUBLIC_KEY` to provisioning script
- Updated CI workflow to retrieve VM IPs dynamically after provisioning
- Removed `PC1_TEST_HOST` and `PC2_TEST_HOST` secrets - no longer needed

### Issue 3: FR-003a contract coverage missing

**Resolution:** Added contract test tasks and detailed specification for MockExecutor vs LocalExecutor/RemoteExecutor parity testing.

**Changes:**
- Added new task 12 in Phase 2: `tests/contract/test_executor_interface.py`
- Added detailed "Contract Tests (FR-003a)" section with example test structure
- Renumbered Phase 3 tasks (13-18)

### Issue 4: Lock contract divergence

**Resolution:** Updated `lock.sh` to use JSON format with holder, acquired timestamp, and hostname, matching `data-model.md` specification.

**Changes:**
- Updated `lock.sh` to write JSON: `{"holder": "...", "acquired": "...", "hostname": "..."}`
- Updated lock script to use `jq` for reading JSON fields
- Lock directory mechanism (`LOCK_DIR`) retained for atomic creation

### Issue 5: Env var skip logic incomplete

**Resolution:** Updated the pytest skip logic to check all required environment variables.

**Changes:**
- Updated `pytest_collection_modifyitems` in `research.md` to check both `PC_SWITCHER_TEST_PC1_HOST` and `PC_SWITCHER_TEST_PC2_HOST`
- Skip message now lists which specific variables are missing

### Issue 6: Reset flow lacks actionable failure for missing baselines

**Resolution:** Added baseline snapshot validation at the start of `reset-vm.sh` with actionable error message.

**Changes:**
- Added validation step that checks `/.snapshots/baseline/@` and `/.snapshots/baseline/@home` exist
- If missing, exits with clear error: "ERROR: Baseline snapshot missing: [paths]. Run provisioning to create baseline snapshots."
- Reset does not proceed on dirty state

### Issue 7: CI key injection unclear

**Resolution:** Updated CI workflow to derive public key from the private key secret using `ssh-keygen -y`.

**Changes:**
- Added step: `ssh-keygen -y -f ~/.ssh/id_ed25519 > ~/.ssh/id_ed25519.pub`
- `SSH_PUBLIC_KEY` env var passed to `provision-vms.sh`
- Public key is injected into VM's authorized_keys during provisioning

### Issue 8: Fork/missing-secret handling absent

**Resolution:** Added secret availability check and fork detection to CI workflow.

**Changes:**
- Added `if` condition to skip integration tests for fork PRs: `github.event.pull_request.head.repo.full_name == github.repository`
- Added `secrets-check` step that verifies secrets are available
- If secrets missing, workflow outputs notice and exits gracefully (not failure)
- Unit tests still run for fork PRs

### Issue 9: CI artifact retention missing

**Resolution:** Added artifact upload step to preserve test logs.

**Changes:**
- Added `actions/upload-artifact@v4` step to upload `pytest-output.log`
- Configured 14-day retention
- Runs on `always()` condition to capture logs even on failure

### Issue 10: Manual playbook location incorrect

**Resolution:** Updated playbook location from `tests/playbook/visual-verification.md` to `docs/testing-playbook.md` per FR-033.

**Changes:**
- Updated directory structure in pre-analysis document
- Updated Implementation Order Phase 3 task 15
- plan.md already had correct location

### Issue 11: Ops/Dev guide content requirements not called out

**Resolution:** Added explicit FR references to Implementation Order tasks.

**Changes:**
- Task 15: `docs/testing-playbook.md` now references FR-018-FR-020 (visual verification + feature tour)
- Task 16: `docs/testing-developer-guide.md` now references FR-021-FR-024 (fixtures, SSH/btrfs patterns, troubleshooting)
- Task 17: `docs/testing-ops-guide.md` now references FR-025-FR-029 (secrets, env vars, cost monitoring, runbooks)
- Task 18: `docs/testing-framework.md` now references FR-030-FR-032 (architecture diagrams, design rationale)

## Files Modified

| File | Changes |
|------|---------|
| `plan.md` | Fixed OpenTofu reference; updated CI workflow structure to single test.yml |
| `data-model.md` | Fixed state diagram to use hcloud CLI commands |
| `research.md` | Fixed env var skip logic to check all required vars |
| `quickstart.md` | Updated secrets table, added fork PR section |
| `pre-analysis/testing-implementation-plan.md` | Updated lock.sh JSON format; added baseline validation to reset-vm.sh; updated CI workflow with fork handling and artifact retention; fixed playbook location; added contract tests section and task; updated FR references in Implementation Order |

## Summary

All 11 review items have been addressed. The plan now:
- Uses hcloud CLI consistently (no OpenTofu references)
- Supports fully automatic provisioning for both CI and local runs
- Includes contract tests for MockExecutor parity (FR-003a)
- Uses JSON lock format with holder, timestamp, and hostname (FR-005)
- Validates all required env vars before running integration tests
- Validates baseline snapshots before reset with actionable errors (FR-004)
- Injects CI SSH keys correctly during auto-provisioning (FR-006a)
- Skips integration tests gracefully for forks and missing secrets (FR-017a/FR-017b)
- Preserves test artifacts for debugging (FR-017c)
- Places manual playbook at correct path (FR-033)
- Explicitly references FR requirements for all documentation deliverables
