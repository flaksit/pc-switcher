# Spec Review 2 Resolution

## ARGUMENTS

(none provided)

## Conversation Log

### Agent Analysis and Questions

After reading the spec review feedback, I identified the following points requiring clarification:

1. **Point 1 (Race Condition during Provisioning):** The review raises a valid concern about concurrent provisioning attempts when no VMs exist. However:
   - CI is already protected by GitHub Actions `concurrency.group`
   - Local developer runs seem rare to collide during provisioning

   Should we:
   - **Option A**: Accept the race condition as rare (provisioning only happens once when VMs don't exist)
   - **Option B**: Add a requirement that the lock mechanism must work independently of VMs (e.g., cloud API-based lock, separate storage)
   - **Option C**: Add a clarifying note that provisioning is protected by CI concurrency controls, and local concurrent provisioning is not supported

2. **Point 2 (Rescue Mode terminology):** The review correctly points out that "rescue mode" is provider-specific (Hetzner). In Resolution 1, you said Hetzner is an implementation detail. Should FR-006 be updated to:
   - **Option A**: Remove "via rescue mode" and say "via provider-specific OS installation mechanism"
   - **Option B**: Remove the mechanism detail entirely and just say "OS installation with btrfs filesystem"

3. **Point 3 (Deterministic VM Naming):** Should we add a requirement for deterministic VM naming to ensure VMs can be reused and prevent resource leaks?
   - **Option A**: Yes, add FR-006a requiring deterministic naming
   - **Option B**: No, this is an implementation detail for later phases

4. **Point 4 (Test Data Fixtures):** The review suggests adding test data generation helpers. This seems like it could be useful but might be scope creep for a "framework" spec. Should we:
   - **Option A**: Add a requirement for test data fixtures
   - **Option B**: Consider this out of scope (implementation detail for when tests are written)
   - **Option C**: Add to Out of Scope section to explicitly defer it

5. **Point 5 (SSH Key Distribution):** How should SSH keys be handled during auto-provisioning? Options:
   - **Option A**: Add a requirement specifying keys are injected from environment/CI secrets during provisioning
   - **Option B**: This is an implementation detail for the operational guide (FR-026 already mentions SSH key management in provisioning instructions)

### User Responses

> 1C
> 2B
> 3B
> 4C to honor YAGNI
> 5A Keys for testuser@pc1 and testuser@pc2 should not be known by anyone; just know by each other. There should be a GitHub CI secret with a private/public key pair and the public key should be in the authorized_keys of testuser@pc1 and testuser@pc2 and should be used during provisioning. This is technical detail, but as we're talking about it now anyway, put it in the spec.md without too much detail ;-)

## Resolution Summary

| Review Point | Feedback | Resolution |
|--------------|----------|------------|
| 1. Locking Mechanism & Provisioning Race Condition | Race condition when two processes try to provision simultaneously | **Clarified in FR-006**: Added note that concurrent provisioning is prevented by CI concurrency controls; local concurrent provisioning is not supported. |
| 2. Provider Agnosticism vs "Rescue Mode" | "Rescue mode" is Hetzner-specific terminology | **Updated FR-006**: Removed "via rescue mode", now just says "OS installation with btrfs filesystem". |
| 3. Resource Lifecycle & Deterministic Naming | VMs should have deterministic names to prevent resource leaks | **No change**: This is an implementation detail for later phases. |
| 4. Test Data Generation Fixtures | Framework should provide test data helpers | **Added to Out of Scope**: Explicitly deferred per YAGNI principle. |
| 5. SSH Key Distribution during Auto-Provisioning | How are SSH keys handled during provisioning? | **Added FR-006a**: SSH keys injected from CI secrets during provisioning; public key in authorized_keys of both test user accounts for CI access and inter-VM communication. |

## Changes Made to spec.md

1. **FR-006**: Removed "via rescue mode" (provider-specific); added clarification that concurrent provisioning is prevented by CI concurrency controls (local concurrent provisioning not supported)
2. **FR-006a**: New requirement for SSH key injection during auto-provisioning; CI secrets provide key pair, public key installed in authorized_keys of test users on both VMs
3. **Out of Scope**: Added "Test data generation fixtures" with note that they will be added when actual tests are written
