# Plan Review 4: Detailed Review

**Reviewer**: GitHub Copilot
**Date**: 2025-12-06
**Spec**: [spec.md](spec.md)
**Plan**: [plan.md](plan.md), [data-model.md](data-model.md), [quickstart.md](quickstart.md)

## Summary

The plan is comprehensive and well-aligned with the specification. It correctly identifies the need for a three-tier testing strategy, isolated VM infrastructure, and proper locking mechanisms. However, there is a notable inconsistency regarding the CI workflow structure that needs resolution before implementation.

## Feedback Points

1. **Inconsistency: CI Workflow Filenames**
    - **Location**: `plan.md` (Project Structure) vs `data-model.md` (Entities/CIWorkflow)
    - **Issue**: `plan.md` proposes a single `.github/workflows/test.yml` to handle all testing. `data-model.md` describes two separate workflows: `ci.yml` (unit tests) and `integration.yml` (integration tests).
    - **Requirement**: Spec FR-013, FR-014.
    - **Recommendation**: Decide on a single approach (consolidated `test.yml` or separate files) and update both documents to match. A consolidated file is often easier for sharing setup steps, but separate files can be clearer for distinct triggers.

2. **Clarification: Auto-provisioning Trigger in CI**
    - **Location**: `plan.md`
    - **Issue**: Spec FR-006 requires automatic provisioning when integration tests are run. While `provision-vms.sh` is defined, the plan doesn't explicitly state that the CI workflow must call this script *before* the test step.
    - **Recommendation**: In `plan.md` (under CI/CD or Scripts section), explicitly confirm that the CI workflow will include a step to run `provision-vms.sh` (or check/provision) before executing integration tests.

3. **Clarification: Forked PR Skip Logic**
    - **Location**: `plan.md` / `data-model.md`
    - **Issue**: Spec FR-017b requires skipping integration tests for forked PRs. `quickstart.md` mentions this will happen, but `plan.md` and `data-model.md` do not specify *how* this is enforced (e.g., checking for secret presence or repo ownership).
    - **Recommendation**: Add a specific note in `data-model.md` under `CIWorkflow` or `plan.md` describing the mechanism (e.g., `if: github.event.pull_request.head.repo.full_name == github.repository` or `if: ${{ secrets.HCLOUD_TOKEN != '' }}`).

4. **Minor: VM Naming Consistency**
    - **Location**: `plan.md` vs `data-model.md`
    - **Issue**: `plan.md` uses `pc1`/`pc2` shorthand in text but `pc1` in code blocks. `data-model.md` uses `pc1`.
    - **Recommendation**: Ensure `pc1` and `pc2` are consistently used as the canonical VM names/hostnames in all technical specifications to avoid ambiguity.
