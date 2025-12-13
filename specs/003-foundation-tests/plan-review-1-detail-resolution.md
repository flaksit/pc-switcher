# Plan Review 1 Detail Resolution

**Review file**: `plan-review-1-detail-codex.md`
**Date**: 2025-12-11

## Arguments

No overriding arguments were provided.

## Feedback Summary

The reviewer raised 2 points:

1. **FR-009 (test independence) not covered**: No guidance on avoiding order/shared-state coupling or verification steps
2. **FR-004 / SC-004 (success + failure paths) partially addressed**: One test per requirement without showing both positive and negative coverage; edge cases not linked to FRs

## Conversation Log

### My question:

For feedback point 2, the reviewer wants explicit failure-path tests linked to each requirement. There are two approaches:

1. **Separate test functions**: For each FR, have `test_001_fr###_success` and `test_001_fr###_failure` as separate functions
2. **Combined test functions**: A single `test_001_fr###_...` function that tests both success and failure paths internally (with multiple assertions)

The current quickstart.md shows approach 1 (separate functions) in section "8. Test Both Success and Failure Paths". However, the data-model.md only lists one test function per requirement.

Which approach do you prefer? Or should I simply add guidance that tests must cover both paths internally without requiring separate named functions?

### User response:

> point 2: add guidance. Depending on the type of tests, it might be easier or more efficient to combine in a single test function; sometimes it might be cleaner to separate

## Resolution

### Point 1: FR-009 (test independence)

**Changes made**:

1. Added "Test Independence (FR-009)" section to `plan.md` with:
   - No shared state rule
   - Isolation via fixtures guidance
   - Integration test isolation via VM snapshot reset
   - Verification via random order execution (`pytest --randomly-seed=<N>`)

2. Added section "10. Ensure Test Independence (FR-009)" to `quickstart.md` with:
   - Concrete rules for test isolation
   - Command examples for random order testing
   - Code example showing bad (shared state) vs good (fixture-based) patterns

### Point 2: FR-004 / SC-004 (success + failure paths)

**Changes made**:

1. Added "Success and Failure Path Coverage (FR-004, SC-004)" section to `plan.md` explaining:
   - Every requirement must have both success and failure path coverage
   - Approach is flexible: separate functions or combined, whichever is cleaner
   - Edge cases in data-model.md cover failure paths for their respective requirements

2. Updated quickstart.md section "8. Test Both Success and Failure Paths" to show:
   - Option A: Separate functions (for complex scenarios)
   - Option B: Combined function (for simple, related paths)
   - Guidance to choose based on clarity

3. Added verification item to Coverage Verification checklist in plan.md:
   - "Each requirement has both success and failure path coverage (FR-004)"

## Files Modified

- `specs/003-foundation-tests/plan.md` - Added FR-009 and FR-004/SC-004 sections
- `specs/003-foundation-tests/quickstart.md` - Added test independence section, updated success/failure path guidance
