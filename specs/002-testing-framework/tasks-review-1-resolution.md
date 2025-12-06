# Tasks Review 1 Resolution

**Review file**: tasks-review-1-codex.md
**Resolved by**: Claude
**Date**: 2025-12-06

## Arguments

(No arguments provided when invoking this command)

## Conversation Log

### Initial Analysis

After reading the review, I identified the following categories:

**Issues I agreed with and planned to address:**
- G1 (HIGH) - CI SSH key injection (FR-006a): Missing task
- G3 (HIGH) - Secret availability detection (FR-017a): T017 mentions fork detection but not generic secrets check
- G4 (MEDIUM) - CI calling reset script (FR-017): Needed clarification
- G5 (MEDIUM) - Local secret configuration in developer guide (FR-025): Missing task
- I1 (LOW) - Mermaid format for T032: Minor clarification needed
- D1 (LOW) - Cost cap duplication (FR-012/SC-005): Spec issue to fix

**Issues I raised questions about:**
- C1 (CRITICAL) - Principle annotations
- C2 (CRITICAL) - Performance budgets and owned tasks
- G2 (HIGH) - Cost cap enforcement
- U1 (MEDIUM) - Unit test safety verification criteria

### User Responses

**Q1 (C1 - Principle annotations):**
User response: "ignore. I removed it from the constitution."

**Q2 (C2 - Performance budgets and owned tasks):**
User asked me to analyze what "tasks with owners" meant. I explained it seemed to refer to project management tasks, not tasks.md entries, and the "owners" tracking is only valuable for team coordination. User updated the constitution line themselves to remove the team-specific "owners" requirement.

**Q3 (G2 - Cost cap enforcement via infrastructure choices):**
User response: "Just re-mention the infrastructure chosen in the description of the tasks that implement the provisioning."

**Q4 (U1 - Unit test safety verification):**
User response: "It's only about the EXISTING tests."

**G4 (Fixture-integrated provisioning):**
User asked: "Should the auto-provisioning, reset, ... be called as separate scripts by the CI? Wouldn't it be a good idea to integrate this in the fixture of the integration tests, so that the CI (and the developer) don't need to call multiple scripts for launching the integration tests?"

I agreed this was better UX. The fixture flow would be:
1. Acquire lock
2. Check if VMs exist (auto-provision if not, or skip if secrets unavailable)
3. Reset VMs to baseline
4. Run tests
5. Release lock

User confirmed: "OK. Update other documents as well if they need to be brought in line with this decision."

**D1 (Cost cap duplication):**
User: "update the spec.md for this one"

**VM Instance Type Correction:**
User corrected: "NOT cx22! It is CX23 now!"

## Resolutions Applied

### spec.md Changes

- **D1**: Consolidated SC-005 to reference FR-012 instead of duplicating the cost cap text:
  - Changed: `"Test infrastructure costs remain under EUR 10/month; cost is constrained by infrastructure choices..."`
  - To: `"Test infrastructure costs remain under EUR 10/month (see FR-012 for details)"`

### plan.md Changes

- **G4**: Updated "Frictionless Command UX" section to describe fixture-integrated provisioning
- **G4**: Added row to Complexity Tracking table for "Provisioning integration" decision

### tasks.md Changes

| Issue | Task(s) | Change |
|-------|---------|--------|
| G1 | T005 | Added "including CI-provided public key injection per FR-006a" |
| G2/Q3 | T004 | Added "use CX23 instances (2 shared vCPUs, 4GB RAM) to stay within EUR 10/month cost cap (FR-012)" |
| G3 | T017 | Added "secret availability check (skip with clear notice per FR-017a when HCLOUD_TOKEN or SSH key unavailable)" |
| G4 | T015 | Rewrote to describe fixture handling: lock acquisition, VM existence check with auto-provisioning, reset |
| G4 | T017 | Clarified "job runs `uv run pytest -m integration` (fixtures handle provisioning/reset automatically)" |
| G5 | T025a | New task: Add local development setup section for secret configuration per FR-025 |
| U1/Q4 | T009 | Added safety verification criteria for existing tests (no real btrfs, no network calls, no fs-type deps) |
| U1/Q4 | T010 | Added "confirm mocks are used for all external system interactions (FR-002 safety)" |
| I1 | T032 | Added "(Mermaid format per FR-030)" |
| - | Summary | Updated task counts: Total 39, US5 now 5 tasks |

### Issues Not Addressed in tasks.md

| Issue | Reason |
|-------|--------|
| C1 | User removed requirement from constitution |
| C2 | User modified constitution to remove "owners" requirement |
| D1 | Fixed in spec.md, not a tasks.md issue |
| T035/T036 unmapped | Reviewer noted these aren't tied to requirements; these are valid polish tasks |

## Summary

All feedback points from tasks-review-1-codex.md have been addressed:
- 2 CRITICAL issues: Resolved by user updating constitution
- 3 HIGH issues: All addressed in tasks.md
- 3 MEDIUM issues: All addressed in tasks.md
- 2 LOW issues: Both addressed (I1 in tasks.md, D1 in spec.md)

Files modified:
- `specs/002-testing-framework/spec.md` - D1 consolidation
- `specs/002-testing-framework/plan.md` - G4 fixture-integrated provisioning
- `specs/002-testing-framework/tasks.md` - All remaining issues
