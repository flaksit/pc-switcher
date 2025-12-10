# Specification Analysis Report

| ID | Category | Severity | Location(s) | Summary | Recommendation |
|----|----------|----------|-------------|---------|----------------|
| C1 | Constitution Alignment | CRITICAL | .specify/memory/constitution.md:85; specs/002-testing-framework/spec.md:1; specs/002-testing-framework/plan.md:1; specs/002-testing-framework/tasks.md:1 | Constitution requires specs/plans/tasks to annotate primary principles and trade-offs, but none of the three artifacts contain principle tags or trade-off notes. | Add principle tags per section/task and explicitly note any trade-offs; regenerate artifacts or edit to include these annotations. |
| C2 | Constitution Alignment | CRITICAL | .specify/memory/constitution.md:87; specs/002-testing-framework/plan.md:25; specs/002-testing-framework/tasks.md:18 | Constitution demands performance budgets, conflict detection strategies, and documentation updates as tracked tasks with owners plus verification evidence; tasks lack owners and no tasks cover measuring the stated performance targets (plan lines 25-28) or collecting evidence. | Add owned tasks for each performance target (unit/integration/reset duration, cost cap), conflict-detection validation, and documentation updates; include verification/evidence steps in tasks. |
| G1 | Coverage Gap | HIGH | specs/002-testing-framework/spec.md:212 | FR-006a (inject CI-provided SSH keys into both VMs during auto-provisioning) has no task; provisioning/config scripts tasks omit key handling. | Add a task to implement and validate CI key injection into VM authorized_keys (both VMs) and to document key paths. |
| G2 | Coverage Gap | HIGH | specs/002-testing-framework/spec.md:234; specs/002-testing-framework/spec.md:315; specs/002-testing-framework/tasks.md:138 | Cost cap EUR 10/month (FR-012/SC-005) only has a doc section task (T029); no tasks choose VM sizes, enforce budgets, or monitor spend. | Add tasks to set VM sizes/pricing assumptions, add budget checks/alerts, and validate projected monthly cost against cap. |
| G3 | Coverage Gap | HIGH | specs/002-testing-framework/spec.md:248; specs/002-testing-framework/tasks.md:89 | FR-017a requires CI to skip integration tests with a clear notice when secrets are unavailable; tasks for CI (T017) mention fork detection but not secret-availability detection or messaging. | Extend CI tasks to include secret checks with explicit skip messaging for both forks and missing secrets on main repo runs. |
| G4 | Coverage Gap | MEDIUM | specs/002-testing-framework/spec.md:246; specs/002-testing-framework/tasks.md:72; specs/002-testing-framework/tasks.md:89 | FR-017 (CI must reset VMs before integration tests) is only covered by local fixture reset (T015); CI workflow task (T017) does not state it will call reset script. | Amend CI tasks to call reset-vm.sh (or equivalent) before integration job execution and capture/reset failures. |
| U1 | Underspecification | MEDIUM | specs/002-testing-framework/spec.md:200; specs/002-testing-framework/tasks.md:54 | FR-002 (unit tests safe on any machine) relies on “verify existing tests” tasks (T009/T010) without criteria to detect prohibited actions (real btrfs ops, network calls). | Define safety checks (e.g., audit for forbidden commands, sandbox mocks) and add tasks to enforce them or add guardrails in test config. |
| G5 | Coverage Gap | MEDIUM | specs/002-testing-framework/spec.md:272; specs/002-testing-framework/tasks.md:138 | FR-025 requires developer guide to document local secret configuration; tasks cover CI secrets in ops guide (T026) but no task adds local-dev secret guidance to developer guide. | Add a developer-guide task to document local secret/environment setup mirroring CI. |
| I1 | Inconsistency | LOW | specs/002-testing-framework/spec.md:282; specs/002-testing-framework/tasks.md:156; specs/002-testing-framework/tasks.md:157 | FR-030 mandates Mermaid diagrams; T031 notes Mermaid, but T032 (component interaction diagram) omits format, risking mixed diagram standards. | Clarify in T032 that the interaction diagram must also be Mermaid to satisfy FR-030. |
| D1 | Duplication | LOW | specs/002-testing-framework/spec.md:234; specs/002-testing-framework/spec.md:315 | Cost cap appears both as FR-012 and SC-005 with identical wording, risking divergent updates. | Consolidate into one canonical requirement (cross-reference from the other) to avoid drift. |

## Coverage Summary

| Requirement Key | Has Task? | Task IDs | Notes |
|-----------------|-----------|----------|-------|
| three-tier-test-structure (FR-001) | Yes | T001, T002, T003, T020, T021 | Tiered coverage via config + playbook |
| unit-tests-safe (FR-002) | Yes | T009, T010 | Verification only; no safety audit |
| integration-on-isolated-vms (FR-003) | Yes | T004, T012 |  |
| contract-tests-mock-vs-real (FR-003a) | Yes | T011 |  |
| vm-reset-baseline (FR-004) | Yes | T007, T015 |  |
| lock-prevent-concurrency (FR-005) | Yes | T008, T014, T017 |  |
| auto-provision-vms (FR-006) | Yes | T004 |  |
| ci-ssh-key-injection (FR-006a) | No | — | No task for CI key injection |
| unit-command-single (FR-007) | Yes | T001, T009, T038 |  |
| integration-marker (FR-008) | Yes | T001, T003 |  |
| skip-integration-by-default (FR-008a) | Yes | T002, T038 |  |
| skip-when-env-missing (FR-008b) | Yes | T003 |  |
| vm-config-ubuntu-btrfs (FR-009) | Yes | T004, T005 |  |
| vm-testuser-sudo (FR-010) | Yes | T005 |  |
| vm-inter-ssh (FR-011) | Yes | T006, T012 |  |
| two-vms-with-subvols (FR-011a) | Yes | T004, T005, T006 | Topology details implicit |
| cost-cap-10eur (FR-012) | No | — | Only doc mention via T029 |
| ci-lint-unit-on-push (FR-013) | Yes | T016 |  |
| ci-integration-pr-main (FR-014) | Yes | T017 |  |
| ci-manual-integration-trigger (FR-015) | Yes | T019 |  |
| ci-concurrency-control (FR-016) | Yes | T017, T008, T014 |  |
| ci-reset-before-tests (FR-017) | Partial | T015 | CI path not specified |
| ci-skip-when-secrets-missing (FR-017a) | No | — | No task for secret-based skip |
| ci-skip-forks (FR-017b) | Yes | T017 |  |
| ci-preserve-artifacts (FR-017c) | Yes | T018 |  |
| playbook-visual-verification (FR-018) | Yes | T020 |  |
| playbook-feature-tour (FR-019) | Yes | T021 |  |
| playbook-usable-onboarding (FR-020) | Yes | T020, T021 |  |
| dev-guide-writing-tests (FR-021) | Yes | T022 |  |
| dev-guide-vm-patterns (FR-022) | Yes | T023 |  |
| dev-guide-organization (FR-023) | Yes | T024 |  |
| dev-guide-troubleshooting (FR-024) | Yes | T025 |  |
| ops-guide-secrets-and-local (FR-025) | Partial | T026 | Local dev secrets missing |
| ops-guide-provisioning (FR-026) | Yes | T027 |  |
| ops-guide-env-vars (FR-027) | Yes | T028 |  |
| ops-guide-cost-monitoring (FR-028) | Yes | T029 |  |
| ops-guide-runbooks (FR-029) | Yes | T030 |  |
| architecture-diagrams-mermaid (FR-030) | Partial | T031, T032 | Format for interaction diagram unstated |
| architecture-rationale (FR-031) | Yes | T033 |  |
| architecture-adr-links (FR-032) | Yes | T034 |  |
| doc-file-structure (FR-033) | Yes | T020-T034 | Files enumerated |
| vm-command-fixtures (FR-034) | Yes | T012, T013 |  |
| reset-fast-btrfs (SC-001) | No | — | No performance validation task |
| ci-coverage-100-percent (SC-002) | Partial | T016, T017 | No monitoring/alerting |
| lock-blocks-concurrency (SC-003) | Partial | T008, T014, T017 | No contention testing |
| playbook-covers-visuals (SC-004) | Yes | T020 |  |
| cost-under-10 (SC-005) | No | — | No enforcement/measurement |
| developer-guide-enables-new-dev (SC-006) | Yes | T022-T025 |  |
| ops-guide-enables-setup (SC-007) | Yes | T026-T030 |  |
| architecture-understandable (SC-008) | Yes | T031-T034 |  |

**Constitution Alignment Issues:** C1, C2 (both CRITICAL)

**Unmapped Tasks:** T035, T036 (not tied to a specific requirement)

**Metrics:** Total Requirements 50; Total Tasks 38; Coverage ≥1 task 90% (45/50, counting partials); Ambiguity Count 1; Duplication Count 1; Critical Issues Count 2.

## Next Actions

- Resolve CRITICAL constitution violations before `/speckit.implement` (add principle annotations and owned performance/evidence tasks).
- Add tasks for SSH key injection, cost cap enforcement, CI secret checks, and CI reset flow.
- Clarify unit-test safety criteria and Mermaid format for all diagrams.
- Would you like me to suggest concrete remediation edits for the top 3 issues?
