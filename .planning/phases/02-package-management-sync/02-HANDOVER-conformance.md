# Handover — bringing package sync in line with its requirements

The requirements review is finished. This file carries the plan that follows it. Start a new session with this file, `docs/planning/package-sync-user-requirements.md` and `docs/planning/package-sync-conformance-criteria.md`.

## Where things stand

`docs/planning/package-sync-user-requirements.md` states the intent and is settled — read top to bottom by the user, every open question ruled on, no *Open questions* section left. `docs/planning/package-sync-conformance-criteria.md` decomposes it into 126 checkable articles with a traceability table. Where the two disagree, the narrative wins and the article is what gets fixed.

Fourteen rulings the shipped code does not implement are recorded twice, deliberately: as entries in the criteria's *Where the tool does not yet meet these requirements*, and with their evidence in `02-DIVERGENCES.md` (DIV-01 … DIV-14). Do not restate them elsewhere. One of them, DIV-12, is a shipped bug rather than a requirement the code has not caught up with.

ADR-020 records the decisions and was edited to match the rulings — legal because it is still Draft. ADR-021 is new and holds the logging and credential-privacy rules, extending ADR-010 rather than superseding it.

## The plan

### Step 1 — One sweep, three targets

Read the code once; check three things against it.

- All 126 articles. Today's gap register names only the fourteen that the review happened to check; **the other 112 are unknown, not clean**, and "in line" is unverifiable until that is fixed.
- `docs/system/package-sync.md` and `docs/jobs/package-sync.md`, whole, not only the parts a change would touch.
- The rest of the doc surface: `docs/system/architecture.md`, `core.md`, `data-model.md`, `_index.md`, `docs/configuration.md`, `docs/jobs/folder-sync.md`, `docs/ops/testing-architecture.md`, `docs/ops/testing-ops.md`, `docs/README.md`, `README.md`, `docs/system/logging.md` (ADR-021's rules), `docs/planning/high-level-requirements.md`.

One pass because all three read the same ~12,600 lines; splitting them would read `apt_sync.py` three times and judge it three ways.

Record evidence as **symbol names, never line numbers**. The behaviour ledger was deleted for exactly this: its line numbers were taken at one commit and the branch moved. Step 2 will move every line in `apt_sync.py`.

Every doc lands in one of four buckets: correct, wrong about today, wrong once a fix lands, wrong once the restructure lands.

Outputs: a complete gap register, with a line recording the commit swept at and that all 126 were checked, so the register holds only failures and no second artefact goes stale. The wrong-about-today doc corrections, fixed and committed in this step. The remaining three buckets as a worklist at `02-DOC-DEBT.md`, which exists to be consumed and is deleted in Step 4. And the work units the fixes group into.

### Step 2 — Restructure chosen modules, wholesale

`apt_sync.py` is the candidate: 4053 lines of which 1633 are code, and `AptSyncJob` alone is 2412 lines with 69 methods and 35 instance attributes carrying state from `plan()` to `converge()` with the phase ordering enforced by convention rather than types. `flatpak_sync.py` (860 code lines) is arguable; `snap_sync.py` is fine.

Two rules the user set. A module is restructured **completely or not at all** — half a refactored class leaves two conventions in one file and no way to tell which is authoritative. And a module nothing will change is left alone; restructuring code no fix touches is pure cost.

The 35 attributes and the 4000-line file are one problem with one solution: the class does seven jobs, and extracting them as collaborators is what turns the attributes into explicit state owned by whoever needs it. The split is discovered rather than invented — `apt/items.py`, `apt/origins.py`, `apt/messages.py` (the nine `build_*_detail` text builders), `apt/diffing.py`, `apt/sources.py`, `apt/commands.py`, and `apt/job.py` reduced to orchestration over an origin planner, a repository-group converger and a collateral analyser. Sizes land between 150 and 600 lines because concerns are that size; 500 is not a target.

Pure moves and extraction only, no logic edits in the same commit, full suite green either side, integration tests as the backstop. The risk is real: a behaviour-preserving change of this size can hide a behavioural one in the noise.

Doc work here: every doc naming a module path, class or test layout ships in the restructure commit, which empties the wrong-once-the-restructure-lands bucket.

### Step 3 — Work units on clean ground

Per unit, one commit: the fix, its tests, and every document it affects — `docs/system/package-sync.md` for behaviour, `docs/jobs/package-sync.md` for anything the user sees, `docs/configuration.md` and `docs/jobs/folder-sync.md` where flags or job ordering are stated. Close that unit's gap-register entries and its `02-DIVERGENCES.md` entries in the same commit. A unit is not done while its doc lines are stale.

Units come from Step 1. The shape the review already suggests, to be confirmed or replaced by the sweep: collateral correctness (DIV-12 first, it is the only shipped bug); logging and credential privacy together, since verbatim output without redaction writes credentials into a world-readable log, and both live in `executor.py`; orchestration (`READ-FAILS-JOB`, `JOB-ORDER`); apt repositories and holds; flatpak remotes and the filter, which widens the remote capture to four columns and reshapes every remote fixture; snap sideloads.

### Step 4 — Closing re-sweep

Re-check the articles and the whole doc surface against the finished code, not only what was touched. `02-DOC-DEBT.md` must be empty and is then deleted. Register empty, or every survivor justified. Lint, typecheck, full suite, doc checks.

### Step 5 — UAT

`02-UAT.md` and `02-UAT-01-RUNBOOK.md` still describe the superseded two-pass review flow (DIV-09). Rewrite them for the current one-decision-per-group flow, extend to the new behaviour, then run with the user — giving copy-paste commands for them to run, not driving it.

### Step 6 — ADR statuses

ADR-020 and ADR-021 leave Draft once the code matches what they claim. Accepting freezes them under ADR-001's immutability rule, so this is last.

## How to work

Documentation checks, after every documentation change, all three passing before the commit:

```
anchors resolve (GitHub slug: lowercase, drop non-word chars, EACH space -> one hyphen)
every article mapped in the traceability table, no phantom or dangling ids
no banned vocabulary reintroduced
```

Do not chain the check and the commit with `&&` — gate the commit on the check's exit code, or a failing check still commits.

Commit with an explicit pathspec: `git commit -F - -- path1 path2`. Never `git add -A`, and never a bare `git commit` — the user edits the same tree and stages their own files while you work, and a bare commit takes them under your message. It has happened three times.

The user does not want GSD for this work: the planning and execution phases cost more time and tokens than the changes justify. Work directly, one commit per unit.

## Standing instructions on the documents

- **Be concise.** The narrative's reader has about fifteen minutes. Reasoning, evidence and analysis belong in ADR-020 and `docs/adr/considerations/`, which is linked.
- **One home per fact.** Narrative states intent; criteria state the checkable obligation with a one-line why; the ADR states the decision and what forced it; `considerations/` holds the evidence; `docs/system/` says how it is built; `docs/jobs/` says what the user sees; `.planning/*` is scaffolding to delete when spent. The ESM measurement currently lives in six files and the firefox-epoch evidence in eight — collapse duplicates when the sweep passes through them.
- **No jargon invented here.** Terms used must be defined, terms defined must be used. Removed so far: "diff", "direction", "travel", "vendor", "screen", "rows", "keystrokes", "provenance", "rehearsal", "prompts", "blocks" as a collective noun.
- Say "synced", not "travel". Say "the user", never "you", in the requirement documents. Say "job", not "ecosystem".
- **No UI mechanics** in the requirements — state the user's need, not the widget.
- **No restating core pc-switcher behaviour** beyond what is specific to package sync.
- Never use the maintainer's real machine names. The documents use `Atlas`, `Nomad`, `Vega`.
- ADR-020 reads as a design dump rather than a decision story, and the user knows it. Do not restructure it unasked; if asked, the shape is decisions in the ADR and evidence moved to `docs/adr/considerations/adr-020-*.md`.

## Loose ends unrelated to the plan

- `~/go/bin/crit` was installed for the review TUI and is no longer used.
- `docs/planning/issue-triage-2025-12-31.md` mentions the package jobs and is a dated artefact; the sweep should decide whether it is still worth keeping.
