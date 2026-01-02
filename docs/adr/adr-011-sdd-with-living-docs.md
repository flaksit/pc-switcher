# ADR-011: Specification-Driven Development with Living Specs

Status: Accepted
Date: 2026-01-02

## TL;DR
Adopt a hybrid SDD workflow where `specs/` folders are immutable history and `docs/system/` is the living "Golden Copy", using semantic IDs to prevent reference rot.

## Implementation Rules
- **Immutable History**: `specs/` folders (e.g., `specs/001-foundation`) are frozen after feature completion. Never edit them to reflect current state.
- **Living Truth**: `docs/system/` must always reflect the current state of the code in `main`.
- **Simple Merge**: Domain specs (`docs/system/*.md`) are direct consolidations of `spec.md` files.
  - Merge everything from the User Stories and down: Acceptance Criteria, Requirements, Edge Cases and other sections *verbatim* (or nearly so). Modify existing content as needed.
  - Do not remove items; bar them out and mark as deprecated or superseded.
- **Lineage**: Maintain references to the original spec in the living docs. E.g., "Lineage: 004-US-1 → 005-US-7", which means the item originated in SpecKit run 004 as User Story 1 and was modified by User Story 7 from run 005. Do this for all items, not only user stories.
- **Semantic IDs**: Use stable, semantic IDs (e.g., `LOG-FR-ROTATION`) in `docs/system/` and in code comments. A Semantic ID has format `<DOMAIN>-<TYPE>-<DESCRIPTOR>`.
- **Temporary IDs**: Use sequential IDs (e.g., `FR-001`) only within active SpecKit runs (`specs/00x`).
- **Consolidation**: Upon completing a SpecKit run, requirements must be merged into `docs/system/` and IDs converted to semantic ones. Any references in code, tests or documentation must be updated accordingly.
- **Data Model Centralization**: The contents of `data-model.md` is merged into `docs/system/data-model.md`.
- **Architecture Updates**: `docs/system/architecture.md` is updated as needed to reflect the current high-level design.
- **Big Feature**: Use SpecKit for large features, followed by a consolidation step.
- **Fast Track**: Small fixes and maintenance tasks should edit `docs/system/` directly, skipping the SpecKit folder creation. Lineage/changelog notes must be added for each change, e.g. refering to a GitHub issue ID.

## Context
The project currently uses SpecKit for all changes. While effective for large foundational features, this process has high overhead for small fixes, leading to "documentation drift" where small changes bypass the spec process. Additionally, using sequential IDs (like `FR-005`) in code leads to "reference rot" as those IDs become ambiguous or hard to trace back to the current system behavior when scattered across multiple historical spec folders.

## Decision
We will adopt a hybrid workflow that distinguishes between "Artifacts of Work" and "Artifacts of Knowledge":

1. **Establish `docs/system/`**: This directory will hold the "Golden Copy" of the system documentation (Architecture, Data Model, Domain Specs).
2. **Semantic ID Strategy**: Requirements in the Golden Copy will use semantic identifiers (e.g., `FND-FR-NET-RETRY`) instead of sequential numbers. Code must reference these semantic IDs.
3. **Workflow Split**:
    - **Big Features**: Continue using SpecKit (`specs/00x`). Add a final "Consolidation" step to merge outcomes into `docs/system/` and update code references.
    - **Small Fixes**: Edit `docs/system/` directly, then implement the change.

## Consequences
- **Positive**: Significantly reduces friction for maintenance and small features.
- **Positive**: Eliminates ambiguity in code references (Semantic IDs are self-describing).
- **Positive**: Provides a single, navigable location for the current system architecture.
- **Negative**: Requires discipline to perform the "Consolidation" step manually after big features.
- **Negative**: Two ID systems (temporary vs. permanent) may cause confusion during the transition of a feature.

## References
- [Final Proposal](considerations/011-spec-driven-dev+living-specs/final-proposal.md)
