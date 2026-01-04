---
description: Consolidate SpecKit artifacts into the Golden Copy living docs in docs/system/ per ADR-011
---

## User Input

```text
$ARGUMENTS
```

You **MUST** consider the user input before proceeding (if not empty).

## Sub-Agent Strategy

This command uses sub-agents extensively to parallelize work and reduce main agent context.

**Sub-agents used:**

1. **Domain Analysis Agent** (Phase 2): Single orchestrator that reads all domain files and spec.md, determines target domain(s), detects domain splits, and returns structured analysis.

2. **Data Model Merge Agent** (Phase 6a): Reads FEATURE_DIR/data-model.md and docs/system/data-model.md, performs merge.

3. **Architecture Update Agent** (Phase 6b): Reads FEATURE_DIR/plan.md and docs/system/architecture.md, performs high-level updates.

4. **Code Reference Agents** (Phase 7): Three agents (one per folder: src/, tests/, docs/) that search AND replace temporary IDs directly.

**Parallelization:**
- Phase 2: Domain Analysis Agent runs independently
- After Phase 4 (ID mapping complete): Phase 5, 6a, 6b, and 7 all run IN PARALLEL

## Outline

### Phase 1: Setup

1. Run `.specify/scripts/bash/check-prerequisites.sh --json --require-tasks --include-tasks` from repo root and parse FEATURE_DIR. All paths must be absolute.

2. Extract feature number from FEATURE_DIR (e.g., "004" from "specs/004-python-logging").

3. Verify prerequisites:
   - **REQUIRED**: spec.md exists in FEATURE_DIR
   - **CHECK**: tasks.md shows all tasks completed (all items marked `[X]` or `[x]`)
   - If tasks are incomplete, **STOP** and inform user: "Implementation appears incomplete. Run `/speckit.implement` first or confirm consolidation should proceed anyway."

### Phase 2: Domain Analysis (Single Orchestrator Sub-Agent)

Launch a single `Explore` sub-agent with this prompt:

> "Analyze domains and feature spec for consolidation.
>
> **Part A - Existing Domains:**
> Read all markdown files in `docs/system/` excluding `_index.md`, `architecture.md`, and `data-model.md`.
> For each file, extract:
> 1. Domain code from semantic IDs (e.g., `LOG-US-*` indicates domain `LOG`)
> 2. All semantic IDs with their type (US, FR, SC)
> 3. One-liner description of what this domain covers
> 4. Key topics/concepts from headings
>
> **Part B - Feature Spec Analysis:**
> Read `[FEATURE_DIR]/spec.md` and:
> 1. Extract ALL temporary IDs (US-1, FR-001, SC-001, etc.) with their titles and content summaries
> 2. Derive the conceptual domain from User Story definitions (what problem domain do they address?)
> 3. Count total User Stories
>
> **Part C - Domain Matching:**
> Compare the feature's conceptual domain against existing domain descriptions.
> Determine:
> 1. Does it match exactly one existing domain? → Return that domain
> 2. Does it overlap with NO existing domains? → Recommend new domain (suggest name and code)
> 3. Does it overlap with PART of an existing domain?
>    - If feature has >5 US AND overlaps with a subset of existing domain content → Recommend DOMAIN SPLIT (identify which existing content should move to new domain)
>    - If feature has ≤5 US → Flag as ambiguous, needs user decision
>
> **Return structured result:**
> ```json
> {
>   existing_domains: [{filename, domain_code, description, semantic_ids[], topics[]}],
>   feature_spec: {
>     temp_ids: [{id, type, title, content_summary}],
>     conceptual_domain: string,
>     user_story_count: number
>   },
>   recommendation: {
>     action: 'use_existing' | 'create_new' | 'domain_split' | 'ask_user',
>     target_domain: string,
>     new_domain_suggestion?: {name, code},
>     split_details?: {content_to_move[], new_domain_name, new_domain_code},
>     ambiguity_reason?: string
>   }
> }
> ```"

**After agent returns:**

1. If recommendation is `ask_user`: **STOP** and ask: "This feature has [N] user stories and overlaps with [DOMAIN]. Should it: (A) be integrated into existing [domain].md, or (B) create a new [suggested-name].md domain?"

2. If recommendation is `domain_split`: Execute the split first:
   a. Create new domain file `docs/system/<new-domain>.md` with standard structure
   b. Move identified content from existing domain to new domain file
   c. Rename semantic IDs from old domain code to new (e.g., `CORE-FR-LOG-*` → `LOG-FR-*`)
   d. Launch a sub-agent to search codebase and replace old semantic IDs with new ones
   e. Continue with new domain as target

3. Store the `existing_domains` index and `feature_spec.temp_ids` for subsequent phases.

### Phase 3: Classify Items

Using the data from Phase 2 (no file reading needed):

1. For each temporary ID from `feature_spec.temp_ids`, classify by comparing with `existing_domains` semantic IDs and content:
   - **NEW**: No match in target living doc → needs new semantic ID, will be appended
   - **UPDATE**: Matches existing item (same concept, related topic) → keeps existing semantic ID, modify in-place
   - **SUPERSEDED**: Spec explicitly states it replaces existing item with something fundamentally different → mark old as superseded, add new item
   - **DEPRECATED**: Existing living doc item is removed by this feature with nothing replacing it → mark as deprecated

2. Use semantic matching on titles and content summaries. Look for explicit statements in spec like "supersedes", "replaces", "removes".

### Phase 4: Generate ID Mapping

1. For each **NEW** item, generate semantic ID with format `<DOMAIN>-<TYPE>-<DESCRIPTOR>`:
   - `<DOMAIN>`: From target domain code (e.g., `CORE`,`LOG`, `TST`)
   - `<TYPE>`: From item type (`US`, `FR`, `SC`)
   - `<DESCRIPTOR>`: 2-3 key words from title, UPPERCASE, hyphen-joined
     - Examples: "Configure Log Levels" → `CONFIG`, "External Library Logs" → `EXTERNAL`

2. Check for collisions with existing semantic IDs:
   - If collision: append numeric suffix (`-2`, `-3`, etc.)
   - If still unclear: **STOP** and ask user for preferred descriptor

3. Build complete mapping table:
   ```text
   | Temp ID | Semantic ID | Action |
   |---------|-------------|--------|
   | US-1    | LOG-US-CONFIG | NEW |
   | FR-001  | LOG-FR-FILE-LEVEL | NEW |
   | US-2    | LOG-US-SYSTEM | UPDATE (existing) |
   ```markdown

4. **Immediately after mapping is complete**: Launch Phases 5, 6a, 6b, and 7 IN PARALLEL (single message with multiple Task tool calls).

### Phase 5: Merge Spec into Domain File

**This phase runs in parallel with Phases 6a, 6b, and 7.**

1. Read target domain file from `docs/system/`.

2. Merge **ALL sections** from spec.md into the domain file. This includes but is not limited to:
   - User Stories / User Scenarios
   - Functional Requirements
   - Success Criteria
   - Edge Cases
   - Key Entities
   - Clarifications
   - Any other sections present in spec.md

3. **Format for User Stories** (heading format, lineage first line after heading):
   ```markdown
   ### Title Here (DOMAIN-US-DESCRIPTOR)

   Lineage: NNN-US-N

   Description paragraph here...

   **Independent Test**: [test description]

   **Acceptance Scenarios**:

   1. **Given** ... **When** ... **Then** ...
   ```

   For UPDATE items: append to lineage chain `Lineage: 001-US-4 -> 005-US-2`

4. **Format for Functional Requirements** (two spaces before Lineage newline):
   ```markdown
   - **DOMAIN-FR-DESCRIPTOR**: Description text here.
     Lineage: NNN-FR-NNN
   ```

   For UPDATE items: append to lineage chain `Lineage: 001-FR-002 -> 005-FR-003`

5. **Format for Success Criteria** (same as FR):
   ```markdown
   - **DOMAIN-SC-DESCRIPTOR**: Criteria description.
     Lineage: NNN-SC-NNN
   ```

6. **Format for Edge Cases**:
   ```markdown
   - What happens when [condition]?
     - [Answer/behavior]
     Lineage: NNN-edge-cases
   ```

7. **Format for Key Entities**:
   ```markdown
   - **EntityName**: Description of the entity.
     Lineage: NNN-entities
   ```

8. **Handle deprecated items** (keep lineage):
   ```markdown
   ### ~~Old Feature Title (LOG-US-OLD)~~

   Lineage: 001-US-4
   *(Deprecated)* Previously provided X functionality for Y purpose.
   ```

   For requirements:
   ```markdown
   - ~~**LOG-FR-OLD**: Old requirement description.~~
     Lineage: 001-FR-002
     *(Deprecated)* Was used for X.
   ```

9. **Handle superseded items** (keep lineage):
   ```markdown
   ### ~~Old Feature Title (LOG-US-OLD)~~

   Lineage: 001-US-4
   *(Superseded by LOG-US-NEW)* Previously provided X functionality.
   ```

10. **Do NOT** add horizontal lines (`---`) between sections.

### Phase 6a: Merge Data Model (Sub-Agent)

**This phase runs in parallel with Phases 5, 6b, and 7.**

Launch an `Explore` sub-agent (or `general-purpose` if edits needed) with prompt:

> "Merge data model from feature spec into living docs.
>
> 1. Check if `[FEATURE_DIR]/data-model.md` exists. If not, return immediately with no changes.
> 2. Read `[FEATURE_DIR]/data-model.md` for entity definitions.
> 3. Read `docs/system/data-model.md` for existing entities.
> 4. For each entity in feature data-model:
>    - If new: append with lineage `Lineage: NNN-entities`
>    - If updates existing: modify in-place, append to lineage
> 5. Apply edits to `docs/system/data-model.md`.
> 6. Return summary of changes made."

### Phase 6b: Update Architecture (Sub-Agent)

**This phase runs in parallel with Phases 5, 6a, and 7.**

Launch an `Explore` sub-agent (or `general-purpose` if edits needed) with prompt:

> "Update architecture docs with high-level changes from feature.
>
> 1. Read `[FEATURE_DIR]/plan.md` for architectural context.
> 2. Determine if there are HIGH-LEVEL design changes (not implementation details).
> 3. If yes:
>    - Read `docs/system/architecture.md`
>    - Update only high-level design sections
>    - Add lineage for modified sections
>    - Apply edits
> 4. Return summary of changes made (or 'no changes needed')."

### Phase 7: Update Code References (Parallel Sub-Agents)

**This phase runs in parallel with Phases 5, 6a, and 6b.**

Launch THREE sub-agents IN PARALLEL, one for each folder. Each agent receives the ID mapping table and searches AND replaces directly.

**Agent for src/:**
> "Search and replace temporary IDs in `src/` folder.
>
> ID Mapping:
> [INSERT MAPPING TABLE HERE]
>
> Search patterns:
> - `NNN-US-N` (e.g., '004-US-1')
> - `NNN-FR-NNN` (e.g., '004-FR-001')
> - `NNN-SC-NNN` (e.g., '004-SC-001')
> - Bare IDs like `FR-001`, `US-1` in comments
>
> For each match: replace with corresponding semantic ID from mapping.
> Return: `{files_modified: number, replacements: number, details: [{file, line, old, new}]}`"

**Agent for tests/:**
> Same prompt but for `tests/` folder.

**Agent for docs/ (excluding docs/system/):**
> Same prompt but for `docs/` folder, excluding `docs/system/`.

**After all three agents return:**
Aggregate statistics for final report.

### Phase 8: Update Index & Finalize

1. If a new domain spec file was created:
   - Read `docs/system/_index.md`
   - Add entry in "Domain Specifications" section:
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

   ### Next Steps
   git add .
   git commit -m "docs: consolidate NNN-feature-name into golden specs"
   git push
   gh pr create --title "Consolidate NNN-feature-name" --body "Consolidates SpecKit artifacts into living docs per ADR-011"
   ```

**IMPORTANT**: Do NOT execute the git commands. Only show them as next steps.

## Error Handling

### Collision Resolution
If a generated semantic ID collides with an existing one and suffix doesn't resolve:
- **STOP** and ask: "Generated ID [DOMAIN-TYPE-DESCRIPTOR] collides with existing. Suggest alternative descriptor or choose: (A) [DESCRIPTOR-2], (B) [DESCRIPTOR-ALT], (C) Enter custom"

### Ambiguous Update Target
If spec content could match multiple existing items:
- **STOP** and show matches: "Item '[title]' could update multiple existing items: (A) [ID-1]: [title], (B) [ID-2]: [title]. Which should it update, or (C) create as new?"

### Ambiguous Domain Target
If domain cannot be determined and feature has ≤5 US:
- **STOP** and ask user to choose between integrating into existing domain or creating new.

### Incomplete Tasks
If tasks.md shows incomplete items:
- **STOP** and ask: "Tasks.md shows [N] incomplete tasks. Proceed with consolidation anyway? (yes/no)"
