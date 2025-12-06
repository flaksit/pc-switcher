# Plan Review 4 Resolution

**Date**: 2025-12-06
**Reviewer**: GitHub Copilot
**Review File**: [plan-review-4-detail-gemini3.md](plan-review-4-detail-gemini3.md)

## Arguments

```text
For 4: use pc1 / pc2 everywhere. They will be in a separate Hetzner Cloud project. No risk for name clashes
```

## Conversation Log

No clarifying questions were needed. All feedback points were clear, and the override instruction for point 4 was explicit.

## Resolution Summary

### Point 1: CI Workflow Filenames

**Issue**: `plan.md` proposes a single `.github/workflows/test.yml` while `data-model.md` describes two separate workflows (`ci.yml` and `integration.yml`).

**Resolution**: Unified to single `test.yml` workflow in `data-model.md` to match `plan.md`. A consolidated workflow is simpler to maintain and allows sharing setup steps (checkout, Python setup, dependency caching).

### Point 2: Auto-provisioning Trigger in CI

**Issue**: The plan doesn't explicitly state that CI must call `provision-vms.sh` before running integration tests.

**Resolution**: Added explicit provisioning step in `data-model.md` CIWorkflow section and updated `quickstart.md` to clarify the CI workflow includes auto-provisioning.

### Point 3: Forked PR Skip Logic

**Issue**: The mechanism for skipping integration tests on forked PRs is not specified.

**Resolution**: Added explicit condition in `data-model.md`: `if: github.event.pull_request.head.repo.full_name == github.repository || github.event_name == 'workflow_dispatch'`. This ensures integration tests only run for PRs from the main repository or manual triggers.

### Point 4: VM Naming Consistency

**Issue**: Mixed usage of `pc1`/`pc2` shorthand and `pc-switcher-pc1`/`pc-switcher-pc2` full names.

**Resolution**: Per override instruction, standardized to `pc1`/`pc2` everywhere. VMs will be in a separate Hetzner Cloud project, so there's no risk of name clashes with other resources.

## Files Modified

- `data-model.md`: Updated CIWorkflow entity to single `test.yml`, VM naming to `pc1`/`pc2`, added fork detection logic and auto-provisioning flow
- `quickstart.md`: Updated VM naming from `pc-switcher-pc1`/`pc-switcher-pc2` to `pc1`/`pc2`
- `research.md`: Updated CI/CD section to document single `test.yml` workflow with fork detection; updated VM naming to `pc1`/`pc2`
- `contracts/README.md`: Updated lock contract to reference `pc1` instead of `pc-switcher-pc1`
