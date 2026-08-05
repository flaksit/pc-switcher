# Phase 2: Package Management Sync - Pattern Map

**Mapped:** 2026-07-22 | **Files analyzed:** ~9 new modules + 3 modified files | **Analogs found:** 9 / 9

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
| - | - | - | - |---|
| `src/pcswitcher/jobs/package_sync_core.py` | service (shared core: item model, diff, decision-file I/O, snippet registry, review renderer) | transform + CRUD | `src/pcswitcher/jobs/vscode_state_sync.py` (module-level helpers, not a Job) | role-match |
| `src/pcswitcher/jobs/apt_sync.py` | controller (SyncJob) | CRUD (manifest capture → diff → converge) | `src/pcswitcher/jobs/folder_sync.py` | exact (SyncJob shape, validate/execute split, shlex-quoted shell-out) |
| `src/pcswitcher/jobs/snap_sync.py` | controller (SyncJob) | CRUD | `src/pcswitcher/jobs/apt_sync.py` (sibling, same core) | exact |
| `src/pcswitcher/jobs/flatpak_sync.py` | controller (SyncJob) | CRUD | `src/pcswitcher/jobs/apt_sync.py` (sibling, same core) | exact |
| Machine-local decision file I/O (in `package_sync_core.py`) | model / config (per-machine, unsynced state file) | file-I/O (read/write through executor, not local os calls, since target-side writes go over SSH) | `_RUNTIME_EXCLUDE_RELPATHS` handling in `folder_sync.py` (~line 101) + `vscode_state_exclude_paths()` in `vscode_state_sync.py` | role-match |
| Snippet registry (in `package_sync_core.py`) | model + service (opaque text blob store + replay) | request-response (replay via executor, exit-code decides success) | `target_sql_command`/`_run_sql` pattern in `vscode_state_sync.py` (build a shell command, run via executor, check exit) | role-match |
| Batched TUI review (in `package_sync_core.py`, invoked from each job's `execute()`) | component (TUI) | request-response (blocking prompt, paused Live) | `src/pcswitcher/confirmer.py` (`TerminalUIConfirmer.confirm`) | exact (pause/resume + Panel pattern; swap `Prompt.ask` for `questionary.checkbox().ask()`) |
| folder_sync exclusion export wiring (modify `folder_sync.py`) | utility (path-export consumer) | transform | `_vscode_state_exclude_filters` / `vscode_state_exclude_paths()` pair (existing ADR-018 mechanism) | exact — same mechanism, just three more path providers |
| `default-config.yaml` + `config.py` CONFIG_SCHEMA additions | config | CRUD (schema validation) | `folder_sync` CONFIG_SCHEMA block (`folder_sync.py` lines 150-176) + `sync_jobs:`/`folder_sync:` sections in `default-config.yaml` | exact |
| `tests/unit/jobs/test_package_sync_core.py`, `test_apt_sync.py`, `test_snap_sync.py`, `test_flatpak_sync.py` | test | request-response (mocked executor) | `tests/unit/jobs/test_vscode_state_sync.py` + `tests/unit/jobs/test_folder_sync.py` | exact |

## Pattern Assignments

### `src/pcswitcher/jobs/apt_sync.py` / `snap_sync.py` / `flatpak_sync.py` (controller, CRUD)

**Analog:** `src/pcswitcher/jobs/folder_sync.py`

**Imports pattern** (folder_sync.py lines 13-27):
```python
from __future__ import annotations

import getpass
import os
import re
import shlex
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, override

from pcswitcher.disk import format_bytes
from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.vscode_state_sync import vscode_state_exclude_paths
from pcswitcher.models import ConfigError, FirstSyncScope, Host, LogLevel, ProgressUpdate, ValidationError
```
Each new job imports its shared core the same way `folder_sync` imports `vscode_state_exclude_paths` from a sibling job module — `from pcswitcher.jobs.package_sync_core import ...`.

**Job class shape / CONFIG_SCHEMA pattern** (folder_sync.py lines 135-176):
```python
class FolderSyncJob(SyncJob):
    name: ClassVar[str] = "folder_sync"
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {...},
        "required": [...],
        "additionalProperties": False,
    }
```
Each of `AptSyncJob`/`SnapSyncJob`/`FlatpakSyncJob` follows this exact shape: `name: ClassVar[str] = "apt_sync"` etc., own `CONFIG_SCHEMA`, matching `default-config.yaml`'s per-job section (D-15: independent config per job).

**`describe_first_sync_scope` pattern** (folder_sync.py lines 238-260, vscode_state_sync.py lines 240-253):
```python
@classmethod
@override
def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
    ...
    return FirstSyncScope(job_name=cls.name, scope_items=paths, mechanism="rsync --delete")
```
Every package job must implement this (D-07/D-08 note "apply" can mean destructive removal — this is the ADR-015 first-sync warning hook, required per `jobs/base.py` `SyncJob`).

**Validate pattern — sequential system-state checks, `_validation_error` helper** (folder_sync.py lines 262-365):
```python
async def validate(self) -> list[ValidationError]:
    errors: list[ValidationError] = []
    result = await self.source.run_command("sudo rsync --version")
    if result.exit_code != 0:
        errors.append(self._validation_error(Host.SOURCE, "..."))
    ...
    return errors
```
Each package job's `validate()` checks its manager's availability (`apt-mark --version`/`dpkg-query --version`, `snap version`, `flatpak --version`), sudo access where needed (apt/snap installs need root; flatpak `--user` does not — Security V4), and remote reachability, exactly this sequential-checks-into-a-list shape.

**shlex.quote / shell-out pattern** (folder_sync.py lines 340-363, vscode_state_sync.py lines 313, 364-382):
```python
quoted = shlex.quote(folder.path)
result = await self.source.run_command(f"test -d {quoted}")
```
Every apt/snap/flatpak/dpkg invocation with an interpolated value (package name, path, ref) must go through `shlex.quote()` exactly this way — never format directly into the command string (Security V5, RESEARCH "Don't Hand-Roll").

**Error handling / continue-on-failure pattern** (folder_sync.py lines 685-712, `_run_rsync_pass`):
```python
result = await proc.wait_result()
if result.exit_code != 0:
    self._log(Host.SOURCE, LogLevel.CRITICAL, f"... failed: {result.stderr.strip()}", exit_code=result.exit_code)
    raise RuntimeError(f"... failed with exit code {result.exit_code}")
```
folder_sync raises on any rsync failure (halts sync); package jobs must NOT copy this directly — D-27 requires continue/collect/report instead. Use `vscode_state_sync.py`'s `_raise` helper (lines 395-399) as the fallback-raise shape only for validate()-time/unrecoverable errors, but per-item converge failures must be caught, logged with `self._log(host, LogLevel.WARNING/ERROR, ..., stderr=...)`, collected into a summary list, and the job's overall result marked failed without raising per-item.

**Progress reporting pattern** (vscode_state_sync.py lines 300-330):
```python
self._report_progress(ProgressUpdate(percent=int((index + 1) / total * 100)))
...
self._log(Host.TARGET, LogLevel.INFO, f"{prefix}{total} VS Code state DB file(s) synced")
```
Per-item detail at FULL, per-job summary at INFO (D-16 for package jobs mirrors this exactly) — use this item-loop-with-percent pattern rather than folder_sync's byte-stream parsing (which is rsync-specific and not needed here).

**Dry-run pattern** (vscode_state_sync.py lines 317-324):
```python
if self.context.dry_run:
    self._log(Host.TARGET, LogLevel.FULL, f"{prefix}Would sync {label} ({mode}); preserving keys matching {globs}")
    self._report_progress(...)
    continue
```
Package jobs' dry-run doubles as the batched-review dry-run contract (ADR-014) — the diff+review IS the dry-run output; no separate rsync-style `--dry-run` flag exists in these ecosystems, so follow this "compute + log + skip the mutating call" shape per item.

---

### `src/pcswitcher/jobs/package_sync_core.py` (shared core: item model, diff, decision file, snippet registry, review)

**Analog for module structure/ownership docstring convention:** `src/pcswitcher/jobs/vscode_state_sync.py` (module-level pure functions + one `SyncJob` class, not the whole-module-is-one-job pattern folder_sync uses)

**Item model dataclass pattern** — follow `FolderEntry` (folder_sync.py lines 106-132):
```python
@dataclass
class FolderEntry:
    path: str
    enabled: bool = True
    filter_file: str | None = None

    def expanded_filter_file(self) -> str | None:
        ...
```
Each `Item` subtype (`AptPackageItem`, `AptSourceItem`, `SnapItem`, `FlatpakItem`, etc. per D-02) should be a small `@dataclass(frozen=True)` with a stable-identity field set, matching RESEARCH.md's illustrative shapes (RESEARCH.md lines 235-254) — planner/researcher already sketched the fields; this is Claude's Discretion per CONTEXT.md, but the dataclass-per-item-class shape itself should match `FolderEntry`'s style.

**Machine-local decision file I/O — read via executor, write via correct-end executor** (pattern derived from `_needs_copy_pass`'s dual-host manifest read, folder_sync.py lines 452-481):
```python
async def _needs_copy_pass(self, folder: FolderEntry) -> bool:
    path = shlex.quote(folder.path.rstrip("/") or "/")
    manifest_cmd = f"sudo find {path} -name .pcswitcher-filter -exec sha256sum {{}} +"
    src = await self.source.run_command(manifest_cmd)
    ...
    tgt = await self.target.run_command(manifest_cmd, login_shell=False)
```
D-08a requires writing the decision file "on the correct end of the connection" — mirror this dual-executor-read shape: read/write the source's decision file via `self.source.run_command(...)` and the target's via `self.target.run_command(..., login_shell=False)`, never via local `pathlib`/`open()` for the target side (no direct filesystem access to target — ADR-002 stateless remote scripts).

**Non-overridable exclusion export contract (D-29)** — copy verbatim from ADR-018 mechanism:
```python
# vscode_state_sync.py lines 131-147
def vscode_state_exclude_paths() -> list[Path]:
    home = Path.home()
    return [home / relpath for relpath in VSCODE_STATE_HANDLED_RELPATHS]
```
```python
# folder_sync.py lines 388-408
@staticmethod
def _vscode_state_exclude_filters(folder_path: str) -> list[str]:
    root = Path(folder_path.rstrip("/") or "/")
    filters: list[str] = []
    for abs_path in vscode_state_exclude_paths():
        try:
            rel = abs_path.relative_to(root)
        except ValueError:
            continue
        filters.append(f"--filter={shlex.quote(f'- /{rel}')}")
    return filters
```
`flatpak_sync.py` must expose a `flatpak_sync_exclude_paths() -> list[Path]` (owns `~/.local/share/flatpak`) and `snap_sync.py` a `snap_sync_exclude_paths() -> list[Path]` (owns `~/snap/<app>/<rev>` — needs to enumerate revision dirs, unlike vscode's fixed relpath list), each following this exact function-not-constant shape (paths resolved against `Path.home()` at call time). `folder_sync.py` then needs one more `_*_exclude_filters` static method per new provider, translating absolute paths into `--filter=` args exactly like `_vscode_state_exclude_filters`, and both added to the GLOBAL-FIRST filter chain in `_build_rsync_cmd` (folder_sync.py lines 541-550) alongside the existing two.

**Snippet execution (opaque blob, non-interactive, exit code decides)** — pattern from `target_sql_command`/`_run_sql` (vscode_state_sync.py lines 194-225):
```python
def target_sql_command(db_path: str, sql: str) -> str:
    script = "...\n"
    return f"python3 -c {shlex.quote(script)} {shlex.quote(db_path)} {shlex.quote(sql)}"
```
Snippet replay follows the same "build a shell command, pass content as an argv-quoted string, run via `self.target.run_command(cmd, login_shell=False)`, check `.success`" shape — the snippet body itself is the opaque blob (D-20), quoted via `shlex.quote()` exactly like the SQL script here, never parsed.

**Version comparison** (no direct in-repo analog — new pure function): implement as a thin wrapper shelling out to `dpkg --compare-versions <v1> <op> <v2>` via `self.source.run_command(...)`, following the same `shlex.quote()`-every-value + check `.exit_code` convention as every other shell-out in this codebase (folder_sync.py line 342, vscode_state_sync.py line 313).

---

### Batched TUI review (in `package_sync_core.py`, D-24)

**Analog:** `src/pcswitcher/confirmer.py` — `TerminalUIConfirmer.confirm` (lines 90-135)

**Pause/resume + blocking-prompt pattern to copy verbatim, swapping the prompt call:**
```python
# confirmer.py lines 121-134
self._ui.pause()
try:
    self._console.print()
    self._console.print(Panel(message, title=title, border_style="yellow"))
    self._console.print()
    response = Prompt.ask("[bold]Continue anyway?[/bold]", choices=["y", "n"], default="n")
    return response.lower() == "y"
finally:
    self._ui.resume()
```
Replace `Prompt.ask(...)` with `await asyncio.to_thread(lambda: questionary.checkbox(...).ask())` (RESEARCH.md Pattern 3, lines 271-291) inside the same `self._ui.pause()` / `try` / `finally: self._ui.resume()` structure. Reuse `is_interactive(self._console)` (confirmer.py line 101, imported from `pcswitcher.terminal`) to implement D-26's non-interactive skip-all-and-report behavior — same branch structure as `TerminalUIConfirmer.confirm`'s non-interactive branch (lines 101-119): log + print a warning instead of prompting, and here return "everything unresolved" rather than True/False.

**`JobContext` access to the confirmer/UI:** `context.py` line 29 already exposes `confirmer: Confirmer | None` on `JobContext`; the package jobs' shared review renderer needs the same `PausableUI` handle `TerminalUIConfirmer` takes in its constructor — check how the orchestrator wires `TerminalUIConfirmer(console, ui)` today (not read this session) and thread the same `ui` handle to the review renderer, since `questionary.checkbox()` needs the identical pause/resume around it that `Prompt.ask()` gets.

---

## Shared Patterns

### shlex-quoted shell-out via Executor protocol

**Source:** `folder_sync.py` (`shlex.quote(folder.path)`, line 341 etc.), `vscode_state_sync.py` (`shlex.quote(target_db)`, line 313; `shlex.quote(script)`, line 225). **Apply to:** every apt/snap/flatpak/dpkg command built in `apt_sync.py`/`snap_sync.py`/`flatpak_sync.py`/`package_sync_core.py`.
```python
result = await self.target.run_command(f"test -f {shlex.quote(target_db)}", login_shell=False)
```
Never interpolate a package name, path, ref, or URI into a command string without `shlex.quote()` first (Security V5).

### Three-phase validate() with `_validation_error` helper

**Source:** `jobs/base.py` `Job._validation_error` (lines 142-152), used throughout `folder_sync.py.validate()`. **Apply to:** all three new jobs' `validate()` — manager-availability check → sudo/access check → item-existence/reachability check, each appending to an `errors: list[ValidationError]` and returning it at the end (never raising mid-validate).

### `describe_first_sync_scope` classmethod override

**Source:** `jobs/base.py` `SyncJob.describe_first_sync_scope` (lines 172-195), implemented in both `folder_sync.py` (238-260) and `vscode_state_sync.py` (240-253). **Apply to:** all three new jobs — name the concrete destructive scope (D-07's "apply is the destructive branch as often as the additive one" makes this doubly important here) so ADR-015's first-sync warning stays accurate.

### Per-item FULL logging, per-job INFO summary

**Source:** `vscode_state_sync.py` execute() (lines 300-330), folder_sync.py execute() (lines 773-787). **Apply to:** all package job execute() loops and the shared converge step in `package_sync_core.py` — `self._log(host, LogLevel.FULL, ...)` per item, one `LogLevel.INFO` summary line per job/manager at the end (ADR-010).

### Dry-run = "compute, log, skip the mutating call" (ADR-014)

**Source:** `vscode_state_sync.py` execute() (lines 317-324). **Apply to:** all package jobs — the batched review + diff computation IS the dry-run preview (D-24/D-25 note the review satisfies "reported before destructive change" for real runs too); when `self.context.dry_run` is True, converge is skipped entirely after the review is shown/logged.

### Pause/resume around any blocking terminal interaction

**Source:** `confirmer.py` `TerminalUIConfirmer.confirm` (lines 121-134). **Apply to:** the batched TUI review's `questionary.checkbox()` call — must be wrapped in the same `self._ui.pause()`/`finally: self._ui.resume()` as every other blocking prompt in this codebase, and the blocking `.ask()` call itself wrapped in `asyncio.to_thread` (ADR-005 — no blocking calls on the event loop).

### Non-overridable path-export contract (ADR-018)

**Source:** `vscode_state_sync.py` `vscode_state_exclude_paths()` (lines 131-147) + `folder_sync.py` `_vscode_state_exclude_filters` (lines 388-408) + `_RUNTIME_EXCLUDE_RELPATHS` (lines 101-103). **Apply to:** `snap_sync.py`'s and `flatpak_sync.py`'s new `*_exclude_paths()` functions (D-29), consumed by two new `folder_sync.py` static methods following the exact translate-absolute-path-to-rsync-filter shape, added to the GLOBAL-FIRST section of `_build_rsync_cmd`'s filter chain (lines 541-550) alongside the existing vscode one, before the user filter surfaces.

## No Analog Found

| File | Role | Data Flow | Reason |
| - | - | - | - |
| `questionary.checkbox()` integration itself | component | request-response | No existing multi-select TUI in this codebase (Rich has no built-in checkbox widget — RESEARCH.md "Don't Hand-Roll"); RESEARCH.md Pattern 3 (lines 271-291) is the only reference, not a codebase analog. Treat first integration as a spike per RESEARCH.md Assumption A2. |
| Snippet-registry storage format (shared, synced config location) | config/model | CRUD | No existing "shared synced config beyond `config.yaml`" precedent in the codebase this session; `config_sync.py` was not read in depth this session — planner should verify how `config.yaml` reaches the target (`config_sync.py`, referenced in CONTEXT.md canonical refs) before deciding whether the snippet registry piggybacks on that same sync path or needs its own. |
| Version-comparison wrapper (`dpkg --compare-versions`) | utility | transform | No existing deb-version-comparison code in the codebase; RESEARCH.md Code Examples (lines 378-384) is the reference, not a codebase analog — implement as a small new pure-shell-out function following the shlex/executor conventions above. |

## Metadata

**Analog search scope:** `src/pcswitcher/jobs/`, `src/pcswitcher/executor.py`, `src/pcswitcher/confirmer.py`, `src/pcswitcher/config.py`, `src/pcswitcher/default-config.yaml`, `tests/unit/jobs/`

**Files scanned:** `jobs/base.py`, `jobs/context.py`, `jobs/folder_sync.py`, `jobs/vscode_state_sync.py`, `executor.py`, `confirmer.py`, `config.py`, `default-config.yaml`, `orchestrator.py` (grep only), `tests/unit/jobs/test_vscode_state_sync.py` (partial)

**Pattern extraction date:** 2026-07-22
