---
phase: 260718-np8-folder-sync-include-override-filter-rule
reviewed: 2026-07-18T15:57:53Z
depth: quick
files_reviewed: 14
files_reviewed_list:
  - docs/adr/adr-016-hardcoded-runtime-file-excludes.md
  - docs/configuration.md
  - pyproject.toml
  - src/pcswitcher/cli.py
  - src/pcswitcher/default-config.yaml
  - src/pcswitcher/home.filter
  - src/pcswitcher/jobs/folder_sync.py
  - src/pcswitcher/root.filter
  - src/pcswitcher/schemas/config-schema.yaml
  - tests/integration/test_end_to_end_sync.py
  - tests/local_rsync/__init__.py
  - tests/local_rsync/test_folder_sync_filters.py
  - tests/unit/cli/test_commands.py
  - tests/unit/jobs/test_folder_sync.py
findings:
  critical: 0
  warning: 1
  info: 2
  total: 3
status: issues_found
---

# Phase 260718-np8: Code Review Report

- **Reviewed:** 2026-07-18T15:57:53Z
- **Depth:** quick (escalated to targeted verification per the focus areas called out in the review request — real rsync was exercised directly, not just pattern-matched)
- **Files Reviewed:** 14
- **Status:** issues_found

## Summary

Reviewed the `excludes` → `filter_file` migration, the GLOBAL-FIRST `merge`/`dir-merge` filter surface, the shipped `home.filter`/`root.filter` starter files, `init` wiring, and the missing-filter_file validation path, reading every changed file, running the full quality gate (`ruff format --check`, `ruff check`, `basedpyright`, `pytest`, `codespell`), and independently re-deriving the four real-rsync acceptance scenarios plus two additional ad hoc rsync experiments against the actual `rsync` binary rather than trusting the shipped tests at face value; shell-quoting, filter ordering (GLOBAL-FIRST), the floating-vs-anchored pattern decision, `validate()`'s missing-filter_file check, and `init`'s starter-file writing are all correct and match the locked CONTEXT/PLAN decisions — verified empirically, not just read — but one documentation self-contradiction was found in `docs/configuration.md`'s ancestor-descent idiom section (a code example is described as showing something it does not show), which will actively mislead a user editing a filter file for a non-`/home` path, and two minor quality/robustness observations are recorded as Info.

## Warnings

### WR-01: Ancestor-descent idiom doc contradicts its own code example

- **File:** `docs/configuration.md:171-181`
- **Issue:** The "The ancestor-descent idiom" section's code block (lines 176-178) is:

```
+ .cache/
+ .cache/uv/***
- .cache/*
```

  This is the FLOATING form (no leading `/`) — identical to what ships in `home.filter`. But the very next paragraph (line 181) says: "the shipped `home.filter` uses **floating** (non-leading-slash) patterns — **the leading-slash form above** is the transfer-root-relative template; drop the leading `/` when editing a filter file meant to apply under `/home`…" The code block shown has no leading slashes at all, so there is no "leading-slash form above" to refer to. A user syncing a `path:` other than `/home` (e.g. a custom `path: /srv/data` folder where the transfer root IS the directory itself) is told to look at "the form above" for the anchored template, but what's actually above is the floating form — the opposite of what they need. This directly undermines the exact floating-vs-anchoring distinction this review was asked to confirm the docs get right.
- **Fix:** Show the anchored form in the code block (matching CONTEXT.md's original idiom) and describe the floating form as the variant used by the shipped `home.filter`, e.g.:

```markdown
+ /.cache/
+ /.cache/uv/***
- /.cache/*
```

  Patterns anchor to the transfer root; because pc-switcher syncs `/home` with the folder's contents as the transfer root, each user's home sits one level below that root (`alice/.cache/uv`, not `/.cache/uv`), so the shipped `home.filter` drops the leading `/` from every line above (floating, non-leading-slash patterns), and the leading `/` shown above should be kept when the transfer root itself is the directory you mean (e.g. a `path:` that is already the user's own home, such as `/root`).

## Info

### IN-01: `+ .cache/` ancestor line is functionally redundant in the shipped `home.filter`

- **File:** `src/pcswitcher/home.filter:29`, `docs/configuration.md:176`
- **Issue:** Verified empirically (`rsync --dry-run` with and without the `+ .cache/` line, both producing identical transferred-file sets) that in this specific filter — where no earlier rule excludes `.cache` itself — the ancestor line changes nothing: `.cache` is never excluded by a preceding rule, so rsync already descends into it by default, and `+ .cache/uv/***` / `+ .cache/pip/***` already match the `.cache/uv` and `.cache/pip` directories themselves (the trailing `***` includes the directory, not just its contents) before the later `- .cache/*` is reached. This matches the idiom CONTEXT.md locked in, so it is not a functional defect, but it is dead weight in the current rule set and could confuse a future maintainer into thinking it's load-bearing here.
- **Fix:** No action required if kept intentionally as defensive/future-proofing (e.g. in case a broader `- .cache/` rule is ever added above the block). If precision is preferred, a one-line comment noting "kept for defensiveness; not required by the current rule set" would prevent a future "why is this here" investigation.

### IN-02: `init` writes config.yaml and filter files without a shared error boundary

- **File:** `src/pcswitcher/cli.py:483-499`
- **Issue:** `config_path.write_text(default_config)` and the two subsequent `(config_path.parent / name).write_text(contents)` calls are not wrapped in a try/except. If the second or third write fails (disk full, permissions, race on the directory), `init` raises an unhandled `OSError` with a raw Python traceback instead of the Rich-formatted error style used elsewhere in `cli.py` (e.g. `_load_configuration`), and leaves a partial state (config.yaml present, one or both filter files missing) that will only surface later as a confusing `validate()` ValidationError during `sync`. This mirrors a pre-existing gap for `config_path.write_text` itself (not a new regression), so it's not a blocker, but the surface area for a partial-write got larger (1 file → 3 files).
- **Fix:** Wrap the three writes in a single try/except OSError that prints a Rich error and `raise typer.Exit(1)`, consistent with the rest of the command's error handling.

## Verification Performed (not just read)

- `uv run pytest tests/local_rsync/test_folder_sync_filters.py -q` — all 4 acceptance tests pass against the real rsync binary (3.2.7).
- `uv run pytest tests/unit/jobs/test_folder_sync.py tests/unit/cli/test_commands.py -q` — 73 passed.
- `uv run pytest -q` (whole repo, non-integration) — 594 passed, 61 deselected.
- `uv run ruff format --check . && uv run ruff check .` — clean.
- `uv run basedpyright src/pcswitcher/jobs/folder_sync.py src/pcswitcher/cli.py` — 0 errors/warnings/notes.
- `uv run codespell` on all touched docs/filter files — clean.
- `grep -rn "excludes" src/pcswitcher/` — only prose/comment occurrences remain (`# ... excludes ...`), no config field; matches the plan's cross-cutting check.
- Package-data check: `home.filter`/`root.filter` load via `importlib.resources.files("pcswitcher")` and contain the expected cache include-override rules.
- Manually re-ran the shipped `home.filter` against a hand-built `<root>/alice/.cache/{uv,pip,nvidia,fontconfig}` + `.ssh/id_rsa` + `docs/keep.txt` tree via real rsync `--dry-run`: `.cache/uv` and `.cache/pip` transfer, `.cache/nvidia`/`.cache/fontconfig`/`.ssh/id_rsa` do not, `docs/keep.txt` does — confirms the floating pattern decision is correct at a `/home`-style root, independent of the shipped test.
- Manually verified `a/**/b` does NOT match `a/b` with real rsync (confirms the "Coming from .gitignore" doc claim) and that a merge filter file at a path containing a space works correctly when passed as a single shlex-quoted argv token (confirms the shell-injection-safety and space-quoting claims independent of the unit test).
- Confirmed removing `+ .cache/` from a copy of the shipped filter produces an identical transfer set (basis for IN-01).

## Focus-Area Verdicts

- **Shell-injection safety**: Correct. `expanded_filter_file()` performs `~`/env-var expansion entirely in Python before the result ever reaches `shlex.quote`; no literal `~` or unexpanded `$VAR` reaches the shell. `--filter=` is directly concatenated (no space) with a `shlex.quote`-wrapped `"merge <path>"` / `"dir-merge /.pcswitcher-filter"` string, producing one argv token each — verified both via unit tests and by feeding a space-containing path to a real rsync invocation.
- **GLOBAL-FIRST ordering**: Correct. `_build_rsync_cmd` emits runtime excludes, then `merge` (when set), then `dir-merge` (always), in that literal order in the command string; confirmed via the unit ordering test and via `TestGlobalFirstEnforcement` (real rsync) that a per-dir `+` cannot re-expose a centrally-excluded path. The identical mechanism (an earlier `-` filter argument on the command line) protects the runtime excludes, which sit even earlier.
- **Floating vs. leading-slash patterns**: Correct in the shipped `home.filter`/`root.filter` and proven at a real `<root>/<user>/.cache` tree by `TestCentralMergeCacheIncludeOverride`. The documentation of this same distinction has the self-contradiction in WR-01.
- **`validate()` missing-filter_file check**: Correct — `Host.SOURCE`, uses the expanded path for both the `test -f` command and the check itself, runs before any transfer, and is covered by unit tests including one that proves no literal `~` reaches the command.
- **`init` starter-file writes**: Correct — the existence/`--force` gate is checked once at the top of the command before any writes occur, so `--force` overwrites all three files consistently and a fresh run without `--force` never overwrites any of them. Partial-write robustness is the (non-blocking) IN-02 gap.
- **`+ .cache/` ancestor line correctness**: Not over/under-matching — behaves as documented — but empirically redundant in the current rule composition (IN-01).

---

_Reviewed: 2026-07-18T15:57:53Z — Reviewer: Claude (gsd-code-reviewer) — Depth: quick_
