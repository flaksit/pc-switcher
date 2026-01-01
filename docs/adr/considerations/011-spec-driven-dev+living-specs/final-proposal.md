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
│       ├── data-model.md       # Current schemas and data flows
│       ├── logging.md          # Domain-specific spec
│       ├── testing.md          # Domain-specific spec
│       └── orchestration.md    # Domain-specific spec
```

## The ID Strategy: Semantic over Sequential

We solve the "FR-001 collision" and "reference rot" problems by using **Semantic IDs** in the Living Truth, while allowing SpecKit to use temporary sequential IDs during development.

1. **In SpecKit (`specs/00x`):** Use whatever the tool generates (e.g., `FR-01`, `AC-01`). These are **temporary, local IDs**.
2. **In Living Docs (`docs/system`):** Use stable, **Semantic IDs**.
    * Instead of `FR-005`, use `REQ-LOG-ROTATION`.
    * Instead of `AC-002`, use `CON-SYNC-ATOMICITY`.
3. **In Code:** Always reference the **Semantic ID**.
    * `# Implements REQ-LOG-ROTATION`

## Workflows

### Scenario A: The "Big Feature" (Standard SDD)

1. **Run SpecKit:** Create `specs/005-feature`. Generate `spec.md`, `plan.md`, `tasks.md`.
2. **Implement:** Write code. You may use temporary IDs (`FR-01`) in comments during the heat of development if needed.
3. **The "Consolidation" Step (Crucial):**
    * Before closing the feature, open the relevant `docs/system/<topic>.md` (and `architecture.md`/`data-model.md`).
    * Copy the *outcome* requirements from `specs/005/spec.md` into the system docs.
    * **Convert IDs:** Change `FR-01` to `REQ-FEATURE-NAME`.
    * **Update Code:** Find/Replace any temporary `FR-01` references in code/tests with `REQ-FEATURE-NAME`.
4. **Commit:** The feature is done. `specs/005` is now frozen history.

### Scenario B: The "Small Fix" (Fast Track)

1. **Edit Truth First:** Open `docs/system/<topic>.md`.
    * Modify the text of an existing `REQ-EXISTING-FEATURE`.
    * Or add a new `REQ-NEW-TWEAK`.
2. **Implement:** Update code and tests to match the modified spec.
3. **Commit:** Link the commit to the spec change (e.g., `Update retry logic (ref REQ-NET-RETRY)`).
4. **Skip SpecKit:** Do not create a `specs/` folder. Do not update old `plan.md` files.

## Migration Strategy

We will adopt a **Lazy Migration** approach. We do not need to refactor the entire history immediately.

1. **Initialize:** Create `docs/system/` and move the current `architecture.md` and `data-model.md` there (likely from `specs/001-foundation` or the most recent source).
2. **On-Demand Creation:** The next time we touch a specific domain (e.g., Testing):
    * Create `docs/system/testing.md`.
    * Copy the *relevant, current* parts from `specs/002-testing/spec.md`.
    * Assign Semantic IDs to the requirements we are touching.
    * Update code comments to match.
3. **Legacy Folders:** Leave existing `specs/` folders as they are. They serve as the history of how we got here.
