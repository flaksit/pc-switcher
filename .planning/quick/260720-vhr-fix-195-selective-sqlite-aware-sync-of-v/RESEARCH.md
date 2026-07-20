# RESEARCH — Fix #195: Codebase integration for `vscode_state_sync`

**Researched:** 2026-07-20

**Scope:** Implementation-integration facts only. The design in `CONTEXT.md` is LOCKED; nothing here re-opens it. Every claim below is cited to `file:line` in `/home/janfr/dev/pc-195` as read this session, or marked `VERIFIED:` where a command was run.

## 1. SyncJob contract (what a new job must implement)

`SyncJob` lives in `src/pcswitcher/jobs/base.py:164` and subclasses `Job` (`base.py:22`). A new `VscodeStateSyncJob(SyncJob)` must provide:

- `name: ClassVar[str]` — MUST equal the module name. Set `name = "vscode_state_sync"` so `src/pcswitcher/jobs/vscode_state_sync.py` resolves (orchestrator convention, see §4). Declared abstract-by-convention at `base.py:32`.
- `CONFIG_SCHEMA: ClassVar[dict[str, Any]]` — JSON Schema (draft-07) for this job's config block; default `{}` at `base.py:34`. Validated by the inherited `validate_config` classmethod (`base.py:54-81`) via `jsonschema.Draft7Validator`. If empty it short-circuits to `[]` (`base.py:65`).
- `async def validate(self) -> list[ValidationError]` — abstract, `base.py:83-93`. Called before any writes. Return `[]` on success. For #195: check `sqlite3` present on source and target (design point 7).
- `async def execute(self) -> None` — abstract, `base.py:95-103`. Raising any exception halts the sync with a CRITICAL log (handled by orchestrator, see §9 / `orchestrator.py:1079-1099`).
- `required: ClassVar[bool] = False` — inherited from `SyncJob` (`base.py:170`); leave as-is (it is an optional, toggleable job).
- `describe_first_sync_scope(cls, config) -> FirstSyncScope | None` — OPTIONAL override, `base.py:172-195`. Default returns `None`. This job DOES destructively replace target state (it `mv`s a rebuilt DB over the target's live DB), so per ADR-015 it MAY override this to name its scope (the editor state DBs it will overwrite) and mechanism (e.g. `"sqlite merge + atomic mv"`). `FolderSyncJob` is the reference impl (`folder_sync.py:171-193`). `FirstSyncScope` is a frozen dataclass `(job_name: str, scope_items: list[str], mechanism: str)` at `models.py:154-166`. Not strictly required for the job to function — decide during planning; if omitted, the orchestrator's first-sync warning simply won't enumerate this job's DBs.

Helper methods inherited from `Job`:

- `self._log(host, level, message, **extra)` — `base.py:105-124`. `host` is `Host.SOURCE` / `Host.TARGET` (`models.py:30`); `level` is a `LogLevel` (IntEnum, `models.py:37`, values: DEBUG/FULL/INFO/WARNING/ERROR/CRITICAL). `**extra` becomes structured logging context; job name + host are injected automatically. Example usage: `folder_sync.py:590-595` passes `session_id=self.context.session_id`.
- `self._report_progress(update: ProgressUpdate)` — `base.py:126-140`. `ProgressUpdate` at `models.py:66` has `percent/current/total/item`. Publishes a `ProgressEvent` on the event bus. A final `ProgressUpdate(percent=100)` marks the job complete (`folder_sync.py:638-639`).
- `self._validation_error(host, message) -> ValidationError` — `base.py:142-152`. Fills `job=self.name` automatically. Use inside `validate()`.
- Convenience properties: `self.source -> LocalExecutor` (`base.py:44-47`) and `self.target -> RemoteExecutor` (`base.py:49-52`). Same objects as `self.context.source` / `self.context.target`.

`JobContext` (frozen dataclass, `src/pcswitcher/jobs/context.py:14-33`) exposes to the job:

- `config: dict[str, Any]` — this job's own config block (already schema-validated), routed by name (see §7).
- `source: LocalExecutor`, `target: RemoteExecutor`.
- `event_bus: EventBus`, `session_id: str`, `source_hostname: str`, `target_hostname: str`.
- `dry_run: bool = False` (`context.py:25`) — honor per ADR-014 (§9).
- `allow_first_sync: bool = False`, `confirmer: Confirmer | None = None`, `target_username: str | None = None` (`context.py:26-33`). `target_username` falls back to `getpass.getuser()` when `None` (pattern at `folder_sync.py:335`). There is NO `source_username` field.

Canonical simple job shape: `src/pcswitcher/jobs/dummy_success.py` (`name` + `CONFIG_SCHEMA` + `validate` returning `[]` + `execute`; `__init__` reads config via `context.config.get(...)` at `dummy_success.py:52-53`). Canonical real job: `folder_sync.py`.

Register the new class in `src/pcswitcher/jobs/__init__.py` (add import + `__all__` entry alongside `FolderSyncJob`) for consistency, though the orchestrator imports it dynamically by module name regardless (see §4).

## 2. Executor API (exact methods and return type)

`CommandResult` is a dataclass `(exit_code: int, stdout: str, stderr: str)` with a computed `success` property `== (exit_code == 0)` — `models.py:55-63` (`success` at `models.py:60-62`). All `run_command` calls return this.

`LocalExecutor` (source, `executor.py:148`):

- `async run_command(cmd: str, timeout: float | None = None) -> CommandResult` — `executor.py:154-186`. Runs via `asyncio.create_subprocess_shell` (so `cmd` is a shell string; quote args yourself with `shlex`). No `login_shell` param.
- `async start_process(cmd: str) -> LocalProcess` — `executor.py:188-203`. Streaming; `LocalProcess.stdout()`/`stderr()` async line iterators, `wait()`/`wait_result()` return `CommandResult` (`executor.py:71-145`).
- NO `send_file`/`get_file` — the source is local; write temp files directly (e.g. Python `tempfile`/`shutil`, or `run_command("cp ...")`).

`RemoteExecutor` (target, `executor.py:260`):

- `async run_command(cmd: str, timeout: float | None = None, login_shell: bool | None = None) -> CommandResult` — `executor.py:288-325`. Runs via the asyncssh connection's `.run(cmd)`. `login_shell=True` wraps in `bash -l -c <shlex.quote(cmd)>` to source `~/.profile`/PATH (`executor.py:268-286`); default is `False` (`executor.py:266`). `folder_sync` calls target system commands with `login_shell=False` explicitly (e.g. `folder_sync.py:221, 242, 262, 388`). For #195, `sqlite3` and `mv`/`test` live in `/usr/bin` (on default PATH), so use `login_shell=False` (default) — matches the folder_sync pattern; no need for profile sourcing.
- `async start_process(cmd, login_shell=None) -> RemoteProcess` — `executor.py:327-349`.
- `async send_file(local: Path, remote: str) -> None` — `executor.py:362-370`. SFTP `put` over the existing connection. Runs as the connecting NORMAL user (no sudo). This is the transfer primitive for the neutral DB (see §3).
- `async get_file(remote: str, local: Path) -> None` — `executor.py:372-380`. SFTP `get`.
- File existence check: no dedicated method — use `run_command("test -f <path>", login_shell=False)` and check `.success` (folder_sync uses `test -d`/`test -f` this way, `folder_sync.py:275, 289`).

## 3. Transferring a single file source→target as the NORMAL user

The design (CONTEXT §4b) transfers a small neutral SQLite DB (few MB) from source to a target temp path, no root. Three mechanisms available — reporting all, planner picks:

(a) **asyncssh SFTP via `RemoteExecutor.send_file` — RECOMMENDED, already wired.** `executor.py:362-370` calls `self._conn.start_sftp_client()` then `sftp.put(str(local), remote)`. `RemoteExecutor` holds the live connection (`executor.py:263`). Runs as the SSH-connecting user (the invoking normal user) — exactly the no-sudo requirement in CONTEXT §4e. The underlying `Connection.start_sftp_client` is at `connection.py:141-152`. The mock executor fixture already stubs `send_file`/`get_file` as `AsyncMock` (`tests/unit/conftest.py:41-42`), so unit tests can assert the call. This is the cleanest option: one `await self.target.send_file(Path(local_neutral), remote_tmp)`.

(b) **rsync-over-ssh like `folder_sync._transport_args` WITHOUT elevation.** `folder_sync.py:319-360` builds `-e <ssh …>` + `--rsync-path='sudo rsync'`. Dropping the `--rsync-path='sudo rsync'` token (line 360) and running plain `rsync` (not `sudo -E rsync`, line 420) would transfer as the normal user. This is heavier than needed for a single small file and re-implements SSH-credential plumbing (`-l/-i/-F/-o UserKnownHostsFile`, `folder_sync.py:334-358`) that `send_file` already gets for free from the shared connection. Not recommended for one small binary file; noted for completeness.

(c) **base64 heredoc over `run_command`.** Read source bytes, `base64`-encode, and `run_command("base64 -d > <remote_tmp> <<'EOF'\n…\nEOF", login_shell=False)`. Works and is sudo-free, but wasteful (33% inflation, whole payload in one shell arg/stdin) and fragile vs. SFTP. Not recommended.

**Atomic replace:** after target-side inject, `mv` the temp DB over the live DB. To preserve ownership/perms and stay atomic, the temp file MUST sit in the SAME directory (same filesystem) as the live DB, e.g. transfer to `<globalStorage>/state.vscdb.pcswitcher-tmp` then `mv -f <tmp> <globalStorage>/state.vscdb` on the target via `run_command(login_shell=False)`. CONTEXT §4e confirms same-dir `mv` preserves ownership/perms. `shlex.quote` all paths (folder_sync quotes every config-derived value, `folder_sync.py:274, 288`).

## 4. Job discovery & ordering (orchestrator)

`_discover_and_validate_jobs` — `orchestrator.py:825-899`. Iterates `self._config.sync_jobs.items()` IN CONFIG ORDER (`orchestrator.py:861`). `sync_jobs` is a `dict[str, bool]` (`config.py:62`) preserving YAML insertion order. For each enabled job it calls `_resolve_sync_job_class(job_name)` (`orchestrator.py:870`), then `validate_config` (`orchestrator.py:876`), builds a `JobContext` with that job's config block (`orchestrator.py:880`), and appends the instance (`orchestrator.py:881`). Execution is sequential in list order (`orchestrator.py:1049 for job_index, job in enumerate(jobs)`).

`_resolve_sync_job_class` — `orchestrator.py:566-605`. `importlib.import_module(f"pcswitcher.jobs.{job_name}")` (`orchestrator.py:580`) then scans module attributes for a `SyncJob` subclass whose `name` ClassVar `== job_name` (`orchestrator.py:589-597`). So: **module filename, `name` ClassVar, and the `sync_jobs` key must all be `vscode_state_sync`.** A missing module or name-mismatch logs a WARNING and returns `None` (job silently skipped, not fatal — `orchestrator.py:581-587, 599-605`).

**Ordering requirement:** because iteration follows `sync_jobs` dict order and execution follows discovery order, `vscode_state_sync` runs after `folder_sync` iff its key appears AFTER `folder_sync` in `sync_jobs`. Required `default-config.yaml` shape:

```yaml
sync_jobs:
  dummy_success: true
  dummy_fail: false
  folder_sync: true
  vscode_state_sync: true   # MUST be listed after folder_sync
```

(`default-config.yaml:41-52` currently ends the block at `folder_sync: true`, line 46.)

## 5. `folder_sync` exclude integration point

The GLOBAL-FIRST, non-overridable exclude tier is:

- `_RUNTIME_EXCLUDE_RELPATHS: tuple[str, ...]` — `folder_sync.py:59-61`, currently just `(".local/share/pc-switcher",)`. These are home-relative relpaths.
- `_runtime_exclude_filters(folder_path) -> list[str]` (staticmethod) — `folder_sync.py:300-317`. Computes `rel = Path.home().relative_to(folder_path)` (`folder_sync.py:313`); `prefix = "/" if rel == "." else f"/{rel}/"` (`folder_sync.py:316`); returns `[f"--filter={shlex.quote(f'- {prefix}{sub}')}" for sub in _RUNTIME_EXCLUDE_RELPATHS]` (`folder_sync.py:317`). So each relpath becomes a ROOT-ANCHORED rsync exclude relative to the transfer root. When home is outside `folder_path` it returns `[]` (`folder_sync.py:314-315`).
- Spliced FIRST in `_build_rsync_cmd` via `parts.extend(self._runtime_exclude_filters(folder.path))` at `folder_sync.py:447`, BEFORE the central `merge` filter (`folder_sync.py:449-451`) and the `dir-merge` (`folder_sync.py:452`). Comment block `folder_sync.py:440-446` documents the GLOBAL-FIRST precedence.

**Exact integration:** the vscode module owns a constant (design point 2), e.g.:

```python
# in src/pcswitcher/jobs/vscode_state_sync.py
EDITOR_STATE_EXCLUDE_RELPATHS: tuple[str, ...] = (
    ".config/Code/User/globalStorage/state.vscdb",
    ".config/Code/User/globalStorage/state.vscdb.backup",
    ".config/Antigravity/User/globalStorage/state.vscdb",
    ".config/Antigravity/User/globalStorage/state.vscdb.backup",
    ".config/Cursor/User/globalStorage/state.vscdb",
    ".config/Cursor/User/globalStorage/state.vscdb.backup",
    ".config/VSCodium/User/globalStorage/state.vscdb",
    ".config/VSCodium/User/globalStorage/state.vscdb.backup",
)
```

`folder_sync.py` imports it and folds it into the same tier. Two equivalent splice options; the minimal one is to iterate over the concatenation inside `_runtime_exclude_filters`:

```python
from pcswitcher.jobs.vscode_state_sync import EDITOR_STATE_EXCLUDE_RELPATHS
...
for sub in (*_RUNTIME_EXCLUDE_RELPATHS, *EDITOR_STATE_EXCLUDE_RELPATHS):
    ...
```

**Anchoring composes correctly — VERIFIED by reading the code path:** the editor relpaths are home-relative (`.config/…`), identical in form to `.local/share/pc-switcher`. With `folder.path == "/home"` and `Path.home() == /home/janfr`, `rel == "janfr"`, `prefix == "/janfr/"`, so each emits e.g. `--filter='- /janfr/.config/Code/User/globalStorage/state.vscdb'` — a single root-anchored location, un-re-exposable by any user `+` rule because it precedes both merge surfaces (`folder_sync.py:447` before `449-452`). Both `state.vscdb` and `state.vscdb.backup` are listed explicitly (design point 2). Watch the import direction: `vscode_state_sync.py` must NOT import from `folder_sync.py` (would create a cycle) — the constant is OWNED by the vscode module and imported one-way by `folder_sync`.

**Editors present on this machine (VERIFIED):** `~/.config/Code/User/globalStorage/state.vscdb` and `~/.config/Antigravity/User/globalStorage/state.vscdb` both exist (with `.backup` sidecars); `Cursor` and `VSCodium` are absent. This confirms the design's editor list is real and that the job must skip editors whose DB is absent on the source (design point 3). Directory names for `Cursor`/`VSCodium`/`Antigravity` are `[ASSUMED]` from the standard `~/.config/<Editor>/User/globalStorage/` layout — `Code` and `Antigravity` are confirmed on-disk; verify `Cursor`/`VSCodium` casing if a machine has them.

## 6. sqlite3 on the machines (stdlib vs CLI) + verified command sequences

`sqlite3` CLI is present locally: **VERIFIED** `/usr/bin/sqlite3`, version `3.45.1`. The TARGET runs commands over SSH (no Python there), so all target-side SQL MUST go through the `sqlite3` CLI via `RemoteExecutor.run_command`. Source-side may use either Python stdlib `sqlite3` (source is local) or the CLI; using the CLI on both ends keeps the two code paths symmetric and sidesteps any BLOB round-tripping through Python. Recommend the CLI + `ATTACH` approach on both ends because cross-DB `INSERT … SELECT` copies BLOBs at the storage layer — no shell-escaping of binary values at any point.

**Schema (locked, CONTEXT §"Verified facts"):** `ItemTable(key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)`, `journal_mode=delete`, no WAL, quiescent during sync.

### Step A — source-strip (produces the neutral DB)

Run on SOURCE (local). Copy the live source DB to a temp file first (never mutate the user's live DB), then delete preserved rows:

```bash
cp <source_live.vscdb> <neutral_tmp.vscdb>
sqlite3 <neutral_tmp.vscdb> "DELETE FROM ItemTable WHERE key LIKE 'secret://%';"
```

For a multi-glob `preserve_key_globs` config, OR the patterns:

```bash
sqlite3 <neutral_tmp.vscdb> "DELETE FROM ItemTable WHERE key LIKE 'secret://%' OR key LIKE 'vscode.auth://%';"
```

### Step B — transfer

`await self.target.send_file(Path(neutral_tmp), remote_tmp)` (§3a), where `remote_tmp` sits in the target's `globalStorage/` dir for a same-dir atomic `mv`.

### Step C — target-inject + atomic replace

Run on TARGET. Only when the target live DB exists (design §4d); guard with `test -f`. ATTACH the target's live DB and copy the preserved rows into the neutral DB, then `mv`:

```bash
sqlite3 <remote_tmp.vscdb> "ATTACH '<target_live.vscdb>' AS live; INSERT INTO ItemTable SELECT key, value FROM live.ItemTable WHERE key LIKE 'secret://%';"
mv -f <remote_tmp.vscdb> <target_live.vscdb>
```

The `INSERT` needs no `OR REPLACE`: after Step A the neutral DB has no preserved-key rows, so there is no conflict; and the table's own `UNIQUE ON CONFLICT REPLACE` clause makes even a colliding insert replace rather than error (both behaviors confirmed below). Multi-glob inject mirrors the DELETE's `OR key LIKE …`.

### First sync / target DB absent (design §4d)

If `test -f <target_live.vscdb>` is false: SKIP Step C entirely and just `mv`/place the neutral DB as the target DB (it already carries no secret rows). Do NOT run the ATTACH inject against a missing file — **VERIFIED** that `ATTACH '/nonexistent.db'` auto-CREATES an empty DB file (exit 0, selects 0 rows); harmless to correctness but it would litter a stray empty file, so gate on existence.

### VERIFIED end-to-end (real `/usr/bin/sqlite3`, binary BLOBs)

Source rows: `secret://src-cred`=x'00ff0102', `settings.theme`='dark', `mru.files`=x'deadbeef'. Target rows: `secret://src-cred`=x'aabbccdd', `secret://tgt-only`=x'11223344', `settings.theme`='light-STALE', `target.only.key`='should-be-dropped'. After Step A then Step C, the final DB was:

```
mru.files          X'DEADBEEF'   (source value; binary intact)
secret://src-cred  X'AABBCCDD'   (TARGET's value preserved, not source's)
secret://tgt-only  X'11223344'   (target-only secret preserved)
settings.theme     'dark'        (source value; target STALE overwritten)
```

`target.only.key` was DROPPED. This satisfies every fidelity rule in CONTEXT §5 (full mirror except preserved keys; target-only non-preserved dropped; preserved keys keep the target's value) and confirms BLOBs copy binary-safe via `ATTACH` with zero shell-escaping. Multi-pattern (`secret://%` + `vscode.auth://%`) VERIFIED to preserve both families' target values simultaneously.

## 7. Config plumbing (exact edits)

Loading: `Configuration.from_yaml` — `config.py:67-178`. `sync_jobs` is read raw (`config.py:143`) as `dict[str, bool]`. Per-job config blocks are extracted from any TOP-LEVEL key that is not a known global key and whose value is a dict (`config.py:162-170`; `global_keys = {"logging","sync_jobs","disk_space_monitor","btrfs_snapshots"}`, `config.py:164-169`). The result is `job_configs: dict[str, dict]` (`config.py:65, 170`). The orchestrator passes `self._config.job_configs.get(job_name, {})` into the `JobContext.config` for that job (`orchestrator.py:875, 880`). So a top-level `vscode_state_sync:` block is auto-routed to the job's `context.config` by name — no code change in `config.py` needed.

Schema: `src/pcswitcher/schemas/config-schema.yaml`. Two edits, both gated by `additionalProperties: false`:

1. Add the toggle under `sync_jobs.properties` (after `folder_sync`, `config-schema.yaml:67-70`):
   ```yaml
   vscode_state_sync:
     type: boolean
     default: true
     description: "SQLite-aware selective sync of VS Code editor state.vscdb"
   ```
   Without this the unknown key is rejected by `sync_jobs.additionalProperties: false` (`config-schema.yaml:87`).
2. Add a top-level `vscode_state_sync:` object block (sibling of `folder_sync:` at `config-schema.yaml:183`), else the top-level `additionalProperties: false` (`config-schema.yaml:233`) rejects the config block:
   ```yaml
   vscode_state_sync:
     type: object
     description: "Configuration for VscodeStateSyncJob"
     properties:
       preserve_key_globs:
         type: array
         items: {type: string}
         default: ["secret://%"]
         description: "SQLite LIKE patterns for keys whose TARGET value is preserved"
     additionalProperties: false
   ```

The job's own `CONFIG_SCHEMA` (§1) should mirror the `preserve_key_globs` shape so `validate_config` also guards it (the two validations are independent: `config-schema.yaml` guards file-load, `CONFIG_SCHEMA` guards at discovery `orchestrator.py:876`).

`default-config.yaml`: add `vscode_state_sync: true` under `sync_jobs` after `folder_sync` (line 46) AND a `vscode_state_sync:` block with `preserve_key_globs: ["secret://%"]` in the job-config section (design/docs point). Reading of the default via `context.config.get("preserve_key_globs", ["secret://%"])` gives a safe fallback (dummy_success uses this `.get` default pattern, `dummy_success.py:52`).

## 8. Test patterns for jobs

Unit tests with FAKE executors — canonical helper is `make_context(...)` in `tests/unit/jobs/test_folder_sync.py:51-71`: builds `source`/`target` as `MagicMock()` whose `run_command` is an `AsyncMock` returning `CommandResult(0,"","")`; wraps them in a real `JobContext`. Shared fixtures in `tests/unit/conftest.py`: `mock_local_executor` (`conftest.py:24-31`), `mock_remote_executor` (`conftest.py:34-44`, already stubs `send_file`/`get_file`/`start_process` as AsyncMocks), `mock_job_context` (`conftest.py:57-81`), and `mock_job_context_factory(config, dry_run)` (`conftest.py:84-110`). For a job needing config, prefer a local `make_context`-style factory (mirror test_folder_sync) or the factory fixture.

Faking `run_command` per-command: the `fail_when(substring, stderr)` and `arch_reports(machine)` `side_effect` builders (`test_folder_sync.py:29-48`) show the pattern — a `def _side_effect(cmd, **_): ... return CommandResult(...)` assigned as `AsyncMock(side_effect=...)`. Testing-guide §"Different responses per command" (`docs/dev/testing-guide.md:102-107`) documents the same. Assert calls via `mock.run_command.assert_called_once_with(...)` / `.call_args_list` (`testing-guide.md:150-152, 301-303`). For `send_file` transfer assertions, use `target.send_file.assert_awaited_once_with(...)`.

For the SQLite merge logic specifically: because the real `sqlite3` binary is available and deterministic, the highest-value unit tests build real temp DBs with Python stdlib `sqlite3` in `tmp_path`, run the job's source-strip / target-inject helpers (ideally factored as pure functions taking DB paths + globs), and assert row contents — this directly exercises the §6 sequences rather than mocking them. Mock the SSH `run_command`/`send_file` only for the orchestration wrapper (which editor loop, dry-run gating, absent-DB skip).

Contract tests: `tests/contract/test_job_interface.py` (`ExampleTestJob` at line 15) verifies `name`/`CONFIG_SCHEMA` presence, `validate_config` empty-vs-error, `validate` returns `list[ValidationError]`, `execute` runs. A new job will be picked up if added to the contract suite's job list.

Integration: `tests/integration/conftest.py` provides `pc1_executor`/`pc2_executor` as `BashLoginRemoteExecutor` (login-shell RemoteExecutors) plus variants with/without pc-switcher installed (`conftest.py:10-17`). Real VMs, module-scoped, reset once per session; tests MUST clean up in `try/finally` with unique artifact names (`testing-guide.md:155-213`). An integration test would seed a `state.vscdb` with `secret://` rows on both VMs, run the job, and assert the target keeps its own secret value while non-secret keys mirror the source. Note CONTEXT constraint: this stacked PR targets a non-`main` base, so integration CI does NOT run — integration coverage is "where practical" (design "Tests" section).

## 9. Dry-run contract (ADR-014)

`docs/adr/adr-014-unified-dry-run-contract.md`. In dry-run the job MUST perform a full read-only rehearsal — validate, and detect/preview what it would do — but MUST NOT write any file on source or target, take snapshots, or update history (`adr-014:12-22`). A no-op stub is explicitly non-compliant (`adr-014:13`). `JobContext.dry_run` (`context.py:25`) is the gate; folder_sync's pattern: it still logs intended actions and skips the state-mutating passes when `dry_run` (e.g. seeding-pass skip `folder_sync.py:599`, `--dry-run` toggle `folder_sync.py:434-435`, and `[dry-run] ` log prefix `folder_sync.py:589`).

**Our plan matches ADR-014.** In dry-run, `vscode_state_sync` should: iterate editors, read which DBs exist on source/target, and LOG intended actions (which editors would sync, which keys preserved, first-sync vs merge) with a `[dry-run] ` prefix — but perform NO source temp-DB write is acceptable to skip, and MUST perform NO `send_file`, NO target `sqlite3` inject, and NO `mv` on the target. Since Step A only writes a source-local temp file (not user state), it may run or be skipped; the binding constraints are: zero target writes and zero mutation of either machine's live DB. The orchestrator itself skips the post-sync history update in dry-run (`orchestrator.py:396-397`), so the job need only guard its own writes.

## Assumptions Log

| # | Claim | Section | Risk if wrong |
|---|-------|---------|---------------|
| A1 | Cursor dir `~/.config/Cursor/User/globalStorage/`, VSCodium `~/.config/VSCodium/User/globalStorage/` | §5 | Wrong exclude relpath → that editor's DB not excluded from folder_sync / not synced. Low: Code+Antigravity confirmed on-disk; job skips absent editors anyway. Verify casing on a machine that has them. |
| A2 | `describe_first_sync_scope` override is optional for this job | §1 | If planner wants the first-sync warning to enumerate the editor DBs, must implement it; functional correctness unaffected either way. |

## Sources

Primary (read this session): `src/pcswitcher/jobs/base.py`, `jobs/context.py`, `jobs/dummy_success.py`, `jobs/folder_sync.py`, `jobs/__init__.py`, `executor.py`, `connection.py`, `orchestrator.py`, `config.py`, `models.py`, `schemas/config-schema.yaml`, `default-config.yaml`, `docs/adr/adr-014-…md`, `docs/adr/adr-016-…md`, `tests/unit/jobs/test_folder_sync.py`, `tests/unit/conftest.py`, `tests/contract/test_job_interface.py`, `tests/integration/conftest.py`, `docs/dev/development-guide.md`, `docs/dev/testing-guide.md`, `CONTEXT.md`.

VERIFIED via command: `/usr/bin/sqlite3` 3.45.1 present; editor DBs on-disk (Code, Antigravity present; Cursor, VSCodium absent); the full source-strip → transfer → target-inject → mv sequence with binary BLOBs and multi-pattern globs; ATTACH-on-missing-file auto-create behavior.
