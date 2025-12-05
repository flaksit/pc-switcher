# Spec Review 1 Resolution

## ARGUMENTS

(none provided)

## Conversation Log

### Agent Analysis and Questions

After reading the spec review feedback, I identified the following points requiring clarification:

**Questions for you:**

1. **Auto-provisioning (point 1):** Should we:
   - **Option A**: Keep auto-provisioning as a feature goal (detect if VMs don't exist, provision them automatically via CI) - but this adds complexity
   - **Option B**: Make provisioning explicitly manual (a documented prerequisite) and only resets are automatic

   Based on the pre-analysis, Option B seems like the realistic current design. Is that correct?

2. **Lock robustness (point 3):** The reviewer asks for TTL/heartbeat/owner metadata and recovery path. Given the concurrency is already handled at GitHub Actions level (`concurrency.group`), how robust does the lock.sh script need to be? Options:
   - **Option A**: Simple file-based lock as designed (accept that stuck locks need manual cleanup)
   - **Option B**: Add TTL-based stale lock detection (e.g., if lock is >1 hour old, consider it stale)

   I lean toward Option B with a reasonable TTL (e.g., 1 hour) since tests shouldn't run that long.

3. **Reset SLO (point 4):** 30 seconds per VM seems tight (need snapshot ops + reboot + SSH reconnect). Should we:
   - **Option A**: Increase to 60 seconds per VM
   - **Option B**: Define it as total reset time for all VMs (e.g., 90 seconds for both)
   - **Option C**: Remove the specific time target and just say "fast reset via btrfs snapshots"

4. **CI scope and secrets (point 6):** For forked PRs and missing secrets, should we:
   - **Option A**: Skip integration tests with clear notice (integration tests only run on PRs from the main repo)
   - **Option B**: Fail early with guidance

### User Responses

> Answers to your questions:
> 1A
> 2A
> 3C
> 4A
>
> For feedback 8: no measurement/alerting necessary for now. The cost is fixed by the choice of the cloud provider and the VM type.
> For feedback 9: Hetzner is an implementation detail. Will be specified in subsequent speckit phases. Not here.

### Follow-up: FR-008a/b Clarification

**User**: I want to be able to run the integration tests when the env vars are set. But it's best that they are not run by default on a local machine, even if the env vars are set.

**Resolution**: Split into two requirements:
- FR-008a: `uv run pytest` never runs integration tests (even with env vars set)
- FR-008b: When explicitly requested (`-m integration`), skip with message if env vars not configured

### Follow-up: FR-033 Documentation Structure

**User**: Operational guide and manual playbook should be distinct documents

**Resolution**: Updated FR-033 to specify four separate files:
- `docs/testing-framework.md` (architecture)
- `docs/testing-developer-guide.md` (developer guide)
- `docs/testing-ops-guide.md` (operational guide)
- `docs/testing-playbook.md` (manual playbook)

## Resolution Summary

| Review Point | Feedback | Resolution |
|--------------|----------|------------|
| 1. Auto-provisioning vs current design | User Story 2 + FR-006 promise auto-provisioning but docs require manual steps | **Clarified**: Auto-provisioning is the goal. Updated User Story 2 and FR-006 to explicitly state provisioning includes cloud VM creation and OS installation with btrfs via rescue mode. |
| 2. Baseline reset prerequisites | No requirement for baseline snapshots to exist or fail-fast when missing | **Added**: FR-004 now requires reset to fail with actionable error if baselines missing. FR-006 requires baseline snapshots created at provisioning time. New edge case documented. |
| 3. Lock robustness | No stale lock detection, TTL, or recovery path | **Kept simple per user decision**: FR-005 clarified that stuck locks require manual cleanup (documented in operational guide). Added edge case for crashed test runs. |
| 4. Realistic reset SLO | SC-001 requires <30s which may be optimistic | **Removed specific time target per user decision**: SC-001 now says "fast due to btrfs snapshot rollback" without specific time constraint. |
| 5. Safe default test commands | No requirement that integration tests skip when env vars missing | **Added**: FR-008a requires integration tests to never run by default (even with env vars). FR-008b requires skip with message when explicitly requested but env vars missing. |
| 6. CI scope and secrets | No coverage of forked PRs or missing secrets | **Added**: FR-017a and FR-017b require CI to skip integration tests with clear notice when secrets unavailable (e.g., forked PRs). Integration on forks explicitly not supported. |
| 7. Documentation placement & format | Playbook location unclear, no Mermaid requirement | **Clarified**: FR-033 specifies four separate files including `docs/testing-playbook.md`. FR-030 requires all diagrams in Mermaid format per repo standards. |
| 8. Cost requirement enforcement | No measurement/alerting for €10/month cap | **No change needed per user decision**: SC-005 clarified that cost is constrained by infrastructure choices rather than active monitoring. |
| 9. Provider specificity | Assumptions mention generic "cloud provider" | **No change needed per user decision**: Hetzner is implementation detail to be specified in later speckit phases, not in spec. |

## Changes Made to spec.md

1. **User Story 2**: Clarified auto-provisioning includes OS installation with btrfs via rescue mode
2. **User Story 2 Acceptance Scenario 2**: Added fail-fast behavior when baseline snapshots missing
3. **Edge Cases**: Added 5 new edge cases:
   - Baseline snapshots missing/invalid
   - Test run crashes without releasing lock
   - CI secrets misconfigured/missing
   - PR from forked repository
   - VM environment variables not set locally
4. **FR-004**: Added requirement for actionable error when baselines missing
5. **FR-005**: Clarified lock stores holder identity and requires manual cleanup for stuck locks
6. **FR-006**: Expanded to include full provisioning scope and baseline snapshot creation
7. **FR-008**: Clarified pytest marker syntax
8. **FR-008a**: New requirement that integration tests never run by default (even with env vars set)
9. **FR-008b**: New requirement for skip with message when integration explicitly requested but env vars missing
10. **FR-014**: Clarified "from main repository only"
11. **FR-017a**: New requirement for CI to skip integration with notice when secrets unavailable
12. **FR-017b**: New requirement explicitly stating forked PR integration not supported
13. **FR-030**: Added Mermaid format requirement for diagrams
14. **FR-033**: Specified four separate documentation files (architecture, developer guide, ops guide, playbook)
15. **SC-001**: Removed specific 30s target, replaced with qualitative "fast due to btrfs snapshot rollback"
16. **SC-002**: Clarified "from main repository"
17. **SC-005**: Clarified cost constrained by infrastructure choices, not active monitoring
