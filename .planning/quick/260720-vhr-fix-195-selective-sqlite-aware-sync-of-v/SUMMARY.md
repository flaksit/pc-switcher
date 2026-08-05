---
type: quick-summary
quick_id: 260720-vhr
issue: 195
status: complete
date: 2026-07-20
pr: 196
---

# SUMMARY — Fix #195: Selective SQLite-aware sync of VS Code `state.vscdb`

New toggleable `vscode_state_sync` `SyncJob` (default on, runs after `folder_sync`) that mirrors each editor's global `state.vscdb` except machine-bound `secret://` keys, which keep the target's own keyring-decryptable value — so auth-backed extensions no longer re-login after every sync. `vscode_state_sync` owns which absolute paths are the editor DBs and exposes them via `vscode_state_exclude_paths()`; `folder_sync` imports it one-way and only translates each into an rsync filter, holding no VS Code/home knowledge of its own. Covers Code, Antigravity, Cursor, VSCodium — and, per file, both `state.vscdb` and its `.backup` sidecar (exclude set == merge set, one `VSCODE_STATE_HANDLED_RELPATHS` tuple). The preserved-key pattern (`secret://%`, VS Code's SecretStorage namespace) is hardcoded in the module (`PRESERVE_KEY_GLOBS`), not user-configurable: the module owns every VS-Code-specific fact (editor list, DB layout, preserved keys), so the job is off-the-shelf with no config. Runs as the normal user (no sudo): source-strip → SFTP transfer → target `ATTACH`+`INSERT` → atomic `mv`. Documented as ADR-018 (amends ADR-017's "only hardcoded exclude" claim); the same-uid/same-path fleet assumption it relies on is ADR-019.

## Commits

| Hash | Subject |
|------|---------|
| `e9f6142` | feat(vscode_state_sync): SQLite-aware selective sync of VS Code state.vscdb |
| `11d8871` | feat(folder_sync): hardcode-exclude editor state.vscdb from the mirror |
| `3482f7d` | feat(config): enable vscode_state_sync by default with preserve_key_globs |
| `0fbdbcd` | docs(vscode_state_sync): ADR-018, config + step-count reconciliation |
| `ed7751b` | fix(vscode_state_sync): mkdir -p target dir before SFTP put |
| `fcf09ed` | docs(planning): quick-task artifacts for #195 vscode_state_sync |
| `f26ec60` | refactor(vscode_state_sync): own exclude paths via function; folder_sync just translates |
| `3390616` | fix(vscode_state_sync): merge state.vscdb.backup too — exclude set == merge set |
| `69b7c42` | docs(readme): simplify user-facing prose to concise phrasing |
| `9469b3c` | refactor(vscode_state_sync): Path-typed exclude paths; ADR-018 and wording cleanup |
| `7fb4e8a` | docs(debug): VS Code state-loss-on-sync bug analysis |
| `0a87e3e` | remove false negative consequence |
| `e23583b` | docs(adr): ADR-019 homogeneous fleet — matching real users and paths |
| `c38aece` | refactor(vscode_state_sync): drop _resolve_homes user-mapping; rename to VS Code terms |
| `241cce8` | docs: use VS Code terminology for the state.vscdb selective sync |
| `d9c6478` | refactor(vscode_state_sync): hardcode preserved-key pattern, drop config |

## Follow-up refinements (post initial delivery)

The task grew past its first five commits with structural and correctness refinements:
- `folder_sync` no longer hardcodes the DB paths; `vscode_state_sync` owns them and exposes `vscode_state_exclude_paths()`, imported one-way — folder_sync just translates to filters (`f26ec60`, `9469b3c`).
- The `.backup` sidecar is merged too, not only excluded, so the exclude set and merge set are identical (`3390616`).
- Dropped the `_resolve_homes` user/path mapping in favour of the same-uid/same-path fleet assumption, captured as ADR-019; terminology narrowed to "VS Code-based editors" throughout (`e23583b`, `c38aece`, `241cce8`).
- Preserved-key pattern made non-configurable — hardcoded `PRESERVE_KEY_GLOBS`, dropping the job's `CONFIG_SCHEMA` and the `vscode_state_sync` config section; consistent with the already-hardcoded editor list and DB layout (`d9c6478`).

## Verification

Full gate green: `ruff format --check`, `ruff check`, `basedpyright` (0/0/0), `pytest tests/unit tests/contract` → 656 passed. PR #196 (draft, base `main`).

## Deferred

T5 (two-VM integration coverage) — VM fixtures not available in this environment. The unit + contract suite is the green gate. Marked in PLAN.md as optional/non-blocking.

## Review note (added beyond plan)

Added a defensive `mkdir -p` of the target's `globalStorage` dir before `send_file`: jobs are independently toggleable, so `folder_sync: false` + this job on a fresh target would otherwise leave the SFTP put with no parent directory.
