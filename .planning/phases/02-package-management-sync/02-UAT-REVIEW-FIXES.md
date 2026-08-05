# Phase 02 — Pre-UAT review fixes

Status: Implemented (2026-07-24). All decisions below are executed in code and docs; ADR-020 amended to match (see its 2026-07-24 amendment note).

Decisions from the pre-UAT doc/code review (README.md + package-sync.md). Ground truth for the executor subagents. Plan → execute (code, then docs) → review.

## Accepted deviations from these decisions

1. Snap auto-refresh hold mechanism (decision 4). Implemented via `snap set system refresh.hold=<timed timestamp>` (the read-write system option), NOT the `snap refresh --hold` verb this doc's decision 4 named. Rationale: `snap refresh --hold` with no snap name sets an INDEFINITE global hold and its `--unhold` clears unrelated per-snap holds, whereas `snap set system refresh.hold` writes only the general option, is symmetric with the read-only `snap get system refresh.hold`, and takes a timed value that self-expires as a crash backstop. Behaviour is as decision 4 intended (pause automatic refresh only, restore prior policy in cleanup); only the verb differs.
2. New source-side sudo requirement (consequence of decisions 3+4). Because the auto-refresh hold is written on BOTH hosts via `sudo snap set system`, `snap_sync.validate()` now requires passwordless sudo on the SOURCE too, not just the target. This is a new environmental prerequisite not spelled out in the original decisions.

## Locked decisions

1. apt-get update timing. Run one `apt-get update` per run before the first package install whenever there is ≥1 approved install — not only when a repo-group (key/source/pin/config) item changed. If that refresh fails in the install-only path, ABORT the job (installing against stale lists is unsafe). Keep the existing repo-group rollback path unchanged. Idempotent: at most one refresh per run.

2. Version divergence. apt and flatpak float (report-only on a version difference) — correct, keep. snap DOES converge versions (revision + channel) — this asymmetry is intentional and now documented (reason below). manual_installs has no version comparison (issue-deferred).

   Snap reason (issue #118): snap stores per-user app data in revision-NUMBER-named dirs `~/snap/<app>/<rev>/`; apt uses stable paths and flatpak uses id-named `~/.var/app/<id>`, so only snap's data path embeds the version. Convergence keeps both machines on the same rev so folder_sync mirrors the active-revision data dir cleanly.

3. Snap data gap fix (extends decision 2). Today snap_sync converges the revision but folder_sync EXCLUDES all `~/snap/<app>/<rev>` dirs, so per-revision app data never travels. Fix: sync the CURRENT revision's data dir (folder_sync mirrors it; convergence + D-17 order guarantee both machines are on that rev by folder_sync time), keep excluding retained-old rev dirs (avoid orphan data dirs snapd does not track).

4. Snap mid-sync race guard. snapd auto-refreshes in the background (~4x/day, even for closed apps). Pause snapd AUTOMATIC refresh on BOTH source and target for the sync window (spanning convergence through folder_sync); restore the prior policy in the always-run cleanup. `--hold` blocks only automatic refreshes, not our manual `--revision` convergence (verified, snapcraft "Manage updates"). Prefer a timed hold as a crash backstop; do not clobber a hold the user already set (capture prior state, restore it). Compatible with D-06 (no standing block left behind).

5. Deletions. apt/snap/flatpak already produce removal review items (document it). manual_installs is install-only (no target manifest) — removal is deferred to a GitHub issue.

6. `/etc/apt` is CONFIG, not state. Change "repository state"/"/etc/apt state" → "repository configuration"/"/etc/apt config" in docs, comments, default-config. Leave genuinely-stateful uses alone (sync-history state, target-state check, system-state validation, dpkg selection state).

7. flatpak remotes: diff by URL too. Currently compared by name+scope only; a same-name remote with a changed URL is silently not propagated. Fix: a differing URL becomes a CHANGE that converges (remote-modify / delete+add).

8. apt collateral. Confirmed the code flags collateral ONLY for remove/downgrade of a package in the target's `apt-mark showmanual` set (auto-installed collateral proceeds silently; pure installs never flagged) — correct. Fix: also protect packages in the SOURCE's manual set (union target ∪ source manual sets) — the rare edge case. Do NOT implement the machine-specific-decision-list case (accepted limitation: skip-always packages are normally showmanual anyway).

9. Snippet registry overwrite. Source overwrites the target's `package-snippets.yaml` wholesale (confirmed). Acceptable, but if the overwrite is NOT purely additive (target holds entries absent-from or differing-in the source that would be lost/changed), show the diff and require explicit user confirmation. Decline → ABORT the run (user consolidates manually and re-runs). Non-interactive + non-additive → abort. Purely additive → proceed silently.

10. Unresolved must be unrepresentable. Interactive Ctrl-C / EOF at the review = the user wants to abort → ABORT THE ENTIRE SYNC (not mark items skip-once). Empty snippet capture is NOT accepted — force a real snippet or an explicit skip-once/skip-always; never fall through to "unresolved". Non-interactive run: every undecided item = skip-once (keep). Net: remove the review.py fall-through that manufactures an unresolved SKIP_ONCE, and make Ctrl-C abort rather than skip.

11. Snippets run unprivileged. Already correct: replayed as `bash -c '<body>'`, no outer sudo, sudo is the author's responsibility. Documentation only.

12. Doc rewrites (package-sync.md): job-ordering is an enforced ConfigError (not convention) — state as a MUST with the general "defaults-then-user-data" rationale; drop the illogical "decisive for flatpak" example (also in default-config.yaml). Rewrite "Batched review" to lead with what the review IS, user-facing. Reword apt-collateral (the user never "names" anything). Make "that machine" unambiguous in the machine-specific section. Correct the "unresolved only on non-interactive runs" claim.

13. README: update Status; move "Key Design Principles" to dev docs; GitHub rate-limit 403 is correct (primary limit), optionally note 429; refresh the stale Documentation list; fix the "don't have no configurable options" double negative.

## Work packages

Code (wave 1, disjoint file sets, parallel):
- WP-A apt_sync.py — decisions 1, 8.
- WP-B snap (snap_sync.py, folder_sync.py, orchestrator.py, home.filter) — decisions 3, 4.
- WP-C flatpak_sync.py — decision 7.
- WP-D manual_installs (manual_installs_sync.py, packages/review.py, packages/state.py) — decisions 9, 10.

Docs (wave 2, after code lands so they match reality):
- WP-E package-sync.md full rewrite — decisions 5, 6, 11, 12, and reflect 1-3, 7-10.
- WP-F README.md — decision 13.
- WP-G default-config.yaml + apt_sync/snap docstrings — decision 6 sweep + drop flatpak-ordering example.
- WP-H ADR-020 (D-04/D-06/D-29 + apt-get-update, flatpak-URL, collateral, snippet-overwrite, unresolved) and this .planning record.

Review (wave 3):
- Correctness + doc/code consistency + full `uv run pytest`, `ruff`, `basedpyright`. Then file the issues.

GitHub issues to file:
- Issue A (merged 1+2): manual_installs update/upgrade support AND removal — needs a target-side manifest of what pc-switcher installed; snippets need update/upgrade semantics since no package manager reconciles .deb/manual installs on both ends. Architect together.
- Issue B (#3): replicate hold/block/config intent across managers — `apt-mark hold`, `snap refresh --hold`, `flatpak mask` should travel source→target as review Items (per-item: overwrite src>tgt / skip-once / skip-always). Changes are rare; no fancy UI.
