---
status: resolved
trigger: |
  gh #191 "Folder sync progress bar not showing until done".
  When syncing, even a very long first sync (>10 min), the progress bar doesn't
  appear while rsync runs. It appears only when done — and empty, not at 100%
  (or an instant 100% then cleared as next phases start). Two unclear phone
  photos of the screen attached to the issue; not needed — root cause verified
  from code + rsync behavior.
created: 2026-07-20
updated: 2026-07-22
resolution: "Root cause: `_PROGRESS2_RE` matched only `to-chk=`, while rsync's default incremental recursion emits `ir-chk=` for nearly the whole run of a large first sync, so no progress frame ever matched and the bar was never created. Fixed by widening the regex to `(ir|to)-chk=`, then by running rsync with `--no-inc-recursive` and driving the bar from checked files rather than rsync's percent (commits 2965ccc/16ba5ec/b35afc5, merged in #199). Issue #191 closed."
---

# Debug: folder-sync progress bar hidden until done (#191)

## Symptoms

- expected: per-folder progress bar climbs smoothly while rsync transfers, reaching 100% at completion.
- actual: no bar at all during the (long) rsync run; a bar appears only at the end, and looks empty / not-100%.
- error_messages: none — purely a TUI progress display gap.
- timeline: reproducible on long first syncs (>10 min) of a large tree (e.g. /home). Known/deferred: flagged in `.planning/debug/folder-sync-dry-run-7-bytes.md` (Eliminated + blind_spots) as a separate minor issue.
- reproduction: `pc-switcher sync <target>` on a large first sync; watch the folder_sync progress bar.

## Current Focus

status: RESOLVED. Fix merged in #199; issue #191 closed.
next_action: none.

reasoning_checkpoint:
  hypothesis: >
    `_PROGRESS2_RE` (src/pcswitcher/jobs/folder_sync.py:38) hard-codes `to-chk=` in
    its trailing group. rsync `--info=progress2` emits the counter as `ir-chk=`
    (incremental-recursion check) while it is still BUILDING the file list, and
    only switches to `to-chk=` once the full list is known. Incremental recursion
    is rsync's default and, for a large tree, interleaves with the transfer and
    dominates the whole run — so nearly every progress2 frame during a big first
    sync carries `ir-chk=`, none of which the regex matches. With no match,
    `_stream_rsync` never calls `_report_progress`, so `update_job_progress`
    never lazily creates the Rich progress task (ui.py:224) and no bar is drawn.
    The bar appears only once rsync flips to `to-chk=` near the very end — by which
    point most bytes are already transferred, so it flashes near-done/at-100 rather
    than climbing, matching the "appears only when done, looks empty/not-100%" report.
  confirming_evidence:
    - "Ran rsync 3.2.7 `-aAXHS --numeric-ids --info=progress2` on a 2400-file / 60-dir tree (mirrors the job's flag baseline). Converting \\r→\\n and counting chk tokens: 1370 `ir-chk=` frames vs 1091 `to-chk=` frames. The ir-chk frames come FIRST and carry climbing percentages (0%→…), none of which `_PROGRESS2_RE` matches."
    - "Sample captured ir-chk frame: `20.000   0%    0,00kB/s    0:00:00 (xfr#1, ir-chk=1039/1101)` — structurally identical to a to-chk frame except the literal `ir-chk`."
    - "The larger and more directory-heavy the source tree, the longer rsync stays in the ir-chk phase relative to to-chk — so a real /home first sync spends the overwhelming majority of its runtime emitting only ir-chk frames, exactly the reported no-bar-for-10+-min behavior."
  falsification_test: >
    If the hypothesis were wrong, feeding an `ir-chk=` progress2 line through the
    UNMODIFIED `_stream_rsync` would still emit a `_report_progress` call. It does
    not (regex returns no match). After widening the regex to accept `ir-chk`, the
    same line must produce a ProgressUpdate — and it does.
  fix_rationale: >
    Change the regex's `to-chk=` to match either token, e.g.
    `(?:ir|to)-chk=\d+/(\d+)`. The rest of the line shape is identical across both
    phases, so group numbering and `_parse_size_to_bytes` are unaffected. The bar is
    driven by group-2 percent (byte-based, present in ir-chk frames too), so the
    fluctuating group-4 denominator during ir-chk (total grows as files are
    discovered) does NOT destabilize the bar — `update_job_progress` only uses
    `total` when `percent is None`, and percent is always set on this path.
  blind_spots: >
    Not reproduced against a real multi-minute /home sync from this sandbox (no
    target SSH). The "empty at end" second screenshot is not fully explained — it
    may be the brief to-chk flash, execute()'s final percent=100 update
    (folder_sync.py:624), or a subsequent phase's bar; confirm the widened regex
    makes the bar climb during ir-chk and that the end state reads 100%.

## Evidence

- timestamp: 2026-07-20
  finding: `_PROGRESS2_RE` = `(\d+[\d,.]*[KMGT]?)\s+(\d+)%\s+\S+\s+\S+\s+\(xfr#(\d+),\s*to-chk=\d+/(\d+)\)` (src/pcswitcher/jobs/folder_sync.py:38). The trailing `to-chk=` is literal. `_stream_rsync` (folder_sync.py:495-513) calls `_report_progress` ONLY on a regex match; the bar's Rich task is created lazily on the first `update_job_progress` call (src/pcswitcher/ui.py:224-230). No match → no task → no bar.
- timestamp: 2026-07-20
  finding: EMPIRICAL REPRO — rsync 3.2.7, `-aAXHS --numeric-ids --info=progress2`, 2400 files / 60 dirs. `tr '\r' '\n' | grep -oE '(ir-chk|to-chk)=' | sort | uniq -c` → 1370 ir-chk, 1091 to-chk. ir-chk frames are emitted first (during file-list build) with climbing percentages; only after the list is complete does rsync switch to to-chk. `_PROGRESS2_RE` matches none of the 1370 ir-chk frames.
- timestamp: 2026-07-20
  finding: Existing tests (tests/unit/jobs/test_folder_sync.py) exercise ONLY `to-chk` fixtures (lines 694, 737, 784-785, 799, 816). No test covers an `ir-chk` frame — which is why the gap shipped. This is the test hole to close.
- timestamp: 2026-07-20
  finding: Prior session `.planning/debug/folder-sync-dry-run-7-bytes.md` already documented this gap as known-but-deferred (line 99, and Eliminated hypothesis lines 115-116; blind_spots line 71-76): "the regex never matches ir-chk lines... left unfixed here since it is not the reported bug and fixing it is a separate, larger change (would need to also handle the fluctuating denominator during ir-chk)." #191 is that deferred bug.

## Eliminated

(none yet — root cause confirmed on first hypothesis via empirical repro)

## Resolution

root_cause: `_PROGRESS2_RE` (src/pcswitcher/jobs/folder_sync.py:38) matched only the literal `to-chk=` token, but rsync `--info=progress2` emits `ir-chk=` (incremental-recursion) frames while building the file list — the dominant phase of a large first sync. No match meant `_stream_rsync` never called `_report_progress`, so the Rich progress task was never lazily created (ui.py:224) and no bar appeared until rsync flipped to `to-chk=` near the end.

fix: widened `_PROGRESS2_RE`'s trailing token from `to-chk=` to `(?:ir|to)-chk=` so it matches both incremental-recursion and total-known frames. Updated the surrounding comment block to document both prefixes and the #191 rationale.

verification: added `test_ir_chk_progress_line_emits_report_progress` (tests/unit/jobs/test_folder_sync.py) feeding a real ir-chk progress2 line through the unmodified `_stream_rsync`; confirmed it FAILS against the pre-fix regex (0 progress calls) via `git stash` on folder_sync.py, then PASSES post-fix. Full suite: `uv run pytest` (643 passed), `uv run ruff check .` + `ruff format --check .` (clean), `uv run basedpyright` (0 errors), `uv run codespell` on changed files (clean).

files_changed:
- src/pcswitcher/jobs/folder_sync.py (regex + comment)
- tests/unit/jobs/test_folder_sync.py (regression test)
