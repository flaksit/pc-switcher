# Quick Task 260718-np8: folder_sync include-override filter rules (#166) - Context

**Gathered:** 2026-07-18

**Status:** Ready for planning

<domain>
## Task Boundary

Give `folder_sync` an include-override capability so a user can express "exclude a folder but keep select subfolders" (real case: drop most of `~/.cache` but keep `~/.cache/uv`, `~/.cache/pip`, etc.). Achieve this by adopting **native rsync filter syntax** (`+ pattern` / `- pattern`, first-match-wins) via two authoring surfaces, replacing the current exclude-only `excludes: list[str]` config surface.

Full spec: GitHub issue #166.
</domain>

<decisions>
## Implementation Decisions

### Precedence: central filter vs per-directory files (RESOLVED — was the issue's OPEN DECISION)

GLOBAL-FIRST. Emit the central `merge <file>` rule BEFORE the `dir-merge /.pcswitcher-filter` rule. Central policy is authoritative; a per-directory `.pcswitcher-filter` file can NEVER re-expose a globally-excluded path on a `--delete` mirror (e.g. cannot `+ .ssh/id_*` past a central `- .ssh/id_*`). This uniquely enables enforced, non-overridable rules. The gitignore convention remains fully available under global-first: leave the central file empty and drop a single `.pcswitcher-filter` in the root folder.

Runtime-protection excludes (ADR-016, `_runtime_exclude_filters`) stay emitted FIRST of all, ahead of both the central merge and the dir-merge — un-overridable by any user rule.

### Final rsync filter rule order (per folder)

1. Runtime-protection excludes (ADR-016) — hardcoded, first, un-overridable.
2. `--filter='merge <filter_file>'` — the folder's central filter file (when configured).
3. `--filter='dir-merge /.pcswitcher-filter'` — activates per-directory files tree-wide.

### filter_file shape: PER-FOLDER

Each `FolderEntry` gets an optional `filter_file: str` field (replaces `excludes`). Default config: `/home` → `~/.config/pc-switcher/home.filter`, `/root` → `~/.config/pc-switcher/root.filter`. Unset/empty `filter_file` for a folder = no central merge rule for that folder (runtime excludes + dir-merge still apply).

### Missing filter_file: VALIDATION ERROR

When a folder's `filter_file` is set but the file does not exist at sync time, `validate()` MUST fail fast with a clear ValidationError (source-side check, consistent with the existing per-folder `test -d` existence check). Silently syncing everything on a `--delete` mirror is the dangerous failure mode this guards against.

Path expansion: `~` and env vars in `filter_file` must be expanded before the existence check and before passing to rsync's `merge` directive.

### init: SHIP STARTER FILTER FILES

`pc-switcher init` writes `config.yaml` AND the referenced starter filter files (`home.filter`, `root.filter`) into `~/.config/pc-switcher/`, pre-populated with the current machine-specific/cache rules translated to native rsync syntax. Works out-of-the-box; first sync never hits the missing-file validation error.

Starter filter files ship as package data alongside `default-config.yaml` (see `cli.py:484` `files("pcswitcher").joinpath(...)`). `init` reads each and writes it next to `config.yaml`. Honor `--force` for overwrite, same as config.yaml.

### dir-merge modifiers

Single `dir-merge /.pcswitcher-filter` rule, NO `e` modifier — the `.pcswitcher-filter` files themselves sync to the target as ordinary content (like a committed `.gitignore`). Default inherit-into-subdirs behavior (no `n` modifier).

### Safety: never enable rsync's built-in per-dir mechanisms

The rsync command MUST NEVER pass `-F`, `-FF`, or `-C`/`--cvs-exclude`. `-a` (`-rlptgoD`) includes none of them. Per-dir activation is ONLY via our explicit `dir-merge /.pcswitcher-filter`. Regression test required: plant a `.rsync-filter` with a hostile rule in a synced tree and assert the targeted file still transfers.

### The default ~/.cache re-include idiom (rsync ancestor descent)

To re-include `/.cache/uv/` while dropping the rest of `.cache`, rsync must be able to descend into `.cache` first. The central home.filter must include ancestor + leaf + trailing exclude:

```
+ /.cache/
+ /.cache/uv/***
+ /.cache/pip/***
- /.cache/*
```

pc-switcher passes rules to rsync verbatim — it does NOT auto-generate ancestor includes. Document this idiom for users.
</decisions>

<specifics>
## Specific Ideas

Central home.filter starter content (native rsync syntax) collapses today's `/home` excludes plus the new cache-keep idiom. Machine-specific excludes to carry over from default-config.yaml: `.ssh/id_*`, `.ssh/known_hosts`, `.config/tailscale`, `.cache/nvidia`, `.cache/mesa_shader_cache`, `.nv`, `.cache/fontconfig`, and the VS Code `.config/Code/{Cache,CachedData,GPUCache,Code Cache,CachedExtensionVSIXs}`. Add `+` keep-rules for dev caches `~/.cache/uv` and `~/.cache/pip` (issue also names `~/.npm` — note `.npm` is not under `.cache`, so it needs its own rule if kept; default: keep uv + pip as the documented example, others left to the user).

root.filter starter content: `.ssh/id_*`, `.ssh/known_hosts`, `.config/tailscale`.

`.pcswitcher-filter` is pc-switcher's own filename (NOT rsync's `.rsync-filter`).

## Interaction with --delete (D-06)

No `--delete-excluded` (unchanged). A re-included subtree becomes subject to `--delete` within itself, as intended. Verify an include doesn't accidentally re-expose something a prior exclude protected from deletion.
</specifics>

<canonical_refs>
## Canonical References

- GitHub issue #166 (full spec, acceptance criteria, docs requirements).
- ADR-016 (`docs/adr/adr-016-hardcoded-runtime-file-excludes.md`) — hardcoded runtime excludes stay first and un-overridable; MUST be updated to describe the new filter surface.
- `src/pcswitcher/jobs/folder_sync.py` — `FolderEntry`, `to_rsync_filter_args` (:57-64), `_build_rsync_cmd` (:254-357), `_runtime_exclude_filters` (:235-252), `CONFIG_SCHEMA` (:82-106), `_active_folders` (:132-142), `describe_first_sync_scope` (:144-166), `validate` (:168-233).
- `src/pcswitcher/schemas/config-schema.yaml:181-210` — folder_sync schema (drop `excludes`, add `filter_file`).
- `src/pcswitcher/default-config.yaml:114-177` — folder_sync block + header comment (collapse excludes into filter files).
- `src/pcswitcher/cli.py:461-491` — `init` command (ship starter filter files).
- `tests/unit/jobs/test_folder_sync.py`, `tests/unit/jobs/test_folder_sync_deletion_log.py` — existing tests.
- Docs: a "coming from .gitignore" guide (signs not `!`; first-match-wins; leading `/` to anchor; per-dir `.pcswitcher-filter` == `.gitignore`; glob edge cases incl. rsync `a/**/b` not matching `a/b`).
</canonical_refs>
