---
phase: 2
slug: package-management-sync
# status lifecycle: draft (seeded by plan-phase) → validated (set by validate-phase §6)
# audit-milestone §5.5 distinguishes NOT-VALIDATED (draft) from PARTIAL (validated + nyquist_compliant: false) (#2117)
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-22
---

# Phase 2 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Seeded from `02-RESEARCH.md` § Validation Architecture at plan time. The Per-Task Verification Map is filled once PLAN.md task IDs exist.

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.1.1 + pytest-asyncio 1.4.0 (`asyncio_mode=auto`), pytest-randomly |
| **Config file** | `pyproject.toml` (`[tool.pytest]`) |
| **Quick run command** | `uv run pytest tests/unit/jobs/ -x` |
| **Full suite command** | `uv run pytest` (unit) + `tests/run-integration-tests.sh` (VM-isolated, btrfs-reset) |
| **Estimated runtime** | Not measured at plan time — measure and record during Wave 0 |

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/unit/jobs/ -x`
- **After every plan wave:** Run `uv run pytest` (full unit suite) + the relevant `tests/run-integration-tests.sh` targets
- **Before `/gsd-verify-work`:** Full gate green — `uv run ruff check . && uv run ruff format . && uv run basedpyright && uv run pytest` plus the integration suite
- **Max feedback latency:** Not measured at plan time — record the quick-run wall time during Wave 0 and set the ceiling from it

## Per-Task Verification Map

Task IDs do not exist until PLAN.md files are written. The requirement→test mapping below is lifted from `02-RESEARCH.md` § Validation Architecture and is the source the per-task rows must be derived from.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | TBD | TBD | REQ-sync-scope-packages | — | apt manifest capture returns exactly the `apt-mark showmanual` set with correct versions | unit | `uv run pytest tests/unit/jobs/test_apt_sync.py -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | REQ-sync-scope-packages | — | snap manifest capture parses `snap list --all` by header, not fixed offsets | unit | `uv run pytest tests/unit/jobs/test_snap_sync.py -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | REQ-sync-scope-packages | — | flatpak manifest capture separates user/system scope correctly | unit | `uv run pytest tests/unit/jobs/test_flatpak_sync.py -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | REQ-sync-scope-packages | — | version comparison uses `dpkg --compare-versions` and correctly ranks epoch versions | unit | `uv run pytest tests/unit/jobs/test_package_sync_core.py -k version -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | REQ-sync-scope-packages | — | a machine-local decision-file entry (skip-always) makes an item inert in both source and target roles (D-08) | unit | `uv run pytest tests/unit/jobs/test_package_sync_core.py -k decision_file -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | REQ-conflict-detection-no-resolution | — | held (`apt-mark hold`) and pinned (`preferences.d`) packages surface as distinguishable diff facts | unit | `uv run pytest tests/unit/jobs/test_package_sync_core.py -k held_or_pinned -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | REQ-conflict-detection-no-resolution | — | non-interactive run skips all items, records nothing, reports everything (D-26) | integration | `tests/run-integration-tests.sh tests/integration/jobs/test_package_sync.py::test_non_interactive_skip_all` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | REQ-conflict-detection-no-resolution | — | a failing item does not stop the job; job result is failure, other items still processed (D-27) | integration | `tests/run-integration-tests.sh tests/integration/jobs/test_package_sync.py::test_continue_on_item_failure` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

## Wave 0 Requirements

- [ ] `tests/unit/jobs/test_package_sync_core.py` — shared Item model, three-way decision, decision-file I/O, version comparison
- [ ] `tests/unit/jobs/test_apt_sync.py` — apt manifest capture, diff, converge (mocked executor)
- [ ] `tests/unit/jobs/test_snap_sync.py` — snap manifest capture (header-based column parsing), revision converge
- [ ] `tests/unit/jobs/test_flatpak_sync.py` — flatpak manifest capture (user/system scope), remote provisioning
- [ ] `tests/integration/jobs/test_package_sync.py` — VM-isolated: real apt/snap/flatpak convergence against the pc1/pc2 test VMs, non-interactive skip-all, continue-on-failure
- [ ] Framework install: none needed — pytest and pytest-asyncio are already present. `questionary` requires no test-framework addition; it is exercised via a mocked `.ask()` in unit tests, never driven interactively in CI

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Interactive batched review screen (D-24) — the human reads the per-item diff and chooses apply/skip/skip-always | REQ-conflict-detection-no-resolution | The review flow is a TUI checkbox interaction; unit tests mock `.ask()` and therefore never exercise the real rendering, keybindings, or selection ergonomics | Run a real sync between the pc1/pc2 test VMs with packages diverged in both directions; confirm every diff class (missing, extra, version mismatch, held, pinned) renders distinguishably and that each of apply / skip / skip-always produces the recorded outcome |

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency ceiling set from a measured Wave 0 quick-run time
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
