---
status: resolved
trigger: |
  Ran `pc-switcher sync fleksi --dry-run`. Log shows:
  "Completed sync of '/home/janfr': 1119279 files transferred, 7 bytes, 18514 deletions".
  Only 7 bytes reported for 1.1M files — is that right?
created: 2026-07-19
updated: 2026-07-22
resolution: "Root cause: `_PROGRESS2_RE` captured only the fragment of rsync's byte counter after the last comma (e.g. `29,958,458` → `458`). Fixed by parsing the comma-grouped counter (commit 047daa9, merged in #184) with a regression test in tests/unit/jobs/test_folder_sync.py."
---

# Debug: folder-sync dry-run reports only 7 bytes

## Symptoms

- expected: bytes_transferred figure roughly proportional to 1.1M files (many GB), or a clear reason it's tiny.
- actual: `7 bytes` reported alongside `1119279 files transferred` and `18514 deletions`.
- error_messages: none — sync completed, this is a summary log line.
- timeline: observed on a `--dry-run` to target `fleksi`.
- reproduction: `pc-switcher sync fleksi --dry-run`; read the per-folder "Completed sync of '/home/janfr'" INFO line.

## Current Focus

status: RESOLVED. Regex fix merged in #184 with a regression test.
next_action: none.

reasoning_checkpoint:
  hypothesis: >
    `_PROGRESS2_RE`'s group-1 character class `(\d+[\d.]*[KMGT]?)` does not include
    the comma character. rsync's `--info=progress2` byte-so-far figure is
    comma-grouped for large numbers under `LC_ALL=C` (the locale this codebase
    forces) — grouping is NOT disabled by C locale, only the grouping CHARACTER
    changes across locales (comma under C/en_US, dot under nl_BE). Because
    `re.search()` finds the leftmost position where the WHOLE pattern matches, a
    comma breaks the digit run required immediately before whitespace+percent, so
    the search skips forward past every comma group until it lands on the LAST
    group (1-3 digits) directly followed by whitespace. `_parse_size_to_bytes`
    then receives only that trailing digit group instead of the full number,
    silently producing a byte count wrong by orders of magnitude while the
    adjacent `xfr#(\d+)` capture (no commas) stays correct.
  confirming_evidence:
    - "Ran real `rsync 3.2.7` under `LC_ALL=C` at scale (300k files, local + genuine SSH-loopback transport): raw progress2 output shows comma-grouped figures like '29,958,458' and '30,140,148' even under forced C locale — falsifying the code's own comment/docstring claim that C locale yields 'an ungrouped integer'."
    - "Tested LC_ALL=nl_BE.UTF-8 for comparison: byte figure uses '.' grouping ('5.000.000') instead of ','  — confirms locale changes the SEPARATOR CHARACTER, not whether grouping happens at all; C locale still groups (with commas)."
    - "Fed the exact real captured comma-grouped lines through the unmodified `_PROGRESS2_RE` directly in Python: group(1) captured only '458' and '148' (trailing digits after the last comma), not the full number."
    - "Ran the ACTUAL, unmodified `_stream_rsync` production function (not a mock) against a fake async chunk stream built from real captured rsync bytes ending in '...,140,148': result was files_xfr=300000 (correct) but bytes_xfr=148 (wrong — should be 30140148). This is the exact same failure shape as the field report (correct file count, byte count wrong by orders of magnitude, both from the SAME matched line)."
    - "The mechanism exactly explains the reported '7 bytes': a true total ending in a comma group like ',...,007' truncates to '007' -> `_parse_size_to_bytes('007')` == `int('007')` == 7, verbatim matching the observed value."
    - "Applying the one-character fix (adding ',' to the group-1 character class) against the same real captured lines correctly recovers '29,958,458' -> 29958458 and '30,140,148' -> 30140148."
    - "Ruled out alternatives via direct local testing: dry-run whole-file vs --no-whole-file delta-transfer mode (no difference observed), local-path vs genuine SSH-loopback remote transport (no difference observed), pure attribute-only itemization via ownership mismatch (correctly produces xfr#0/0 bytes, not the reported combination), incremental-recursion ir-chk/to-chk transition (real but separate gap — ir-chk lines never match the regex at all, though this doesn't affect the final captured total since the last emitted line is always the terminal to-chk line in every test run). None of these reproduce a large-correct-file-count-with-tiny-wrong-byte-count combination; only the comma-truncation mechanism does, and it reproduces it exactly through the unmodified production code."
  falsification_test: >
    Re-running the same real captured comma-grouped progress2 lines through the
    FIXED regex should still show a truncated capture if the hypothesis is wrong.
    It does not: both test lines are captured in full and parse to the correct
    byte values.
  fix_rationale: >
    Add ',' to `_PROGRESS2_RE`'s group-1 character class so the size token
    capture spans the entire grouped figure. `_parse_size_to_bytes` already
    strips both '.' and ',' as thousands separators (per its own docstring and
    existing tests `test_parse_size_to_bytes_tolerates_thousands_separators`) —
    this fix only widens what the REGEX captures to match what the PARSER
    already correctly handles. Addresses the root cause (regex too narrow, not a
    parser or buffering issue) with a minimal, targeted change; `_stream_rsync`'s
    chunk-splitting/buffering logic is untouched (directly tested, not
    implicated).
  blind_spots: >
    Not verified against the real fleksi machine's raw output (no SSH access
    from this sandbox) — the exact real trailing digit group for the "7 bytes"
    report is inferred to end in "...,007", not directly observed. Comma-grouping
    behavior confirmed on rsync 3.2.7 (locally installed version) under C and
    en_US locales; if fleksi runs a materially different rsync version the exact
    formatting could in principle differ, though the fix is a strict superset
    (accepts dot-grouped, comma-grouped, and ungrouped forms) so it is safe
    either way. The separately-confirmed ir-chk/to-chk gap (regex never matches
    "ir-chk=" lines during incremental-recursion scanning) is a real but distinct
    minor issue — it only suppresses TUI progress updates during the scan phase
    in every tested scenario, not the final reported total; left unfixed here
    since it is not the reported bug and fixing it is a separate, larger change
    (would need to also handle the fluctuating denominator during ir-chk).

## Evidence

- timestamp: 2026-07-19
  finding: Summary line built in `src/pcswitcher/jobs/folder_sync.py:590-598`. `bytes_transferred = seed_bytes + mirror_bytes`; in dry-run there is no seeding pass (line 570 gated on `not dry_run`), so the figure is purely `mirror_bytes` from the dry-run rsync pass.
- timestamp: 2026-07-19
  finding: `mirror_bytes` = `bytes_xfr` in `_stream_rsync` (folder_sync.py:461-512), set ONLY from `_PROGRESS2_RE` group 1 via `_parse_size_to_bytes`; "last progress line wins" (line 481-483). Never derived from rsync's `--stats` totals.
- timestamp: 2026-07-19
  finding: `_PROGRESS2_RE` = `(\d+[\d.]*[KMGT]?)\s+(\d+)%\s+\S+\s+\S+\s+\(xfr#(\d+),\s*to-chk=\d+/(\d+)\)` (line 31). `_parse_size_to_bytes` (120-141) strips `.`/`,` as thousands separators for plain integers; C locale forced via `env LC_ALL=C` (line 392-393), no `-h`, so the counter is an ungrouped integer.
- timestamp: 2026-07-19
  finding: LOCAL REPRO 1 — plain `--dry-run` (50 files, real content): final progress2 line shows the FULL cumulative size (5,000,000), not near-zero. Falsifies "dry-run always reports ~0 bytes".
- timestamp: 2026-07-19
  finding: LOCAL REPRO 2 — C-locale dry-run, `-aAXHS --numeric-ids --delete --info=progress2`, METADATA-ONLY diff (identical content, only perms+mtime differ) + 2 deletions: final progress2 line still shows the FULL file size (10,000,000). Falsifies "metadata-only diff reports ~0 bytes". rsync's progress2 size column reflects total size of files it considers transferred, regardless of dry-run or metadata-only.
- timestamp: 2026-07-19
  finding: Because both easy explanations are falsified, a final figure of exactly `7` is anomalous. Candidate mechanisms still open: (a) the last regex-matching progress2 line was an EARLY line (~7 bytes into the first file) and later progress2 lines failed to match / were mangled by the `[\r\n]` chunk-splitting in `_stream_rsync`; (b) rsync incremental-recursion behavior over a 1.1M-file tree emits a final progress2 line differing from the small-tree case; (c) something folder/target-specific to fleksi.
- timestamp: 2026-07-19
  finding: LOCAL REPRO 3 — real `rsync 3.2.7` under `LC_ALL=C` at 300k-file scale (local paths and genuine SSH-loopback transport): raw progress2 byte figures are COMMA-GROUPED (e.g. "29,958,458", "30,140,148") even though C locale was forced. Confirmed via `man rsync` and by testing `LC_ALL=nl_BE.UTF-8` for comparison (groups with '.' instead) that locale changes only the grouping CHARACTER, never whether grouping happens — falsifies the code's own comment/docstring assumption that C locale gives "an ungrouped integer".
  implication: This is a NEW angle not previously tested — earlier LOCAL REPRO 1/2 examined raw rsync text output directly and never actually exercised the production `_stream_rsync`/`_PROGRESS2_RE` code path against it, so the regex's handling of comma-grouped numbers was never checked.
- timestamp: 2026-07-19
  finding: Fed the real captured comma-grouped lines through the unmodified `_PROGRESS2_RE` in Python — `.search()` skips past every comma-broken digit run and captures only the trailing 1-3 digit group ("29,958,458" -> group(1)="458"; "30,140,148" -> group(1)="148"). Ran the ACTUAL, unmodified `_stream_rsync` production function (not mocked) against these bytes: files_xfr=300000 (correct), bytes_xfr=148 (wrong — should be 30140148). Root cause confirmed at the production-code level, not just via standalone regex testing.
  implication: A true final total ending in a comma group like "...,007" would truncate to "007" -> `_parse_size_to_bytes("007")` == 7 — verbatim matches the field-reported "7 bytes". The adjacent `xfr#(\d+)` group has no comma issue, explaining why files_transferred (1,119,279) was reported correctly from the SAME matched line while bytes_transferred was wrong by orders of magnitude.
- timestamp: 2026-07-19
  finding: Ruled out alternative mechanisms via direct local testing before landing on the regex bug — dry-run `--whole-file` (local-path default) vs `--no-whole-file` (forces delta-transfer, closer to real remote codepath): no difference in reported bytes. Local-path vs genuine SSH-loopback remote transport: no difference. Pure attribute-only itemization via ownership (`--numeric-ids` uid/gid) mismatch: correctly produces `xfr#0`/`0 bytes` (attribute-only updates don't increment xfr#), not the reported large-count/tiny-bytes combination. Incremental-recursion ir-chk/to-chk transition: confirmed real (regex never matches "ir-chk=" lines per `man rsync`, so TUI progress is silently frozen during the scan phase of a large tree) but does not affect the final captured total in any tested run — a separate, minor, unfixed gap, not the reported bug.
- timestamp: 2026-07-19
  finding: Applied the fix (added ',' to `_PROGRESS2_RE`'s group-1 character class) and re-ran the real captured lines and the new production-code test — both now correctly capture and report 29958458 and 30140148. Added regression test `test_comma_grouped_progress_line_reports_full_bytes` (tests/unit/jobs/test_folder_sync.py); confirmed non-vacuous by checking the pre-fix regex against the same input in isolation (captures "458", not "29,958,458"). Full unit suite (547 tests), ruff check/format, basedpyright, and codespell all pass on the changed files.
- timestamp: 2026-07-19
  finding: NOTE — the fix was found already present in the working tree/HEAD when reaching the point of applying it (commit 608c1384, bundled under an unrelated "docs: add user guide for reading FULL-level sync logs" message alongside pre-existing uncommitted docs changes from a separate concurrent session). Diff-verified byte-for-byte identical to the intended fix. This session's own edit is therefore not a distinct commit; the regression test and docstring corrections added afterward remain uncommitted pending the standard archive_session commit step.

## Eliminated

- hypothesis: "7 bytes" is normal because `--dry-run` transfers no real data.
  reason: Falsified by LOCAL REPRO 1 — dry-run progress2 reports full cumulative size.
- hypothesis: "7 bytes" is because the diff is metadata-only (no content bytes).
  reason: Falsified by LOCAL REPRO 2 — metadata-only diff still reports full file size.
- hypothesis: rsync's `--whole-file` vs delta-transfer mode, or local-path vs genuine remote SSH transport, changes progress2's reported byte figure.
  reason: Falsified by LOCAL REPRO 3/4/5 — no difference observed across `--no-whole-file`, local-path, and genuine SSH-loopback transport for content-identical, mtime-differing files (all report full source-file size).
- hypothesis: 1.1M files are mostly attribute-only (`--numeric-ids` uid/gid mismatch) and 7 bytes is the correct sum of the handful of genuinely-changed files.
  reason: Falsified — pure attribute-only itemization (`.f....og...`) produces `xfr#0`, not a large xfr# count; the reported 1,119,279 xfr# count is inconsistent with a mostly-attribute-only tree.
- hypothesis: rsync's incremental-recursion ir-chk/to-chk transition causes an early small progress2 line to be the last one matched.
  reason: Falsified as the explanation for THIS symptom — confirmed the regex gap is real (ir-chk lines never match), but in every tested large-scale run the last emitted progress2 line is always the terminal to-chk line, and it correctly carries the true cumulative file count; the byte figure on that same line is what the comma-truncation bug corrupts, not an earlier line winning out.

## Resolution

root_cause: >
  `_PROGRESS2_RE`'s size-token capture group (`(\d+[\d.]*[KMGT]?)`, folder_sync.py:31,
  pre-fix) did not include ',' in its character class. rsync's `--info=progress2`
  byte-so-far figure is comma-grouped for large numbers even under the forced
  `LC_ALL=C` (C locale groups with ',', it does not disable grouping — only the
  separator character is locale-dependent). Because `re.search()` finds the
  leftmost position where the whole pattern matches, a comma breaks the
  required digit run, so the match landed on only the LAST 1-3 digits after
  the final comma (e.g. "29,958,458" -> captured "458"). This silently produced
  a `bytes_transferred` wrong by orders of magnitude while the adjacent
  `xfr#(\d+)` group (comma-free) stayed correct — exactly matching the field
  report of a correct file count (1,119,279) alongside a tiny, wrong byte
  count (7, consistent with a true total ending in "...,007").
fix: >
  Added ',' to `_PROGRESS2_RE`'s size-token character class:
  `(\d+[\d,.]*[KMGT]?)`. `_parse_size_to_bytes` already stripped both '.' and
  ',' as thousands separators, so no change was needed there — the regex was
  simply too narrow for what the parser already correctly handled. Also
  corrected two docstrings/comments that asserted the false premise that C
  locale produces an ungrouped counter (folder_sync.py `_parse_size_to_bytes`
  docstring and `_build_rsync_cmd`'s LC_ALL=C comment), and the matching false
  premise in `test_rsync_forced_to_c_locale`'s docstring.
verification: >
  Self-verified: ran the actual unmodified `_stream_rsync` production
  function against real rsync-captured comma-grouped bytes (LC_ALL=C, rsync
  3.2.7, 300k-file local scale test) — pre-fix produced bytes_xfr=148 instead
  of 30140148; post-fix produces the correct value. Added regression test
  `test_comma_grouped_progress_line_reports_full_bytes`, confirmed
  non-vacuous against the pre-fix regex in isolation. Full unit suite (547
  tests), ruff check, ruff format --check, basedpyright, and codespell all
  pass on the changed files. NOT yet verified end-to-end against the real
  fleksi machine (no SSH access from this sandbox) — awaiting human
  confirmation that a real dry-run sync now reports a plausible
  bytes_transferred figure.
files_changed:
  - src/pcswitcher/jobs/folder_sync.py (regex fix + corrected locale comments/docstring)
  - tests/unit/jobs/test_folder_sync.py (new regression test + corrected docstring)
