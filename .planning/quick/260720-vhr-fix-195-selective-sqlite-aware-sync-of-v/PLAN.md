---
type: quick-plan
quick_id: 260720-vhr
issue: 195
status: ready
---

# PLAN — Fix #195: Selective SQLite-aware sync of VS Code `state.vscdb`

Task decomposition for the LOCKED design in `CONTEXT.md`, using the integration facts in `RESEARCH.md`. The executor MUST read `CONTEXT.md` and `RESEARCH.md` (this directory) before starting, plus `docs/adr/adr-001-adr.md`, `docs/dev/development-guide.md`, and `docs/dev/testing-guide.md`. Do NOT re-open any locked decision.

## Scope and ground rules

Add a new toggleable `SyncJob` (`vscode_state_sync`) that mirrors each editor's global `state.vscdb` except for machine-bound `secret://` rows, which keep the target's own value. `folder_sync` hardcode-excludes those DBs so the mirror never clobbers them; the new job reconstructs each DB via a source-strip → transfer → target-inject → atomic-`mv` sequence, running as the normal user with no sudo. Config, schema, docs, and an ADR follow.

Every commit MUST leave the tree green. The green gate for this stacked PR is the unit + contract suite plus static checks (integration CI does not run on a non-`main` base — `CONTEXT.md` constraints): `uv run ruff format --check . && uv run ruff check . && uv run basedpyright && uv run codespell && uv run pytest tests/unit tests/contract`. Follow the plans-are-specs rule: the actions below give signatures, SQL shape (by reference), and behavior rules, not implementation bodies. Reuse the exact verified SQL sequences in `RESEARCH.md` §6 (Steps A/C) and the constant relpaths in `RESEARCH.md` §5 rather than inventing new ones.

## Verified findings that constrain the plan

- The live UI step total is computed dynamically as `8 + <enabled sync-job count> + 1` (`src/pcswitcher/orchestrator.py:278` and `:354`), and each enabled sync job is its own UI step (`orchestrator.py:1052`). Enabling `vscode_state_sync` by default raises the default enabled-job count from 1 to 2, so the default UI total goes from 10 to 11. This is a docs-only reconciliation (README narrative); no orchestrator/UI code change is required because the count is already `len(jobs)`-derived.
- ADR-016 is ALREADY superseded by ADR-017 (`docs/adr/_index.md:27`, `docs/adr/adr-016-…md:3`). The live "this is the ONLY hardcoded exclude" claim now lives in `docs/adr/adr-017-mirror-pcswitcher-install.md:13`. The new ADR therefore amends ADR-017, not ADR-016; leave the already-superseded ADR-016 untouched.
- Root `CLAUDE.md` does NOT enumerate sync jobs (its "CLI Commands" block lists subcommands, not jobs). No `CLAUDE.md` change is needed; record this as verified rather than editing it.
- `tests/unit/jobs/test_folder_sync.py::TestRuntimeExcludeFilters` (from line 532) asserts the EXACT `_runtime_exclude_filters` output against a class-level `_RELPATHS`, and its docstring/comment (lines 528, 535) claims these are the "ONLY hardcoded excludes". T2 changes that output, so this test and its comment MUST be updated in the same commit.
- `docs/configuration.md:5` states the config is copied to the target at "step 10 of the sync sequence", but config-copy is orchestrator Step 8 (`orchestrator.py:374`; README step 8). This is pre-existing drift unrelated to #195; correct it to Step 8 during the numbering reconciliation and note it is pre-existing.

## Commit order (each leaves the tree green)

1. T1 `feat(vscode_state_sync)`: new job module + merge-logic unit tests + contract entry.
2. T2 `feat(folder_sync)`: fold the editor-DB relpaths into the global-first non-overridable excludes + update exclude-emission tests.
3. T3 `feat(config)`: schema + default-config entries enabling the job after `folder_sync`.
4. T4 `docs`: ADR-018 + ADR-017 cross-reference + `_index.md` + `configuration.md` + README step-count reconciliation.
5. T5 (optional, non-blocking) `test(vscode_state_sync)`: integration coverage where practical.

T1 precedes T2 (T2 imports the constant T1 owns). T3 follows T1 and T2 so that enabling the job by default coincides with the DBs already being folder_sync-excluded and the module already existing (avoids double-handling and orchestrator discovery WARNINGs). T4 is docs-only and green regardless. Watch the import direction throughout: `vscode_state_sync.py` MUST NOT import from `folder_sync.py` (`RESEARCH.md` §5 — one-way constant ownership avoids a cycle).

## T1 — New job module `vscode_state_sync.py` with merge logic and tests

id: T1

description: Create the `VscodeStateSyncJob` module: the editor-DB relpath constants (owned here, imported by `folder_sync` in T2), the `SyncJob` subclass implementing validate/execute per the locked merge mechanics (`CONTEXT.md` §4), and pure SQL-builder helpers factored for direct unit testing. Register the job in the jobs package. Add unit tests for the merge semantics and the execute() orchestration, plus a contract-suite entry.

### Files

- `src/pcswitcher/jobs/vscode_state_sync.py` (new)
- `src/pcswitcher/jobs/__init__.py` (register `VscodeStateSyncJob`)
- `tests/unit/jobs/test_vscode_state_sync.py` (new)
- `tests/contract/test_job_interface.py` (add a `VscodeStateSyncJob` contract class)

### Actions

- Define `EDITOR_STATE_DB_RELPATHS: tuple[str, ...]` = the four home-relative main-DB relpaths (`Code`, `Antigravity`, `Cursor`, `VSCodium`, each `.config/<Editor>/User/globalStorage/state.vscdb`) exactly as enumerated in `RESEARCH.md` §5. Derive `EDITOR_STATE_EXCLUDE_RELPATHS: tuple[str, ...]` from it as each main DB plus its `.backup` sidecar (eight entries, main-then-backup order). `EDITOR_STATE_EXCLUDE_RELPATHS` is the constant `folder_sync` imports in T2; keep a single source of truth so the exclude set and the merge set cannot drift. Note in a comment that `Cursor`/`VSCodium` directory casing is `[ASSUMED]` per `RESEARCH.md` §5/Assumption A1 (`Code`/`Antigravity` confirmed on-disk).
- Define `class VscodeStateSyncJob(SyncJob)` with `name: ClassVar[str] = "vscode_state_sync"` (MUST equal the module filename and the `sync_jobs` key — `RESEARCH.md` §4) and a draft-07 `CONFIG_SCHEMA: ClassVar[dict[str, Any]]` of `{type: object, properties: {preserve_key_globs: {type: array, items: {type: string}, default: ["secret://%"]}}, additionalProperties: false}` mirroring the file-schema block in T3 (`RESEARCH.md` §7). Read `preserve_key_globs` in `__init__` via `self.context.config.get("preserve_key_globs", ["secret://%"])` (the `dummy_success.py:52` `.get` default pattern).
- Implement `async def validate(self) -> list[ValidationError]`: check `sqlite3` is available on BOTH hosts (`command -v sqlite3` via `self.source.run_command(...)` and `self.target.run_command(..., login_shell=False)`; `.success` gate). Emit one `self._validation_error(host, message)` per missing host (host = `Host.SOURCE`/`Host.TARGET`); return `[]` when both present. No btrfs dependency (`CONTEXT.md` decision 7).
- Implement `async def execute(self) -> None` per the locked mechanics (`CONTEXT.md` §4, verified sequence in `RESEARCH.md` §6). Iterate `EDITOR_STATE_DB_RELPATHS`; resolve the source path from local `Path.home()`; resolve the target user's home the same way `FolderSyncJob` does (via `target_username`, fallback `getpass.getuser()` — `RESEARCH.md` §1; do NOT hardcode `/home/<user>`; single-user fleet assumption per ADR-017). For each editor: skip when the source DB is absent (source-side `test`/`Path.exists`); otherwise (a) source-strip — copy the live source DB to a source-local temp and delete preserve-matched rows using RESEARCH §6 Step A's SQL; (b) transfer the neutral temp DB to a target temp path that sits in the SAME directory as the target's live DB (`await self.target.send_file(Path(local_tmp), remote_tmp)` — `RESEARCH.md` §3a) so the later `mv` is atomic and ownership/perms are preserved; (c) if the target live DB exists (`test -f`, `login_shell=False`), run RESEARCH §6 Step C's ATTACH+INSERT to copy preserve-matched rows from the target's live DB into the neutral DB, else skip the inject (first-sync / absent-DB path, `CONTEXT.md` §4d; the neutral DB carries no secret rows); then `mv -f <remote_tmp> <target_live>` on the target. Clean up the source temp. Drive `sqlite3` through the executors on both ends (CLI, not stdlib) so BLOBs copy binary-safe with zero shell-escaping and every side is mock-assertable (`RESEARCH.md` §6). Emit progress via `self._report_progress`, ending with `ProgressUpdate(percent=100)`.
- Factor the SQL as pure string builders so semantics are unit-testable without executors: a `_where_clause(globs: list[str]) -> str` returning `key LIKE '<g1>' OR key LIKE '<g2>' …`, a source-strip `DELETE FROM ItemTable WHERE <where>;`, and a target-inject `ATTACH '<target_live>' AS live; INSERT INTO ItemTable SELECT key, value FROM live.ItemTable WHERE <where>;`. Escape single quotes inside every SQL string literal (SQLite doubles `'`), including glob values and the ATTACH path; then `shlex.quote` the whole SQL argument and every filesystem path passed to `cp`/`mv`/`test`/`sqlite3` at the shell layer (`folder_sync` quotes all config-derived values — `RESEARCH.md` §3). No `OR REPLACE` is needed on the INSERT (`RESEARCH.md` §6 rationale).
- Honor dry-run (`CONTEXT.md` §4f, ADR-014 via `RESEARCH.md` §9): when `self.context.dry_run`, iterate editors and read-only detect source/target DB presence, LOG intended actions per editor (which editors would sync, first-sync vs merge, which globs are preserved) with a `[dry-run] ` prefix, and perform ZERO target writes — no `send_file`, no target `sqlite3` inject, no `mv` — and mutate neither machine's live DB.
- OPTIONAL (executor discretion, not an acceptance gate): override `describe_first_sync_scope(cls, config)` to enumerate the editor DBs it overwrites and a `"sqlite merge + atomic mv"` mechanism, for parity with `FolderSyncJob` (`RESEARCH.md` §1, Assumption A2). Omitting it only means the orchestrator's first-sync warning won't list these DBs; correctness is unaffected.
- Register `VscodeStateSyncJob` in `src/pcswitcher/jobs/__init__.py` (import + `__all__`) alongside `FolderSyncJob`, keeping the no-back-import rule intact.
- Tests — merge semantics (build real temp `ItemTable` DBs with stdlib `sqlite3` in `tmp_path`, execute the pure SQL builders in-process, assert rows; `RESEARCH.md` §8): source-strip removes only preserve-matched rows; target-inject preserves the TARGET's value for matched keys (including a target-only secret) while non-matched keys take the source's value and target-only non-matched keys are dropped (reproduce the verified end-to-end table in `RESEARCH.md` §6); multi-pattern `preserve_key_globs` (e.g. `secret://%` + `vscode.auth://%`) preserves both families simultaneously; a glob (or path) containing a single quote is escaped and does not corrupt the SQL. Tests — execute() orchestration (mock executors via a `make_context`-style factory, `RESEARCH.md` §8): first-sync/absent-target-DB path skips the ATTACH inject and still places the neutral DB; a source editor with no DB is skipped; dry-run performs no `send_file`, no target inject, and no `mv`, and does not mutate either live DB; the neutral DB is transferred to a temp path in the live DB's directory and `mv -f` targets the live DB.
- Contract entry: add a `TestVscodeStateSyncJobContract` class in `tests/contract/test_job_interface.py` mirroring `TestFolderSyncJobContract` (lines 258-298) — assert `name == "vscode_state_sync"`, `CONFIG_SCHEMA` is a dict, and `validate()` returns a list (stub `source`/`target` `run_command` as `AsyncMock` returning a success `CommandResult`, as that class does).

### Acceptance criteria

- `uv run pytest tests/unit/jobs/test_vscode_state_sync.py tests/contract/test_job_interface.py -q` passes, covering every semantic and orchestration case listed above.
- `VscodeStateSyncJob.name == "vscode_state_sync"`; module filename, class `name`, and (in T3) the `sync_jobs` key all match so `_resolve_sync_job_class` resolves it (`RESEARCH.md` §4).
- `EDITOR_STATE_EXCLUDE_RELPATHS` has eight entries (main + `.backup` for four editors) and is importable from `pcswitcher.jobs.vscode_state_sync`; `vscode_state_sync.py` imports nothing from `folder_sync.py`.
- Full gate green: `uv run ruff format --check . && uv run ruff check . && uv run basedpyright && uv run codespell && uv run pytest tests/unit tests/contract`.

### Commit

`feat(vscode_state_sync): SQLite-aware selective sync of VS Code state.vscdb`

## T2 — `folder_sync` hardcode-excludes the editor state DBs

id: T2

description: Import `EDITOR_STATE_EXCLUDE_RELPATHS` from the vscode module and fold it into the same GLOBAL-FIRST, non-overridable exclude tier as `_RUNTIME_EXCLUDE_RELPATHS`, so a user `+` rule can never re-expose the editor DBs and the user cannot forget to exclude them (`CONTEXT.md` decision 2, `RESEARCH.md` §5). Update the exclude-emission tests and the module's explanatory comments.

### Files

- `src/pcswitcher/jobs/folder_sync.py`
- `tests/unit/jobs/test_folder_sync.py`

### Actions

- Add a top-level import of `EDITOR_STATE_EXCLUDE_RELPATHS` from `pcswitcher.jobs.vscode_state_sync` to `folder_sync.py`.
- In `_runtime_exclude_filters` (staticmethod, `folder_sync.py:300-317`), iterate `(*_RUNTIME_EXCLUDE_RELPATHS, *EDITOR_STATE_EXCLUDE_RELPATHS)` so each editor relpath emits the same root-anchored `--filter=- /<home-rel>/…` form and stays spliced FIRST in `_build_rsync_cmd` (before the central `merge` and the `dir-merge`, `folder_sync.py:447` ahead of `449-452`). Runtime excludes first, then editor-DB excludes; preserve that order.
- Update the module docstring/comments where the exclude tier is documented (the GLOBAL-FIRST comment block near `folder_sync.py:440-446`) to state that the editor state DBs (`state.vscdb` and `state.vscdb.backup` for the covered editors) are now part of this non-overridable tier and WHY — they are handed to `vscode_state_sync`, which merges them selectively so machine-bound `secret://` rows are never clobbered — referencing ADR-018 (created in T4). Where existing comments cite ADR-016 for the runtime excludes, update the reference to the current ADRs (ADR-017 for the runtime-state exclude, ADR-018 for the editor DBs), since ADR-016 is superseded.
- Update `TestRuntimeExcludeFilters` (`tests/unit/jobs/test_folder_sync.py:532`): extend its expected relpath set so the asserted `_runtime_exclude_filters` output includes all eight editor-DB relpaths in the anchored form and correct order (runtime excludes then editor DBs), and update the class docstring/comment (lines 528, 535) that claims these are the "ONLY hardcoded excludes" to reflect the added editor DBs. Add a focused assertion that, for each covered editor, both `state.vscdb` and `state.vscdb.backup` are emitted and precede the central `merge` filter in `_build_rsync_cmd`.
- Re-check the GLOBAL-FIRST ordering tests (`test_folder_sync.py:440-465`, `563-569`) still hold with the larger exclude list and adjust expectations if they pin an exact count or index.

### Acceptance criteria

- `_runtime_exclude_filters(Path("/home"))` (with home under `/home`) returns the runtime exclude followed by the eight editor-DB excludes, each as `--filter=- /<user>/.config/<Editor>/User/globalStorage/state.vscdb[.backup]`, and all precede the `merge`/`dir-merge` filters in `_build_rsync_cmd`.
- No editor-DB exclude can be re-exposed by a user `+` rule (it is emitted before both filter surfaces) — asserted by the ordering test.
- `uv run pytest tests/unit/jobs/test_folder_sync.py -q` passes; full gate green (`ruff` format+check, `basedpyright`, `codespell`, `pytest tests/unit tests/contract`).

### Commit

`feat(folder_sync): hardcode-exclude editor state.vscdb from the mirror`

## T3 — Config: enable the job by default with `preserve_key_globs`

id: T3

description: Add the `vscode_state_sync` toggle and its config block to both the JSON Schema and the shipped default config, ordered so the job runs AFTER `folder_sync` and so the default config validates (`RESEARCH.md` §7, `CONTEXT.md` decisions 1/6).

### Files

- `src/pcswitcher/schemas/config-schema.yaml`
- `src/pcswitcher/default-config.yaml`

### Actions

- In `config-schema.yaml`, under `sync_jobs.properties` (after `folder_sync`, near line 67-70), add a `vscode_state_sync` boolean with `default: true` and a one-line description; the `sync_jobs.additionalProperties: false` gate (line 87) otherwise rejects the key. Add a top-level `vscode_state_sync` object block (sibling of `folder_sync:` at line 183) with `properties.preserve_key_globs` = array of strings, `default: ["secret://%"]`, and `additionalProperties: false`; the top-level `additionalProperties: false` gate (line 233) otherwise rejects the config block. Use the exact shapes in `RESEARCH.md` §7.
- In `default-config.yaml`, add `vscode_state_sync: true` under `sync_jobs` immediately AFTER `folder_sync` (line 46) — dict insertion order determines execution order, so it MUST follow `folder_sync` (`RESEARCH.md` §4). Add a top-level `vscode_state_sync:` block in the job-config section with `preserve_key_globs: ["secret://%"]` and a short annotating comment (what it preserves and that matches keep the target's own value). Keep the file's comment style; if the existing exclude-tier comment mentions ADR-016, update it to the current ADRs.
- Verify no test enumerates the default `sync_jobs` set in a way the new key breaks (`tests/unit/orchestrator/test_config_system.py`); update any such expectation.

### Acceptance criteria

- `Configuration.from_yaml(<shipped default-config.yaml>)` loads without a schema error and yields `sync_jobs["vscode_state_sync"] is True` with `folder_sync` ordered before it, and `job_configs["vscode_state_sync"]["preserve_key_globs"] == ["secret://%"]`. If no test already exercises the shipped default config against the schema, add a small unit test that does.
- `VscodeStateSyncJob.validate_config({"preserve_key_globs": ["secret://%"]}) == []` and a wrong-typed value (e.g. `preserve_key_globs: "secret://%"`) produces a `ConfigError` (the `CONFIG_SCHEMA` guard from T1).
- Full gate green (`ruff` format+check, `basedpyright`, `codespell`, `pytest tests/unit tests/contract`).

### Commit

`feat(config): enable vscode_state_sync by default with preserve_key_globs`

## T4 — Docs: ADR-018, ADR-017 cross-reference, index, configuration, step numbering

id: T4

description: Document the decision as ADR-018, amend ADR-017's "ONLY hardcoded exclude" claim via a two-way cross-reference, update the ADR index, extend `configuration.md`, and reconcile the sync-step numbering that the new default job changes. The executor MUST read `docs/adr/adr-001-adr.md` first for ADR conventions and the required document structure.

### Files

- `docs/adr/adr-018-<slug>.md` (new; next number is 018 — highest current is ADR-017)
- `docs/adr/adr-017-mirror-pcswitcher-install.md` (add cross-reference note only)
- `docs/adr/_index.md`
- `docs/configuration.md`
- `README.md`

### Actions

- Create ADR-018 following the ADR-001 template (`Status`, `Date: 2026-07-20`, `TL;DR`, `Implementation Rules`, `Context`, `Decision`, `Consequences`, `References`). Content: SQLite-aware selective sync of editor global `state.vscdb` — `folder_sync` hardcode-excludes the editor state DBs (owned by `vscode_state_sync`) into the same global-first non-overridable tier, and `vscode_state_sync` rebuilds each DB by mirroring all keys except `preserve_key_globs` matches (default `secret://%`), which keep the target's own machine-bound value; the merge runs as the normal user via source-strip → SFTP transfer → target ATTACH+INSERT → atomic `mv`; rejected alternatives (filter-file injection into the user's config tree; a btrfs-snapshot dependency) per `CONTEXT.md` decision 2. Explicitly state that this EXTENDS ADR-017's "ONLY hardcoded exclude" claim: as of ADR-018 the hardcoded-exclude tier contains (a) `.local/share/pc-switcher/` (ADR-017) and (b) the editor state DBs (this ADR).
- Add a two-way cross-reference between ADR-017 and ADR-018. Recommended mechanism (executor confirms against ADR-001): a plain cross-reference note rather than a `Supersedes`/`Superseded by` status, because ADR-017's core decision (mirror the install, exclude only runtime state) still stands and is not replaced — only its "ONLY" exclude claim is amended. In ADR-017 add a short "Amended by ADR-018 (hardcoded-exclude set extended to the editor state DBs)" pointer near the top (this cross-reference is the sanctioned exception to ADR immutability, mirroring the ADR-016→017 supersession pointers); in ADR-018 add the reciprocal "Amends ADR-017" reference. Leave the already-superseded ADR-016 untouched.
- Update `docs/adr/_index.md`: add ADR-018 to "Active Decisions"; note the ADR-017↔018 amendment relationship (ADR-017 stays Active); bump the "Last updated" date to 2026-07-20.
- Update `docs/configuration.md`: add `vscode_state_sync: true` to the `sync_jobs` example (lines 55-60, after `folder_sync`); add a new `## vscode_state_sync` section documenting the job and `preserve_key_globs` (SQLite `LIKE` patterns, default `["secret://%"]`, matched keys keep the TARGET's value so machine-bound secrets stay local; covered editors and skip-when-absent; both `state.vscdb` and `state.vscdb.backup` are folder_sync-excluded; runs after `folder_sync`; no sudo). Also correct the pre-existing drift at line 5: config-copy is Step 8, not "step 10" (verify against the orchestrator `# Step` comments; note in the commit body it is pre-existing).
- Reconcile the step-count narrative in `README.md` (lines ~80, 94-97): with `vscode_state_sync` enabled by default the sync-jobs phase now expands to two UI steps (`folder_sync` then `vscode_state_sync`), so the live `Step N/total` total is `8 + <enabled sync jobs> + 1` = 11 by default, not 10; post-sync snapshots become Step 11. Rewrite the prose so it no longer hardcodes a fixed total of ten UI steps, explains that the sync-jobs phase (logical Step 9) produces one UI step per enabled job, and mentions `vscode_state_sync` runs immediately after `folder_sync`. Keep the logical `# Step 9`/`# Step 10` code comments as phase labels (no code change) and verify the "matching `# Step N` comments" statement stays coherent (logical phases 1-10; UI numbering dynamic).
- Confirm (and state in the commit body) that root `CLAUDE.md` needs no change — it does not enumerate sync jobs.

### Acceptance criteria

- ADR-018 exists, follows the ADR-001 structure, and explicitly amends the "ONLY hardcoded exclude" claim; ADR-017 and ADR-018 carry reciprocal cross-references; ADR-016 is unchanged; `_index.md` lists ADR-018 as Active with the amendment noted and an updated date.
- `docs/configuration.md` documents the new job and `preserve_key_globs`, lists it in the `sync_jobs` example after `folder_sync`, and no longer misstates the config-copy step number.
- `README.md` step narrative reflects a default UI total of 11 with the sync-jobs phase expanding per enabled job and `vscode_state_sync` after `folder_sync`; no residual claim of a fixed ten-step UI total.
- `uv run codespell` passes (add project-specific tokens to the codespell ignore config only if it flags legitimate identifiers such as `vscdb`); docs are otherwise unaffected by the test gate.

### Commit

`docs(vscode_state_sync): ADR-018, config + step-count reconciliation`

## T5 — Integration coverage (optional, non-blocking)

id: T5

description: Add VM integration coverage for the merge where practical. Non-blocking: integration CI does NOT run on this stacked PR (base is not `main`, `CONTEXT.md` constraints), and the unit + contract suite is the green gate. Include this only if the VM fixtures are readily available; otherwise defer.

### Files

- `tests/integration/test_vscode_state_sync.py` (new, optional)

### Actions

- Following `RESEARCH.md` §8 and `docs/dev/testing-guide.md`, seed a `state.vscdb` with `secret://` and non-secret rows on both VMs (`pc1_executor`/`pc2_executor`), run the job (or its target-inject step) end to end, and assert the target keeps its own `secret://` value while non-secret keys mirror the source and target-only non-secret keys are dropped. Use unique artifact paths and clean up in `try/finally` (testing-guide `try/finally` rule).

### Acceptance criteria

- If implemented, `tests/run-integration-tests.sh tests/integration/test_vscode_state_sync.py` passes on the VM infrastructure and cleans up its artifacts. If deferred, record the deferral in the PR description; the PR still merges on the unit + contract gate.

### Commit

`test(vscode_state_sync): VM integration coverage for selective merge`

## Out of scope

Workspace-trust loss (VS Code version skew) — the other half of #190 — is explicitly excluded (`CONTEXT.md` Problem). Per-workspace `workspaceStorage/*/state.vscdb` are not touched (no `secret://` keys there). Do not rebase this branch onto `main`; open the PR as a DRAFT (`CONTEXT.md` constraints).
