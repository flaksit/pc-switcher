---
phase: 260718-np8-folder-sync-include-override-filter-rule
verified: 2026-07-18T15:53:40Z
status: passed
score: 6/6 must-haves verified
behavior_unverified: 0
overrides_applied: 0
---

# Quick Task 260718-np8: folder_sync include-override filter rule Verification Report

**Phase Goal:** Replace the exclude-only `excludes: list[str]` config surface with native rsync filter syntax via two authoring surfaces (per-folder central `merge <filter_file>` + tree-wide `dir-merge /.pcswitcher-filter`), GLOBAL-FIRST precedence, runtime excludes first; init ships starter filter files; missing filter_file fails validation. Satisfy the three issue #166 acceptance criteria.

**Verified:** 2026-07-18T15:53:40Z

**Status:** passed

**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| - | ----- | ------ | -------- |
| 1 | `pc-switcher init` on a fresh machine writes config.yaml plus home.filter and root.filter, and the first sync passes validate() with no missing-filter_file error | VERIFIED | `cli.py:461-491` init writes `config.yaml`, then reads `home.filter`/`root.filter` via `files("pcswitcher")` and writes both into `config_path.parent`, honoring `--force` gate above. `default-config.yaml` folder entries point at `~/.config/pc-switcher/home.filter` and `~/.config/pc-switcher/root.filter` — exact match to what init writes. `tests/unit/cli/test_commands.py::TestInitCommand` (2 tests) pass. |
| 2 | A central home.filter that excludes .cache but re-includes .cache/uv and .cache/pip makes a real rsync --dry-run transfer .cache/uv while dropping the rest of .cache (acceptance #1) | VERIFIED | `tests/local_rsync/test_folder_sync_filters.py::TestCentralMergeCacheIncludeOverride` reads the real shipped `home.filter` and runs it through actual `rsync --dry-run`; ran it myself — PASSED (`.cache/uv`/`.cache/pip` transferred, `.cache/nvidia`/`.cache/fontconfig`/`.ssh/id_rsa` dropped, `docs/keep.txt` transferred). |
| 3 | A `.pcswitcher-filter` placed in a subdirectory takes effect for that subtree, inherits into deeper directories, and the filter file itself transfers to the target (acceptance #2) | VERIFIED | `TestPerDirDirMerge::test_dir_merge_inherits_and_transfers_itself` — ran, PASSED. Confirms exclusion in `proj/` and inherited `proj/sub/`, plus `.pcswitcher-filter` itself transfers (no `e` modifier in `_build_rsync_cmd`). |
| 4 | A planted `.rsync-filter` carrying a hostile rule has zero effect on the transfer (acceptance #3) | VERIFIED | `TestHostileRsyncFilterNoOp::test_hostile_rsync_filter_is_inert` — ran, PASSED. `_build_rsync_cmd` never emits `-F`/`-FF`/`-C`/`--cvs-exclude` (grepped source, confirmed absent; also asserted by `test_no_built_in_per_dir_flags` unit test). |
| 5 | A folder whose filter_file is set but absent on the source aborts validate() with a ValidationError before any transfer | VERIFIED | `folder_sync.py:247-259` — `validate()` step 3 runs `test -f <expanded>` on source when `expanded_filter_file()` is not None, appending a `Host.SOURCE` `ValidationError` naming both filter_file and folder on non-zero exit. `test_missing_filter_file`, `test_existing_filter_file_produces_no_error`, `test_filter_file_check_uses_expanded_path` all pass (ran them). |
| 6 | pc-switcher's runtime files (ADR-016) stay excluded first and can never be re-exposed by a central merge or per-dir rule | VERIFIED | `_build_rsync_cmd:373-378` — `_runtime_exclude_filters` extended first, then merge (if set), then dir-merge always. `test_merge_arg_ordering` proves `idx_runtime < idx_merge < idx_dir_merge` (ran, PASSED). `TestGlobalFirstEnforcement::test_central_exclude_cannot_be_reexposed_by_per_dir_include` proves a per-dir `+` cannot re-expose a central `-` (ran against real rsync, PASSED). ADR-016 documents this in its new "Interaction with user filter rules (#166)" section. |

**Score:** 6/6 truths verified (0 present, behavior-unverified)

### Required Artifacts

| Artifact | Expected | Status | Details |
| -------- | -------- | ------ | ------- |
| `src/pcswitcher/jobs/folder_sync.py` | FolderEntry.filter_file, expanded_filter_file(), GLOBAL-FIRST emission, validate() filter_file check | VERIFIED | Read in full; matches must-haves exactly (lines 46-74, 235-261, 282-390). |
| `src/pcswitcher/schemas/config-schema.yaml` | `excludes` dropped, `filter_file` string property added | VERIFIED | Lines 199-201; `excludes` gone from schema. |
| `src/pcswitcher/home.filter` | Floating patterns, cache include-override block | VERIFIED | Read in full; exact rule set matches plan spec (machine-identity excludes, `.nv`, VS Code caches, `+ .cache/` / `+ .cache/uv/***` / `+ .cache/pip/***` / `- .cache/*`). |
| `src/pcswitcher/root.filter` | Floating patterns, machine-identity excludes only | VERIFIED | Read in full; 3 exclude lines as specified. |
| `src/pcswitcher/default-config.yaml` | Both folders use filter_file, header rewritten | VERIFIED | Lines 116-177; `/home` and `/root` entries use `filter_file:` pointing at the init-shipped files; header explains new surface. |
| `src/pcswitcher/cli.py` | init writes filter files, honors --force | VERIFIED | Lines 461-491; writes home.filter/root.filter after config.yaml, gated by the same `--force` check. |
| `tests/local_rsync/test_folder_sync_filters.py` | Tests A/B/C/D against real rsync | VERIFIED | Read in full; ran — 4/4 pass, exercising real `rsync --dry-run` subprocess calls, not mocks. |
| `pyproject.toml` | local_rsync marker registered, not excluded from default gate | VERIFIED | Marker registered at line 67; NOT added to the `-m "not integration"` addopts (line 63) — confirmed via grep. |
| `docs/adr/adr-016-hardcoded-runtime-file-excludes.md` | New section on merge/dir-merge interaction | VERIFIED | Read in full; new "Interaction with user filter rules (#166)" section (lines 27-29) plus References update (line 44). |
| `docs/configuration.md` | filter_file docs, two surfaces, precedence, gitignore guide | VERIFIED | Read in full (lines 108-192); covers filter_file config, Filter rules section, Coming from .gitignore section, ancestor-descent idiom, always-excluded runtime files. No hard-wrapped paragraphs observed. |

### Key Link Verification

| From | To | Via | Status | Details |
| ---- | --- | --- | ------ | ------- |
| `_build_rsync_cmd` | rsync `--filter` args | runtime excludes -> `merge <filter_file>` (when set) -> `dir-merge /.pcswitcher-filter` (always) | WIRED | Confirmed by direct code read (lines 373-378) and `test_merge_arg_ordering`/`test_dir_merge_always_present` (ran, both pass). Never emits `-F`/`-FF`/`-C`/`--cvs-exclude` — grepped, confirmed absent, and asserted by `test_no_built_in_per_dir_flags`. |
| `init` | `home.filter`/`root.filter` package data | `files("pcswitcher").joinpath(name).read_text()` written to `config_path.parent`, honoring `--force` | WIRED | Code read (cli.py:484-491); `TestInitCommand` (2 tests, ran, pass) confirm files exist post-init and post-`--force`, and that `home.filter` content includes `+ .cache/uv/***`. |
| `validate()` | filter_file existence check | expand `~`/env vars before `test -f` on source, before use in merge directive | WIRED | Code read (folder_sync.py:247-259, 375-377); same `expanded_filter_file()` call site feeds both the validate() check and the `_build_rsync_cmd` merge arg — single source of truth for expansion. `test_filter_file_check_uses_expanded_path` (ran, pass) confirms no literal `~` reaches the shell command. |
| `default-config.yaml` | init-written filter files | filter_file paths match write targets | WIRED | `~/.config/pc-switcher/home.filter` / `root.filter` in default-config.yaml exactly match `config_path.parent / "home.filter"` / `"root.filter"` in cli.py init (both resolve under `~/.config/pc-switcher/` via `Configuration.get_default_config_path()`). |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| -------- | ------- | ------ | ------ |
| local_rsync acceptance tests run (not skip) and pass | `uv run pytest tests/local_rsync/ -v` | 4 passed in 0.19s (all 4: TestCentralMergeCacheIncludeOverride, TestPerDirDirMerge, TestHostileRsyncFilterNoOp, TestGlobalFirstEnforcement) | PASS |
| unit tests for folder_sync + cli pass | `uv run pytest tests/unit/jobs/test_folder_sync.py tests/unit/cli/test_commands.py -q` | 73 passed | PASS |
| No `excludes` config field remains | `grep -rn "excludes" src/pcswitcher/` | Only comment prose in ADR wording and `.filter` file header comments (`` `+` includes, `-` excludes ``) — no config field | PASS |
| Whole-repo lint/format | `uv run ruff check . && uv run ruff format --check .` | All checks passed; 87 files already formatted | PASS |
| Whole-repo type check | `uv run basedpyright` | 0 errors, 0 warnings, 0 notes | PASS |
| Whole-repo test suite | `uv run pytest -q` | 594 passed, 61 deselected (integration-marked VM tests) | PASS |
| Docs spell check | `uv run codespell docs/configuration.md docs/adr/adr-016-hardcoded-runtime-file-excludes.md` | Clean, no output | PASS |
| Integration e2e collects without schema error | `uv run pytest tests/integration/test_end_to_end_sync.py --collect-only -q -m integration` | 7 tests collected, 0 errors | PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| ----------- | ---------- | ----------- | ------ | -------- |
| #166 | 260718-np8-PLAN.md | Include-override filter rules for folder_sync | SATISFIED | All 6 must-have truths verified above; all 3 issue acceptance criteria plus GLOBAL-FIRST enforcement proven against real rsync. |

### Anti-Patterns Found

None found in the reviewed source files (`folder_sync.py`, `cli.py`, `home.filter`, `root.filter`, `default-config.yaml`, `config-schema.yaml`). No TBD/FIXME/XXX/TODO/HACK/PLACEHOLDER markers; no stub returns; no hardcoded-empty data flowing to output.

### Human Verification Required

None. Task 4 (integration e2e migration) explicitly defers full VM behavioral verification to the integration CI run per project practice (noted in both the plan and SUMMARY) — this is a documented, intentional deferral consistent with the codebase's established pattern (confirmed via git log: prior phases also relied on "push and let CI run integration tests"), not a gap in this quick task's own goal (which is scoped to the filter-rule feature, proven locally by the real-rsync acceptance tests). The e2e test module itself collects cleanly with the new schema (7 tests, 0 errors), so no schema regression exists.

### Gaps Summary

No gaps. All must-have truths, artifacts, and key links verified directly against the codebase and via independently re-run tests (not merely trusted from SUMMARY.md). The three issue #166 acceptance criteria pass against real rsync via `--dry-run`, GLOBAL-FIRST precedence is enforced and tested, missing filter_file fails validation before any transfer, init ships and writes the starter filter files, and no `excludes` config field remains anywhere in `src/pcswitcher/`.

---

_Verified: 2026-07-18T15:53:40Z_

_Verifier: Claude (gsd-verifier)_
