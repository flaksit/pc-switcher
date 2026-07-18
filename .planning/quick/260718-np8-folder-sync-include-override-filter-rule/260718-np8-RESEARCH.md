# Quick Task 260718-np8: folder_sync native rsync filter rules (#166) - Research

**Researched:** 2026-07-18

**Domain:** rsync filter-rule mechanics (merge / dir-merge), pc-switcher folder_sync

**Confidence:** HIGH — every rsync claim below was verified with a local `rsync 3.2.7` `--dry-run` probe this session (probe scripts under scratchpad). Claims not probed are labelled.

## Summary

All seven focus questions were verified empirically against rsync 3.2.7 (matches the project's Ubuntu 24.04 target). The CONTEXT.md design is sound and directly implementable: `--filter='merge <abs>'` before `--filter='dir-merge /.pcswitcher-filter'` gives enforced global-first precedence; without an `e` modifier the per-dir files transfer to the target; `-aAXHS` never reads any per-dir file, so a planted `.rsync-filter` is a verified no-op. The ancestor-descent idiom keeps `.cache/uv` while dropping the rest of `.cache` on a `--delete` mirror. Unit tests inspect the built command string (mocked executor); a real-rsync local `--dry-run` integration test against temp dirs is feasible and needs no SSH.

**Primary recommendation:** Emit filter args in this exact argv order per folder: (1) runtime-protection excludes (unchanged), (2) `--filter=merge <expanded-abs-filter_file>` when `filter_file` set, (3) `--filter=dir-merge /.pcswitcher-filter` always. Quote each as a single argv element via `shlex.quote(f"merge {abs}")` / `shlex.quote("dir-merge /.pcswitcher-filter")`.

## 1. `merge` directive path semantics [VERIFIED: local probe]

`--filter='merge /abs/path'` reads the file at that absolute path. Spelling: `merge <file>` and its one-char synonym `. <file>` are equivalent (rsync man "MERGE-FILE FILTER RULES"). Use `merge` for readability. Relative paths resolve relative to rsync's CWD (the process cwd, not the transfer root) — brittle, so the plan's decision to expand `~`/env vars in Python and pass an absolute path is correct.

The merge file's OWN rules anchor relative to the **transfer root**, not the merge file's location. Probe: a central file containing `- .ssh/id_*` and `- .cache/nvidia`, passed as `merge $PWD/central.filter` while syncing `src/`, excluded `src/.ssh/id_rsa` and `src/.cache/nvidia` — i.e. rules are evaluated as if written inline at the top of the rule list against the root of the `src/` transfer. This is exactly the current per-folder-relative anchoring, unchanged.

Blank lines and `#` comment lines in a merge file are allowed and ignored [VERIFIED] — the probe file contained a `# comment` line and a blank line and rsync produced no error and the rules still applied. (Caveat: a `#` only starts a comment at the start of a rule line; inline trailing `# ...` on a rule is NOT stripped — keep comments on their own lines.)

Single-argv-element form: the code must pass ONE argv token `--filter=merge /abs/path`. rsync takes everything after `merge ` as the filename to end of the rule, so a path with spaces is fine as long as `shlex.quote` keeps it a single shell word. Mirror the existing pattern: `f"--filter={shlex.quote(f'merge {abs_path}')}"`.

## 2. `dir-merge` semantics, inheritance, no `e` modifier [VERIFIED: local probe]

`--filter='dir-merge /.pcswitcher-filter'` (synonym `: /.pcswitcher-filter`) makes rsync look for a file named `.pcswitcher-filter` in every traversed directory and splice its rules in scoped to that directory's subtree. Probe confirmed all three required behaviors in one run:

- **Inheritance into subdirs:** a `.pcswitcher-filter` containing `- secret.env` placed in `proj/` excluded BOTH `proj/secret.env` and `proj/sub/secret.env` — per-dir rules inherit into descendant directories by default (no `n` modifier needed to get inheritance; `n` would DISABLE it).
- **No `e` modifier ⇒ the filter files themselves transfer:** `proj/.pcswitcher-filter` appeared in the target file list (like a committed `.gitignore`). Adding an `e` modifier would auto-exclude them; the plan correctly omits it.
- **Found in every dir:** a `.pcswitcher-filter` placed only in `proj/sub/` (probe 2b) took effect there, proving rsync reads the file wherever it appears during traversal.

Leading `/` on the dir-merge filename subtlety [VERIFIED]: in `dir-merge /.pcswitcher-filter` the `/` before the filename does NOT root-anchor the file's location. It means "the filename is exactly `.pcswitcher-filter` with no path component" — i.e. don't treat an embedded slash as a path. rsync still searches for that filename in *every* directory. Probe 2b confirmed a file placed only in a deep subdir was honored there. Doc phrasing for users: "`dir-merge /.pcswitcher-filter` activates a `.pcswitcher-filter` file in ANY directory of the tree; the leading `/` just says the name is a bare filename, it does not restrict the file to the root."

## 3. Global-first splice point [VERIFIED: local probe]

Placing `merge <central>` in argv BEFORE `dir-merge /.pcswitcher-filter` makes central rules win under first-match-wins: rsync evaluates rules in argv order, and a merge/dir-merge rule splices the file's rules at that position in the sequence. Probe 3: central file `- secret.env`, per-dir `.pcswitcher-filter` with `+ secret.env` → `secret.env` stayed EXCLUDED. The central `-` matched first; the per-dir `+` could not re-expose it. This is the enforced, non-overridable-rules property the plan wants.

Ordering constraint relative to runtime-protection excludes: the ADR-016 `_runtime_exclude_filters` MUST remain first of all three groups (runtime excludes → central merge → dir-merge), so no central `+` and no per-dir `+` can re-expose pc-switcher's own runtime files. This matches the current code where runtime filters are `parts.extend`-ed before user filters (folder_sync.py:340). Insert the merge arg where `folder.excludes` are emitted today (:342-345), then append the single dir-merge arg after it.

## 4. Ancestor-descent idiom for `.cache/uv` [VERIFIED: local probe]

The CONTEXT idiom works on a `--delete` mirror without `--delete-excluded`. Probe 4 with:
```
+ /.cache/
+ /.cache/uv/***
+ /.cache/pip/***
- /.cache/*
```
kept `.cache/`, `.cache/uv/pkg1`, `.cache/pip/pkg2` and dropped `.cache/nvidia`, `.cache/fontconfig`. Correct.

Nuances found (document these for users):

- **`***` vs `**` vs trailing `/` on the leaf matters** [VERIFIED, probe 4c]: `+ /.cache/uv/***` keeps `uv/pkg1` ✓; `+ /.cache/uv/**` does NOT (0 files) — `**` matches the dir's *contents* but not the `.cache/uv` directory ENTRY itself, so `- /.cache/*` excludes the `uv` dir before rsync descends into it; `+ /.cache/uv/` works too (trailing `/` matches the dir, and its deeper contents are not matched by the one-level `- /.cache/*`). **Recommend `/***`** as the idiomatic, least-surprising form — it explicitly matches "this directory and everything under it."
- **The ancestor `+ /.cache/` line is NOT strictly required with `- /.cache/*`** [VERIFIED, probe 4b]: omitting it still kept uv/pip, because `- /.cache/*` matches only the *children* of `.cache`, never `.cache` itself, so the `.cache` dir survives via the implicit include-all default and rsync descends. The ancestor line becomes REQUIRED only if a broader rule would exclude `.cache` itself (e.g. `- .cache` or `- /.cache`) — then you must `+ /.cache/` before that `-` so rsync can descend to reach the `+ /.cache/uv/***` leaf. Keep the ancestor line in the shipped starter file anyway: it is harmless, makes intent explicit, and is the safe template if a user later tightens the trailing exclude.

## 5. `-a` does not enable per-dir files [VERIFIED: local probe]

`-aAXHS` (= `-rlptgoD` + ACLs/xattrs/hardlinks/sparse) reads NO per-directory filter file. Probe 5: a hostile `proj/.rsync-filter` containing `- main.py`, synced with the exact project baseline `rsync -aAXHS --delete` (no `-F`), left `proj/main.py` present — the hostile file was a complete no-op (it merely transferred as ordinary content). Only `-F`/`--filter=': .rsync-filter'`, `-FF`, or `-C`/`--cvs-exclude` activate built-in per-dir mechanisms, and none are in the baseline. This underpins the required regression test: plant `.rsync-filter` (and, for our own mechanism, confirm it does nothing without our explicit `dir-merge`), assert the targeted file still transfers.

## 6. gitignore-vs-rsync differences (for the docs guide) [VERIFIED except where noted]

Crisp, verified statements for a "coming from .gitignore" section:

- **Signs, not `!`:** rsync uses `+ pattern` (include) and `- pattern` (exclude) prefixes; there is no `!` negation. A per-dir `.pcswitcher-filter` with only `-` lines behaves like a `.gitignore`.
- **First-match-wins (top wins)**, the OPPOSITE of gitignore's last-match-wins. In rsync the FIRST rule that matches a path decides it; later rules never override an earlier match. [VERIFIED via probe 3.]
- **Leading `/` anchors to the transfer root; a MIDDLE slash does NOT anchor** [VERIFIED, probe with nested `a/direct`]: `- /a/direct` removed only root `a/direct` and left `deep/a/direct`. `- a/direct` (slash present but not leading) removed BOTH `a/direct` and `deep/a/direct` — a non-leading slash makes the pattern match the full multi-segment path but it still floats to any depth. This differs from gitignore, where a middle slash anchors the pattern to the `.gitignore`'s directory.
- **Trailing `/` = directory-only** (e.g. `node_modules/` matches only directories) — same as gitignore.
- **No-slash pattern matches the final path component at any depth** (e.g. `nvidia` matches `.cache/nvidia` and `alice/.cache/nvidia`) — like a gitignore basename pattern.
- **`a/**/b` does NOT match `a/b`** [VERIFIED, probe]: `x/**/b` left `x/b/leaf` present — `**` requires at least the slash-structure it spans; the zero-intermediate-directory case is not matched. Use `a/b` (or `a/**b`) if you also need the zero-depth case. (`**` matches across `/`; single `*` matches within one segment only.)

## 7. Testing approach [VERIFIED: codebase inspection + probe feasibility]

**Unit tests — string inspection, executor mocked.** `tests/unit/jobs/test_folder_sync.py` builds the command via `job._build_rsync_cmd(folder, dry_run)` under a `patch("pcswitcher.jobs.folder_sync.Path.home", ...)` and asserts on the returned shell string (see `TestBuildRsyncCmd._build`, :312-327, and `TestRuntimeExcludeFilters`, :449-461). No SSH/subprocess is exercised; `make_context` supplies `AsyncMock` source/target. Add cases here: merge arg emitted only when `filter_file` set; merge appears AFTER runtime excludes and BEFORE dir-merge (assert index ordering, mirroring `test_runtime_excludes_precede_user_excludes_in_command`); dir-merge arg always present; no `-F`/`-FF`/`-C`/`--cvs-exclude` in cmd; `filter_file` path shlex-quoted. `validate()` missing-file error: extend the existing preflight tests (`validate()` already does per-folder `test -d`; add an `os.path`/`Path.exists`-based check for the expanded `filter_file` and a unit test asserting a `ValidationError` when it is absent — this is a local source-side check, no executor round-trip needed if you stat the path directly).

**Integration test — real rsync, local, no SSH: FEASIBLE and recommended.** All acceptance behaviors were reproduced this session with plain `rsync -a --delete --dry-run --out-format='%n' --filter=... src/ dst/` against temp dirs. A pytest integration test can build a temp source tree with `tmp_path`, invoke the real `rsync` binary via `subprocess`/`asyncio` with the same filter args the job builds (or drive `_build_rsync_cmd` and strip the SSH transport), and assert on `--dry-run` `%n` output for: (a) central filter keeps `.cache/uv` and drops `.cache/nvidia`; (b) a `.pcswitcher-filter` per-dir file takes effect, inherits into subdirs, and itself transfers; (c) a hostile `.rsync-filter` is a no-op. Gating: integration tests live in `tests/integration/` and are marked `@pytest.mark.integration`; `pyproject.toml` addopts default-excludes them (`-m "not integration"`), and the existing `integration` marker is declared there. NOTE: current integration tests require VM infrastructure; a pure-local `rsync --dry-run` test needs only the `rsync` binary — consider a lighter marker or keep it under `integration` but document it needs no VM. Confirm the test host has `rsync` (present: 3.2.7).

## Implementation notes carried from CONTEXT (unchanged, do not revisit)

Runtime-protection excludes stay first and un-overridable (ADR-016). `filter_file` is per-`FolderEntry`, replaces `excludes`; unset ⇒ no merge arg for that folder (runtime + dir-merge still apply). Missing `filter_file` ⇒ fail-fast `ValidationError` after `~`/env expansion. `pc-switcher init` ships `home.filter`/`root.filter` as package data next to `default-config.yaml` (`files("pcswitcher").joinpath(...)`, cli.py:484), honoring `--force`. Never pass `-F`/`-FF`/`-C`. No `--delete-excluded` (unchanged).

## Sources

- Local `rsync 3.2.7` `--dry-run` probes run this session (scratchpad `rsyncprobe/`) — probes 1–5 + gitignore-diff + ancestor/glob nuance. [VERIFIED]
- `rsync(1)` man page, sections "FILTER RULES", "MERGE-FILE FILTER RULES", "INCLUDE/EXCLUDE PATTERN RULES". [CITED]
- Codebase: `src/pcswitcher/jobs/folder_sync.py`, `tests/unit/jobs/test_folder_sync.py`, `pyproject.toml`, `docs/adr/adr-016-*.md`. [VERIFIED]
