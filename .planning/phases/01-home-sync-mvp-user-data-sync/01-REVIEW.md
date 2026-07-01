---
phase: 01-home-sync-mvp-user-data-sync
reviewed: 2026-07-01T00:00:00Z
depth: standard
files_reviewed: 6
files_reviewed_list:
  - src/pcswitcher/sync_history.py
  - src/pcswitcher/config_sync.py
  - src/pcswitcher/jobs/folder_sync.py
  - src/pcswitcher/cli.py
  - src/pcswitcher/orchestrator.py
  - src/pcswitcher/ui.py
findings:
  critical: 2
  warning: 1
  info: 1
  total: 4
status: issues_found
---

# Phase 01: Code Review Report

- **Reviewed:** 2026-07-01T00:00:00Z
- **Depth:** standard
- **Files Reviewed:** 6
- **Status:** issues_found

## Summary

Reviewed the gap-closure changes (commits `f1825fb`..`796e3a8`) that resolved the prior review's CR-01/CR-02/WR-01..03/IN-01..03 findings in this data-loss-critical rsync sync tool. The state-machine refactor (`DivergenceStatus` tri-state, `UNKNOWN_GENERATION` sentinel, fail-closed on findmnt/find-new failure), the merge-preserving history writes, the bytes-parsing and `c`/`h` change-type additions, the progress-bar `set_total_steps` correction, and the SIGINT dead-code removal are all individually sound. `uv run basedpyright` reports 0 errors on the six files.

However, the empty-prefix tool-state filter that underpins CR-01 has two correctness defects that the existing test suite does not exercise, both centered on `FolderSyncJob._target_diverged_since` and the new pre-transfer re-check.

First, the tool-state tokens are matched unanchored anywhere in the find-new path, so a real user file nested under any `.config/pc-switcher/` or `.local/share/pc-switcher/` subpath (common with dotfile managers) is silently masked, meaning real divergence goes undetected and data is lost.

Second, the filter only masks pc-switcher's own state dirs, but `install_on_target` (Phase 7) writes upgrade artifacts to `~/.local/bin/` and `~/.local/share/uv/` under the synced `@home` subvolume. The new Phase-9 pre-transfer re-check (WR-03) runs after Phase 7 and flags those artifacts as divergence, so legitimate "upgrade then sync" runs are falsely blocked, pushing the user to `--allow-divergence` (which disables the guard entirely).

## Critical Issues

### CR-01: Tool-state filter matches unanchored, masking real user divergence at any nesting depth

**File:** `src/pcswitcher/jobs/folder_sync.py:381-397`

**Issue:** In the empty-prefix branch the exclusion is `any(token in line for token in tool_state_tokens)` where `history_token = "/.local/share/pc-switcher/"` and `config_token = "/.config/pc-switcher/"`. `btrfs subvolume find-new` emits paths relative to the subvolume root, e.g. `... flags UNKNOWN janfr/.config/pc-switcher/config.yaml`. The substring test is not anchored to the top-level (single username segment) position, so it also matches these paths at arbitrary depth: `janfr/dotfiles/.config/pc-switcher/config.yaml`, `janfr/backups/home/.local/share/pc-switcher/anything`, or any git/dotfile-manager checkout (chezmoi, yadm, stow) containing a copy of `.config/pc-switcher/`.

Because the default synced folder is `/home` (empty prefix), a genuine user modification to any such nested path on the target is silently classified `CLEAN`. The divergence guard — whose entire purpose is to prevent `rsync --delete` from destroying independent target changes — then proceeds and overwrites the user's data. This is a false-negative data-loss path. The target audience (power users syncing Linux desktops, often with dotfile repos) makes the trigger realistic, not theoretical. The existing tests only cover the depth-1 case (`janfr/.config/pc-switcher/…`), so this gap is uncaught.

**Fix:** Anchor the match to the top-level home-relative position — the tool-state dir must appear immediately after the first (username) path segment, not anywhere. Extract the path (last whitespace-delimited field of the find-new line) and test against an anchored pattern, e.g.:

```python
import re

# path is the final field of a find-new line: "... flags UNKNOWN <user>/<rest>"
_TOOL_STATE_RE = re.compile(r"^[^/]+/(?:\.local/share|\.config)/pc-switcher/")

def _is_tool_state_path(find_new_line: str) -> bool:
    path = find_new_line.rsplit(" ", 1)[-1]  # relative to subvolume root
    return bool(_TOOL_STATE_RE.match(path))
```

Then in the empty-prefix loop use `if _is_tool_state_path(line): continue`. This masks only `<user>/.config/pc-switcher/…` and `<user>/.local/share/pc-switcher/…` at the true home root and no longer masks nested user copies. The derived `.local/share`/`.config` segments should still come from `sync_history.HISTORY_DIR` / `config_sync.CONFIG_REMOTE_DIR` to keep the single source of truth.

### CR-02: Pre-transfer re-check flags `install_on_target` upgrade artifacts as divergence, falsely blocking legitimate syncs

**File:** `src/pcswitcher/jobs/folder_sync.py:577-586` (re-check) combined with the tool-state token set at `381-383`

**Issue:** The WR-03 pre-transfer re-check calls `_check_divergence(folder)` in `execute()` immediately before the destructive rsync, comparing the target against the previous run's stored generation `G(N)`. In the orchestrator pipeline this runs in Phase 9 — after Phase 7 `install_on_target`. When the target's pc-switcher version differs from the source (verified: `install_on_target.execute()` only skips when `target_version == source_version`, `src/pcswitcher/jobs/install_on_target.py:70-75`), Phase 7 runs `install.sh`, which writes upgrade artifacts under the synced `@home` subvolume: `uv tool install` writes `$HOME/.local/bin/pc-switcher` and `$HOME/.local/share/uv/tools/pcswitcher/…` (`install.sh:190-200`), and uv bootstrap (if absent) writes `$HOME/.local/bin/uv` (`install.sh:111-114`).

These land under `janfr/.local/bin/…` and `janfr/.local/share/uv/…`. The tool-state filter only masks `/.local/share/pc-switcher/` and `/.config/pc-switcher/`, so `find-new` since `G(N)` reports the install artifacts as changed files, yielding `DivergenceStatus.DIVERGED`, and the re-check raises `RuntimeError("Pre-transfer divergence re-check failed …")` and aborts the sync before rsync runs.

Result: any sync that also upgrades pc-switcher on the target (every version bump in production; nearly every sync during active development where the source dev version changes constantly) fails with a false "target has been modified since the last sync" error. The only escape is `--allow-divergence`, which disables the guard entirely and reopens the exact data-loss window CR-01/CR-02 were meant to close. This is a regression newly introduced by the WR-03 re-check (the old Phase-4-only check ran before Phase 7 and never saw install writes), and it is not covered by the unit tests (which mock a single-folder `/home` with no install step).

**Fix:** Exclude pc-switcher's install footprint from the divergence scope for the empty-prefix case, in addition to the two state dirs. Option A (preferred; pairs with the CR-01 fix) is to broaden the anchored tool-state matcher:

```python
_TOOL_STATE_RE = re.compile(
    r"^[^/]+/(?:"
    r"\.local/share/pc-switcher|"
    r"\.config/pc-switcher|"
    r"\.local/bin/(?:pc-switcher|uv)|"
    r"\.local/share/uv"
    r")(?:/|$)"
)
```

Option B (more robust) is to capture the target subvolume generation after Phase 7/Phase 8 (i.e. establish the re-check baseline post-install) rather than reusing `G(N)`, so pc-switcher's own pipeline writes are never inside the compared window. Whichever is chosen, add a test that runs the re-check with a `find-new` line under `janfr/.local/bin/pc-switcher` and asserts the sync is not blocked, and one under a genuine user path that is blocked.

## Warnings

### WR-01: `lstrip("~/")` used for prefix stripping is character-set removal, not prefix removal

**File:** `src/pcswitcher/jobs/folder_sync.py:381-382`; `src/pcswitcher/config_sync.py:317`

**Issue:** The tool-state tokens and the config absolute-path derivation strip the leading `~/` with `str.lstrip("~/")`. `str.lstrip` removes any leading characters in the set `{"~", "/"}`, not the literal prefix `~/`. For the current constants (`"~/.local/share/pc-switcher"`, `"~/.config/pc-switcher/config.yaml"`) the output happens to be correct because the third character is `.`. But this is a silent landmine: any future constant beginning with an additional `~` or `/` (e.g. a normalized `"~//.config/…"`, or a value that starts with a `/` after the tilde) would be over-stripped, and here it directly feeds the security-relevant divergence filter tokens — a wrong derivation would silently disable the guard rather than error.

**Fix:** Use explicit prefix removal so the intent is unambiguous and robust: `sync_history.HISTORY_DIR.removeprefix("~/")`, `config_sync.CONFIG_REMOTE_DIR.removeprefix("~/")`, and likewise `CONFIG_REMOTE_PATH.removeprefix("~/")` in `config_sync._copy_config_to_target`.

## Info

### IN-01: Duplicated per-line skip logic in the two branches of `_target_diverged_since`

**File:** `src/pcswitcher/jobs/folder_sync.py:385-409`

**Issue:** The empty-prefix branch and the `else` (non-empty prefix) branch each re-implement the same iteration scaffold — skip `transid marker` summary lines, skip blank lines — differing only in the accept/reject predicate. This duplication is what let the tool-state filter diverge from the prefix filter and made CR-01/CR-02 easy to miss. Consolidating into one loop that computes `is_tool_state` and `is_in_prefix` per line (with the prefix always `""` meaning match-all in the empty case) would remove the duplication and give a single place to reason about which paths count as divergence.

**Fix:** Extract a single helper that yields the candidate paths from `find-new` stdout (dropping the `transid marker` and blank lines once), then apply the branch-specific predicate to that stream.

---

- _Reviewed: 2026-07-01T00:00:00Z_
- _Reviewer: Claude (gsd-code-reviewer)_
- _Depth: standard_
