# Issue triage — 2026-08-05

Triage of every open GitHub issue that is not claimed by an open PR and does not carry `status:done` or `status:working`. 40 issues qualify. Excluded: the 29 issues linked as closing references on PR #206 (package sync) and PR #157, plus #118 and #216 (`status:working`).

## Labels

No qualifying issue is missing `comp:sync:packages`. Every package-sync issue already carries it: #255, #254, #253, #252, #251, #248, #246, #245, #207.

Two deliberate non-additions:

- #244 "Nice job names in TUI" — about job display names generally (including "VSCode state"), so `comp:tui` is correct.
- #219 "Use JobResult SKIPPED rather than SUCCESS" — reported from a package-sync session and the four package jobs are the main offenders, but it also covers `vscode_state_sync`, `folder_sync` and orchestrator job discovery, so `comp:core` is correct.

## Duplicates and overlaps

| Relationship | Verdict |
| --- | --- |
| #65 "Faster integration tests" vs #216 | Duplicate. #65 closed; the concrete children #76, #69, #150, #64 stay open |
| #247 vs #207 | Merged into #207 — update/upgrade, removal, payload drift and snippet drift are one design, not four |
| #245 ⊂ #48 | Subset, not duplicate. #245 is the package slice of "flags for all interactive questions"; both stay open |
| #82 vs #79 | #82 (remove the GitHub API dependency from core) subsumes #79. #79's own scope is already implemented — see below |
| #254 umbrella of #251, #252, #253 | Confirmed. #207 is not part of it |
| #181 ⊂ #182 | A log viewer with level filtering satisfies "see warnings during run". Warning capture, the status-bar counter and the end-of-run summary already exist (`ui.py:74-80,152-155,369-402`); only consulting them mid-run is missing |
| #218 + #219 | Coupled, not duplicate. SKIPPED stays a bare log line until #218 renders per-job outcomes |
| #217 ⊂ #126 | Same problem — a bash implementation duplicating the Python path for the target side. Declared in #126's scope; #217 still open |
| #24 blocked by #28 + #29 | Stated in the issue |

## Already implemented, awaiting merge

#210 "Output executed commands to DEBUG" is implemented in PR #206 by two commits: `6d162411` (every command, transfer and background process traced verbatim at DEBUG before it runs, plus `declare_modification()` for in-process writes) and `9d646821` (each command's stdout and stderr traced verbatim after it). The trace lives in `Executor`, the single funnel, enforced by `tests/unit/test_mutates_audit.py`. The one exception is `withhold=` for the ESM subscriber payload (`PKG-FR-ESM-PRIVACY`), where the command and the reason are still logged.

#211 "Package sources/keys/channels follow package selection" is implemented in PR #206 under ADR-020 D-11–D-14, D-34, D-36, D-41, D-02. Repository files, keyrings, pins, flatpak remotes and blocks are derived from the packages or refs approved in the review and provisioned before the install that needs them; "package ticked, its repository unticked" is unrepresentable (`diffing.py:351-357`, `flatpak_sync.py:22-35`). Removal still asks, which is the one case the issue wanted kept. A failed derived write fails every item that depended on it, naming the file or the remote.

Both are now `Closes #NNN` references on PR #206 and labelled `status:done`; they close when it merges.

#79 "GitHub API rate limits when running InstallOnTargetJob" — the revised plan from the issue's own comments is implemented: `tests/integration/scripts/internal/configure-vm.sh:97-102` writes `GITHUB_TOKEN` into testuser's `~/.profile` over stdin, `tests/integration/conftest.py:484-497` does the same for the `pc1`/`pc2` executors, `README.md:163-180` documents the user-facing remedy and `docs/ops/testing-ops.md:90-95` documents CI's `GH_API_TOKEN_RO` secret. Nothing remains that #82 does not cover.

## Proposed order — package sync

1. #248 apt timers and an actionable busy-dpkg abort. The only issue with an observed aborted run; it hits real desktops and CI alike.
2. #207 update/upgrade, removal, payload drift, snippet drift. Silent divergence: a `/opt` payload can differ on both machines forever with no job that would notice, and nothing propagates a removal.
3. #245 `--apply-package-installs` / `--apply-package-removals`. Unblocks unattended convergence and sets the pattern for #48.
4. #255 which side a machine-specific mark lands on for a conflicting item. The read path already tolerates a mark on either side; only the write path and the review question are missing.
5. #254 umbrella: #253 (move .debs out of `manual_installs_sync`) first, since it extracts code that already works, then #251 sideloaded snaps and #252 manual flatpaks reuse its shape. All three inherit the update and removal semantics #207 defines, so they follow it.
6. #246 ADR-020 cleanup. Cheap, and ADR-020 is the document every item above cites — worth doing before it is edited further.

Package-motivated but labelled `comp:core`, best slotted between 3 and 4: #219 and #218 together. Every headless run currently reports SUCCESS for four package jobs that converged nothing.

## Proposed order — everything else

Cheap wins, any time: #244 nice job names, #183 compress log files (a 335 MB initial-sync log), #79 verify and close.

Correctness of the run model: #219 and #218, then #220 job failure independence, which is the first real bite of the DAG work.

Test and CI cluster, worth one push: #78 (integration lock not cleared when a run is cancelled), #82 which also closes #79, #69, #76, #150, #64, #68, #40, #156, #85.

Architecture milestone: #28 DAG, then #29 forever-running jobs, then #30 (one module, one job class), then #24 DiskSpaceMonitor. #23 (no btrfs), #26 (no Ubuntu 24.04 constraint) and #31 (rollback command) all need the snapshot job to become config-controlled first.

After package sync merges: #119 Feature 7 system configuration sync is the next milestone; #126 internal CLI (now including #217) is worth doing alongside it.

Backlog: #182 log viewer with #181, #178 pre-folder-sync hooks, #88 MADR migration.

## Open questions

- #132 is claimed by PR #157, a Copilot draft untouched since 2026-07-18. Not labelled `status:done` — whether it actually implements the SIGINT test is unverified.
- #217 is declared in #126's scope but still open. Close it or keep it as the tracking entry for that scope bullet.
- #79 can be closed once #82 is confirmed as its successor.
