---
type: quick-summary
quick_id: 260720-vhr
issue: 195
status: complete
date: 2026-07-20
pr: 196
---

# SUMMARY — Fix #195: Selective SQLite-aware sync of VS Code `state.vscdb`

New toggleable `vscode_state_sync` `SyncJob` (default on, runs after `folder_sync`) that mirrors each editor's global `state.vscdb` except machine-bound `secret://` keys, which keep the target's own keyring-decryptable value — so auth-backed extensions no longer re-login after every sync. `folder_sync` hardcode-excludes the editor DBs (owned constant, one-way import) so the file-granular mirror never clobbers them. Covers Code, Antigravity, Cursor, VSCodium. Preserve pattern is configurable (`preserve_key_globs`, default `["secret://%"]`). Runs as the normal user (no sudo): source-strip → SFTP transfer → target `ATTACH`+`INSERT` → atomic `mv`. Documented as ADR-018 (amends ADR-017's "only hardcoded exclude" claim).

## Commits

| Hash | Subject |
|------|---------|
| `e9f6142` | feat(vscode_state_sync): SQLite-aware selective sync of VS Code state.vscdb |
| `11d8871` | feat(folder_sync): hardcode-exclude editor state.vscdb from the mirror |
| `3482f7d` | feat(config): enable vscode_state_sync by default with preserve_key_globs |
| `0fbdbcd` | docs(vscode_state_sync): ADR-018, config + step-count reconciliation |
| `ed7751b` | fix(vscode_state_sync): mkdir -p target dir before SFTP put |

## Verification

Full gate green: `ruff format --check`, `ruff check`, `basedpyright` (0/0/0), `codespell`, `pytest tests/unit tests/contract` → 654 passed (+30 new). PR #196 (draft, base `main`) checks green: Lint, Unit Tests, CI Status pass; Integration correctly skipping on draft.

## Deferred

T5 (two-VM integration coverage) — VM fixtures not available in this environment. The unit + contract suite is the green gate. Marked in PLAN.md as optional/non-blocking.

## Review note (added beyond plan)

Added a defensive `mkdir -p` of the target's `globalStorage` dir before `send_file`: jobs are independently toggleable, so `folder_sync: false` + this job on a fresh target would otherwise leave the SFTP put with no parent directory.
