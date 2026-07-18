---
phase: 260718-np8-folder-sync-include-override-filter-rule
plan: 01
subsystem: folder_sync
tags: [rsync, filter-rules, config-schema, cli-init, gitignore-migration]

requires: []
provides:
  - Per-folder `filter_file` config field replacing `excludes` on FolderEntry
  - GLOBAL-FIRST rsync filter emission (runtime excludes -> central merge -> dir-merge)
  - Starter home.filter/root.filter package data, written by `pc-switcher init`
  - Real-rsync acceptance tests (tests/local_rsync/, `local_rsync` marker)
  - ADR-016 + configuration.md updated for the new filter surface
affects: [folder_sync, config-schema, cli-init, docs-configuration]

tech-stack:
  added: []
  patterns:
    - "GLOBAL-FIRST rsync filter precedence: runtime excludes, then --filter=merge <file> (when set), then --filter=dir-merge /.pcswitcher-filter (always)"
    - "Floating (non-leading-slash) rsync filter patterns for /home-style transfer roots where each user's dir sits one level below the root"

key-files:
  created:
    - src/pcswitcher/home.filter
    - src/pcswitcher/root.filter
    - tests/local_rsync/__init__.py
    - tests/local_rsync/test_folder_sync_filters.py
  modified:
    - src/pcswitcher/jobs/folder_sync.py
    - src/pcswitcher/schemas/config-schema.yaml
    - src/pcswitcher/default-config.yaml
    - src/pcswitcher/cli.py
    - tests/unit/jobs/test_folder_sync.py
    - tests/unit/cli/test_commands.py
    - tests/integration/test_end_to_end_sync.py
    - pyproject.toml
    - docs/adr/adr-016-hardcoded-runtime-file-excludes.md
    - docs/configuration.md

key-decisions:
  - "FolderEntry.filter_file (str | None) replaces excludes: list[str] entirely; expanded via expanded_filter_file() (~ and env vars, os.path.expandvars + Path.expanduser)"
  - "_build_rsync_cmd emits runtime excludes -> --filter='merge <expanded>' (when filter_file set) -> --filter='dir-merge /.pcswitcher-filter' (always) -- GLOBAL-FIRST, never -F/-FF/-C/--cvs-exclude"
  - "validate() adds a source-side 'test -f <expanded>' check; a configured-but-absent filter_file is a Host.SOURCE ValidationError before any transfer"
  - "Shipped home.filter/root.filter use FLOATING patterns (no leading slash) per the plan's FLAGGED PLANNER DECISION -- /home syncs with user dirs one level below the transfer root, so a leading-slash /.cache/... would silently fail to match"
  - "New tests/local_rsync/ test dir (not tests/integration/) for real-rsync acceptance tests, avoiding the VM-required session-scoped fixture in tests/integration/conftest.py; local_rsync marker registered but NOT added to the default -m 'not integration' filter, so it runs in the standard uv run pytest gate"

requirements-completed: ["#166"]

coverage:
  - id: D1
    description: "FolderEntry gains filter_file (replaces excludes) with expanded_filter_file() for ~/env expansion"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestFolderEntry"
        status: pass
    human_judgment: false
  - id: D2
    description: "_build_rsync_cmd emits GLOBAL-FIRST filter order (runtime excludes -> merge -> dir-merge) and never enables built-in rsync per-dir mechanisms"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestBuildRsyncCmd (test_merge_arg_ordering, test_no_built_in_per_dir_flags, test_dir_merge_always_present)"
        status: pass
    human_judgment: false
  - id: D3
    description: "validate() fails fast with a Host.SOURCE ValidationError when a configured filter_file is absent on source, using the expanded path"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestValidatePreflight (test_missing_filter_file, test_existing_filter_file_produces_no_error, test_filter_file_check_uses_expanded_path)"
        status: pass
    human_judgment: false
  - id: D4
    description: "pc-switcher init writes config.yaml + home.filter + root.filter (package data), honoring --force"
    verification:
      - kind: unit
        ref: "tests/unit/cli/test_commands.py::TestInitCommand (test_init_writes_config_and_filter_files, test_init_force_overwrites_all_three_files)"
        status: pass
    human_judgment: false
  - id: D5
    description: "Three issue #166 acceptance criteria (central merge cache include-override, dir-merge inheritance + self-transfer, hostile .rsync-filter no-op) plus GLOBAL-FIRST enforcement, proven against a real local rsync binary"
    verification:
      - kind: integration
        ref: "tests/local_rsync/test_folder_sync_filters.py (TestCentralMergeCacheIncludeOverride, TestPerDirDirMerge, TestHostileRsyncFilterNoOp, TestGlobalFirstEnforcement)"
        status: pass
    human_judgment: false
  - id: D6
    description: "Integration e2e config migrated from excludes to filter_file; module collects without schema error"
    verification:
      - kind: other
        ref: "uv run pytest tests/integration/test_end_to_end_sync.py --collect-only -q -m integration (7 tests collected, 0 errors)"
        status: pass
    human_judgment: true
    rationale: "Full behavioral verification requires the VM integration suite (deferred to integration CI per project practice); only schema-validity was verified locally."
  - id: D7
    description: "ADR-016 and docs/configuration.md document the new filter surface, precedence, and coming-from-.gitignore guide"
    verification:
      - kind: other
        ref: "grep checks in <verification>: dir-merge present, first-match-wins present, ADR-016 references merge; codespell clean"
        status: pass
    human_judgment: false

duration: ~7min (commit span)
completed: 2026-07-18
status: complete
---

# Quick Task 260718-np8: folder_sync include-override filter rule Summary

**Replaced folder_sync's exclude-only config with a per-folder rsync `filter_file` (native `merge`/`dir-merge` filter syntax, GLOBAL-FIRST precedence), shipping starter home.filter/root.filter via `init` and real-rsync acceptance tests proving all three issue #166 criteria.**

## Performance

- **Tasks:** 5/5 completed
- **Files created:** 4 (home.filter, root.filter, tests/local_rsync/__init__.py, tests/local_rsync/test_folder_sync_filters.py)
- **Files modified:** 9 (folder_sync.py, config-schema.yaml, default-config.yaml, cli.py, 3 test files, pyproject.toml, ADR-016, configuration.md)

## Accomplishments

- `FolderEntry.filter_file` (replaces `excludes`) with `expanded_filter_file()` for `~`/env-var expansion; `CONFIG_SCHEMA` and `config-schema.yaml` updated to match.
- `_build_rsync_cmd` emits GLOBAL-FIRST filter precedence: runtime excludes (ADR-016, un-overridable) -> `--filter='merge <expanded>'` (when `filter_file` set) -> `--filter='dir-merge /.pcswitcher-filter'` (always); never passes `-F`/`-FF`/`-C`/`--cvs-exclude`.
- `validate()` adds a source-side `test -f` check on the expanded `filter_file` path, failing fast with a `Host.SOURCE` `ValidationError` when a configured filter file is absent.
- `home.filter`/`root.filter` ship as package data with floating (non-leading-slash) patterns, including the `.cache/uv`+`.cache/pip` include-override idiom; `pc-switcher init` writes both next to `config.yaml`, honoring `--force`.
- `tests/local_rsync/test_folder_sync_filters.py` proves the three issue #166 acceptance criteria plus GLOBAL-FIRST enforcement against a real local rsync binary via `--dry-run`; registered under a new `local_rsync` pytest marker that runs in the default gate.
- Integration e2e config migrated to `filter_file` (seeded via a new `_write_filter_file` VM helper); ADR-016 and `docs/configuration.md` document the new surface, precedence, and a "coming from .gitignore" guide.

## Task Commits

1. **Task 1: Filter-rule code surface + schema + unit tests** - `d6879fd` (feat)
2. **Task 2: Starter filter files + default-config rewrite + init wiring** - `1e25250` (feat)
3. **Task 3: Real-rsync acceptance tests + local_rsync marker** - `f81fa78` (test)
4. **Task 4: Migrate integration e2e config** - `0bc93f5` (test)
5. **Task 5: Docs — ADR-016 amendment + configuration.md** - `caded83` (docs)

**Follow-up fix:** `983a4b4` (style) — `ruff format` collapsed a now-short-enough line in `validate()`'s error message, applied after Task 1 was already committed; re-committed separately rather than amending.

_Note: no plan-metadata commit is made by this executor per the quick-task instructions (`.planning/` docs artifacts are committed by the orchestrator separately)._

## Files Created/Modified

- `src/pcswitcher/jobs/folder_sync.py` - FolderEntry.filter_file, expanded_filter_file(), GLOBAL-FIRST filter emission, validate() filter_file existence check
- `src/pcswitcher/schemas/config-schema.yaml` - `excludes` array replaced by `filter_file` string property
- `src/pcswitcher/home.filter` / `src/pcswitcher/root.filter` - new starter filter files (package data)
- `src/pcswitcher/default-config.yaml` - folder_sync header rewritten; `/home` and `/root` entries use `filter_file`
- `src/pcswitcher/cli.py` - `init` writes home.filter/root.filter alongside config.yaml
- `tests/unit/jobs/test_folder_sync.py` - migrated excludes-based tests to filter_file; added merge/dir-merge ordering and validate() filter_file tests
- `tests/unit/cli/test_commands.py` - new `TestInitCommand` (writes + `--force` overwrite)
- `tests/local_rsync/__init__.py`, `tests/local_rsync/test_folder_sync_filters.py` - real-rsync acceptance tests (new directory)
- `tests/integration/test_end_to_end_sync.py` - e2e config + new `_write_filter_file` helper, migrated off `excludes`
- `pyproject.toml` - registered `local_rsync` marker (not added to default `-m` exclusion)
- `docs/adr/adr-016-hardcoded-runtime-file-excludes.md` - new "Interaction with user filter rules (#166)" section
- `docs/configuration.md` - `folder_sync` section rewritten: filter_file, two authoring surfaces, GLOBAL-FIRST precedence, coming-from-.gitignore guide, ancestor-descent idiom

## Decisions Made

- Followed the plan's FLAGGED PLANNER DECISION exactly: shipped filter files use floating (non-leading-slash) patterns, not the leading-slash idiom from CONTEXT.md's ancestor-descent example, because `/home` syncs with each user's directory one level below the transfer root.
- Used `Path(os.path.expandvars(...)).expanduser()` for `expanded_filter_file()` (satisfies both the CLAUDE.md `Path`-for-filesystem-paths convention and ruff's `PTH111`, since `Path` has no direct env-var-expansion equivalent).
- Placed the real-rsync acceptance tests under a new `tests/local_rsync/` directory (not `tests/integration/`) per the plan, to avoid the VM-required session-scoped `pytest.exit()` fixture in `tests/integration/conftest.py`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] ruff format collapsed a validate() error message onto one line**
- **Found during:** Whole-repo gate run (after Task 5)
- **Issue:** `uv run ruff format .` reformatted a line-wrapped f-string in `validate()`'s `ValidationError` message in `folder_sync.py` (written during Task 1) since it now fit under the line-length limit after minor surrounding edits.
- **Fix:** Accepted the formatter's output (single-line f-string), no semantic change.
- **Files modified:** `src/pcswitcher/jobs/folder_sync.py`
- **Verification:** `uv run ruff format --check .` clean afterward; full test suite still 594 passed.
- **Committed in:** `983a4b4` (separate follow-up commit, not amended into Task 1's commit)

---

**Total deviations:** 1 auto-fixed (formatting only, Rule 1)

**Impact on plan:** No functional or scope change.

## Issues Encountered

The plan's Task 4 `<verify>` command (`uv run pytest tests/integration/test_end_to_end_sync.py --collect-only -q`) exits 5 ("no tests collected") in this repo even on an unmodified tree, because `pyproject.toml`'s default `addopts` includes `-m "not integration"`, which deselects every test in that module before collection succeeds. This is a pre-existing environment property, not something Task 4 changed. Verified the migration's actual claim (schema-valid collection, no errors) with `-m "integration"` added on the command line (which overrides the addopts default): `uv run pytest tests/integration/test_end_to_end_sync.py --collect-only -q -m integration` collects all 7 tests with 0 errors.

## Gate Results (whole-repo, after all 5 tasks)

- `uv run ruff format . && uv run ruff check .` — 1 file reformatted (see deviation above), then all checks passed.
- `uv run basedpyright` — 0 errors, 0 warnings, 0 notes.
- `uv run pytest` — **594 passed, 61 deselected** (deselected = `integration`-marked VM tests, excluded by default `-m "not integration"`). The 4 `local_rsync` acceptance tests (`tests/local_rsync/test_folder_sync_filters.py`) are included in the 594 and confirmed to **run** (not skip) via a targeted `-k` run — `rsync` is present on this machine (3.2.7), matching RESEARCH.md's tested version.
- `uv run codespell` — clean, no output.
- Cross-cutting check `grep -rn "excludes" src/pcswitcher/` — returns only comment prose (ADR-016/runtime-exclude wording and generic "includes/excludes" syntax descriptions in the new `.filter` files' comments); **no `excludes:` config field remains**.
- `uv run pytest tests/integration/test_end_to_end_sync.py --collect-only -q -m integration` — 7 tests collected, 0 schema errors (see Issues Encountered for the `-m` override note).

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- folder_sync's filter surface is fully migrated; no remaining references to the old `excludes` config field anywhere in `src/pcswitcher/`.
- Full VM behavioral verification of the migrated e2e test (Task 4) is deferred to the integration CI run, per project practice (push and let CI exercise it).

---

*Quick task: 260718-np8-folder-sync-include-override-filter-rule*

*Completed: 2026-07-18*

## Self-Check: PASSED

All 6 task/follow-up commits (`d6879fd`, `1e25250`, `f81fa78`, `0bc93f5`, `caded83`, `983a4b4`) verified present in `git log`. All 4 new files (`src/pcswitcher/home.filter`, `src/pcswitcher/root.filter`, `tests/local_rsync/__init__.py`, `tests/local_rsync/test_folder_sync_filters.py`) verified present on disk.
