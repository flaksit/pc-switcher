Detailed plan review for 003-foundation-tests plan artifacts (plan.md, research.md, quickstart.md, data-model.md, contracts/coverage-map.yaml).

1) FR-009 (test independence) is not covered: None of the plan documents describe how tests will avoid order/shared-state coupling or how to validate independence (e.g., per-test isolation guidance or CI check). Add explicit guidance and a verification step so the suite cannot silently rely on execution order.

2) FR-004 / SC-004 (success + failure paths) are only partially addressed: data-model.md and coverage-map.yaml list one test per requirement but don’t show that each requirement has both positive and negative coverage. Edge cases are listed separately without linking them back to the relevant FR/AS. Clarify, per requirement, which tests cover failure/error paths so we can prove SC-004 and avoid gaps (e.g., ensure install, snapshot, logging, and interrupt requirements all have explicit failure-path tests). 
