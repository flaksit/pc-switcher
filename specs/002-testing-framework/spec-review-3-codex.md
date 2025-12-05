# Spec Review 3

## Blocking / inconsistencies
- Provisioning flow conflicts with current docs (`docs/testing-framework.md` describes manual tofu+installimage setup, while User Story 2 / FR-006 say VMs are auto-provisioned when integration tests run). Please clarify the source of truth: is initial provisioning manual, auto-triggered by the test runner, or CI-only? Spell out how to avoid accidental VM creation/costs for local developers and how credentials/secrets are supplied in each mode.
- Reset strategy is underspecified relative to ADR-006 and SC-001. FR-004 only says “reset to a clean baseline” but does not mandate persistent VMs with btrfs snapshot rollback. Codify the reset mechanism (btrfs snapshots of `@`/`@home`, rollback steps, reboot expectations) so implementers do not re-image or recreate VMs per run.
- VM topology is implicit. The spec should explicitly require two VMs (source/target) with the expected btrfs subvolume layout to mirror real sync scenarios; otherwise a single-VM setup would still satisfy current FRs but violate ADR intent.
- Documentation location mismatch: FR-033 points manual playbook to `docs/testing-playbook.md`, but the referenced doc currently lives at `specs/001-foundation/testing-playbook.md`. Clarify whether the playbook is being relocated or replaced.

## Gaps / clarifications
- Locking: FR-005/edge cases note manual cleanup, but there is no requirement for lock placement, format, TTL, or automatic release on failures (SSH unreachable, crash mid-run). Specify these to avoid stuck locks and to align with CI concurrency controls (double-layer behavior).
- Baseline health: Edge cases mention missing/invalid snapshots, but the spec does not define how validity is checked (presence, age, checksum?) or how to rebuild baselines. Add acceptance criteria/runbook coverage in the ops guide.
- Auto-provisioning details: If auto-provisioning is required, define minimum provider spec (currently Hetzner CX23 in docs), region, and cost guardrails, and whether provisioning is allowed outside CI. Tie FR-012 (<€10/mo) to concrete enforcement (e.g., static VM types + periodic teardown) instead of assumptions.
- CI requirements name “type checks, lint checks” but do not specify the commands/tools (ruff, basedpyright) that are project standards. Add concrete commands to avoid ambiguity and ensure alignment with repo conventions.
- Artifact/debuggability expectations for integration runs are missing. Define what logs/artifacts are persisted (pytest logs, VM syslogs, provisioning/reset logs) and how they are exposed in CI to make failures actionable.
- Developer/ops documentation scope: FR-021–029 don’t mention worked examples (sample integration test skeletons, fixture usage, SSH/btrfs code snippets), lock cleanup/runbook steps, baseline rebuild instructions, or secret rotation. Consider making these explicit to ensure the docs are testable against Acceptance Scenarios in Stories 5–6.
- Manual playbook acceptance criteria are qualitative. Add expectations for evidence of success (e.g., screenshots or recorded outputs, checklist of visual elements) to avoid subjective outcomes and to make the playbook runnable in CI artifacts if needed.
- Out-of-scope note excludes “test data generation fixtures,” but the framework may still need minimal shared fixtures to make integration tests feasible (e.g., helpers for creating btrfs subvolumes, test file trees, permission matrices). Consider pulling a minimal baseline fixture set into scope or explicitly deferring integration-test authoring until a follow-up feature.

## Suggestions
- Add an explicit section summarizing the runtime contract for integration tests: required env vars, how skips are signaled, lock acquisition/release flow, reset timing, and provisioning behavior. This will reduce scattering across Edge Cases/FRs.
- Include a Mermaid diagram for the testing workflow (provision → baseline → lock → reset → tests → artifacts → unlock) to satisfy FR-030 and give a quick systems view.
