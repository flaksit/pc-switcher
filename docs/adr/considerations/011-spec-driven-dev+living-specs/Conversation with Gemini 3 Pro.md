## User

I want to spar with you and get advice from you about requirements specification and documentation for a solo developer project.

I use specification driven development (SDD) in this project, at least for the laying out the foundations and for implementing big features. We use githubs speckit as workflow for SDD with AI agents. I find this works quite well.
For smaller fixes and changes, I don't follow this workflow because it think it is too slow and wieldy for smaller changes. I could be wrong about that. So I don't generate new specification documents. There is often not a real record of a change (nor of the motivation for it), except for the git commit message, github pr description or github issue description, or a conversation hidden somewhere in the history of one of the several AI agent tools I use.
Now, I end up with specifications that are incomplete and outdated. Other documents, code comments and tests (comments and names) refer to specifications that are maybe outdated or entirely "removed", and I feel uncomfortable about that.

I wonder what is the "rule" or the standard/best practice in SDD projects for smaller extensions and changes:
- Use the same workflow as for large request that need thorough requirements analysis, but go faster through the process
- Update the existing specification documents, e.g. the spec.md files in the speckit folders. But what about all other related documents, such as plan.md, research.md, tasks.md? Update them as well? And modify the definition of user stories (US), functional requirements (FR), acceptance criteria (AC), tasks (T), ... Code comments and test comments and names often refer to these numbered US/FR/AC/T. The I have to change there references as well...
- Keep a "changelog" of specifications. But then for knowing the actual spec, one has to "replay" all these changelogs on top of the original specs.

Summarized, the question is: how to keep up-to-date specs and all documents and code/comments up-to-date with them?

This is first and foremost a generic question. So answer me from your internal knowledge and research from more recent resources and experiences on the web. GitHub speckit is quite recent and you don't have not enough internal knowledge.

If you want more insight in how I work now, see the #file:specs folder. But don't dive in too deeply now. I want you to keep a helicopter view.

## GitHub Copilot

This is a classic tension in software engineering: **Agility vs. Traceability**.

In a solo project using Specification Driven Development (SDD), the "Standard" corporate approach (updating every single artifact) will kill your velocity. However, the "Cowboy" approach (just changing code) leads to "Documentation Drift," where your specs become historical fiction rather than useful guides.

Here is the best-practice approach for maintaining SDD in a solo/agile context, specifically tailored to your folder structure and workflow.

### 1. The Core Distinction: Living vs. Transient Documents

The root of your problem is likely treating all documents in your `specs/00x-.../` folders as equal. They are not. You must mentally (and physically) separate them into two categories:

**A. Transient Artifacts (The "History")**:
- **Files:** `plan.md`, `tasks.md`, `research.md`, `*-review-*.md`, `checklists/`.
- **Role:** These are tools to get you from Point A to Point B. Once the feature is shipped, their job is done.
- **Rule:** **Never update these for small changes.** They represent the state of the world *at the time the feature was built*. Updating a `plan.md` for a bug fix 3 months later is wasted effort.

**B. Living Artifacts (The "Truth")**:
- **Files:** `spec.md`, `architecture.md`, `data-model.md`.
- **Role:** These describe the *current* state of the system.
- **Rule:** **These must be updated for every change that affects behavior.**

### 2. The "Lite" Workflow for Small Changes

Do not run the full SpecKit cycle (`/speckit.plan`, `/speckit.tasks`) for small fixes. Instead, use a "Spec-First Patch" workflow:

1. **Modify the Living Spec:** Open the relevant `spec.md`. Change the requirement text to reflect the new reality.
    - *Crucial:* Do not worry about the `tasks.md` or `plan.md` associated with that spec. They are dead.
2. **Code the Change:** Implement the fix.
3. **Update Tests:** Ensure tests match the new spec.
4. **Commit:** Your commit message links the code change to the spec update.

### 3. Solving the "Numbered References" (US/FR/AC) Problem

You mentioned code comments referring to `FR-05` or `US-02`. This is the "Traceability Trap."

**The Problem:**
If you change `FR-05` in the spec, the code comment `// Implements FR-05` is now ambiguous. Did it implement the *old* FR-05 or the *new* one?

**The Solution: Decouple Code from Feature Specs**
Since you have a folder structure based on features (`001-foundation`, `002-testing`), your IDs are likely scoped to those features (e.g., `001-FR-05`).

- **For new features:** Continue using IDs in the full SDD process.
- **For small changes/fixes:**
    1. **Do not change the original ID definition.** (e.g., Don't rewrite `FR-05` to mean something else).
    2. **Add a "Delta" or "Refinement" section** in the spec, or simply append a version (e.g., `FR-05 (Rev 1)`).
    3. **Better yet:** Stop referencing specific Requirement IDs in code comments for granular logic. Instead, reference the **Concept**.
        - *Bad:* `// Checks limit per FR-05`
        - *Good:* `// Checks sync file limit (see Spec: Sync Constraints)`

### 4. Structural Recommendation for your Repo

Looking at your file list (`specs/001...`, `specs/002...`), you are organizing specs by **Project/Milestone**. This is great for planning, but terrible for long-term maintenance because the "System Specification" is fragmented across 10 different folders.

**The "Golden Copy" Strategy:**

You need a central place for the **Current System State**.

1. **Keep your `specs/00x-.../` folders.** These are your "Change Requests" or "Projects". They contain the `plan.md`, `tasks.md`, and the *delta* `spec.md` for that specific effort.
2. **Create a `docs/system/` (or similar) folder.**
    - This contains the consolidated `architecture.md`, `data-model.md`, and `system-spec.md`.
3. **The Workflow:**
    - When you finish 003-foundation-tests, you **merge** the relevant parts of that spec into `docs/system/system-spec.md`.
    - **For small fixes:** You only edit `docs/system/system-spec.md`. You do *not* create a `specs/005-small-fix` folder.

### Summary of Advice

1. **Stop updating** `plan.md`, `tasks.md`, and `research.md` after the feature is done. They are historical artifacts.
2. **Maintain a "Golden Copy"** of the specification in a central docs folder that represents the *current* system, separate from the `specs/00x` feature folders.
3. **For small changes:** Update the Golden Copy spec -> Update Code -> Update Test. Skip the planning/task generation phase.
4. **Refactor Comments:** Gradually remove code comments that link to specific, transient Requirement IDs. Link to architectural concepts or the Golden Copy spec sections instead.

---

## User

I like your simple and pragmatic approach. The golden copy seems the way to go. Maybe with different spec.md documents per domain (e.g. testing-framework, logging, orchestration, ...)?

We still have a referencing problem: speckit starts numbered sequences for US, AC, FR from 0 on each speckit run. So there are many different FR-001 around! I don't want to fiddle with speckit to modify it to use "concept references" instead of numbered references. And I find it practical to have precise references, so that we can lookup codes/tests/doc that are related to an FR, e.g. in case we want to change that FR and need to know/review where it is implemented/referenced. What would you propose?

## GitHub Copilot

Yes, splitting the Golden Copy by domain (e.g., `docs/system/logging.md`, `docs/system/orchestration.md`) is an excellent idea. It keeps files manageable and reduces merge conflicts.

Regarding the **referencing problem** (colliding `FR-001` IDs):

Since you want precise references but cannot change how `speckit` generates them, you are facing a classic "Local vs. Global" identifier problem.

I propose the **"Prefix-on-Merge" Strategy**.

### The Concept
Treat the `speckit` generated IDs (`FR-001`) as **temporary, local IDs** valid only during the active development of that specific feature branch. Once the feature graduates to the "System" (the Golden Copy), the IDs must graduate too.

### The Workflow

1. **Development Phase (Local IDs)**
    - Let `speckit` generate `FR-001`, `AC-001` in your `specs/00x-.../` folder.
    - In your code and tests, use these short IDs temporarily: `// Implements FR-001`.
    - *Benefit:* You don't fight the tool while iterating fast.

2. **Consolidation Phase (The "Promotion")**
    - When the feature is ready, you copy the spec content into your domain-specific Golden Copy (e.g., `docs/system/logging.md`).
    - **The Magic Step:** Perform a **Find & Replace** on the new content *and* the related source code/tests.
    - Replace `FR-` with `[PREFIX]-FR-`.

### Example: The Logging Feature

1. **Draft:** You work in 004-python-logging. Speckit gives you `FR-005`.
2. **Code:** You write `// Implements FR-005` in `src/logging/handlers.py`.
3. **Merge:**
    - You create/update `docs/system/logging.md`.
    - You decide the prefix for this domain is `LOG`.
    - You run a global search and replace in `docs/system/logging.md` and `src/logging/`:
        - `FR-`  → `LOG-FR-`
        - `AC-`  → `LOG-AC-`
        - `US-`  → `LOG-US-`

### Why this is the best compromise

1. **Global Uniqueness:** `LOG-FR-005` will never collide with `FND-FR-005` (Foundation).
2. **Searchability:** You can grep the entire codebase for `LOG-FR-005` and find exactly the spec definition, the implementation, and the tests.
3. **Tool Compatibility:** You let `speckit` do its thing without modification. You only apply the namespace when you "commit" to the system architecture.
4. **Low Effort:** It is literally one "Replace All" action per feature merge.

### Recommended Directory Structure

```text
docs/system/
├── 00-architecture.md
├── 01-foundation.md      (Contains FND-FR-xxx)
├── 02-testing.md         (Contains TST-FR-xxx)
├── 04-logging.md         (Contains LOG-FR-xxx)
└── ...
```

This maps cleanly to your specs history folders while maintaining a pristine, non-colliding current state.

---

## User

Ok, this looks good.

I like your simple and pragmatic approach and style.
Because this is an important topic, I asked some other LLMs for advice as well. You find the conversations in the three attached files. Read them. If there is something valuable in there, take it into account and change/update/extend your proposed approach accordingly.

## GitHub Copilot

This is a powerful synthesis. The other agents (especially GPT-5.2 and Claude Sonnet) provided crucial structural insights that refine my original "Golden Copy" idea.

Here is the **Unified SDD Workflow for Solo Developers**, incorporating the best advice from all sources.

### 1. The Core Philosophy: "Immutable History vs. Living Truth"

We separate **Artifacts of Work** (what we did and why) from **Artifacts of Knowledge** (what the system is now).

- **`specs/00x-.../`**: **Immutable History.**
    - Contains `plan.md`, `tasks.md`, `research.md`, and the *draft* `spec.md`.
    - **Rule:** Once the feature is shipped, these folders are **never touched again**. They are an audit trail.
- **`docs/spec/`**: **Living Truth.**
    - Contains the current, authoritative system definition.
    - **Rule:** This must always reflect the code in `main`.

### 2. The Directory Structure

We will introduce a `docs/spec` folder to hold the "Golden Copies".

```text
/
├── specs/                      # HISTORY (Immutable)
│   ├── 001-foundation/         # Old run
│   ├── 002-testing/            # Old run
│   └── 005-new-feature/        # Active SpecKit run
│
├── docs/
│   └── spec/                   # TRUTH (Living)
│       ├── _index.md           # Entry point (Map of topics)
│       ├── system.md           # Global constraints & glossary
│       ├── logging.md          # Domain spec
│       ├── testing.md          # Domain spec
│       └── orchestration.md    # Domain spec
```

### 3. The ID Strategy: Semantic over Sequential

The "FR-001" collision problem is solved by using **Semantic IDs** in the Living Truth, while letting SpecKit use whatever it wants in the temporary phase.

- **In SpecKit (`specs/00x`):** Use `FR-01`, `AC-01` (whatever the tool generates). It doesn't matter because these are temporary.
- **In Living Docs (`docs/spec`):** Use stable, semantic tags.
    - Instead of `FR-005`, use `REQ-LOG-ROTATION`.
    - Instead of `AC-002`, use `CON-SYNC-ATOMICITY`.
- **In Code:** Reference the **Semantic ID**.
    - `# Implements REQ-LOG-ROTATION`

### 4. The Workflows

#### Scenario A: The "Big Feature" (Standard SDD)

1. **Run SpecKit:** Create `specs/005-feature`. Generate `spec.md`, `plan.md`, `tasks.md`.
2. **Implement:** Write code. Use temporary IDs (`FR-01`) if needed during the heat of coding.
3. **The "Consolidation" Step (Crucial New Step):**
    - Before closing the feature, open the relevant `docs/spec/<topic>.md`.
    - Copy the *outcome* requirements from `specs/005/spec.md` into `docs/spec/<topic>.md`.
    - **Convert IDs:** Change `FR-01` to `REQ-FEATURE-NAME`.
    - **Update Code:** Find/Replace `FR-01` in your code with `REQ-FEATURE-NAME`.
4. **Commit:** The feature is done. `specs/005` is now a fossil.

#### Scenario B: The "Small Fix" (Fast Track)

1. **Edit Truth First:** Open `docs/spec/<topic>.md`.
    - Modify the text of `REQ-EXISTING-FEATURE`.
    - Or add `REQ-NEW-TWEAK`.
2. **Implement:** Update code and tests.
3. **Commit:** Message: `Update retry logic (ref REQ-NET-RETRY)`.
4. **Ignore specs:** Do not touch `plan.md` or create a new folder.

### 5. What goes in the Living Doc?

Per GPT-5.2's excellent advice, keep `docs/spec/*.md` lean. It is not a copy-paste of the verbose SpecKit spec.

**Structure for `docs/spec/<topic>.md`:**
1. **Scope & Goals:** What this domain covers.
2. **Requirements (The "What"):**
    - `REQ-TOPIC-CONCEPT`: The system must...
3. **Contracts & Invariants (The "Rules"):**
    - "All log files must be UTF-8."
    - "Sync must never delete files without a manifest check."
4. **Traceability (Optional):**
    - "Introduced in `specs/001`, updated in `specs/005`."

### 6. Migration Plan (How to start today)

You don't need to refactor everything at once.

1. **Create `docs/spec/_index.md`**.
2. **Lazy Migration:** Next time you touch the "Testing" code:
    - Create `docs/spec/testing.md`.
    - Copy the *relevant, current* parts from `specs/002-testing/spec.md`.
    - Invent Semantic IDs (`REQ-TEST-...`) for the parts you are touching.
    - Update your code comments to match.
3. **Leave the rest:** Let the old specs folders rot peacefully. They are history.

### Summary of the "Rule"

> **"SpecKit folders are for *building*. `docs/spec` is for *knowing*. Always build in a folder, but always consolidate to the docs."**

---

## User

In your first answer, you proposed to have architecture.md and data-model.md in the "golden copy". I liked that, because I have a hard time remembering the architecture, so a (single?) up-to-date architecture document sounds appealing. Is there a reason to remove these docs from your new proposal?

## GitHub Copilot

No, that was an oversight in my simplified example. You are absolutely right: **`architecture.md` and `data-model.md` are the most critical parts of your Golden Copy.**

For a solo developer, having a single source of truth for "How this thing is built" (Architecture) and "What data it handles" (Data Model) is essential to avoid cognitive load.

### Updated Directory Structure

I recommend renaming the folder from `docs/spec/` to `docs/system/` to better reflect that it contains both requirements *and* design.

```text
/
├── specs/                      # HISTORY (Immutable)
│   ├── 001-foundation/
│   └── ...
│
├── docs/
│   └── system/                 # TRUTH (Living Golden Copy)
│       ├── _index.md           # Entry point
│       ├── architecture.md     # Current high-level design & component map
│       ├── data-model.md       # Current schemas and data flows
│       ├── logging.md          # Specific domain specs
│       ├── testing.md          # Specific domain specs
│       └── ...
```

### How to handle them in the workflow

1. **Migration (One-time):** Copy architecture.md and `data-model.md` to `docs/system/`.
2. **The "Consolidation" Step:** When a new feature (e.g., `specs/005-cloud-sync`) changes the architecture:
    - You update `docs/system/architecture.md` to reflect the *new* reality.
    - You update `docs/system/data-model.md` if you added new fields.
3. **Small Fixes:** If you refactor a class that changes the data model, you update `docs/system/data-model.md` directly. No specs folder needed.