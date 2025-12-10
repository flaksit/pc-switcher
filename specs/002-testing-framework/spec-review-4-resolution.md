# Spec Review 4 Resolution

## Arguments Passed
```
(none)
```

## Conversation Log

### Agent Analysis

I've read through the Gemini 3 Pro review feedback. Here's my analysis:

**Points I agree with and can address directly:**

- **#3 (Contract Tests)**: Valid - FR-007 references `tests/contract` but contract tests aren't defined in the spec. I'll add a definition.

- **#5 (VM Lifecycle)**: Valid - need to clarify VMs run persistently (reset via btrfs snapshots, not reprovisioning).

- **#7 (Python Version)**: Valid - should state Python 3.14 requirement.

- **Minor (FR-017b)**: Valid - clarify tests are skipped (not failed) for fork PRs.

**Points requiring clarification/decision:**

- **#1 (Locking location)**: The spec already defines behavior (holder identity, acquisition time, manual cleanup). WHERE the lock lives is an implementation detail, not a spec requirement. Previous resolution (review 3, #5) already decided this.

- **#2 (Cloud Provider)**: Should we name a specific provider due to cost constraints?

- **#4 (Local Secrets)**: How should developers get secrets for local integration test runs?

- **#6 (VMExecutor)**: Should VMExecutor wrap production RemoteExecutor or be a separate test implementation?

### User Responses

**Q1 (Cloud Provider - #2):**
> A: it's good as it is

**Decision**: Keep generic with just the cost constraint. No change needed.

**Q2 (Local Secrets - #4):**
> B

**Decision**: Just note in Developer Guide scope (FR-025) that it should cover local secret setup.

**Q3 (VMExecutor - #6):**
> This is an implementation detail (it'd better be separate)

**Decision**: This is an implementation detail, not for the spec. The user notes preference for separate implementation, but that's for the implementation phase.

---

## Resolution Summary

| # | Feedback | Resolution |
|---|----------|------------|
| 1 | Locking location | No change - implementation detail; previous resolution (review 3, #5) already decided this |
| 2 | Cloud Provider | No change - keep generic with cost constraint per user decision |
| 3 | Contract Tests Definition | Added FR-003a defining contract tests |
| 4 | Local Secrets | Updated FR-025 to include local secret setup in Developer Guide scope |
| 5 | VM Lifecycle | Updated FR-012 to clarify VMs run persistently |
| 6 | VMExecutor vs RemoteExecutor | No change - implementation detail per user decision |
| 7 | Python Version | Added Python 3.14 to Assumptions |
| Minor | FR-017b skip vs fail | Updated FR-017b to clarify tests are skipped (not failed) |

---

## Changes Made to spec.md

1. **FR-003a** (new): Added contract tests definition - "Contract tests MUST verify that MockExecutor and real executor implementations (LocalExecutor, RemoteExecutor) adhere to the same behavioral interface, ensuring mocks remain reliable representations of production behavior"

2. **FR-012**: Clarified VM lifecycle - added "VMs are expected to remain running persistently (reset via btrfs snapshots, not reprovisioning); manual destruction is acceptable when extended downtime is expected"

3. **FR-017b**: Clarified skip behavior - changed "CI MUST clearly indicate" to "CI MUST skip (not fail) integration tests and clearly indicate"

4. **FR-025**: Added local secret setup - appended "Developer guide MUST document how to configure equivalent secrets for local integration test runs (e.g., environment variables or config file)"

5. **Assumptions**: Added "Python 3.14 is used (per ADR-003)"

6. **FR-034** (new section): Added missing Test Fixture Requirements section with FR-034 - "Testing framework MUST provide minimal pytest fixtures for VM command execution, enabling integration tests to run commands on test VMs via a RemoteExecutor-like interface" (this was referenced in Out of Scope but never defined)
