---
phase: 02-package-management-sync
plan: 02
subsystem: infra
tags: [questionary, tui, checkbox-prompt, package-review, rich-live, asyncio]

requires:
  - phase: 01-home-sync-mvp-user-data-sync
    provides: TerminalUI pause/resume Live model, TerminalUIConfirmer pause/prompt/resume pattern, is_interactive predicate
provides:
  - "questionary as a vetted runtime dependency"
  - "src/pcswitcher/jobs/package_review.py: review_items, ReviewGroup, ReviewEntry, ReviewOutcome, Decision, PACKAGE_REVIEW_AUTOMATION_ENV"
  - "Confirmed (partly by test, partly deferred to human) that a questionary checkbox composes with a paused Rich Live"
affects: [02-04, 02-05, 02-06, 02-07, 02-08, 02-09, 02-10, 02-11]

tech-stack:
  added: [questionary 2.1.1]
  patterns:
    - "Batched checkbox review as the single interaction surface for a diff (D-24), reusing TerminalUIConfirmer's pause/try/finally-resume shape with the blocking call swapped for questionary.checkbox().ask() under asyncio.to_thread"
    - "Hidden automation env var (PCSWITCHER_PACKAGE_REVIEW_AUTOMATION) checked before the interactivity branch, so integration tests can answer a review deterministically regardless of TTY state"

key-files:
  created:
    - src/pcswitcher/jobs/package_review.py
    - tests/unit/jobs/test_package_review.py
  modified:
    - pyproject.toml
    - uv.lock

key-decisions:
  - "questionary chosen over InquirerPy: the legitimacy gate (T-02-SC) was cleared by explicit user approval plus live PyPI/GitHub verification, not training-data recall — see Deviations section for the measured facts."
  - "Removal-direction is determined by ReviewGroup.action against a private {remove, delete, disable} set, not a DiffAction enum (that type doesn't exist yet — plan 02-05 owns the real item model). Any other action value (install/add/enable/change) defaults to checked."
  - "PACKAGE_REVIEW_AUTOMATION_ENV is checked before the is_interactive branch, so it can answer a review deterministically in tests regardless of measured TTY state."
  - "requirements-completed left empty: this plan's frontmatter carries the phase-level REQ-conflict-detection-no-resolution ID, but 11 plans remain before that requirement is actually satisfied. Per orchestrator directive, .planning/REQUIREMENTS.md was not touched and requirements.mark-complete was not run."

patterns-established:
  - "Any future blocking prompt_toolkit-based prompt in this codebase should follow review_items's shape: ui.pause() -> try/finally: ui.resume(), with the blocking .ask() dispatched via asyncio.to_thread."

requirements-completed: []

coverage:
  - id: D1
    description: "questionary is a declared runtime dependency in pyproject.toml and resolves in uv.lock"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: other
        ref: "git diff pyproject.toml uv.lock (questionary>=2.1.1 dependency line + resolved package entry with prompt-toolkit/wcwidth transitive deps)"
        status: pass
    human_judgment: false
  - id: D2
    description: "review_items pauses the Live display, runs the checkbox prompt off the event loop via asyncio.to_thread, and resumes the display even when the prompt raises or is aborted"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestInteractive::test_ui_resumed_when_prompt_raises"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestInteractive::test_abort_skips_current_and_remaining_groups"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestBlockingPromptOffLoop::test_synchronous_sleep_in_ask_does_not_block_loop"
        status: pass
    human_judgment: false
  - id: D3
    description: "Non-interactive run prompts for nothing, returns every item as skip-once, and records no permanent decision (D-26)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestNonInteractive::test_no_prompt_constructed_and_everything_skipped_once"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestNonInteractive::test_warns_with_unresolved_count_and_reports_groups"
        status: pass
    human_judgment: false
  - id: D4
    description: "Removals are presented in their own group, labelled with the concrete action, and never share a group with installs (D-07, D-24)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestInteractive::test_no_group_mixes_install_and_removal_entries_in_one_prompt"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestInteractive::test_install_group_defaults_checked_removal_group_defaults_unchecked"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestInteractive::test_removal_group_title_names_concrete_verb"
        status: pass
    human_judgment: false
  - id: D5
    description: "A human confirms the checkbox prompt renders, navigates and returns selections correctly while the single persistent Live panel is paused around it"
    verification: []
    human_judgment: true
    rationale: "Unit tests stub questionary.checkbox() and never exercise real prompt_toolkit rendering, keybindings, or terminal-mode handoff with a live TTY — exactly what RESEARCH Assumption A2 flags as unverified. This autonomous run cannot drive a real terminal; see 'Deferred human verification' below for the exact command and what to look for. Piped (non-TTY) behavior was verified automatically."

duration: ~20min (active work; excludes the human wait on Task 1's package-legitimacy checkpoint)
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 02: questionary dependency + batched review primitive Summary

**`review_items()` in `src/pcswitcher/jobs/package_review.py` pauses the persistent Rich Live display, runs a `questionary` checkbox off the event loop, and resumes it even on failure or abort — the single interaction surface every package job's diff will use (D-24), backed by 11 passing unit tests and a throwaway manual spike for the one thing tests can't prove.**

## Performance

- **Duration:** ~20 min active work (Task 1's checkpoint paused the session for an out-of-band human approval, not counted)
- **Completed:** 2026-07-23
- **Tasks:** 3 (1 checkpoint, 1 auto/tdd, 1 checkpoint deferred)
- **Files modified:** 4 (2 created, 2 modified)

## Accomplishments

- `questionary` 2.1.1 added as a runtime dependency after an explicit human approval plus live registry verification (see Deviations) — resolved in `uv.lock` with its sole runtime dependency `prompt-toolkit<4.0,>=2.0`.
- `src/pcswitcher/jobs/package_review.py` implements `review_items`, `ReviewGroup`, `ReviewEntry`, `ReviewOutcome`, `Decision`, and the hidden `PACKAGE_REVIEW_AUTOMATION_ENV` escape hatch — all pinned by 11 unit tests covering interactive selection, abort, exception-safety, non-interactive skip-all, removal/install default-checked-state, group isolation, off-loop dispatch, and the automation env var.
- Untrusted item text (package names, versions, stderr fragments) is wrapped in `rich.text.Text` before reaching a `Panel`, mitigating T-02-02 (MarkupError from bracketed content).
- The throwaway manual driver `/tmp/pcswitcher-review-spike.py` (Task 2's explicit deliverable) exists, compiles, stays untracked, and its piped/non-TTY path was verified end-to-end (no prompt, no hang, every item reported as `skip_once`).

## Task Commits

1. **Task 1: Verify questionary's package legitimacy before installing it** — checkpoint, no commit (human approval; see Deviations for the measured facts recorded here)
2. **Task 2: Add questionary and build the batched review primitive** - `5ff3631` (feat)
3. **Task 3: Confirm the checkbox prompt composes with the paused Live display** — checkpoint deferred to human (see "Deferred human verification" below); no code change

**Plan metadata:** (this commit)

## Files Created/Modified

- `src/pcswitcher/jobs/package_review.py` - `review_items` and its supporting types; the batched-review primitive
- `tests/unit/jobs/test_package_review.py` - 11 tests covering every behavior bullet in the plan
- `pyproject.toml` - `questionary>=2.1.1` added to `dependencies`
- `uv.lock` - `questionary` 2.1.1 and its transitive `prompt-toolkit`/`wcwidth` resolved

## Decisions Made

- **questionary over InquirerPy, legitimacy gate cleared by measured facts, not recall.** The coordinator verified against the live PyPI and GitHub APIs (not training data) and the user was asked directly and chose questionary. Measured this session: PyPI `questionary` 2.1.1, MIT, `requires-python >=3.9`, single runtime dep `prompt_toolkit<4.0,>=2.0`; 24 releases, first 2018-12-01, latest 2025-08-28; all three PyPI `project_urls` (Homepage, Repository, Documentation) resolve to `github.com/tmbo/questionary` and `questionary.readthedocs.io`. GitHub `tmbo/questionary`: 2138 stars, 117 forks, created 2018-12-01 (same day as the first PyPI release — no recreation gap that would indicate a lookalike), last push 2026-07-22, not archived, MIT. RESEARCH's `SUS`/`unknown-downloads` verdict was download telemetry being unreachable (pypistats.org unreachable from this environment too), not a negative signal — no other adverse signal was found.
- **Removal-direction determined by a private action set, not a `DiffAction` enum.** `ReviewGroup.action` is a plain `str` (documented as "DiffAction-shaped") because that enum doesn't exist yet — plan 02-05 owns the real item model per CONTEXT.md's "Claude's Discretion" note. A module-private `{remove, delete, disable}` frozenset decides which groups default their checkboxes unchecked; everything else (`install`/`add`/`enable`/`change`) defaults checked.
- **Automation env checked before the interactivity branch.** `PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` is read first in `review_items`, ahead of `is_interactive(console)`, so integration tests get deterministic behavior regardless of measured TTY state rather than only in the non-interactive branch.
- **`requirements-completed` left empty.** This plan's frontmatter carries the phase-level `REQ-conflict-detection-no-resolution` ID, but 11 plans remain before that requirement is actually satisfied end-to-end. Per orchestrator directive, `.planning/REQUIREMENTS.md` was not touched and `requirements.mark-complete` was not run — the orchestrator marks requirements at phase end.

## Deviations from Plan

None — plan executed exactly as written. Task 1's checkpoint resolved via explicit human approval (with live-registry verification performed by the coordinator) rather than the executor auto-approving; Task 3's checkpoint is deferred per explicit orchestrator directive for this autonomous run (see below) rather than blocking the session.

## Issues Encountered

None.

## Deferred Human Verification

Task 3 (`gate="blocking"`) requires a human to confirm the `questionary` checkbox prompt actually renders, navigates and hands the terminal back correctly while the persistent Live panel is paused around it — unit tests stub `questionary.checkbox()` and can never exercise real `prompt_toolkit` rendering, keybindings, or terminal-mode handoff (RESEARCH Assumption A2). This run is autonomous and has no TTY, so per the orchestrator's directive this check is deferred rather than blocked on.

**Verified automatically this session** (everything a non-interactive agent can prove):
- `/tmp/pcswitcher-review-spike.py` exists, compiles (`uv run python -m py_compile /tmp/pcswitcher-review-spike.py` exits 0), and stays untracked (`git status --porcelain` in the repo shows nothing under the project tree for it).
- Piped/non-TTY run (checkpoint step 6): `echo "" | uv run python /tmp/pcswitcher-review-spike.py` completed without hanging, constructed no prompt, and reported all 5 items (`pkg-a`..`pkg-e`) as `skip_once` with `was_interactive=False`.

**Still needs a human, run interactively:**

```
uv run python /tmp/pcswitcher-review-spike.py
```

Look for:
1. The live status bar + log panel are visible before the checkbox prompt appears, and the prompt takes over cleanly with no overlapping or duplicated lines.
2. Arrow keys navigate, space toggles, enter confirms; the "Install packages" group's 3 entries (ripgrep, fd-find, bat) start ticked, and the "Remove packages" group's 2 entries (brscan3, cnpg) start unticked.
3. The removal group's header reads "Remove packages" — the concrete verb, not "apply".
4. After confirming both groups, the live panel resumes cleanly with no leftover prompt artefacts and no cursor corruption, then prints the returned `ReviewOutcome`.

(Step 6, piped/no-hang, is already verified above — re-running it by hand is optional confirmation only.)

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

`review_items` and its `ReviewGroup`/`ReviewEntry`/`ReviewOutcome`/`Decision` types are ready for plan 02-05 to adapt the real `ItemDiff` model onto `ReviewEntry` and for plan 02-04 to wire `Decision.SKIP_ALWAYS` promotion. Composition with the paused Live display is proven by unit test for the pause/resume contract; real-terminal rendering/keybinding/handoff is proven by piped-mode automated check plus the deferred interactive human check above — no blocker for downstream plans, since none of them can proceed to a real terminal check any sooner than this one could.

---
*Phase: 02-package-management-sync — Completed: 2026-07-23*
