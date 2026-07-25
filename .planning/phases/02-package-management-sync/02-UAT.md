---
status: testing
phase: 02-package-management-sync
source: [02-VERIFICATION.md]
started: 2026-07-24T12:02:22Z
updated: 2026-07-24T12:02:22Z
---

## Current Test

number: 1
name: Real-TTY interactive batched review
expected: |
  On a real terminal, run a sync with packages diverged in both directions. The questionary checkbox review composes cleanly with the paused Rich Live display: installs and removals are grouped separately, removals start unticked, and ticking/unticking then apply/skip/skip-always produces the recorded outcome. The multi-line snippet capture editor works, and an authored snippet lands in ~/.config/pc-switcher/package-snippets.yaml.
awaiting: user response

## Tests

### 1. Real-TTY interactive batched review
expected: |
  On a real terminal (not CI), run a sync with packages diverged in both directions (some to install, some to remove). Confirm:
  - The questionary checkbox list renders cleanly and hands the terminal back to the Rich Live display without corruption.
  - Installs and removals are shown as separate groups.
  - Removal items start unticked (not selected by default).
  - Ticking/unticking items, then choosing apply / skip / skip-always, each produces the outcome recorded in *.decisions.yaml.
  - The on-the-fly multi-line snippet capture editor (02-07) works and an authored snippet lands in ~/.config/pc-switcher/package-snippets.yaml.
result: [pending]

### 2. Physical two-machine end-to-end walkthrough
expected: |
  On two real machines, run a full package sync and confirm all three phase success criteria hold end-to-end:
  - Packages replicate (apt/snap/flatpak installed on target match source).
  - Conflicts and version mismatches are reported before any change, never silently converged.
  - Machine-specific / skip-always packages stay inert (not forced onto target).
  Then inspect ~/.config/pc-switcher/*.decisions.yaml and package-snippets.yaml on both ends and confirm they reflect the run.
result: [pending]

### 3. --confirm-each-command gate and verbatim debug trace
expected: |
  Only reachable by hand: the gate refuses to run without a TTY, so no unit or integration test can drive the prompt. On a real terminal, run a sync with diverged packages using `pc-switcher sync <target> --confirm-each-command`. Confirm:
  - Before EVERY modification a prompt appears showing the exact command (or `send_file <local> -> <remote>`), and the prompt composes cleanly with the paused Rich Live display, same as the batched review.
  - Pressing Enter alone re-prompts — there is no default choice.
  - `p` runs that one command and moves to the next prompt; `a` aborts the whole sync.
  - Prompts appear for target writes (apt/snap/flatpak converge) AND source writes (a skip-always decision file, an authored snippet), and NOT for read-only commands.
  - Ctrl-C at a prompt aborts rather than proceeding.
  Then, in the same run's log file (`pc-switcher logs --last`), confirm every command appears verbatim at DEBUG with its job and host — reads included — and that a mutating line carries its description in brackets.
  Finally, run once WITHOUT the flag and confirm the sync is unchanged (no prompts, same outcome).
result: [pending]

## Summary

total: 3
passed: 0
issues: 0
pending: 3
skipped: 0
blocked: 0

## Gaps

[none yet]
