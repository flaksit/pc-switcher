# Codebase Concerns

**Analysis Date:** 2026-07-23

Baseline is healthy: `uv run ruff check .` passes, `uv run basedpyright` reports 0 errors/0 warnings, 1057 tests collected. The concerns below are structural, operational and coverage risks, not lint debt.

## Tech Debt

### Phase 02 rework is mid-flight

- Issue: Phase 02 (Package Management Sync) shipped 13 plans, then a user review corrected 5 decisions. A delta replan + re-execution + code review (`.planning/HANDOFF.json` tasks 7-9) is outstanding. Current package-sync code implements the *pre-correction* decisions in places.
- Files: `src/pcswitcher/jobs/apt_sync.py`, `src/pcswitcher/jobs/snap_sync.py`, `src/pcswitcher/jobs/flatpak_sync.py`, `src/pcswitcher/jobs/package_sync_core.py`, `.planning/phases/02-package-management-sync/02-CONTEXT.md`
- Impact: Any new work touching package sync risks building on code slated for rework.
- Fix approach: Complete HANDOFF tasks 7-9 before starting Phase 03.

### Shared package-sync core is not actually shared

- Issue: D-16 mandated one extracted core, but `SnapSyncJob`, `FlatpakSyncJob` and `AptSyncJob` each override `plan()` instead of inheriting `PackageSyncJob.plan()`. The base `diff_items()` is apt-package-shaped (hardcoded `ItemClass.APT_PACKAGE`, reads `AptPackageItem.version`) and crashes or mislabels on `SnapItem`.
- Files: `src/pcswitcher/jobs/package_sync_core.py:136`, `src/pcswitcher/jobs/apt_sync.py:724`, `src/pcswitcher/jobs/snap_sync.py:219`, `src/pcswitcher/jobs/flatpak_sync.py:320`
- Impact: Three near-parallel `plan()` implementations must be kept in sync by hand; the base class is a partial abstraction that is unsafe to call generically.
- Fix approach: Make `diff_items()` item-class agnostic (dispatch on `ItemClass`), then collapse the three overrides.

### `apt_sync.py` carries repository/key/pin/config convergence alone

- Issue: 1493 lines, ~40 module-level and method functions. All apt repo/key/pin/config capture, diff and dependency-ordered convergence lives here rather than in the shared core, and repo-group convergence is triggered *eagerly by the first repo-group diff* `converge()` sees, with per-item outcomes cached so the base per-diff loop still drives it.
- Files: `src/pcswitcher/jobs/apt_sync.py`
- Impact: Convergence ordering is implicit in call order rather than explicit; the file is the largest in the codebase and the hardest to reason about.
- Fix approach: Split repo/key/pin/config into its own module with an explicit ordered convergence pass.

### Orchestrator size and unfinished session snapshot

- Issue: `src/pcswitcher/orchestrator.py` is 1304 lines across 27 methods, and carries `# TODO: Add config snapshot` at line 318 — sync history records an empty `config={}`.
- Impact: Post-hoc forensics cannot tell what config a past run used.
- Fix approach: Serialize the resolved config into the history record.

### Hardcoded btrfs subvolume→mountpoint mapping

- Issue: `_subvol_to_mount_point()` assumes the Ubuntu `@`/`@home` convention rather than reading the live mount table. Flagged in-code as a TODO.
- Files: `src/pcswitcher/jobs/btrfs.py:39`
- Impact: Non-standard btrfs layouts get wrong snapshot targets. Bounded today by the Ubuntu 24.04 constraint (issue #26).
- Fix approach: Derive from `findmnt`/`/proc/self/mountinfo`.

### Stray test package `tests/unit_jobs/`

- Issue: A second test package `tests/unit_jobs/` exists alongside `tests/unit/jobs/`, containing only `test_disk_space_monitor.py` (459 lines).
- Files: `tests/unit_jobs/test_disk_space_monitor.py`
- Impact: Test discovery convention is ambiguous; new job tests may land in the wrong tree.
- Fix approach: Move into `tests/unit/jobs/` and delete the directory.

### Hard dependency on GitHub API in core

- Issue: `pygithub` is a runtime dependency of the tool itself for version checks and target installs (open issues #82, #79).
- Files: `src/pcswitcher/version.py:25`, `src/pcswitcher/jobs/install_on_target.py`
- Impact: Unauthenticated calls are capped at 60 req/hr, so `InstallOnTargetJob` can fail on rate limits (#79); a network-isolated LAN sync depends on github.com reachability.
- Fix approach: Per #82, isolate the release-query behind an interface and make version-check failures non-fatal by design rather than by `except Exception`.

### Duplicate command construction for source vs target

- Issue: Open issue #126 — source and target execution paths build separate bash command strings instead of both invoking the tool's internal CLI.
- Impact: Every job maintains two shapes of the same operation; behavioral drift between roles is easy to introduce.

## Known Bugs

### Sync can exit 0 with failed package items (recorded, fixed in ledger, verify)

- Symptoms: `SessionStatus`/CLI exit code derive solely from whether an exception propagated, never from `job_results` content, so a run where a package manager's items failed returned 0.
- Files: `src/pcswitcher/orchestrator.py`, `src/pcswitcher/cli.py`
- Status: Broken-windows ledger entry 1, marked `fixed` 2026-07-23. Worth a regression test asserting non-zero exit on item-level failure.

### Integration-test lock not released on cancelled CI run

- Symptoms: Cancelling the Integration Test workflow leaves the remote flock held; subsequent runs cannot acquire it.
- Files: `src/pcswitcher/lock.py`, `tests/run-integration-tests.sh`
- Status: Open issue #78 (P1).
- Workaround: Manually clear the lock on the VM.

### Integration fixtures re-establish state unnecessarily

- Symptoms: `pc2_executor_without_pcswitcher_tool` / `pc2_executor_with_old_pcswitcher_tool` rebuild VM state every run.
- Files: `tests/integration/conftest.py`
- Status: Open issue #68. Contributes to integration-suite runtime.

## Security Considerations

### Broad `pkill -f` on the remote during interrupt cleanup

- Risk: `kill_all_remote_processes()` runs `pkill -f 'pc-switcher'` with the pattern interpolated into a shell string. The default pattern matches *any* remote process whose full command line contains `pc-switcher` — including an unrelated shell, editor or log tail — and the parameter is not `shlex.quote`d.
- Files: `src/pcswitcher/connection.py:150`
- Current mitigation: The pattern is never caller-supplied today.
- Recommendations: Quote the pattern and narrow the match (PID file, or `pkill -f "^/.*/pc-switcher "`).

### Inconsistent shell quoting of config-derived paths

- Risk: `shlex.quote` is used in ~70 places, but several `sudo`/`find`/`df` commands interpolate config- or discovery-derived paths raw.
- Files: `src/pcswitcher/jobs/folder_sync.py:592` (`sudo find {path} -name .pcswitcher-filter …`), `src/pcswitcher/btrfs_snapshots.py:80,135,179,266,278`, `src/pcswitcher/disk.py:115`, `src/pcswitcher/jobs/btrfs.py:151,186`, `src/pcswitcher/jobs/disk_space_monitor.py:114`
- Current mitigation: Values originate from the user's own YAML config, not from a remote or untrusted source.
- Recommendations: Quote uniformly; a path with a space silently misbehaves today even without malice.

### Passwordless-sudo surface

- Risk: Jobs require `NOPASSWD` grants for `rsync`, apt/snap/flatpak binaries and `/etc/apt` reads on both machines; rsync runs as root on both ends (ADR-013).
- Files: `src/pcswitcher/sudoers.py`, `src/pcswitcher/jobs/folder_sync.py`, `src/pcswitcher/jobs/apt_sync.py`
- Current mitigation: Grants are scoped to explicit absolute binary paths in `/etc/sudoers.d/pc-switcher`; root SSH login stays forbidden; remediation text mandates `visudo -f`.
- Recommendations: Document the residual risk that `NOPASSWD rsync` as root is effectively full filesystem write access.

### Destructive convergence is the default "apply" branch

- Risk: Per D-07, "apply" means remove/delete/disable whenever an item is present on target and absent on source. `folder_sync` uses `rsync --delete`; `apt_sync` runs `remove -y`.
- Files: `src/pcswitcher/jobs/apt_sync.py:1109`, `src/pcswitcher/jobs/folder_sync.py:652`, `src/pcswitcher/jobs/snap_sync.py:362`
- Current mitigation: Pre-sync btrfs snapshots, per-item interactive review, `apt-get -s` simulation before removal, `snap remove` never `--purge`, hardcoded runtime excludes (ADR-016/017) including `.ssh/authorized_keys`.
- Recommendations: Keep the review UI naming concrete actions ("remove brscan3") rather than "apply"; regression-test the non-overridable exclude list.

## Performance Bottlenecks

### Integration suite runtime

- Problem: The VM-backed suite is slow; one test alone takes ~90s.
- Files: `tests/integration/test_end_to_end_sync.py`, `tests/run-integration-tests.sh`
- Cause: Full VM reset per run, unconditional fixture state rebuilds, generous timeouts.
- Improvement path: Open issues #150 (that specific timeout), #76 (skip unnecessary VM reset), #69 (split VM vs non-VM integration tests), #65, #64.

### Per-item SSH round trips

- Problem: Version comparison shells out to `dpkg --compare-versions` twice per item pair, and file digests are read one file at a time.
- Files: `src/pcswitcher/jobs/package_items.py:169,173`, `src/pcswitcher/jobs/apt_sync.py:264,275`
- Cause: One remote command per comparison, over a semaphore capped at 10 concurrent SSH sessions.
- Improvement path: Batch comparisons into a single remote script; the digest capture already uses one `find … sha256sum` pass and is the right model.

### `login_shell=True` overhead

- Problem: Documented 10-50ms per command penalty.
- Files: `src/pcswitcher/executor.py:307,340,393`, `src/pcswitcher/jobs/install_on_target.py:48,124`
- Current state: Already treated as opt-in with an explicit warning — monitor rather than fix.

## Fragile Areas

### TUI + Live display + blocking prompts

- Files: `src/pcswitcher/ui.py`, `src/pcswitcher/logger.py:321`, `src/pcswitcher/jobs/package_review.py:227`
- Why fragile: Anything writing to stderr while the persistent Rich `Live` display is active desyncs its cursor bookkeeping and floods the display. `package_review.py` runs a blocking `questionary` checkbox off the event loop, interleaved with `Live`. `rich` 15 paints Live's first frame past one 10Hz tick, so timing-based TUI tests are flaky. Untrusted log content passed to `Panel(str)` is parsed as markup and raises `MarkupError`.
- Safe modification: Wrap untrusted content in `Text`; poll for a render marker in tests rather than sleeping; never write to stderr under `Live`.
- Test coverage: `tests/unit/ui/test_terminal_ui.py` (600 lines) — no automated coverage of the questionary/Live interleave; that remains a pending human UAT item.

### `folder_sync` filter rules

- Files: `src/pcswitcher/jobs/folder_sync.py:87-93`, `src/pcswitcher/default-config.yaml`, shipped `home.filter`
- Why fragile: rsync filter files do not support trailing inline comments — a `# comment` on a pattern line becomes part of the pattern and silently disables the rule. Omitting `.ssh/authorized_keys` from the excludes locks the sync out of its own target. Excluding the pcswitcher venv while `--delete`-mirroring `uv/python/` deleted the in-use interpreter (#185).
- Safe modification: Comments on their own line only; treat `_RUNTIME_EXCLUDE_RELPATHS` as append-only without review.

### Self-upgrade / re-exec

- Files: `src/pcswitcher/install.py`, `src/pcswitcher/version.py`, `src/pcswitcher/jobs/install_on_target.py`
- Why fragile: An in-place self-upgrade must re-exec or exit once it has touched disk; continuing in the old process mixes new and old code via lazy imports.

### Remote lock acquisition uses a fixed sleep

- Files: `src/pcswitcher/lock.py:150-162`
- Why fragile: Acquisition is decided by `asyncio.sleep(0.5)` then `process.poll()`. On a slow link, flock may not yet have failed, and the lock is reported as acquired. The surrounding `except Exception: return None` maps every error to "could not lock".
- Safe modification: Have the remote command emit a token on successful acquisition and read for it, rather than timing it.

## Scaling Limits

### Concurrent SSH sessions

- Current capacity: `max_sessions=10` semaphore in `Connection`.
- Limit: Per-item remote commands (package diffing) serialize behind it; the effective ceiling is sshd's `MaxSessions` on the target.
- Scaling path: Batch remote work into fewer, larger commands rather than raising the semaphore.

### Snapshot disk consumption

- Current capacity: Retention is `keep_recent` sessions plus an optional `max_age_days`.
- Files: `src/pcswitcher/btrfs_snapshots.py:199-278`
- Limit: Cleanup is a separate CLI command (`pc-switcher cleanup-snapshots`); a user who never runs it accumulates snapshots until the filesystem fills.
- Scaling path: Run retention automatically post-sync; `DiskSpaceMonitorJob` already exists to observe the pressure.

### Platform constraints

- Ubuntu 24.04 only (#26) and btrfs required (#23) are open ideas, not resolved. ADR-019 assumes a homogeneous fleet.

## Dependencies at Risk

### `pygithub`

- Risk: Core runtime dependency for a non-core concern; unauthenticated rate limit of 60 req/hr.
- Impact: `InstallOnTargetJob` failures (#79).
- Migration plan: #82 — remove the dependency from core.

### `questionary`

- Risk: Newly introduced; the legitimacy gate was cleared by explicit user approval plus live PyPI/GitHub verification. Interacts with the Rich `Live` display in a way only human UAT covers today.
- Files: `src/pcswitcher/jobs/package_review.py:48`
- Migration plan: None needed; add automated coverage of the Live interleave.

### `requires-python = ">=3.14"`

- Risk: Very recent floor narrows the pool of usable third-party wheels and CI images.
- Impact: Contributor onboarding friction; already reconciled with `uv`.

## Missing Critical Features

- **Rollback command** (#31): pre/post btrfs snapshots exist at `/mnt/btrfs-snapshots/pc-switcher`, but there is no `pc-switcher rollback` to use them. Recovery is manual `btrfs` work.
- **Non-interactive flags** (#48): every confirmation is interactive-only. Non-TTY runs fall back to "skip all, once" (D-26), so an unattended sync converges nothing that needed a decision.
- **Job DAG / dependencies** (#28): job ordering is a hardcoded sequence in the orchestrator.
- **Warning visibility during a run** (#181) and a real log viewer (#182).
- **System configuration sync** (#119): Feature 7 is not started.
- **SKIP_ALWAYS from the interactive UI**: `PackageSyncJob._record_permanent_skips`/`filter_inert` implement D-08, but the checkbox UI has no path to produce a `SKIP_ALWAYS` outcome for a regular (non-unreproducible) item. Exercised today only via `PACKAGE_REVIEW_AUTOMATION_ENV`. Files: `src/pcswitcher/jobs/package_review.py`, `src/pcswitcher/jobs/package_sync_core.py`.

## Test Coverage Gaps

### No coverage measurement at all

- What's not tested: Unknown — `pytest-cov` is absent from `[dependency-groups] dev` and no coverage threshold is configured.
- Files: `pyproject.toml`
- Risk: Coverage claims are unverifiable; regressions in untouched branches go unnoticed.
- Priority: High. Open issue #156 ("Audit modular test coverage").

### Real integration coverage questioned

- What's not tested: Open issue #62 (P0, `status:working`) — "Are we missing real integration tests?" remains unresolved.
- Priority: High.

### Unrun VM verification for apt repo/key diffing

- What's not tested: Plan 02-06's VM-level check — dry-run against a target missing one vendor repo, key and source shown as separate review entries, intended `apt-get update` reported.
- Files: `.planning/phases/02-package-management-sync/02-06-PLAN.md`, `tests/integration/jobs/test_package_sync.py`
- Risk: The repo/key convergence path — the most destructive part of `apt_sync` — has no proven end-to-end run.
- Priority: High. Broken-windows ledger entry 2, still `open`; `open_count: 1` blocks `/gsd-ship`.

### Interrupt / cleanup semantics

- What's not tested: Real SIGINT handling in integration (#132); cleanup on both source and target when interrupted (#85, P1).
- Files: `tests/integration/test_interrupt_integration.py`, `src/pcswitcher/jobs/dummy_fail.py`, `src/pcswitcher/jobs/dummy_success.py`
- Risk: A mid-sync interrupt is the highest-consequence path (partial `--delete` mirror, held locks) and is proven only by proxy.
- Priority: High.

### Pending human UAT

- What's not tested: The questionary/Live TUI rendering check and the documentation walkthrough are open `human_actions_pending` in `.planning/HANDOFF.json` (tasks 10-11).
- Priority: Medium — blocks Phase 02 closure.
