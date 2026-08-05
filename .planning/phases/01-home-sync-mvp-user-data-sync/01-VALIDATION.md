---
phase: 1
slug: home-sync-mvp-user-data-sync
status: approved
nyquist_compliant: true
wave_0_complete: false
created: 2026-06-30
approved: 2026-06-30
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution. Every Phase-1 requirement is sampled by at least one automated test, and no run of three consecutive tasks lacks an automated verify (Nyquist sampling guarantee). Populated from 01-RESEARCH.md *Validation Architecture* and the inline `<verify>` blocks of plans 01-01 through 01-06.

## Test Infrastructure

| Property | Value |
| -------- | ----- |
| **Framework** | pytest + pytest-asyncio (asyncio_mode = "auto") — verified in codebase |
| **Config table** | `pyproject.toml` `[tool.pytest]` (pytest 9 canonical table; `testpaths = ["tests"]`, default `addopts` excludes integration via `-m "not integration"`) |
| **Quick run command** | `uv run pytest tests/unit/jobs/test_folder_sync.py tests/contract/ -x` |
| **Full suite command** | `uv run pytest tests/unit/ tests/contract/` |
| **Integration command** | `tests/run-integration-tests.sh tests/integration/test_folder_sync.py` (real VMs; not run by default suite) |
| **Estimated runtime** | Unit + contract subset: target < 30s. Not yet measured — populate after Wave 0 scaffolding exists. |

## Sampling Rate

- **After every task commit:** `uv run pytest tests/unit/jobs/test_folder_sync.py tests/contract/ -x`
- **After every plan wave:** `uv run pytest tests/unit/ tests/contract/`
- **Phase gate (before `/gsd-verify-work`):** full unit + contract suite green, then the integration round-trip (`tests/run-integration-tests.sh tests/integration/test_folder_sync.py`) green
- **Max feedback latency:** target < 30s for the per-commit unit subset (actual TBD after Wave 0 — not yet measured)

## Per-Task Verification Map

Task IDs follow `phase-plan-task`. Threat refs point at each plan's STRIDE Threat Register. "File Exists" describes the test/artifact the verify touches: `W0` = created during execution (Wave 0 scaffolding, absent today), `extend` = existing file extended, `doc` = documentation artifact authored by the task.

| Task ID | Plan | Wave | Requirement(s) | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
| ------- | ---- | ---- | -------------- | ---------- | --------------- | --------- | ----------------- | ----------- | ------ |
| 1-01-01 | 01 | 1 | REQ-sync-scope-user-data · REQ-manual-sync-workflow | T-01-01 / T-01-02 | Transport model mandates sudo scoped to the rsync binary; root SSH login forbidden | structural (doc) | `test -f docs/adr/adr-013-rsync-over-ssh-user-data-transport.md && grep -q "^Status: Accepted" … && grep -q "rsync-path" …` | doc | ⬜ pending |
| 1-01-02 | 01 | 1 | REQ-manual-sync-workflow | T-01-02 | Tool-wide dry-run contract recorded as an immutable ADR | structural (doc) | `test -f docs/adr/adr-014-unified-dry-run-contract.md && grep -q "ADR-013" docs/adr/_index.md && grep -q "ADR-014" docs/adr/_index.md` | doc | ⬜ pending |
| 1-02-01 | 02 | 1 | REQ-machine-specific-exclusions · REQ-sync-scope-user-data | T-02-02 | Schema constrains `path`/`excludes` to strings (injection guard); `folder_sync` registered top-level and under `sync_jobs` | unit (schema assert) | `uv run python -c "…assert 'folder_sync' in schema.properties and sync_jobs.folder_sync and required==['folders']"` | created here | ⬜ pending |
| 1-02-02 | 02 | 1 | REQ-sync-scope-user-data · REQ-machine-specific-exclusions | T-02-01 | `.ssh/id_*` and `.config/tailscale` shipped as default excludes; defaults are `/home` + `/root` | unit (config load) | `uv run python -c "…assert paths==['/home','/root'] and '.ssh/id_*' in excludes and '.config/tailscale' in excludes"` | created here | ⬜ pending |
| 1-03-01 | 03 | 1 | REQ-manual-sync-workflow (D-12 dry-run) | T-03-01 / T-03-03 | Dry-run skips the sync-history update; `allow_divergence` is opt-in (default `False`) | unit | `uv run pytest tests/unit/test_dry_run.py -q` + signature/field inspection | extend | ⬜ pending |
| 1-03-02 | 03 | 1 | REQ-manual-sync-workflow (D-08 divergence markers) | T-03-02 | Merge-preserving `target_generations` writes so role switches cannot erase divergence markers | unit | `uv run pytest tests/unit/test_sync_history.py -q` | extend | ⬜ pending |
| 1-04-01 | 04 | 2 | REQ-sync-scope-file-metadata · REQ-sync-scope-user-data | T-04-01 / T-04-03 | `validate()` fails fast if `sudo rsync` or the `acl` package is missing on either end; every config path `shlex.quote`d | unit + contract | `uv run pytest tests/unit/jobs/test_folder_sync.py tests/contract -q` + import check | W0 | ⬜ pending |
| 1-04-02 | 04 | 2 | REQ-manual-sync-workflow (D-06/D-07/D-08/D-18) | T-04-02 / T-04-02b | Divergence guard blocks the mirror when the target changed; first-sync and snapshot-bump cases handled; override logged at WARNING | unit | `uv run pytest tests/unit/jobs/test_folder_sync.py -q` | W0 | ⬜ pending |
| 1-05-01 | 05 | 3 | REQ-sync-scope-file-metadata · REQ-machine-specific-exclusions · REQ-sync-scope-user-data | T-05-01 / T-05-02 / T-05-04 | `-aAXHS --numeric-ids` metadata flags; secrets mapped to `--filter`; `--delete-excluded` never passed; all config values `shlex.quote`d | unit | `uv run pytest tests/unit/jobs/test_folder_sync.py -q -k "rsync_cmd or build"` | W0 | ⬜ pending |
| 1-05-02 | 05 | 3 | REQ-terminal-ux · REQ-manual-sync-workflow · REQ-sync-scope-user-data | T-05-03 / T-05-05 | `--delete` gated behind the divergence guard; btrfs pre/post snapshots as backstop; progress events streamed; system `ssh` trust honored | unit + contract | `uv run pytest tests/unit/jobs/test_folder_sync.py tests/contract -q` | W0 | ⬜ pending |
| 1-06-01 | 06 | 4 | REQ-sync-scope-user-data · REQ-machine-specific-exclusions · REQ-sync-scope-file-metadata | T-06-01 / T-06-02 | A→B round-trip asserts byte-identical content + metadata (`md5sum`/`stat`/`getfacl`) and that `.ssh/id_*`/`.config/tailscale` are absent on the target; mirrors a dedicated test dir only | integration | plan gate: `uv run pytest tests/integration/test_folder_sync.py --collect-only -q` · requirement run: `tests/run-integration-tests.sh tests/integration/test_folder_sync.py::TestFolderSyncRoundTrip::test_a_to_b` | W0 | ⬜ pending |
| 1-06-02 | 06 | 4 | REQ-manual-sync-workflow · REQ-terminal-ux | T-06-03 | Independently-modified target is blocked (D-06); normal round-trip is not false-flagged (D-07); dry-run writes nothing (D-12) | integration | plan gate: `uv run pytest tests/integration/test_folder_sync.py --collect-only -q` · requirement run: `tests/run-integration-tests.sh tests/integration/test_folder_sync.py::TestFolderSyncRoundTrip::test_bidirectional_round_trip` | W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

Plans 01-06 carry their `<automated>` verify as a `--collect-only` gate so the executor can confirm the tests are wired without provisioning VMs mid-execution. The behavior-proving run is the full `run-integration-tests.sh` invocation at the phase gate (shown in the requirement-run column).

## Requirement Sampling Coverage

Nyquist core: each of the five Phase-1 requirements is sampled by more than one automated test, so no requirement is under-sampled.

| Requirement | Sampling tasks | Sampled |
| ----------- | -------------- | ------- |
| REQ-sync-scope-user-data | 1-02-02 (unit), 1-04-01 (unit), 1-05-02 (unit), 1-06-01 (integration) | ✅ |
| REQ-machine-specific-exclusions | 1-02-01 / 1-02-02 (unit), 1-05-01 (unit), 1-06-01 (integration) | ✅ |
| REQ-sync-scope-file-metadata | 1-04-01 (acl preflight, unit), 1-05-01 (rsync flags, unit), 1-06-01 (integration metadata) | ✅ |
| REQ-manual-sync-workflow | 1-03-01 / 1-03-02 (unit), 1-04-02 (divergence, unit), 1-05-02 (unit), 1-06-02 (integration) | ✅ |
| REQ-terminal-ux | 1-05-02 (progress events, unit), 1-06-02 (integration) | ✅ |

Success criteria 4 (round-trip both directions byte-identical) and 5 (checksum + metadata assertions) are sampled by 1-06-01 and 1-06-02.

## Wave 0 Requirements

No separate Wave 0 plan exists; the test scaffolding is authored inside the implementing tasks rather than as a pre-wave. The following files are created or extended during execution and gate `wave_0_complete`:

- [ ] `tests/unit/jobs/test_folder_sync.py` — new; unit tests for config schema, filter-rule construction, divergence guard, dry-run, progress (plans 02/04/05)
- [ ] `tests/integration/test_folder_sync.py` — new; A→B, metadata, exclusions, B→A round-trip, divergence guard, dry-run (plan 06)
- [ ] `tests/unit/test_sync_history.py` — extend with `target_generations` merge-preserving tests (plan 03)
- [ ] `tests/unit/test_dry_run.py` — extend with `allow_divergence` plumbing and dry-run history-skip tests (plan 03)
- [ ] `tests/contract/test_job_interface.py` — extend so `FolderSyncJob` is exercised against the `SyncJob` contract (plans 04/05)

`wave_0_complete` flips to `true` once these files exist and collect cleanly.

## Manual-Only Verifications

None — every Phase-1 behavior has an automated unit, contract, or integration verification. The integration assertions run unattended against real VMs via `run-integration-tests.sh`.

## Validation Sign-Off

- [x] All tasks have an `<automated>` verify or a Wave 0 dependency
- [x] Sampling continuity: no 3 consecutive tasks without an automated verify (12/12 tasks carry one)
- [x] Wave 0 covers all MISSING references (`test_folder_sync.py` unit + integration)
- [x] No watch-mode flags
- [x] Feedback latency target set (< 30s unit subset; actual to be measured after Wave 0)
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-06-30
