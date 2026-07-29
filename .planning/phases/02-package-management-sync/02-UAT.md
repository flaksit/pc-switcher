---
status: testing
phase: 02-package-management-sync
source: [02-VERIFICATION.md]
started: 2026-07-24T12:02:22Z
updated: 2026-07-29T08:16:47Z
---

## Current Test

number: 1
name: Real-TTY interactive batched review
expected: |
  On a real terminal, run a sync with packages diverged in both directions. Each group is one screen listing its items, every row carrying its own decision in a column; `<y>`/`<s>`/`<n>` set the focused row, `<enter>` confirms, and the recorded outcome in *.decisions.yaml matches the column. The screens name the two machines by hostname. The snippet editor works, and an authored snippet lands in ~/.config/pc-switcher/package-snippets.yaml and replays on the target the same run.
awaiting: user response

## Tests

Intent under test: `docs/planning/package-sync-user-requirements.md` (what the user is promised) and `docs/planning/package-sync-conformance-criteria.md` (its testable form). Hand procedure for test 1: `02-UAT-01-RUNBOOK.md`.

### 1. Real-TTY interactive batched review
expected: |
  On a real terminal (not CI), run a sync with packages diverged in both directions (some to install, some to remove). Confirm:
  - Each group is ONE screen (`packages.decision_list`) listing its own items — no Rich panel above it, no second pass over the leftovers — and it hands the terminal back to the Rich Live display without corruption.
  - Every row carries its current decision in a column: `<y>` applies, `<s>` skips once, `<n>` marks always-skip, `<space>` cycles the focused row, the shift of any of those keys sets every row, `<enter>` confirms. State is a glyph, not a background colour.
  - Installs and removals are separate screens; install-direction rows start applied and removal-direction rows start at skip-once (`PKG-FR-REMOVAL-DISTINCT`).
  - A screen that records nothing (report-only, repository deletion, pin deletion, repository and remote conflicts) is the same widget with `<n>` absent from the legend (`PKG-FR-NO-MARK-ON-ORIGIN`).
  - Every title, detail, prompt and answer names the two machines by hostname; "source" and "target" appear nowhere on screen (`PKG-FR-NAME-THE-MACHINES`).
  - Each answer states its own effect on a named machine (`PKG-FR-EFFECT-NOT-MECHANISM`): a repository deletion names the URLs it takes away, a pin deletion prints the pin file, and the collateral prompt says what protects the package and that stopping ends the whole sync.
  - Whatever each column said is what lands in `*.decisions.yaml`, on the machine that HOLDS the item (`PKG-FR-MACHINE-SPECIFIC`).
  - The multi-line snippet editor (`(Ctrl-D to finish)`) works, rejects a whitespace-only body, and an authored snippet lands in ~/.config/pc-switcher/package-snippets.yaml and is replayed on the target in the same run.
result: [pending]
note: |
  Rehearsal run 2026-07-28, product still unrun. `tests/manual/review_harness.py` was driven interactively from the repo venv against the real `TerminalUI`, the real `review_items` and `ask_gate`, and the real prompt widgets — every screen shape the review can produce, plus Ctrl-C at each. No machine was contacted and nothing was written.

  Found in that rehearsal, all fixed in 9ba0d437 / 6541eae4 / 1db1fc6b:
  - the second screen echoed back the opposite of the decision just chosen ("remove fortunes-min" after the user had declined that removal);
  - the two-pass tick-then-tick-the-leftovers flow itself, replaced by one screen per group with a decision per row;
  - selection state carried by background colour alone, invisible in some terminals — now a glyph per decision;
  - the legend mislabelled `<a>` (conventionally abort) and never mentioned `<enter>`;
  - every row repeated the item's action, which the group title already names;
  - a trailing blank line inside every rendered panel;
  - a whitespace-only install snippet was accepted as a resolution;
  - "never offer again on this machine" named the consequence rather than what is recorded — now "always skip";
  - screens said "source"/"target" instead of the two hostnames;
  - a repository deletion showed only a filename, never the URLs it would take away;
  - a pin deletion showed only a filename, never the pin's content;
  - the collateral prompt said neither what protects the package nor that aborting ends the whole sync rather than the question.

  Two further reports were harness artifacts, not product defects: a stale hardcoded title in the harness, and a traceback on Ctrl-C that the real CLI catches (`cli.py`'s `except SyncAbortedByUser`). The harness now catches it the same way.

  Not exercised, and the reason this test stays pending: everything that needs two machines — a real sync, decision files written to the holding machine, the source-vs-target routing of an always-skip, the snippet registry push and replay, `/etc/apt` and flatpak remote convergence, and the Ubuntu Pro gate's re-probe loop.

### 2. Physical two-machine end-to-end walkthrough
expected: |
  On two real machines, run a full package sync and confirm all three phase success criteria hold end-to-end:
  - Packages replicate (apt/snap/flatpak installed on target match source).
  - Conflicts and version mismatches are reported before any change, never silently converged.
  - Machine-specific / always-skip packages stay inert (not forced onto target).
  Everything the user reads while deciding names the two machines by hostname (`PKG-FR-NAME-THE-MACHINES`). Then inspect ~/.config/pc-switcher/*.decisions.yaml (note `manual.decisions.yaml` for manual installs — the manager id, not the job name) and package-snippets.yaml on both ends, and confirm they reflect the run, each always-skip entry landing on the machine that holds the item.
result: [pending]

### 3. --confirm-each-command gate and verbatim debug trace
expected: |
  Only reachable by hand: the gate refuses to run without a TTY, so no unit or integration test can drive the prompt. On a real terminal, run a sync with diverged packages using `pc-switcher sync <target> --confirm-each-command`. Confirm:
  - Before EVERY modification a prompt appears showing the exact command (or `send_file <local> -> <remote>`), and the prompt composes cleanly with the paused Rich Live display, same as the batched review.
  - Pressing Enter alone re-prompts — there is no default choice.
  - `p` runs that one command and moves to the next prompt; `a` aborts the whole sync.
  - Prompts appear for target writes (apt/snap/flatpak converge) AND source writes (an always-skip decision file, an authored snippet), and NOT for read-only commands.
  - Ctrl-C at a prompt aborts rather than proceeding.
  Then, in the same run's log file (`pc-switcher logs --last`), confirm every command appears verbatim at DEBUG with its job and host — reads included — and that a mutating line carries its description in brackets.
  Finally, run once WITHOUT the flag and confirm the sync is unchanged (no prompts, same outcome).
  Intent: `PKG-FR-CONFIRM-EACH` in `docs/planning/package-sync-conformance-criteria.md`.
result: [pending]

## Summary

total: 3

passed: 0

issues: 0

pending: 3

skipped: 0

blocked: 0

<!-- Test 1 carries rehearsal evidence (see its note): every review screen was driven by hand through tests/manual/review_harness.py on 2026-07-28 and twelve findings were fixed. It stays pending because the test is a real sync between two machines, which has never run. -->

## Gaps

[none open — every rehearsal finding recorded under test 1 was fixed before this file was updated]
