# Handover — bringing package sync in line with its requirements

## Where things stand

`docs/planning/package-sync-user-requirements.md` states the intent and is settled. `docs/planning/package-sync-conformance-criteria.md` decomposes it into 126 checkable articles with a traceability table. Where the two disagree, the narrative wins.

Fourteen rulings the code does not implement: gap register in the criteria, evidence in `02-DIVERGENCES.md` (DIV-01…DIV-14). Do not restate them elsewhere. DIV-12 is a shipped bug.

ADR-020 holds the decisions; ADR-021 the logging and credential-privacy rules. Both Draft.

## Step 1 — Restructure `apt_sync.py`, wholesale

`AptSyncJob` is 2412 lines, 69 methods, 35 attributes carrying state from `plan()` to `converge()`. It does seven jobs; extract them as collaborators and the attributes become explicit state. `flatpak_sync.py` (860 code lines) arguable, `snap_sync.py` no.

Split: `apt/items.py`, `origins.py`, `messages.py` (the nine `build_*_detail`), `diffing.py`, `sources.py`, `commands.py`, `job.py` + collaborators. 150–600 lines each; 500 is not a target.

Complete or not at all. Renaming and reshaping functions and classes is allowed; changing behaviour is not. Unit and integration suites green before and after each commit — otherwise a behavioural change introduced here becomes the baseline Step 2 records.

Docs naming module paths, classes or test layout ship in the restructure commit.

## Step 2 — One sweep, three targets

Read the code once; check against it:

- all 126 articles — only 14 have been checked, the other 112 are unknown, not clean;
- `docs/system/package-sync.md` and `docs/jobs/package-sync.md`, whole;
- `docs/system/{architecture,core,data-model,_index,logging}.md`, `docs/configuration.md`, `docs/jobs/folder-sync.md`, `docs/ops/testing-{architecture,ops}.md`, `docs/README.md`, `README.md`, `docs/planning/high-level-requirements.md`.

Evidence as symbol names, never line numbers — Step 3 moves lines again.

Outputs: complete gap register, with the swept-at commit recorded; docs wrong about current code-state fixed here; docs wrong only once a fix lands listed in `02-DOC-DEBT.md` (deleted in Step 4); the work units.

## Step 3 — Work units

Per unit, one commit: fix, tests, and every doc it affects (`docs/system/package-sync.md`, `docs/jobs/package-sync.md`, `docs/configuration.md`, `docs/jobs/folder-sync.md`), closing its gap-register and `02-DIVERGENCES.md` entries. Units come from Step 2. Likely: collateral (DIV-12 first); logging + credential privacy together in `executor.py`; orchestration; apt repositories and holds; flatpak remotes and filter (widens the remote capture to four columns, reshapes every remote fixture); snap sideloads.

## Step 4 — Closing re-sweep

Articles and the whole doc surface against finished code. `02-DOC-DEBT.md` empty, then deleted. Register empty or every survivor justified. Lint, typecheck, full suite, doc checks.

## Step 5 — UAT

`02-UAT.md` and `02-UAT-01-RUNBOOK.md` describe the superseded two-pass flow (DIV-09). Rewrite for one-decision-per-group, extend to the new behaviour, then give the user copy-paste commands to run.

## Step 6 — ADR statuses

ADR-020 and ADR-021 leave Draft once the code matches them.

## The scenario matrix

`docs/dev/package-sync-scenario-coverage.md`, rebuilt from the 130 articles. Scenario ids were reassigned in that rebuild, so an id quoted in an older document in this directory means nothing in the new one. It outlives the phase; the documents here do not.

## How to work

Commit with an explicit pathspec: `git commit -F - -- path1 path2`. Never `git add -A`, never a bare `git commit` — the user stages their own files in the same tree.

No GSD. Work directly, one commit per unit.

## Standing instructions on the documents

- Be concise. Cut any sentence a reader could skip without acting differently.
- One home per fact: narrative = intent; criteria = checkable obligation + one-line why; ADR = decision + what forced it; `docs/adr/considerations/` = evidence; `docs/system/` = how it is built; `docs/jobs/` = what the user sees; `.planning/*` = scaffolding, deleted when spent. The ESM measurement is in six files, the firefox-epoch evidence in eight — collapse on contact.
- Terms used must be defined, terms defined must be used. Banned: diff, direction, travel, vendor, screen, rows, keystrokes, provenance, rehearsal, prompts, "blocks" as a noun.
- Say "synced", "the user", "job".
- Machine names: `Atlas`, `Nomad`, `Vega`.
- Do not restructure ADR-020 unasked.
