# Spec Review 3 Resolution

## Arguments Passed
```
First, number their feedback, so that it is easier to refer to
```

## Numbered Feedback

### Blocking / Inconsistencies

1. **Provisioning flow conflicts**: docs/testing-framework.md describes manual tofu+installimage setup, while User Story 2 / FR-006 say VMs are auto-provisioned when integration tests run. Clarify the source of truth: is initial provisioning manual, auto-triggered by the test runner, or CI-only? Spell out how to avoid accidental VM creation/costs for local developers and how credentials/secrets are supplied in each mode.

2. **Reset strategy is underspecified**: FR-004 only says "reset to a clean baseline" but does not mandate persistent VMs with btrfs snapshot rollback. Codify the reset mechanism (btrfs snapshots of @/@home, rollback steps, reboot expectations) so implementers do not re-image or recreate VMs per run.

3. **VM topology is implicit**: The spec should explicitly require two VMs (source/target) with the expected btrfs subvolume layout to mirror real sync scenarios; otherwise a single-VM setup would still satisfy current FRs but violate ADR intent.

4. **Documentation location mismatch**: FR-033 points manual playbook to docs/testing-playbook.md, but the referenced doc currently lives at specs/001-foundation/testing-playbook.md. Clarify whether the playbook is being relocated or replaced.

### Gaps / Clarifications

5. **Locking underspecified**: FR-005/edge cases note manual cleanup, but there is no requirement for lock placement, format, TTL, or automatic release on failures (SSH unreachable, crash mid-run). Specify these to avoid stuck locks and to align with CI concurrency controls (double-layer behavior).

6. **Baseline health undefined**: Edge cases mention missing/invalid snapshots, but the spec does not define how validity is checked (presence, age, checksum?) or how to rebuild baselines. Add acceptance criteria/runbook coverage in the ops guide.

7. **Auto-provisioning details missing**: If auto-provisioning is required, define minimum provider spec (currently Hetzner CX23 in docs), region, and cost guardrails, and whether provisioning is allowed outside CI. Tie FR-012 (<€10/mo) to concrete enforcement (e.g., static VM types + periodic teardown) instead of assumptions.

8. **CI commands unspecified**: CI requirements name "type checks, lint checks" but do not specify the commands/tools (ruff, basedpyright) that are project standards. Add concrete commands to avoid ambiguity and ensure alignment with repo conventions.

9. **Artifact/debuggability expectations missing**: Define what logs/artifacts are persisted (pytest logs, VM syslogs, provisioning/reset logs) and how they are exposed in CI to make failures actionable.

10. **Developer/ops documentation scope incomplete**: FR-021–029 don't mention worked examples (sample integration test skeletons, fixture usage, SSH/btrfs code snippets), lock cleanup/runbook steps, baseline rebuild instructions, or secret rotation. Consider making these explicit to ensure the docs are testable against Acceptance Scenarios in Stories 5–6.

11. **Manual playbook acceptance criteria are qualitative**: Add expectations for evidence of success (e.g., screenshots or recorded outputs, checklist of visual elements) to avoid subjective outcomes and to make the playbook runnable in CI artifacts if needed.

12. **Out-of-scope note on fixtures may be too broad**: Out-of-scope excludes "test data generation fixtures," but the framework may still need minimal shared fixtures to make integration tests feasible (e.g., helpers for creating btrfs subvolumes, test file trees, permission matrices). Consider pulling a minimal baseline fixture set into scope or explicitly deferring integration-test authoring until a follow-up feature.

### Suggestions

13. **Add runtime contract section**: Add an explicit section summarizing the runtime contract for integration tests: required env vars, how skips are signaled, lock acquisition/release flow, reset timing, and provisioning behavior. This will reduce scattering across Edge Cases/FRs.

14. **Add workflow diagram**: Include a Mermaid diagram for the testing workflow (provision → baseline → lock → reset → tests → artifacts → unlock) to satisfy FR-030 and give a quick systems view.

---

## Conversation Log

### Agent Analysis

I've read through the feedback and numbered all points. Let me present my analysis:

**Points I agree with and can address directly:**

- **#2, #3**: These are valid - the spec should explicitly specify the two-VM topology with btrfs snapshot rollback mechanism. This is already detailed in docs/testing-framework.md and ADR-006, but should be explicit in the spec.

- **#4**: Valid - FR-033 says docs/testing-playbook.md but the current file is at specs/001-foundation/testing-playbook.md. The spec intends to relocate it to docs/.

- **#5**: Valid - lock details (location, format, TTL) should be specified. docs/testing-framework.md already has some details (/tmp/pc-switcher-integration-test.lock, 5 min timeout).

- **#6**: Valid - need to clarify what "valid" baseline means and how to rebuild.

- **#8**: Valid - can add specific tools (ruff, basedpyright, pytest).

- **#9**: Valid - CI artifact/log expectations should be specified.

- **#10, #11**: Valid - documentation scope can be more specific.

- **#13, #14**: Valid suggestions - adding a runtime contract section and workflow diagram would improve clarity.

**Points requiring clarification/decision:**

- **#1**: The apparent conflict between "auto-provisioned" (spec) and "manual tofu+installimage" (docs) needs resolution. Looking at the documents:
  - docs/testing-framework.md describes initial setup as manual
  - FR-006 says "automatically provisioned when integration tests are run and VMs do not exist"

  **Question**: Should provisioning be:
  - (A) Always manual (developer runs tofu + provision script), OR
  - (B) Auto-triggered in CI if VMs don't exist, but manual for local developers?

- **#7**: Related to #1 - should we enforce that auto-provisioning only happens in CI? Should we add explicit guardrails?

- **#12**: The out-of-scope note on fixtures - should we:
  - (A) Keep it out of scope (no fixtures in this feature, add when writing actual tests), OR
  - (B) Add minimal baseline fixtures (VM connection, SSH execution) to this feature scope?

### User Responses

**Q1 (Provisioning - #1, #7):**
> I want it as described now in spec.md. We'll need to update testing-framework.md. The latter document is important, but not the bible on every detail.

**Decision**: spec.md is the source of truth. Auto-provisioning stays as specified. docs/testing-framework.md will be updated during implementation to align.

**Q2 (Fixtures - #12):**
> B But not much because I think the developer won't need much: just know how to launch commands on the test-VMs (a kind of RemoteExecutor ;-) )

**Decision**: Include minimal fixtures in scope - specifically a RemoteExecutor-like capability for running commands on test VMs.

**On remaining feedback:**
> For the rest, I think the reviewer asks a lot of detailed tech info that is not in scope for the spec phase.

**Decision**: Many points (#5, #6, #7, #10, #11) request implementation details that belong in architecture/ops documentation, not in the spec. The spec defines WHAT, not HOW in exhaustive detail.

---

## Resolution Summary

| # | Feedback | Resolution |
|---|----------|------------|
| 1 | Provisioning flow conflicts | No change - spec is source of truth; docs/testing-framework.md updated during implementation |
| 2 | Reset strategy underspecified | Add explicit mention of btrfs snapshot rollback to FR-004 |
| 3 | VM topology implicit | Add explicit two-VM requirement with btrfs subvolume layout |
| 4 | Documentation location mismatch | Clarify playbook relocation in FR-033 (existing file will be relocated) |
| 5 | Locking underspecified | No change - implementation detail for ops guide |
| 6 | Baseline health undefined | No change - implementation detail for ops guide |
| 7 | Auto-provisioning details | No change - implementation detail; cost constraint (€10/mo) is sufficient |
| 8 | CI commands unspecified | Add specific tools (ruff, basedpyright) to FR-013 |
| 9 | Artifact expectations missing | Add CI artifact/log preservation requirement |
| 10 | Docs scope incomplete | No change - specific examples are implementation detail |
| 11 | Playbook criteria qualitative | No change - checklist format is implementation detail |
| 12 | Fixtures out of scope too broad | Move minimal fixtures (RemoteExecutor-like) into scope |
| 13 | Add runtime contract section | No change - info exists, consolidation is optional |
| 14 | Add workflow diagram | No change - FR-030 already requires diagrams |

---

## Changes Made to spec.md

1. **FR-004**: Added "using btrfs snapshot rollback (not VM re-imaging or reprovisioning)" to clarify reset mechanism

2. **FR-011a** (new): Added explicit two-VM topology requirement with btrfs subvolume layout (`@` and `@home`)

3. **FR-013**: Added specific tools: "(basedpyright), lint checks (ruff), and unit tests (pytest)"

4. **FR-017c** (new): Added CI artifact/log preservation requirement

5. **FR-033**: Added clarification that playbook "relocates existing `specs/001-foundation/testing-playbook.md`"

6. **FR-034** (new): Added minimal pytest fixtures requirement for VM command execution (RemoteExecutor-like)

7. **Key Entities**: Added **VMExecutor** entity for the VM command execution fixture

8. **Out of Scope**: Clarified that minimal fixtures for VM command execution ARE in scope (see FR-034)

