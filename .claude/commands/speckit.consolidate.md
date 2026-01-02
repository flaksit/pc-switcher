---
description: Consolidate SpecKit artifacts into the Golden Copy living docs in docs/system/ per ADR-011
---

## User Input

```text
$ARGUMENTS
```

You **MUST** consider the user input before proceeding (if not empty).

## Outline

### Phase 1: Setup

1. Run `.specify/scripts/bash/check-prerequisites.sh --json --require-tasks --include-tasks` from repo root and parse FEATURE_DIR. All paths must be absolute. For single quotes in args like "I'm Groot", use escape syntax: e.g `'I'\''m Groot'` (or double-quote if possible: `"I'm Groot"`).

2. Extract feature number from FEATURE_DIR (e.g., "004" from "specs/004-python-logging").

3. Verify prerequisites:
   - **REQUIRED**: spec.md exists in FEATURE_DIR
   - **CHECK**: tasks.md shows all tasks completed (all items marked `[X]` or `[x]`)
   - If tasks are incomplete, **STOP** and inform user: "Implementation appears incomplete. Run `/speckit.implement` first or confirm consolidation should proceed anyway."

### Phase 2: Domain Detection

1. Dynamically discover existing domains by listing `docs/system/*.md` files, excluding:
   - `_index.md`
   - `architecture.md`
   - `data-model.md`

   For each domain file, extract the domain code from semantic IDs found within (e.g., `LOG-US-*` indicates domain code `LOG`).

2. Analyze spec.md content to determine which domain(s) the feature relates to:
   - Parse feature folder name for domain hints (e.g., "python-logging" suggests logging domain)
   - Look for references to existing semantic IDs in spec.md
   - Identify conceptual overlap with existing domain content

3. Apply domain decision logic:
   - **Exact match**: Feature clearly maps to exactly one existing domain → use that domain
   - **No overlap**: Feature doesn't overlap with ANY existing domain → automatically create new domain file
   - **Ambiguous overlap**: Feature overlaps with existing domain(s) AND unclear whether to extract or integrate:
     - Count User Stories in spec.md
     - If >5 User Stories → create new domain (substantial enough)
     - If ≤5 User Stories → **STOP** and ask user: "This feature has [N] user stories and overlaps with [DOMAIN]. Should it: (A) be integrated into existing [domain].md, or (B) create a new [suggested-name].md domain?"

4. **For domain splits** (extracting content from existing domain to new):
   a. Create new domain file `docs/system/<new-domain>.md` with standard structure
   b. Identify content in existing domain that belongs to new domain
   c. Move that content to new domain file
   d. Rename semantic IDs from old domain code to new (e.g., `FND-FR-LOG-*` → `LOG-FR-*`)
   e. Search codebase for old semantic IDs and replace with new ones
   f. Continue to Phase 3 with new domain as target

### Phase 3: Load & Analyze

1. Load artifacts from FEATURE_DIR:
   - **REQUIRED**: Read spec.md for user stories, requirements, success criteria, edge cases, entities
   - **IF EXISTS**: Read data-model.md for entity definitions
   - **IF EXISTS**: Read plan.md for high-level architectural context

2. Parse all temporary IDs from spec.md. Look for patterns:
   - User Stories: `User Story 1`, `User Story 2`, `US-1`, `US-2`, etc.
   - Functional Requirements: `FR-001`, `FR-002`, `FR-1`, etc.
   - Success Criteria: `SC-001`, `SC-002`, `SC-1`, etc.
   - Other items with sequential numbering

   Build inventory: `{temp_id, type, title, content}`

3. Load target living doc(s) and index all existing semantic IDs:
    - Parse headings for patterns like `(LOG-US-CONFIG)`, `**LOG-FR-HIERARCHY**`
    - Build index: `{semantic_id, type, title, location_in_file}`

4. Classify each spec item by comparing with existing living doc content:
    - **NEW**: No match in living doc → needs new semantic ID, will be appended
    - **UPDATE**: Matches existing item (same concept, related lineage) → keeps existing semantic ID, modify in-place
    - **SUPERSEDED**: Spec explicitly states it replaces existing item with something fundamentally different → mark old as superseded, add new item
    - **DEPRECATED**: Living doc item is removed by this feature with nothing replacing it → mark as deprecated

    Use fuzzy matching on titles and concepts. Look for explicit lineage references in spec.md like "supersedes FR-001" or "replaces existing logging".

### Phase 4: Generate ID Mapping

1. For each **NEW** item, generate semantic ID with format `<DOMAIN>-<TYPE>-<DESCRIPTOR>`:
    - `<DOMAIN>`: Extract from target file (e.g., `LOG` from logging.md, `FND` from foundation.md, `TST` from testing.md)
    - `<TYPE>`: Preserve from item type (`US` for User Story, `FR` for Functional Requirement, `SC` for Success Criterion)
    - `<DESCRIPTOR>`: Generate from item title:
      - Extract 2-3 key words from title
      - Convert to UPPERCASE
      - Join with hyphens
      - Examples: "Configure Log Levels" → `CONFIG`, "External Library Logs" → `EXTERNAL`, "File Level Output" → `FILE-LEVEL`

2. Check for collisions with existing IDs in the living doc:
    - If collision: append numeric suffix (`-2`, `-3`, etc.)
    - If still unclear: **STOP** and ask user for preferred descriptor

3. Build complete mapping table:
    ```markdown
    | Temp ID | → | Semantic ID | Action |
    |---------|---|-------------|--------|
    | US-1    | → | LOG-US-CONFIG | NEW |
    | FR-001  | → | LOG-FR-FILE-LEVEL | NEW |
    | US-2    | → | LOG-US-SYSTEM | UPDATE (existing) |
    ```

    For UPDATE items, the semantic ID is the existing ID from the living doc.

### Phase 5: Merge into Living Docs

1. Open target domain file(s) in `docs/system/` for editing.

2. **Merge User Stories** into "User Scenarios & Testing" section:

    For NEW items, append with format:
    ```markdown
    ### Title Here (DOMAIN-US-DESCRIPTOR)

    Lineage: NNN-US-N

    Description paragraph here...

    **Independent Test**: [test description]

    **Acceptance Scenarios**:

    1. **Given** ... **When** ... **Then** ...
    ```

    For UPDATE items, modify existing section:
    - Update description/content as needed
    - Append to lineage: `Lineage: 001-US-4 -> 005-US-2`
    - Ensure two trailing spaces before lineage if not immediately after heading

3. **Merge Functional Requirements** into "Requirements" section:

    For NEW items, append with format:
    ```markdown
    - **DOMAIN-FR-DESCRIPTOR**: Description text here.
      Lineage: NNN-FR-NNN
    ```
    (Note: two spaces at end of description line before Lineage newline)

    For UPDATE items, modify existing requirement:
    - Update description text as needed
    - Append to lineage chain: `Lineage: 001-FR-002 -> 005-FR-003`

4. **Merge Success Criteria** into "Success Criteria" section:

    Same format as Functional Requirements:
    ```markdown
    - **DOMAIN-SC-DESCRIPTOR**: Criteria description.
      Lineage: NNN-SC-NNN
    ```

5. **Merge Edge Cases** into "Edge Cases" section:

    For each edge case, format as:
    ```markdown
    - What happens when [condition]?
      - [Answer/behavior]
      Lineage: NNN-edge-cases
    ```

6. **Merge Key Entities** into "Key Entities" section (or create if doesn't exist):

    ```markdown
    - **EntityName**: Description of the entity.
      Lineage: NNN-entities
    ```

7. **Handle deprecated items** in living doc:

    If an existing item is deprecated by this feature:
    ```markdown
    ### ~~Old Feature Title (LOG-US-OLD)~~

    *(Deprecated)* Previously provided X functionality for Y purpose.
    ```

    For requirements:
    ```markdown
    - ~~**LOG-FR-OLD**: Old requirement description.~~
      *(Deprecated)* Was used for X.
      Lineage: 001-FR-002
    ```

8. **Handle superseded items** in living doc:

    If an existing item is superseded (replaced by something fundamentally different):
    ```markdown
    ### ~~Old Feature Title (LOG-US-OLD)~~

    *(Superseded by LOG-US-NEW)* Previously provided X functionality.
    ```

9. **Do NOT** add horizontal lines (`---`) between sections.

### Phase 6: Update Data Model & Architecture

1. If FEATURE_DIR/data-model.md exists:
    - Read `docs/system/data-model.md`
    - For each entity in FEATURE_DIR/data-model.md:
      - If entity is new: append to appropriate section with lineage
      - If entity updates existing: modify in-place, append to lineage
    - Format:
      ```markdown
      - **EntityName**: Description.
        Lineage: NNN-entities
      ```

2. If plan.md contains high-level architectural changes:
    - Read `docs/system/architecture.md`
    - Update only high-level design information
    - **Do NOT** include implementation details (those stay in feature's architecture.md)
    - Add lineage for any modified sections

### Phase 7: Update Code References

1. Search for temporary ID references in codebase:
    - Search scope: `src/`, `tests/`, `docs/` (excluding `docs/system/`)
    - Search patterns:
      - `NNN-US-N` (e.g., "004-US-1", "004-US-2")
      - `NNN-FR-NNN` (e.g., "004-FR-001", "004-FR-002")
      - `NNN-SC-NNN` (e.g., "004-SC-001")
      - Bare temporary IDs in context of feature number comments (e.g., "# Implements FR-001" near feature-related code)

2. For each match found, replace with corresponding semantic ID from the mapping table.

3. Track all files modified and replacement count for final report.

### Phase 8: Update Index & Finalize

1. If a new domain spec file was created:
    - Read `docs/system/_index.md`
    - Add entry in "Domain Specifications" section following existing format:
      ```markdown
      - [Domain Name](filename.md): Brief description.
      ```

2. Create consolidation record at `FEATURE_DIR/golden-spec-consolidation.md`:
    ```markdown
    # Golden Spec Consolidation Record

    **Date**: YYYY-MM-DD
    **Feature**: NNN-feature-name
    **Target Files**: docs/system/domain.md, docs/system/data-model.md

    ## Semantic ID Mapping

    | Temporary ID | Semantic ID | Action |
    |--------------|-------------|--------|
    | US-1 | LOG-US-CONFIG | NEW |
    | FR-001 | LOG-FR-FILE-LEVEL | NEW |
    | ... | ... | ... |

    ## Statistics

    - User Stories: N new, N updated
    - Functional Requirements: N new, N updated
    - Success Criteria: N new
    - Code files modified: N
    - Total ID replacements: N
    ```

3. Output final report:
    ```markdown
    ## Consolidation Complete

    **Feature**: NNN-feature-name
    **Target Docs Updated**: [list of files]

    ### Summary
    - User Stories: N new, N updated
    - Functional Requirements: N new, N updated
    - Success Criteria: N new
    - Code files modified: N
    - Total ID replacements: N
    ```

**IMPORTANT**: Do NOT execute the git commands. Only show them as next steps for the user.

## Error Handling

### Collision Resolution
If a generated semantic ID collides with an existing one and suffix doesn't resolve:
- **STOP** and ask: "Generated ID [DOMAIN-TYPE-DESCRIPTOR] collides with existing. Suggest alternative descriptor or choose: (A) [DESCRIPTOR-2], (B) [DESCRIPTOR-ALT], (C) Enter custom"

### Ambiguous Update Target
If spec content could match multiple existing items:
- **STOP** and show matches: "Item '[title]' could update multiple existing items: (A) [ID-1]: [title], (B) [ID-2]: [title]. Which should it update, or (C) create as new?"

### Missing References
If spec references items that don't exist in living docs:
- **WARN** but continue: "Warning: Spec references [ID] which doesn't exist in living docs. Treating as new item."

### Incomplete Tasks
If tasks.md shows incomplete items:
- **STOP** and ask: "Tasks.md shows [N] incomplete tasks. Proceed with consolidation anyway? (yes/no)"
