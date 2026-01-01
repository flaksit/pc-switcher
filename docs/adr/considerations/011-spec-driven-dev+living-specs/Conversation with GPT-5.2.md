User:
> I want to spar with you and get advice from you about requirements specification and documentation for a solo developer project.
>
> I use specification driven development (SDD) in this project, at least for the laying out the foundations and for implementing big features. We use githubs speckit as workflow for SDD with AI agents. I find this works quite well.
> For smaller fixes and changes, I don't follow this workflow because it think it is too slow and wieldy for smaller changes. I could be wrong about that. So I don't generate new specification documents. There is often not a real record of a change (nor of the motivation for it), except for the git commit message, github pr description or github issue description, or a conversation hidden somewhere in the history of one of the several AI agent tools I use.
> Now, I end up with specifications that are incomplete and outdated. Other documents, code comments and tests (comments and names) refer to specifications that are maybe outdated or entirely "removed", and I feel uncomfortable about that.
>
> I wonder what is the "rule" or the standard/best practice in SDD projects for smaller extensions and changes:
> - Use the same workflow as for large request that need thorough requirements analysis, but go faster through the process
> - Update the existing specification documents, e.g. the spec.md files in the speckit folders. But what about all other related documents, such as plan.md, research.md, tasks.md? Update them as well? And modify the definition of user stories (US), functional requirements (FR), acceptance criteria (AC), tasks (T), ... Code comments and test comments and names often refer to these numbered US/FR/AC/T. The I have to change there references as well...
> - Keep a "changelog" of specifications. But then for knowing the actual spec, one has to "replay" all these changelogs on top of the original specs.
>
> Summarized, the question is: how to keep up-to-date specs and all documents and code/comments up-to-date with them?
>
> This is first and foremost a generic question. So answer me from your internal knowledge and research from more recent resources and experiences on the web. GitHub speckit is quite recent and you don't have not enough internal knowledge.
>
> If you want more insight in how I work now, see the #file:specs folder. But don't dive in too deeply now. I want you to keep a helicopter view.

GitHub Copilot:

## Practical “best practice” for keeping specs current (without making small changes painful)

The pattern that tends to work (especially for solo dev) is:

1. **Keep the *current truth* in one place** (a “living” spec).
2. **Keep the *history* somewhere else** (git + ADRs), not by forcing readers to replay deltas.
3. **Make “spec upkeep” a *cheap default* for behavior changes**, and optional for pure refactors.

That avoids the two failure modes you described:
- specs drift because updates are too heavyweight,
- or specs become an archaeological dig (“apply these 11 changelog entries to know what’s true”).

## A workable policy: what to update, and when

### 1) Treat `spec.md` as the *only* document that must stay correct
For each Speckit feature folder (like your `specs/00x-*` structure):

- **`spec.md` is the normative statement**: “what the system/feature does and must do”.
- **`plan.md`, `tasks.md`, most of `research.md` are *non-normative artifacts***: useful during delivery, but not required to remain perfectly synced forever.

**Rule of thumb**:
- If a change affects externally observable behavior, guarantees, constraints, or acceptance criteria → **update `spec.md`**.
- If it only affects implementation approach or code organization → **don’t touch the spec** (unless the spec accidentally implies something now false).

This alone removes a lot of the maintenance burden (“do I have to update 5 docs?” → usually no).

### 2) Use ADRs for decisions, not for requirements
When a “small change” is actually a **decision** (trade-off, constraint, new dependency, invariants), capture it as a short ADR (or “mini-ADR”) and link it from the spec.

- **Spec**: what must be true.
- **ADR**: why you chose *this* way.

This keeps specs short and stable while still preserving motivation beyond commit messages.

### 3) Stop renumbering requirements; use stable IDs
Your pain around `US/FR/AC/T` renumbering is common. The fix is boring but extremely effective:

- Give each requirement a **stable identifier that never changes**, even if you reorder text.
- Avoid sequential numbers that force renumbering.

Example patterns that work:
- `FR-FOUNDATION-LOGGING-001` (stable)
- `FR-<slug>-<short-hash>` (stable)
- Even `FR-2026-01-01-logging-format` (stable enough)

Then code/tests can reference the stable ID without becoming fragile.

**If you already have sequential IDs:** stop renumbering them; mark removed ones as deprecated/obsolete rather than reusing numbers.

## What to do about `plan.md`, `tasks.md`, `research.md`

### `tasks.md`
Treat as **execution log**, not a permanent contract.

- It’s fine if tasks are “done” and later obsolete.
- If you want it useful later: append a short “Outcome” section at the end, but don’t continuously rewrite tasks.

### `plan.md`
Treat as **design at time of implementation**.

- Update it only when it’s still actively guiding work.
- Once implemented, it’s okay for it to drift **as long as `spec.md` + code + ADRs are accurate**.

### `research.md`
Treat as **notes and links**, not authoritative.

- If research leads to a decision → ADR.
- If research leads to a requirement → spec.

This gives you a clean “source of truth” hierarchy.

## The big lever: stop referencing specs in code in a brittle way

### Prefer “linkable intent” over “requirement-number coupling”
Referencing `FR-12` in test names/comments seems tidy until you edit the spec and everything breaks socially.

Better options:
- Put stable IDs in **test metadata** (docstring or comment), not in the test function name.
- Use a short tag like `req: FR-FOUNDATION-LOGGING-001` so it’s searchable.
- Avoid spraying requirement references throughout production code; keep them primarily in tests (where traceability matters most).

## A lightweight workflow for “small” changes (fast SDD)

You don’t need the full Speckit pipeline every time. Use a “micro-spec delta” habit:

### If the change affects behavior
1. **Edit `spec.md` first** (tiny patch: 3–10 lines is common).
2. Implement + tests.
3. In PR/commit message include: `Spec: specs/00x-.../spec.md (FR-... updated)`

### If the change is purely internal
- Just implement + tests.
- No spec update required.

### If the change introduces a new constraint or trade-off
- Add/update an ADR.
- Link ADR from spec.

This keeps “small” changes documented without forcing the whole `/speckit.*` ceremony.

## A simple enforcement mechanism (prevents drift)

Add a **PR checklist** (even solo) that asks:

- Does this change alter behavior or guarantees?
  - If yes: spec updated (link to section / requirement ID).
- Does this change introduce/modify a decision?
  - If yes: ADR added/updated (immutable rule respected).

Even if you never open a PR, a checklist in your commit template or `CONTRIBUTING.md` works.

## Two concrete questions (to tailor this to your repo)
1. Do you want **`specs/00x-*` to remain immutable “historical deliveries”**, or do you want them to be **living foundation specs** that evolve as the project evolves?
2. Are your requirement IDs today mostly **sequential** (FR-1, FR-2, …), and are they referenced in **test names**, **comments**, or both?

With those answers, a minimal, repo-specific convention can be proposed (IDs, what’s normative, and what gets updated for small changes).

---

User:
> 1. I think it is easiest to keep it immutable. Example showing my reasoning: in a first speckit run I introduced the testing framework. Later, I want to change part of how we organize testing. So I will do a new speckit run because this merits good analysis and need thorough planning and implementation. Now there are two speckit folders for testing: the full original and the second one that changed part of it. This can lead to confusion afterwards: which one is the reference for the testing framework now? Even if I, at the end of the second speckit run update the spec.md of the original run, we still end up with two folders and two spec.md about the same topic. What do you think?
> 2. They are sequential now and referenced in both test names and in test comments and maybe code comments). But I don't renumber them.

GitHub Copilot:

## 1) Immutable Speckit folders: good idea—but you need a “current truth pointer”

Keeping Speckit runs **immutable** is usually the right call, because those folders are *audit trails*: they capture what you believed, decided, and planned *at the time*.

The confusion you described (“two folders about testing—what’s the current truth?”) is solved by introducing an explicit rule:

- **Speckit folders are historical snapshots.**
- **Current requirements live in exactly one place**, and it points to the relevant snapshots.

### Two common patterns that work well

#### Pattern A (recommended): “Living docs + immutable runs”
- Keep Speckit runs under `specs/00x-*` immutable.
- Maintain a **single living reference** for “Testing framework” somewhere else, e.g.:
  - `docs/spec/testing.md` (normative)
  - or `docs/spec/_index.md` + topic pages

Then each living page has a small “History” section like:
- “Introduced in 002-testing-framework”
- “Revised in `specs/00Y-testing-framework-reorg`”
- “Supersedes: …”

This gives you:
- one authoritative current spec per topic,
- plus clean provenance.

#### Pattern B: “Latest wins” inside specs via an index
If you strongly want *spec truth* to remain “in Speckit land”, add an index file that declares which run is current:

- `specs/_current.md` (or `specs/_index.md`) with a table:
  - Topic → Current run folder → Status (active/superseded) → Links to ADRs

This is simpler to maintain than trying to update old `spec.md` files (and avoids two competing `spec.md`s pretending to be current).

### What I would *not* do
- **Don’t update the old run’s `spec.md` to match the new reality.** It breaks the “immutable snapshot” property and turns the folder into a misleading hybrid.
- **Don’t expect readers to “diff” two full specs mentally.** Always add an explicit “supersedes” and “what changed” pointer somewhere central.

## 2) Sequential IDs referenced in tests/comments: keep them, but stop treating them as stable anchors

If you already:
- don’t renumber, and
- don’t reuse numbers,

then sequential IDs can be “stable enough” *as labels*.

The problem is that they are **structurally fragile** for traceability:
- they don’t encode topic,
- they don’t survive splitting/merging specs well,
- they encourage wide coupling (tests named after FR numbers).

### Practical migration without churn
Keep your existing `FR-12` etc, but introduce an additional stable ID that is **content-addressable** or **topic-scoped**, and use *that* for long-term references.

Example:
- Keep: `FR-12`
- Add: `FR-TEST-ISOLATION` (or `FR-TESTING-ISOLATION-001`)
- In the spec, show both on the same line during transition.
- In tests, reference only the stable one (or both temporarily).

This avoids breaking history while giving you a durable handle going forward.

### Also: move requirement references out of test *names*
Best practice for keeping rename churn low:
- Don’t put requirement IDs in the test function name.
- Put them in a docstring/comment/tag line that’s grep-friendly, e.g. `req: FR-TEST-ISOLATION`.

That way, refactors don’t explode your test names, but traceability remains.

## A minimal policy that resolves your two issues

1. **Immutability rule:** Speckit run folders are never edited after acceptance (except fixing typos).
2. **Authority rule:** Exactly one “current spec” location exists (living docs or `specs/_index.md`).
3. **Supersession rule:** Every new run that changes an earlier topic must declare:
   - what it supersedes,
   - what is now obsolete,
   - where the “current truth” is.
4. **ID rule:** Never renumber/reuse sequential IDs; introduce stable topic IDs for anything referenced from code/tests.

## Two quick clarifiers (so the recommendation fits your habits)
1. Do you prefer the “current truth” to live under **docs** (clean separation) or **within specs** (all spec work in one tree)?
2. Is your goal that a new contributor can answer “what are the current testing requirements?” in **under 60 seconds** by following links? If yes, I’d push harder for a single living topic page per area.

---

User:
> 1. docs
> 2. It's more about (a) me remembering the "current truth" about the specs when I need to come back to a project after a few weeks/months. And (b) being able to review implementation and tests compared to the current spec.

GitHub Copilot:

## Recommendation given your goals (docs as current truth, Speckit runs immutable)

### Core rule set
- **Speckit run folders (`specs/00x-*`) are immutable snapshots.** Never edit old runs to “make them current”.
- **Current truth lives in docs**, in a small set of *living* spec pages that are the only normative reference for “what the system does now”.
- **Every time a Speckit run changes an area, it must update the corresponding living doc page** (and point back to the run for provenance).

This removes the “two spec.md files disagree” problem entirely: `spec.md` in runs is historical; docs is current.

## Minimal doc structure that stays navigable months later

### 1) A single entry point: “What is current?”
Create a top-level index that answers “where do I look” in <60 seconds:

- `docs/spec/_index.md`  
  - lists major topics (testing, logging, sync model, CLI, etc.)
  - for each topic: link to the **current** doc page
  - optionally: “Latest Speckit run(s) that produced/changed this”

### 2) One living page per topic
Example:
- `docs/spec/testing.md` (normative)
  - “Current requirements / invariants”
  - “Non-goals”
  - “Interfaces / conventions that tests rely on”
  - “Traceability” section:
    - “Introduced: specs/002-testing-framework”
    - “Revised: specs/0YY-testing-reorg”
    - Links to relevant ADRs

### 3) ADRs remain the “why”
Keep using `docs/adr/*` for decisions and trade-offs; living specs link to ADRs where needed. This prevents “motivation hiding in AI chat logs”.

## What to do for small changes (without running full Speckit)

Use a **micro-update rule** that matches your intent:

### If the change affects observable behavior / guarantees / constraints
- Update the relevant **`docs/spec/<topic>.md`** first (small patch is fine).
- Implement + tests.
- Commit message/PR includes: `Spec: docs/spec/<topic>.md`

### If it’s a refactor only
- No spec update.
- If a refactor invalidates statements in docs, fix the doc (docs should not lie).

### If it introduces a meaningful decision
- Add an ADR (short).
- Link ADR from the topic spec page.

This keeps docs current without dragging in `plan.md/tasks.md/research.md` every time.

## How to review implementation/tests against “current spec”

### 1) Stop treating Speckit IDs as the long-term anchor
Your sequential IDs are stable *enough* if you don’t renumber/reuse, but they won’t map cleanly across multiple Speckit runs.

Instead, in `docs/spec/<topic>.md` define **stable requirement tags** that won’t change across rewrites, e.g.:
- `REQ-TEST-ISOLATION`
- `REQ-TEST-NAMING`
- `REQ-TEST-FAST-VS-INTEGRATION`

Then:
- tests reference these stable tags in comments/docstrings (not necessarily in function names)
- you can grep the repo for `REQ-TEST-ISOLATION` and see coverage quickly

You can still mention old `FR-12` from a Speckit run in a “History” section, but don’t make it the primary handle.

### 2) Keep traceability where it pays: in tests, not everywhere
- Prefer `req: REQ-...` in test docstrings/comments.
- Avoid scattering requirement references through production code unless it’s genuinely helpful.

## Why this fits your two stated needs

1) **Coming back after weeks/months:** you open `docs/spec/_index.md`, pick the topic, and read one page that is guaranteed current. No folder archaeology.

2) **Reviewing implementation vs current spec:** the living page holds stable requirement tags; tests link to those tags; review becomes “spec page ↔ grep tags ↔ tests”.

## One question (answer before going deeper)
Do you want the living docs to be:
- **high-level + testable invariants only** (lean, durable), or
- **fully detailed acceptance-criteria style** (more precise, more maintenance)?

That choice determines how much you’ll rely on tests as the “executable spec” versus docs as the complete spec.

---

User:
> What contents do I need to keep in the living doc?
> - Specification (functional requirements). What would you recommend? The structure of the current speckit spec.mds? I need some minimal structure to be defined, so I know what should be there. Just "functional requirements" is too vague.
> - Architecture? Data model? More? Less?

GitHub Copilot:

## What the living doc should contain (so it stays useful, reviewable, and maintainable)

Treat living docs in `docs/spec/` as **normative “what is true now”**. They should contain **testable requirements + stable contracts**, and only the **minimum architecture/data model** needed to make those requirements unambiguous.

Everything else (exploration, alternative designs, step-by-step plans, task breakdowns) should live in **Speckit run folders** and **ADRs**.

## Recommended minimal set of living docs in docs

### 1) `docs/spec/_index.md` (entry point)
Purpose: “Where is current truth?”
- Topics list (Testing, Sync engine, CLI, Logging, Config, etc.)
- For each topic: link to the living spec page
- “Last changed” date (optional)
- Pointers to key ADRs

### 2) `docs/spec/<topic>.md` (one per area)
Purpose: current requirements + stable contracts for that topic.

### 3) `docs/spec/system.md` (optional but often worth it)
Purpose: cross-cutting invariants you keep repeating otherwise
- terminology/glossary (short)
- global constraints (e.g., “one machine active”, “never sync SSH keys”)
- common interfaces/conventions (config format rules, logging conventions, error policy)

## A concrete, minimal structure for each living topic spec

This is intentionally **lighter than Speckit**. The goal is “easy to keep current” while still being reviewable.

### Suggested outline: `docs/spec/<topic>.md`

1. **Scope**
    - What this page covers (and explicitly does *not* cover)

2. **Goals / Non-goals**
    - 3–7 bullets each (prevents scope creep and ambiguity)

3. **Definitions (only what’s necessary)**
    - Terms used by requirements (keep it short; link to `system.md` glossary if needed)

4. **Requirements (normative, testable, stable IDs)**  
    Use stable requirement IDs that don’t depend on ordering, e.g.:
      - `REQ-TEST-DISCOVERY`
      - `REQ-TEST-ISOLATION`
      - `REQ-TEST-FAST-SUITE`

    For each requirement:
    - **Statement** (must/shall)
    - **Rationale** (1–2 lines if non-obvious)
    - **Acceptance checks** (how you would verify; can be bullet list)
    - **References** (optional: link to Speckit run(s), ADR(s), related requirements)

5. **Contracts / Interfaces (normative)**  
    Only include what must remain stable for correctness:
    - CLI flags, config keys, file layout, environment variables
    - log event schema (if you rely on it)
    - exit codes / error classes / retry semantics (as applicable)

6. **Operational constraints / safety**
    - “Must not run when X” / “Requires Y suspended”
    - data-loss prevention rules
    - conflict detection expectations

7. **Observability (only if it affects correctness/supportability)**
    - required logs/metrics at a high level (not implementation detail)

8. **Traceability**
    - “Introduced in: `specs/00x-...`”
    - “Revised in: `specs/00y-...`”
    - Links to ADR(s) that explain *why* (not re-explaining them here)

That’s usually enough to (a) remember the truth later and (b) review code/tests against it.

## Should the living doc include architecture? Yes—but only the “contract-level” architecture

Include architecture *only to the degree it makes requirements unambiguous*.

Good living-architecture content:
- Component boundaries that imply contracts (e.g., “Runner invokes Steps; Steps are idempotent”)
- Invariants (e.g., “All sync operations are resumable”, “No destructive actions without explicit confirmation”)
- A single diagram (Mermaid) showing data/control flow at a high level

Avoid in living docs:
- detailed class/module layouts
- implementation strategies that might change without changing behavior
- task sequencing, phased rollout plans

Those belong in Speckit `plan.md` and ADRs.

## Should the living doc include a data model? Include only the “shared vocabulary + serialized shapes”

Include data model when:
- the project persists state, writes manifests, exchanges messages, or emits structured logs that tests/tools depend on.

In living docs, keep it to:
- entity names and meanings
- key fields and invariants
- serialized formats that must remain compatible (JSON schema-ish, table definition-ish)

Avoid:
- full ORM schema details unless they are part of a stable external contract

Deep modeling can live in Speckit runs or a dedicated design doc; the living spec should capture only what must not silently drift.

## Mapping to your existing Speckit artifacts (so you don’t duplicate work)

- Living topic spec ≈ **Speckit `spec.md` distilled to “current truth”**
- ADRs ≈ decisions that would otherwise bloat “rationale” sections
- Speckit `plan.md/tasks.md/research.md` ≈ historical context, execution details, and exploration (not kept current)

## One key choice you should make explicitly
Do you want living specs to read more like:
- **“Contracts + invariants”** (lean; tests carry many details), or
- **“Acceptance-criteria heavy”** (more explicit; higher doc maintenance)?

Given your goals (memory + review), “contracts + invariants” plus good tests is usually the best maintenance/reliability trade-off.

If you answer with one topic (e.g., “testing framework”), a concrete living `docs/spec/testing.md` skeleton can be proposed that matches your repo style and your current Speckit conventions—without copying the whole Speckit structure.