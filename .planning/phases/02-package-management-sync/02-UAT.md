---
status: testing
phase: 02-package-management-sync
source: [02-VERIFICATION.md]
started: 2026-07-24T12:02:22Z
updated: 2026-08-01T00:00:00Z
---

## Current Test

number: 1
name: The package review on two machines
expected: |
  Follow `02-UAT-01-RUNBOOK.md`. Every group is one question, every item a line carrying its own answer, and nothing is asked twice. What each column said is what lands in `*.decisions.yaml`, on the machine that holds the item.
awaiting: user response

## Tests

Intent under test: `docs/planning/package-sync-user-requirements.md` and its testable form, `docs/planning/package-sync-conformance-criteria.md`. Hand procedure for tests 1 and 2: `02-UAT-01-RUNBOOK.md`.

### 1. The package review on two machines
expected: |
  On a real terminal, with the two machines diverged as the runbook sets them up:
  - One question per group: arrow keys between lines, `<y>`/`<s>`/`<x>` on the focused line, a shifted key on every line, `<enter>` to confirm, `<ctrl-c>` to abort the sync. Nothing is asked twice and no set is left over. A question that records nothing offers two answers, its legend shorter by exactly `<x>` — a repository being deleted and a snap's revision are both that shape. The questions that must show something first — a repository being deleted, a collateral package, an unreproducible item — come one item at a time, and the ones an answer brings into being come in a second round after it.
  - A hold and a mask are asked about nowhere at all; the shape of an `/opt` directory that could be one application or a publisher's shelf is asked while the run is still planning.
  - Every title, detail and answer names the two machines by hostname and states its own effect on one of them; the permanent answer says the user will not be asked again and whose machine the item is. A collateral question names the change that causes it and the ground that protects the package, and is answered per consequence. "source" and "target" name no machine anywhere.
  - What each column said is what lands in `*.decisions.yaml` on the machine that holds the item: an install on the source, a removal on the target, an unreproducible finding on the source. The snippet editor rejects a whitespace-only body, and an accepted snippet lands in `~/.config/pc-switcher/package-snippets.yaml` and replays on the target in the same run.
result: [pending]

### 2. What the run leaves behind
expected: |
  The same run, checked afterwards on both machines (runbook §6):
  - Packages replicate, except what the user skipped or marked; conflicts and version differences are reported before any change, never silently converged; a package marked as a machine's own stays inert, and is named rather than taken when another change would remove it.
  - A held package arrives at the source's version and its hold follows it; a snap moves to the source's revision; a sideloaded snap is left alone on both machines and mentioned nowhere; the source's flatpak filter reaches the target at the same path and is in force before anything installs from that remote; a repository the target still installs from is not offered for deletion; a remote nothing uses is deleted with no question; only what the target lacks is presented as unreproducible, and the registry reaches the target through that job's own push and nothing else.
  - Three bookkeeping failures end the run while planning, before anything is written, each naming what to repair: a hold on a package its machine does not have, a source remote whose filter does not offer what the source installed from it, and a snippet registry that cannot be parsed (runbook §3).
  - The log names every item and the answer it received, carries each package manager's own output, and shows a URL credential as `***@` and never in full, whatever characters it holds. With no terminal, no snippet registry is transferred and no snapd refresh policy is written on a machine whose own could not be read; a read that went dark fails its own job, the other jobs still run, and the end-of-run message gives one line per failed job naming the reason it recorded (runbook §7).
result: [pending]

### 3. --confirm-each-command gate and verbatim debug trace
expected: |
  Only reachable by hand: the gate refuses to run without a TTY. On a real terminal, run `pc-switcher sync <target> --confirm-each-command` with diverged packages, then once without the flag to confirm the sync is unchanged. Confirm:
  - Before every modification a question shows the job, the machine by hostname and the exact command (or `send_file <local> -> <remote>`), and composes cleanly with the paused Rich Live display; Enter alone re-asks, `p` runs that one command, `a` and Ctrl-C abort the whole sync. The gate appears for target writes and for source writes (a decision file, an authored snippet), and never for reads.
  - In the same run's log, every command appears verbatim at DEBUG with its job and host, and a mutating line carries its description in brackets (`PKG-FR-CONFIRM-EACH`).
result: [pending]

## Summary

total: 3

passed: 0

issues: 0

pending: 3

skipped: 0

blocked: 0

## Gaps

[none open]
