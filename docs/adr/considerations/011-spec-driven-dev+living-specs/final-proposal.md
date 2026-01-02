# Proposal: Hybrid Specification-Driven Development with Living Documentation

## Context and Motivation

We currently use a Specification Driven Development (SDD) workflow powered by SpecKit for laying out foundations and implementing large features. This works well for major initiatives but presents significant friction for smaller fixes and extensions.

**The Problem:**
1. **High Overhead:** Running the full SpecKit cycle (spec -> plan -> tasks) is too slow for small changes.
2. **Documentation Drift:** Small changes often bypass the spec process entirely, leaving `specs/` folders outdated.
3. **Reference Rot:** Code and tests refer to numbered requirements (e.g., `FR-005`) defined in old spec folders. When requirements change or are superseded, these references become ambiguous or misleading.
4. **Fragmented Truth:** The "current state" of the system is scattered across multiple `specs/00x-...` folders, making it difficult to understand the actual system architecture and behavior.

## The Core Philosophy: Immutable History vs. Living Truth

To solve this, we must strictly distinguish between **Artifacts of Work** (what we did and why) and **Artifacts of Knowledge** (what the system is now).

* **Immutable History (`specs/00x-.../`):** These are project folders. They represent the plan and state of the world *at the time the feature was built*. Once a feature is shipped, these folders are **never touched again**. They serve as an audit trail.
* **Living Truth (`docs/system/`):** This is the "Golden Copy." It contains the authoritative, current definition of the system. It must always reflect the code in `main`.

## Directory Structure

We will introduce a `docs/system/` folder to hold the Golden Copies, separating them from the historical SpecKit runs.

```text
/
├── specs/                      # HISTORY (Immutable)
│   ├── 001-foundation/         # Old run (frozen)
│   ├── 002-testing/            # Old run (frozen)
│   └── 005-new-feature/        # Active SpecKit run
│
├── docs/
│   └── system/                 # TRUTH (Living Golden Copy)
│       ├── _index.md           # Entry point
│       ├── architecture.md     # Current high-level design & component map
│       ├── data-model.md       # Consolidated data model
│       ├── logging.md          # Consolidated domain spec
│       ├── testing.md          # Consolidated domain spec
│       └── foundation.md       # Consolidated domain spec
```

## The "Simple Merge" Strategy

We avoid the burden of "translating" specs into new formats (like "Capabilities"). Instead, we treat the domain spec files in `docs/system/` as direct consolidations of the `spec.md` files produced by SpecKit.

1. **Content:** We merge everything from the User Stories and down: Acceptance Criteria, Requirements, Edge Cases and other sections *verbatim* (or nearly so) from the `spec.md` files into the corresponding `docs/system/` file. Merge changes with existing content as needed.
2. **Lineage:** We maintain references to the original spec. E.g. "Lineage: 004-US-1 → 005-US-7", which means the item originated in SpecKit run 004 as User Story 1 and was modified by User Story 7 from run 005.
3. **Updates:** When a new feature modifies existing behavior, we edit the *existing* User Story in the living doc, rather than appending a new one. This ensures the document always reads as a coherent specification of the *current* system.
4. **Removals:** If an item is removed, we do not delete it. Instead, we bar it out and mark it clearly as deprecated or superseded.

**Data Model:** `docs/system/data-model.md` is a merge of all `data-model.md` files produced by SpecKit.

**Architecture:** `docs/system/architecture.md` is updated as needed to reflect the current high-level design. It does NOT contain low-level implementation details, but refers to the SpecKit `architecture.md` files for that.

## The ID Strategy: Semantic over Sequential

We solve the "FR-001 collision" and "reference rot" problems by using **Semantic IDs** in the Living Truth, while allowing SpecKit to use temporary sequential IDs during development.

1. **In SpecKit (`specs/00x`):** Use whatever the tool generates (e.g., `FR-001`, `SC-001`). These are **temporary, local IDs**.
2. **In Living Docs (`docs/system`):** Use stable, **Semantic IDs**.
    * Instead of `US-2`, use `US-LOG-EXT`.
    * Instead of `FR-005`, use `FR-LOG-ROTATION`.
    * Instead of `SC-002`, use `SC-FOUNDATION-ATOMICITY`.
3. **In Code:** Always reference the **Semantic ID**.
    * `# Implements FR-LOG-ROTATION`

## Workflows

### Scenario A: The "Big Feature" (Standard SDD)

1. **Run SpecKit:** Create `specs/005-feature`. Generate `spec.md`, `plan.md`, `tasks.md`.
2. **Implement:** Write code. Speckit may use temporary IDs (`FR-001`) in comments during development.
3. **The "Consolidation" Step:**  Upon feature completion,
    * Open the relevant `docs/system/<topic>.md`. This can be multiple files!
    * Merge the contents from `specs/005/spec.md` into these domain spec files, modifying existing content as needed.
    * If an element was removed, don't delete it; bar it and mark it clearly as deprecated or superseded.
    * Add lineage note for each item, e.g. "Lineage: 005-FR-013".
    * Convert IDs to Semantic IDs.
    * Find/Replace any speckit IDs referenced in code/tests/docs with their Semantic IDs counterpart.
    * Update `docs/system/data-model.md` with new entities.
4. **Commit:** The feature is done. `specs/005` is now frozen history.

### Scenario B: The "Small Fix" (Fast Track)

1. **Edit Truth First:** Open `docs/system/<topic>.md`.
    * Modify the text or add new content.
    * Add lineage notes for each change.
2. **Implement:** Update code and tests to match the modified spec.
3. **Commit:** Link the commit to the spec change: mention the Semantic ID in the commit message.
