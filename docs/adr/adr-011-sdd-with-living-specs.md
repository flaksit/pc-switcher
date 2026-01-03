# ADR-011: Specification-Driven Development with Living Specs

Status: Accepted
Date: 2026-01-02

## TL;DR
Adopt a hybrid SDD workflow where `specs/` folders are immutable history and `docs/system/` is the living "Golden Copy", using semantic IDs to prevent reference rot.

## Implementation Rules

### Core Principles
- **Immutable History**: `specs/` folders (e.g., `specs/001-foundation`) are frozen after feature completion. Never edit them to reflect current state.
- **Living Truth**: `docs/system/` must always reflect the current state of the code in `main`.

### Simple Merge Strategy
Domain specs (`docs/system/*.md`) are direct consolidations of `spec.md` files:
- Merge everything from the User Stories and down: Acceptance Criteria, Requirements, Edge Cases and other sections *verbatim* (or nearly so). Modify existing content as needed.
- When a new feature modifies existing behavior, edit the *existing* items in the living doc, rather than appending new ones. This ensures the document always reads as a coherent specification of the *current* system.
- Do not remove items: bar them out, mark as deprecated (removed and nothing replaces it) or superseded (removed and replaced by something completely different that we should not cover by a change of the current item) and replace the content text with a one-liner summary of the old content.

**Data Model**: Merge `specs/00x/data-model.md` files into `docs/system/data-model.md`, so it is a consolidation of all `data-model.md` files produced by SpecKit.

**Architecture**: Update `docs/system/architecture.md` as needed to reflect the current high-level design. Do NOT include low-level implementation details, but refer to the SpecKit `architecture.md` files for that.

### Lineage Tracking
Maintain references to the original spec in the living docs. E.g., "Lineage: 004-US-1 → 005-US-7", which means the item originated in SpecKit run 004 as User Story 1 and was modified by User Story 7 from run 005. Do this for all items, not only user stories.

### Semantic ID Strategy
Use stable, semantic IDs in `docs/system/` and in code comments. Format: `<DOMAIN>-<TYPE>-<DESCRIPTOR>`:
- `<DOMAIN>`: Short code for the domain (e.g., `LOG` for Logging, `FND` for Foundation)
- `<TYPE>`: Type of item (e.g., `US` for User Story, `FR` for Functional Requirement, `SC` for Success Criterion)
- `<DESCRIPTOR>`: Concise, human-readable description (e.g., `EXT` for "External Systems", `ROTATION` for "Log Rotation")

Examples:
- Instead of `US-2`, use `LOG-US-EXT`
- Instead of `FR-005`, use `LOG-FR-ROTATION`
- Instead of `SC-002`, use `TST-SC-ATOMICITY`

In code: `# Implements LOG-FR-ROTATION`

**Temporary IDs**: Use sequential IDs (e.g., `FR-001`) only within active SpecKit runs (`specs/00x`). These are local to that run.

### Workflow A: Big Feature (Standard SDD)
1. **Run SpecKit**: Create `specs/005-feature`. Generate `spec.md`, `plan.md`, `tasks.md`.
2. **Implement**: Write code. SpecKit may use temporary IDs (`FR-001`) in comments during development.
3. **Consolidation Step**: Upon feature completion:
   - Open the relevant `docs/system/<topic>.md` (can be multiple files)
   - Merge the contents from `specs/005/spec.md` into these domain spec files, modifying existing content as needed
   - If an element was removed, don't delete it; bar it and mark it clearly as deprecated or superseded
   - Add lineage note for each item, e.g., "Lineage: 001-FR-002" for first-time items and "Lineage: 002-FR-007 → 005-FR-013" for modified items
   - Convert IDs to Semantic IDs
   - Find/Replace any SpecKit IDs referenced in code/tests/docs with their Semantic ID counterpart
   - Update `docs/system/data-model.md` with new entities
4. **Commit**: The feature is done. `specs/005` is now frozen history.

### Workflow B: Small Fix (Fast Track)
1. **Edit Truth First**: Open `docs/system/<topic>.md`.
   - Modify the text or add new content
   - Add lineage notes for each change (e.g., referencing a GitHub issue ID)
2. **Implement**: Update code and tests to match the modified spec.
3. **Commit**: Mention the Semantic ID(s) in the commit message.

## Context

We currently use a Specification Driven Development (SDD) workflow powered by SpecKit for laying out foundations and implementing large features. This works well for major initiatives but presents significant friction for smaller fixes and extensions.

### The Problems
1. **High Overhead**: Running the full SpecKit cycle (spec → plan → tasks) is too slow for small changes.
2. **Documentation Drift**: Small changes often bypass the spec process entirely, leaving `specs/` folders outdated.
3. **Reference Rot**: Code and tests refer to numbered requirements (e.g., `FR-005`) defined in old spec folders. When requirements change or are superseded, these references become ambiguous or misleading.
4. **Fragmented Truth**: The "current state" of the system is scattered across multiple `specs/00x-...` folders, making it difficult to understand the actual system architecture and behavior.

### Core Philosophy: Immutable History vs. Living Truth

Strictly distinguish between **Artifacts of Work** (what we did and why) and **Artifacts of Knowledge** (what the system is now).
- **Immutable History (`specs/00x-.../`)**: Represent the plan and state of the world *at the time the feature was built*. Once a feature is shipped, these folders are **never touched again**. They serve as audit trail.
- **Living Truth (`docs/system/`)**: This is the "Golden Copy": the authoritative, current definition of the system. It must always reflect the code in `main`.

## Decision
We will adopt a hybrid workflow that distinguishes between "Artifacts of Work" and "Artifacts of Knowledge":

### 1. Establish `docs/system/`
This directory will hold the "Golden Copy" of the system specification.
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

### 2. Semantic ID Strategy
Docs in the Golden Copy will use semantic identifiers (e.g., `LOG-FR-ROTATION`) instead of sequential numbers to avoid collisions (same numbers from multiple SpecKit runs) and ambiguity. All files (including code) must reference these semantic IDs.

A Semantic ID has format `<DOMAIN>-<TYPE>-<DESCRIPTOR>`.
   - `<DOMAIN>` is a short code for the domain (e.g., `LOG` for Logging, `FND` for Foundation).
   - `<TYPE>` is the type of item (e.g., `US` for User Story, `FR` for Functional Requirement, `SC` for Success Criterion).
   - `<DESCRIPTOR>` is a concise, human-readable description (e.g., `EXT` for "External Systems", `ROTATION` for "Log Rotation", `ATOMICITY` for "Atomicity").

Examples:
   - Instead of `US-2`, use `LOG-US-EXT`
   - Instead of `FR-005`, use `LOG-FR-ROTATION`
   - Instead of `SC-002`, use `TST-SC-ATOMICITY`

### 3. Workflow Split
   - **Big Features**: Continue using SpecKit (`specs/00x`). Allow SpecKit to use whatever the tool generates (e.g., `FR-001`, `SC-001`). Add a final "Consolidation" step to merge outcomes into `docs/system/` and update code references.
   - **Small Fixes**: Edit `docs/system/` directly, then implement the change.

## Consequences
- **Positive**: Significantly reduces friction for maintenance and small features.
- **Positive**: Eliminates ambiguity in code references (Semantic IDs are self-describing).
- **Positive**: Provides a single, navigable location for the current system architecture.
- **Negative**: Requires discipline to perform the "Consolidation" step manually after big features.
- **Negative**: Two ID systems (temporary vs. permanent) may cause confusion during the transition of a feature.
