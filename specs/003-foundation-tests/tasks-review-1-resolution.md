# Tasks Review 1 Resolution

**Review File**: tasks-review-1-codex.md
**Resolved By**: Claude
**Date**: 2025-12-11

## Arguments

```
$ARGUMENTS
```

(No overriding instructions provided)

## Conversation Log

### Initial Analysis

I read the review file and analyzed all feedback points:

1. **C1 (CRITICAL)**: Spec omits constitution alignment - Valid, spec.md needs principle alignment section
2. **C2 (CRITICAL)**: Plan uses wrong principle name "Proven Tooling Only" vs "Well-supported tools and best practices" - Valid
3. **G1 (HIGH)**: FR-010/FR-011/FR-012 have no corresponding tasks - Valid, need verification tasks
4. **G2 (MEDIUM)**: FR-004/SC-004 lack explicit verification gate - Valid, need checklist task
5. **G3 (MEDIUM)**: Edge-case policies not mapped to tasks - Valid but these are guidance, not testable requirements
6. **G4 (MEDIUM)**: US-2-AS-2 failure output linkage uncovered - Valid, need task for failure output spec ID
7. **I1 (MEDIUM)**: T035 cites wrong FR (FR-008 instead of FR-007) - Valid, need to fix reference and add naming check
8. **A1 (LOW)**: Performance budgets lack hardware baseline definition - Valid, need baseline specification

### Clarifying Question

**Me**: The edge cases in spec.md (lines 73-79) describe behaviors for test authors to follow, not testable requirements. Should I:
- Option A: Add tasks to implement automated checks
- Option B: Add as guidance notes for developers to follow manually
- Option C: Skip as already covered by other tasks

**User**: B

### Resolution Decision

Edge-case policies (G3) will be added as guidance notes in a "Test Writing Guidelines" section, not as automated verification tasks.

## Changes Made

### spec.md

Added "Constitution Alignment" section before "Out of Scope" with alignment to:
- Reliability Without Compromise
- Frictionless Command UX
- Well-supported tools and best practices
- Deliberate Simplicity
- Up-to-date Documentation

Trade-offs: None identified.

### plan.md

Changed line 32 from:
- `"Proven Tooling Only"` → `"Well-supported tools and best practices"`

Also added note about following standard Python testing patterns.

### tasks.md

#### Phase 14 Restructured

Reorganized into subsections:
- Performance Verification (T027, T028)
- Test Quality Verification (T029, T036, T037, T038)
- Traceability Verification (T030, T035, T039, T040)
- Coverage Verification (T031-T034, T041)

#### New Tasks Added

| Task | Description | Addresses |
|------|-------------|-----------|
| T036 | Verify unit tests use mock executors | G1 (FR-011) |
| T037 | Verify integration tests use real VM operations | G1 (FR-012) |
| T038 | Verify tests use framework fixtures | G1 (FR-010) |
| T039 | Verify test function names include requirement ID | I1 (FR-008) |
| T040 | Verify test failure output includes spec ID | G4 (US-2-AS-2) |
| T041 | Verify success/failure path coverage for each requirement | G2 (FR-004, SC-004) |

#### T035 Fixed

Changed from incorrectly referencing both FR-007 and FR-008 to only FR-007 (docstrings). FR-008 (naming) is now handled by new task T039.

#### Performance Baseline Added

T027 and T028 now include hardware baseline specification:
- **T027**: 4-core CPU, 16GB RAM, SSD storage
- **T028**: Same as T027 plus test VM with 2 vCPUs, 4GB RAM

#### Test Writing Guidelines Section Added

New section before "Dependencies & Execution Order" with guidance for:
- Gap Between Spec and Implementation (assertion messages with spec ID)
- Ambiguous Spec Interpretation (document interpretation in docstring)
- Extra Functionality Not in Spec (test but note as implementation-specific)

#### Summary Updated

- Total Tasks: 35 → 41
- Polish/Verification: 9 → 15

#### Notes Section Updated

Added cross-references to verification tasks for each quality requirement.

## Coverage Summary After Resolution

| Requirement Key | Has Task? | Task IDs |
|-----------------|-----------|----------|
| FR-004 (success/failure paths) | Yes | T041 |
| FR-008 (naming convention) | Yes | T039 |
| FR-010 (framework fixtures) | Yes | T038 |
| FR-011 (unit mock executors) | Yes | T036 |
| FR-012 (integration real ops) | Yes | T037 |
| SC-004 (success/failure verified) | Yes | T041 |
| SC-006 (gaps surface on failure) | Yes | T040 (via spec ID in output) |
| US-2-AS-2 (failure output linkage) | Yes | T040 |
| Edge-01 (gap between spec/impl) | Guidance | Test Writing Guidelines |
| Edge-02 (ambiguous interpretation) | Guidance | Test Writing Guidelines |
| Edge-03 (extra functionality warning) | Guidance | Test Writing Guidelines |

All HIGH/MEDIUM/LOW issues from review addressed. CRITICAL constitution issues (C1, C2) resolved.
