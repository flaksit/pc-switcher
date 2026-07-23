---
phase: 2
slug: package-management-sync
# status lifecycle: draft (seeded by plan-phase) → validated (set by validate-phase §6)
# audit-milestone §5.5 distinguishes NOT-VALIDATED (draft) from PARTIAL (validated + nyquist_compliant: false) (#2117)
status: draft
nyquist_compliant: false
wave_0_complete: true
created: 2026-07-22
---

# Phase 2 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Seeded from `02-RESEARCH.md` § Validation Architecture at plan time. Filled with real task IDs by plan 02-11 once every plan's task IDs and `<verify>` commands existed.

## Test Infrastructure

| Property | Value |
| -------- | ----- |
| **Framework** | pytest 9.1.1 + pytest-asyncio 1.4.0 (`asyncio_mode=auto`), pytest-randomly |
| **Config file** | `pyproject.toml` (`[tool.pytest]`) |
| **Quick run command** | `uv run pytest tests/unit/jobs/ -x` |
| **Full suite command** | `uv run pytest` (unit) + `tests/run-integration-tests.sh` (VM-isolated, btrfs-reset) |
| **Estimated runtime** | Measured during plan 02-11: `uv run pytest tests/unit/jobs/ -x` — 407 tests in 3.07s (pytest-reported; ~3.7s wall including `uv` startup) |

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/unit/jobs/ -x`
- **After every plan wave:** Run `uv run pytest` (full unit suite) + the relevant `tests/run-integration-tests.sh` targets
- **Before `/gsd-verify-work`:** Full gate green — `uv run ruff check . && uv run ruff format . && uv run basedpyright && uv run pytest` plus the integration suite
- **Max feedback latency:** 10s ceiling — set from the measured `tests/unit/jobs/ -x` quick-run wall time above (~3.7s), rounded up with margin for slower CI runners

## Per-Task Verification Map

One row per requirement-bearing task across every plan in the phase (02-02 through 02-11, plus 02-13, which sits at wave 3 despite its plan number — it was executed as the tracer's VM-level proof one wave after 02-03). `checkpoint:human-verify` tasks with no `<automated>` verify are not rows here; they are listed under Manual-Only Verifications below. 02-01 (ADR authorship, no test-bearing task) and 02-12 (documentation, not yet executed) are out of scope for this map.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
| ------- | ---- | ---- | ----------- | ---------- | --------------- | --------- | ----------------- | ----------- | ------ |
| 02-02.2 | 02-02 | 1 | REQ-conflict-detection-no-resolution | T-02-02 | batched checkbox review renders every group; untrusted item text never reaches `Panel` unwrapped (markup-injection guard) | unit | `uv run pytest tests/unit/jobs/test_package_review.py -x && uv run ruff check src/pcswitcher/jobs/package_review.py tests/unit/jobs/test_package_review.py && uv run basedpyright src/pcswitcher/jobs/package_review.py` | ✅ | ✅ green |
| 02-03.1 | 02-03 | 2 | REQ-sync-scope-packages | T-02-01 | tracer: apt manifest capture → diff → review → `apt-get install` on the target, one path end to end | unit | `uv run pytest tests/unit/jobs/test_apt_sync.py -x && uv run pytest && uv run ruff check . && uv run basedpyright` | ✅ | ✅ green |
| 02-03.2 | 02-03 | 2 | REQ-conflict-detection-no-resolution | T-02-33 | each package job renders its OWN batched review inside its own `execute()` (plan → review → apply); no cross-manager coordinator (corrected D-24, plan 02-15 removed `PackagePhaseCoordinator`) | unit | `uv run pytest tests/unit/jobs/test_package_sync_core.py -x && uv run pytest && uv run ruff check . && uv run basedpyright` | ✅ | ✅ green |
| 02-04.1 | 02-04 | 4 | REQ-conflict-detection-no-resolution | T-02-11 | machine-local decision store: atomic temp-then-`mv` write, malformed file degrades to "no permanent decisions" | unit | `uv run pytest tests/unit/jobs/test_package_state.py -x && uv run ruff check src/pcswitcher/jobs/package_state.py tests/unit/jobs/test_package_state.py && uv run basedpyright src/pcswitcher/jobs/package_state.py` | ✅ | ✅ green |
| 02-04.2 | 02-04 | 4 | REQ-conflict-detection-no-resolution | T-02-08 | skip-always wired into `filter_inert`; decision files get a global-first, non-overridable folder_sync exclusion (D-09) | unit | `uv run pytest tests/unit/jobs/test_folder_sync.py tests/unit/jobs/test_package_state.py -x && uv run pytest && uv run ruff check . && uv run basedpyright` | ✅ | ✅ green |
| 02-05.1 | 02-05 | 3 | REQ-sync-scope-packages | T-02-13 | `dpkg --compare-versions` (never hand-rolled) decides version ordering; full item-shape/`DiffClass` taxonomy defined | unit | `uv run pytest tests/unit/jobs/test_package_items.py -x && uv run ruff check src/pcswitcher/jobs/package_items.py tests/unit/jobs/test_package_items.py && uv run basedpyright src/pcswitcher/jobs/package_items.py` | ✅ | ✅ green |
| 02-05.2 | 02-05 | 3 | REQ-conflict-detection-no-resolution | T-02-12 | removals occupy their own unchecked-by-default review group; apt transaction simulation refuses unapproved collateral removals | unit | `uv run pytest tests/unit/jobs/ -x && uv run pytest && uv run ruff check . && uv run basedpyright` | ✅ | ✅ green |
| 02-06.1 | 02-06 | 4 | REQ-sync-scope-packages | T-02-03 | apt source/key/pin/config item classes captured and diffed; keys travel byte-for-byte, never re-derived | unit | `uv run pytest tests/unit/jobs/test_apt_sync.py -x && uv run ruff check . && uv run basedpyright` | ✅ | ✅ green |
| 02-06.2 | 02-06 | 4 | REQ-sync-scope-packages | T-02-34 | repo-group convergence is transactional: backup-before-write, restore-and-reprobe on a failed `apt-get update` | unit | `uv run pytest tests/unit/jobs/test_apt_sync.py -x && uv run pytest && uv run ruff check . && uv run basedpyright` | ✅ | ✅ green |
| 02-07.1 | 02-07 | 5 | REQ-sync-scope-packages | T-02-01 | unreproducible-item detection (apt-no-candidate, unowned `/usr/local`+`/opt`) and the install-snippet registry, both `shlex.quote()`d | unit | `uv run pytest tests/unit/jobs/test_package_state.py tests/unit/cli/test_config_sync.py -x && uv run pytest && uv run ruff check . && uv run basedpyright` | ✅ | ✅ green |
| 02-07.2 | 02-07 | 5 | REQ-conflict-detection-no-resolution | T-02-18 | mandatory registration: an unreproducible item always ends up snippet-authored or skip-always'd inside the review, never silently unresolved | unit | `uv run pytest tests/unit/jobs/test_package_review.py -x && uv run pytest && uv run ruff check . && uv run basedpyright` | ✅ | ✅ green |
| 02-08.1 | 02-08 | 5 | REQ-sync-scope-packages | T-02-05 | snap revision/channel convergence via `--revision` only; a captured command list asserts `refresh --hold` is never issued | unit | `uv run pytest tests/unit/jobs/test_snap_sync.py -x && uv run pytest && uv run ruff check . && uv run basedpyright` | ✅ | ✅ green |
| 02-09.1 | 02-09 | 5 | REQ-sync-scope-packages | T-02-24 | flatpak ref/remote convergence; a ref whose origin remote is absent from the target scope is skipped with a named failure, not attempted | unit | `uv run pytest tests/unit/jobs/test_flatpak_sync.py -x && uv run pytest && uv run ruff check . && uv run basedpyright` | ✅ | ✅ green |
| 02-10.1 | 02-10 | 6 | REQ-sync-scope-packages | T-02-27 | `snap_sync`/`flatpak_sync` registered; package jobs run before `folder_sync` in the fixed discovery order (D-17) | unit | `uv run pytest tests/unit/orchestrator -x && uv run pytest` | ✅ | ✅ green |
| 02-10.2 | 02-10 | 6 | REQ-sync-scope-packages | T-02-25 | package jobs export owned paths to `folder_sync`; exclusions are gated on the owning job's own enable flag | unit | `uv run pytest tests/unit/jobs/test_folder_sync.py -x && uv run pytest && uv run ruff check . && uv run basedpyright` | ✅ | ✅ green |
| 02-13.1 | 02-13 | 3 | REQ-sync-scope-packages | T-02-28 | VM-level tracer proof: a package removed from pc2 is reinstalled by a real sync, asserted against pc2's own `apt-mark showmanual`; `--dry-run` companion changes nothing | integration | `tests/run-integration-tests.sh tests/integration/jobs/test_package_sync.py` | ✅ | ⬜ pending (CI — see Pending CI Verification) |
| 02-11.1 | 02-11 | 7 | REQ-conflict-detection-no-resolution | T-02-28 / T-02-29 | six whole-run VM-level contracts: non-interactive skip-all (D-26), continue-on-failure (D-27), snap hold-free convergence (D-06), flatpak scoped remote-before-ref (D-06/D-14), skip-always inertness in both roles (D-08), per-manager review-before-own-mutation (corrected D-24 — each manager reviews then converges its OWN diff, no cross-manager coordinator) | integration | `tests/run-integration-tests.sh tests/integration/jobs/test_package_sync.py` | ✅ | ⬜ pending (CI — see Pending CI Verification) |
| 02-21.1 | 02-21 | 6 | REQ-sync-scope-packages | T-02-54 | VM-level apt repository state (ledger #2): a target missing one vendor repo shows the source's repository file and its signing key as two SEPARATE review entries and reports the intended `apt-get update` metadata refresh, asserted against the dry-run review output (ADR-014) | integration | `tests/run-integration-tests.sh tests/integration/jobs/test_package_sync.py` | ✅ | ⬜ pending (CI — see Pending CI Verification) |
| 02-21.2 | 02-21 | 6 | REQ-sync-scope-packages | T-02-55 | `manual_installs_sync` pushes `package-snippets.yaml` to the target with its OWN `send_file` (D-23) and replays the pushed snippet, asserted against pc2's own registry file and the snippet's filesystem marker — never pc-switcher log text | integration | `tests/run-integration-tests.sh tests/integration/jobs/test_package_sync.py` | ✅ | ⬜ pending (CI — see Pending CI Verification) |
| 02-11.2 | 02-11 | 7 | REQ-sync-scope-packages / REQ-conflict-detection-no-resolution | — | this validation record itself: every task named, every automated command exists and passes, `nyquist_compliant` states the truth | unit | `grep -c 'TBD' .planning/phases/02-package-management-sync/02-VALIDATION.md; uv run pytest tests/unit/jobs/ -x` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

**Pending CI Verification:** rows `02-13.1`, `02-11.1`, `02-21.1` and `02-21.2` require the real pc1/pc2 test VMs (`HCLOUD_TOKEN`, `PC_SWITCHER_TEST_PC1_HOST`, `PC_SWITCHER_TEST_PC2_HOST`), unavailable in this execution environment. Per this project's established pattern (ADR-008), they run in GitHub Actions CI on the next non-draft PR targeting `main`. The delta rework (plan 02-21) retargets the suite to the four-job set and the per-manager review (no coordinator) and adds two VM tests: the apt-repository-state dry-run (closing broken-window ledger #2) and the `manual_installs_sync` snippet push+replay. Verified locally instead: `uv run pytest tests/integration/jobs/test_package_sync.py --collect-only -q -m integration` lists all 10 tests (2 apt tracer + 1 apt-repository-state + 6 whole-run contracts + 1 manual-installs); `uv run ruff check` and `uv run basedpyright` on the integration test file are clean; the module collects with every test correctly deselected without VM access. Ledger entry #2 is marked fixed only after CI runs this suite green on PR #206 (draft=false).

## Wave 0 Requirements

- [x] `tests/unit/jobs/test_package_sync_core.py` — shared Item model, three-way decision, decision-file I/O, version comparison
- [x] `tests/unit/jobs/test_apt_sync.py` — apt manifest capture, diff, converge (mocked executor)
- [x] `tests/unit/jobs/test_snap_sync.py` — snap manifest capture (header-based column parsing), revision converge
- [x] `tests/unit/jobs/test_flatpak_sync.py` — flatpak manifest capture (user/system scope), remote provisioning
- [x] `tests/integration/jobs/test_package_sync.py` — VM-isolated: real apt/snap/flatpak convergence against the pc1/pc2 test VMs, non-interactive skip-all, continue-on-failure
- [x] Framework install: none needed — pytest and pytest-asyncio are already present. `questionary` requires no test-framework addition; it is exercised via a mocked `.ask()` in unit tests, never driven interactively in CI

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
| -------- | ----------- | ---------- | ----------------- |
| Package legitimacy of `questionary` before install (02-02 task 1, `gate="blocking-human"`) | REQ-conflict-detection-no-resolution | A one-time human judgment call on a third-party dependency (PyPI/GitHub history, star count, release cadence) that no automated test can substitute for | Historical — already performed during 02-02's execution (see 02-02-SUMMARY.md's `key-decisions`: cleared via explicit user approval plus live PyPI/GitHub verification). No further action; recorded here for audit completeness |
| Checkbox prompt composes with the paused Live display (02-02 task 3) | REQ-conflict-detection-no-resolution | Confirms the real terminal pause/resume/redraw sequence around a blocking `questionary` prompt; a mocked `.ask()` never exercises the actual Live-region handoff | Run a real sync between the pc1/pc2 test VMs with a package diverged; confirm the Live display visibly pauses before the checkbox prompt, the prompt renders cleanly with no display corruption, and the Live display resumes afterward |
| Interactive batched review screen (D-24) — the human reads the per-item diff and chooses apply/skip/skip-always | REQ-conflict-detection-no-resolution | The review flow is a TUI checkbox interaction; unit tests mock `.ask()` and therefore never exercise the real rendering, keybindings, or selection ergonomics | Run a real sync between the pc1/pc2 test VMs with packages diverged in both directions; confirm every diff class (missing, extra, version mismatch, held, pinned) renders distinguishably and that each of apply / skip / skip-always produces the recorded outcome |
| On-the-fly install-snippet capture during the review (02-07 task 2's three-way unreproducible-item prompt: add snippet / skip-always / skip-once) | REQ-conflict-detection-no-resolution | Unit tests stub both `questionary.select` and `questionary.text` prompts (`package_review.py`'s `_review_unreproducible_group`); the real multi-line capture ergonomics — the authoring note, Esc-then-Enter to finish, what a cancelled/empty capture looks like on screen — are never exercised | Run a real sync where the source has an apt-no-candidate package or an unowned `/usr/local`/`/opt` install; confirm the three-way prompt appears, the multi-line snippet editor accepts a worked `dpkg -i`/`apt-get install -f` shape, and the authored snippet appears in `~/.config/pc-switcher/package-snippets.yaml` on the next read |

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency ceiling set from a measured Wave 0 quick-run time
- [ ] `nyquist_compliant: true` set in frontmatter — left `false`: rows 02-13.1 and 02-11.1 have a passing automated command that has not yet been RUN against real VMs (pending CI, not pending existence — the command exists, is correct, and is deselected by default per this project's established VM-less-dev-environment pattern). `nyquist_compliant` requires a passing command, not merely an existing one; `/gsd-validate-phase` or the next CI run is what flips this to `true`.

**Approval:** pending
