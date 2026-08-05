# Quick Task 260719-g13: Check for new versions at startup (#176) - Research

**Researched:** 2026-07-19

**Domain:** Typer/Click CLI lifecycle, subprocess self-update, process re-exec

**Confidence:** HIGH — all findings verified directly against this repo's code, installed Typer/Click version, and a live CliRunner probe. No external library research was needed; everything lives in `pcswitcher.cli`, `pcswitcher.version`, `pcswitcher.confirmer`, `pcswitcher.logger`, and the existing test suite.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- Run on ALL commands via the Typer app-level callback (`src/pcswitcher/cli.py:main`), before the command body executes.
- Guard so it only runs when a real subcommand is being invoked (`ctx.invoked_subcommand is not None`). Skip for bare `pc-switcher` (help) and `--version`.
- Query the latest STABLE release only: `version.get_highest_release(include_prereleases=False)`. Compare against `version.get_this_version()`. Prompt ONLY when latest stable is strictly greater than current. Never prompt when current >= latest stable.
- Inform the user of current vs available version, then interactively ask y/N. Reuse the existing confirmer / Rich console prompt style already used in the codebase.
- On confirm: perform the upgrade inline reusing `_run_uv_tool_install` + `_verify_installed_version`. Refactor the shared install-and-verify core out of `update` into a reusable helper so both `self update` and the startup auto-upgrade call it.
- On successful upgrade: auto re-exec the SAME command line via `os.execvp(sys.argv[0], sys.argv)` with `PCSWITCHER_SKIP_VERSION_CHECK=1` set in the child environment.
- If the upgrade fails: print a warning and CONTINUE on the current version — never block the user's command.
- On decline: continue the command normally.
- Skip conditions (ANY of): `--no-version-check` global flag; env var `PCSWITCHER_SKIP_VERSION_CHECK` set; stdin OR stdout not a TTY; invoked subcommand is `self`; no subcommand (help / `--version`).
- On check failure (offline, rate-limit, API error, RuntimeError): print a brief warning and continue. NEVER fatal.

### Claude's Discretion
- Exact wording of the prompt / warning messages.
- Whether the new check logic lives in a small helper module or in `cli.py` (keep it testable — prefer a dedicated function that can be unit-tested with mocks).
- Message formatting details (Rich styling), as long as consistent with existing CLI output.

### Deferred Ideas (OUT OF SCOPE)
None recorded in CONTEXT.md for this task.
</user_constraints>

## Summary

The whole feature composes cleanly out of existing pieces: `version.get_this_version()` / `get_highest_release()` for the comparison, `_run_uv_tool_install()` / `_verify_installed_version()` for the upgrade mechanics, `logger.is_interactive()` for TTY gating, and the codebase's established `rich.prompt.Prompt.ask(choices=["y","n"], default="n")` idiom for the confirmation. Two structural facts drive the design: (1) Typer/Click's `is_eager` + `no_args_is_help=True` already handle "skip on bare invocation / `--version`" for free — `main()`'s body simply never runs in those cases, so the only guard actually needed in code is `ctx.invoked_subcommand not in (None, "self")`; and (2) `TerminalUIConfirmer` is the wrong tool here — it requires a live `PausableUI` that does not exist yet at startup, so the plain `Prompt.ask` pattern (same one used in `config_sync.py`) is the correct, lighter fit.

A verified CliRunner probe (see Pitfall 1 below) confirms `console.is_terminal` and `sys.stdin.isatty()` are both `False` under `typer.testing.CliRunner`, so the entire existing test suite in `tests/unit/cli/test_commands.py` is automatically safe from the new startup check — no mass-mocking of `get_highest_release` needed across unrelated tests.

**Primary recommendation:** Extract a `_install_and_verify(release) -> Version` helper (raising a new `UpdateFailedError`) out of the `self update` command body in `cli.py`, and add a new `_maybe_check_for_update(console, *, invoked_subcommand, no_version_check)` function — colocated in `cli.py` to avoid a circular import — called from `main()`. Test it with `patch("pcswitcher.cli.is_interactive", ...)`, `patch("pcswitcher.cli.get_highest_release", ...)`, `patch("pcswitcher.cli.Prompt.ask", ...)`, and `patch("pcswitcher.cli.os.execvp", ...)`.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
| ---------- | ----------- | -------------- | --------- |
| Version comparison / GitHub query | CLI (app callback) | `pcswitcher.version` module | `main()` orchestrates *when* to check; `version.py` already owns *how* to fetch/compare |
| Install + verify upgrade | CLI (`cli.py`) | — | Subprocess orchestration is already colocated with `self update`; no separate service layer exists in this codebase |
| Interactive prompt | CLI (`cli.py`, via `rich.prompt`) | — | No TUI/Live display exists at startup — this is plain synchronous stdout/stdin, same tier as `config_sync.py`'s prompts |
| Process re-exec | CLI (`cli.py`, `os.execvp`) | — | OS-process-level concern, must happen after install succeeds, in the same process that ran the check |

## Findings by Focus Question

### 1. Typer/Click app-level callback mechanics

Installed versions: **Typer 0.20.0 / Click 8.3.1** `[VERIFIED: uv run python -c "import typer, click"]`.

- `main()` already exists as `app.callback()` at `src/pcswitcher/cli.py:94-105`, with `version_flag` marked `is_eager=True` and `callback=_version_callback`. Add a second parameter:
  ```
  ctx: typer.Context,
  no_version_check: Annotated[bool, typer.Option("--no-version-check", help="Skip the startup version check")] = False,
  ```
  `typer.Context` must be a plain parameter (no `Annotated`/`typer.Option` wrapper) — Typer injects it directly when it sees the `typer.Context` type. Access `ctx.invoked_subcommand` inside the callback body. `[CITED: github.com/fastapi/typer docs/tutorial/commands/context.md, fetched via Context7 /fastapi/typer]`

- **Eager `--version` interaction:** `is_eager=True` means Click resolves and calls `_version_callback` during *parameter processing*, before the callback function body (`main()`'s `pass`/new logic) ever executes. `_version_callback` raises `typer.Exit()` on `True`, so `pc-switcher --version` never reaches the new version-check code — no extra guard needed for this case beyond what already exists. `[CITED: same Context7 doc, "Previous parameters and is_eager"]`

- **Bare invocation / `no_args_is_help`:** `app = typer.Typer(..., no_args_is_help=True)` (`cli.py:27-31`) causes Click's `MultiCommand.parse_args` to print help and call `ctx.exit()` when `sys.argv` has zero args, **before `main()`'s callback body runs at all**. Verified: `[VERIFIED: live CliRunner probe — see Pitfall 1]` a bare invocation never reaches `main()`. So the `ctx.invoked_subcommand is None` check in the callback body is a defense-in-depth guard for cases like `pc-switcher --no-version-check` alone (non-empty argv, no subcommand) — it IS needed for that case, since `no_args_is_help` only fires on a truly empty argv.

- **Guard to add in `main()` body:**
  ```
  if ctx.invoked_subcommand is not None and ctx.invoked_subcommand != "self":
      _maybe_check_for_update(console, no_version_check=no_version_check)
  ```
  This single condition satisfies both the "no subcommand" and "self subcommand" skip rules from CONTEXT.md.

### 2. Exact reuse points in cli.py — extraction plan

Current `update()` command body, `src/pcswitcher/cli.py:587-643`. Only the tail (lines 620-643) is the shared "install + verify" core; the head (lines 604-618) is `self update`-specific (CLI arg resolution, downgrade warning, already-up-to-date short-circuit) and stays in `update()` only.

Extraction:
- Add `class UpdateFailedError(Exception): """Raised when installing/verifying a new pc-switcher release fails."""` near `GITHUB_REPO_URL` (`cli.py:509`).
- New function `_install_and_verify(release: Release) -> Version`, placed after `_verify_installed_version()` (after `cli.py:547`):
  - Calls `_run_uv_tool_install(release)`; if `returncode != 0`, raise `UpdateFailedError` with message `"Update failed"` (append `result.stderr.strip()` if present, matching the existing `[dim]{stderr}[/dim]` detail — carry it as a second line in the exception message or a `.detail` attribute so `self update`'s existing dim-stderr print can be preserved).
  - Calls `_verify_installed_version()`; if `None`, raise `UpdateFailedError("Verification failed - pc-switcher not working after update")` (exact string reuse — this exact text is not asserted by any automated test but is documented in `tests/self-update-test-playbook.md`, so preserve it).
  - If `installed != release.version`, raise `UpdateFailedError(f"Version mismatch after update. Expected {release.version.semver_str()}, got {installed.semver_str()}")`.
  - On success, return `installed`.
- Rewrite `update()` (`cli.py:620-643`) to:
  ```
  try:
      installed = _install_and_verify(target_release)
  except UpdateFailedError as e:
      console.print(f"[bold red]Error:[/bold red] {e}")
      sys.exit(1)
  console.print(f"[green]Successfully updated to version {installed.semver_str()}[/green]")
  ```
  **Preserve exact substrings** `"Successfully updated"` and `"Already at version"` — both are asserted in `tests/integration/test_self_update.py:146,194`. The already-at-version and downgrade-warning branches (lines 613-618) are untouched by this refactor.

- `_maybe_check_for_update` (new) calls the same `_install_and_verify(release)`, but on `UpdateFailedError` prints a **yellow warning and returns** (never `sys.exit`) — this is the behavioral fork between the two call sites, and is why the shared helper raises rather than printing/exiting itself.

**Verify no regression:** `tests/unit/cli/test_self_update.py` only tests `_run_uv_tool_install` and `_verify_installed_version` directly (unchanged signatures) — this extraction does not touch either, so that file needs no changes. `[VERIFIED: read tests/unit/cli/test_self_update.py]`

### 3. `os.execvp` re-exec mechanics

- Call form per CONTEXT.md (locked): `os.execvp(sys.argv[0], sys.argv)`. `os.execvp` performs a `$PATH` search for the executable named by its first argument — this is *why* `execvp` (not `execv`) is correct even when `sys.argv[0]` is the bare string `"pc-switcher"` (as installed by `uv tool install` into `~/.local/bin` and invoked via `$PATH` lookup by the shell). No manual path resolution needed.
- Set the env var **before** calling exec, via the process's real environment dict (which `execvp`/`execve` reads implicitly): `os.environ["PCSWITCHER_SKIP_VERSION_CHECK"] = "1"`. This matches CONTEXT.md's explicit call form (`os.execvp`, not `os.execvpe`) — `execvp` inherits `os.environ` automatically, so no separate `env` argument is needed or specified.
- **Flush before exec:** `sys.stdout.flush()` and `sys.stderr.flush()` (or at minimum flush the `console`'s underlying file — Rich's `Console.print` writes through a buffered file object) immediately before the `execvp` call. `execvp` replaces the process image via `execve()` at the OS level — it does **not** run `atexit` handlers, `finally` blocks, or flush Python's I/O buffers. Any `console.print(...)` issued just before the re-exec that hasn't hit the underlying fd yet will be silently dropped. `[ASSUMED — standard CPython `os.exec*` semantics, not verified against this repo's I/O buffering config this session; risk if wrong: the "Updated to X, restarting..." message could be swallowed]`
- **Nothing after `execvp` runs.** Structure the call as the last statement in the success branch — no `return` or further code needed/reachable after it (if it returns at all, that itself is the error case — `os.execvp` only returns on failure, raising `OSError`; CONTEXT.md does not specify handling for *that* failure mode — flag as open question below).
- `is_interactive`/env-var write ordering: write `PCSWITCHER_SKIP_VERSION_CHECK=1` to `os.environ` right before exec, not earlier — the check function itself must NOT see this var during its own run (it's meant only for the child).

### 4. TTY / non-interactive detection

`pcswitcher.logger.is_interactive(console: Console) -> bool` (`logger.py:324-335`) is the correct existing helper: `[VERIFIED: read logger.py]`
```python
def is_interactive(console: Console) -> bool:
    return console.is_terminal and sys.stdin.isatty()
```
Requires **both** stdout (`console.is_terminal`) and stdin (`sys.stdin.isatty()`) to be TTYs — exactly matching CONTEXT.md's "stdin OR stdout is not a TTY" skip rule (i.e. skip unless both are TTYs).

Import path: `from pcswitcher.logger import is_interactive` — already imported by `confirmer.py` the same way; safe, no new circular-import risk (`logger.py` does not import `cli.py`).

Monkeypatch-friendly: `patch("pcswitcher.cli.is_interactive", return_value=True/False)` once imported into `cli.py`'s namespace. `console` in `cli.py` is a module-level singleton (`cli.py:41`) constructed once at import time — `[VERIFIED: live CliRunner probe]` `console.is_terminal` re-resolves `sys.stdout` **dynamically at access time**, not cached at construction, so `CliRunner`'s stdout/stdin redirection is correctly observed by a module-level `console` without needing to reconstruct it per-test.

### 5. Interactive prompt pattern — recommendation

**Recommend:** plain `rich.prompt.Prompt.ask` — the same idiom already used twice in this codebase, NOT `Confirmer`/`TerminalUIConfirmer`.

Reasoning: `TerminalUIConfirmer.confirm()` (`confirmer.py:90-134`) unconditionally calls `self._ui.pause()` / `self._ui.resume()` around the prompt — it requires a constructed `PausableUI` (the live `TerminalUI`, built later in `Orchestrator`/sync flow). At the point `main()`'s callback runs, no `TerminalUI` exists yet (it's constructed per-sync inside the async orchestrator path), so `TerminalUIConfirmer` is not instantiable here without inventing a dummy `PausableUI`. It also returns an `async def confirm(...)` — would require wrapping the whole startup check in `asyncio.run(...)`, adding complexity CONTEXT.md doesn't ask for.

`config_sync.py:104` establishes the exact idiom to copy:
```python
response = Prompt.ask("Choice", choices=["y", "n"], default="n")
return response.lower() == "y"
```
Recommended prompt shape for this feature (message content is Claude's discretion per CONTEXT.md):
```python
console.print(f"A new stable version is available: {current.semver_str()} -> {latest.version.semver_str()}")
response = Prompt.ask("Upgrade now?", choices=["y", "n"], default="n")
```
Note `Prompt.ask` in this codebase is called **without** passing `console=` — it uses Rich's own default global console internally, which still targets the real stdout/stdin, matching the pattern in both `confirmer.py:127` and `config_sync.py:104,153`. Follow the same (no `console=` kwarg) for consistency, even though `cli.py` has its own `console` singleton available.

### 6. Testing approach

**Existing CLI test pattern:** `tests/unit/cli/test_commands.py` uses `typer.testing.CliRunner()` + `runner.invoke(app, [...])`, patching internals via `patch("pcswitcher.cli.<name>", ...)`. `tests/unit/cli/test_self_update.py` calls the private functions directly (`from pcswitcher import cli; cli._run_uv_tool_install(...)`) without going through CliRunner at all — appropriate for pure-function-style helpers. Use the CliRunner style for the end-to-end skip/prompt scenarios (asserting on `result.exit_code` / `result.stdout`), and the direct-call style for unit-testing `_maybe_check_for_update` / `_install_and_verify` in isolation.

**Verified via live probe** `[VERIFIED: ran a throwaway Typer app under CliRunner this session]`: under `typer.testing.CliRunner`, a module-level `Console()` reports `is_terminal == False` and `sys.stdin.isatty() == False`. Consequence: **every existing `runner.invoke(app, [...])` call in `tests/unit/cli/test_commands.py` will naturally skip the new version check** (via the `is_interactive` gate) without any modification or added mocking — confirmed no regression risk to the existing 20+ CliRunner-based tests in that file.

**Mocking recipe for the new test file** (recommend `tests/unit/cli/test_version_check.py`, mirroring `test_self_update.py`'s naming):
| What to mock | Patch target | Why |
| - | - | - |
| Latest release / current version | `patch("pcswitcher.cli.get_highest_release", return_value=...)`, `patch("pcswitcher.cli.get_this_version", return_value=...)` | avoid live GitHub calls |
| Install/verify subprocess calls | `patch("pcswitcher.cli.subprocess.run", ...)` (same as `test_self_update.py`) | avoid real `uv tool install` |
| TTY | `patch("pcswitcher.cli.is_interactive", return_value=True)` | force the interactive branch in tests (CliRunner can't fake a real TTY) |
| Prompt | `patch("pcswitcher.cli.Prompt.ask", return_value="y"/"n")` | matches `test_config_sync.py`'s established pattern (`patch("pcswitcher.config_sync.Prompt.ask", ...)`) |
| Re-exec | `patch("pcswitcher.cli.os.execvp") as mock_execvp` | **must always be mocked** — an unpatched `os.execvp` call replaces the pytest process image itself, killing the test run with no traceback. This is the single highest-risk gotcha in this task; assert `mock_execvp.assert_called_once_with(sys.argv[0], sys.argv)` and separately assert `os.environ["PCSWITCHER_SKIP_VERSION_CHECK"] == "1"` was set beforehand (use `monkeypatch.delenv`/`monkeypatch.setenv` to isolate env state per test, restoring after) |
| Env var skip | `monkeypatch.setenv("PCSWITCHER_SKIP_VERSION_CHECK", "1")` / `monkeypatch.delenv(..., raising=False)` | matches `test_version.py`'s existing `patch.dict("os.environ", ...)` pattern for `GITHUB_TOKEN` |
| `--no-version-check` flag | via `runner.invoke(app, ["--no-version-check", "logs"])` (CliRunner, real Typer parsing) | verifies the flag is actually wired through Typer, not just the internal function |

**Coverage checklist** (from CONTEXT.md, mapped to test style):
- update available -> yes -> upgrade + re-exec with guard env set: direct-call test on `_maybe_check_for_update`, asserting `os.environ` mutation + `mock_execvp` call.
- update available -> no -> continue: same, `Prompt.ask` returns `"n"`, assert `execvp` NOT called.
- already up to date -> no prompt: `latest.version <= current`, assert `Prompt.ask` not called.
- non-TTY -> skip: `is_interactive` returns `False`, assert `get_highest_release` not called.
- `--no-version-check` flag -> skip: CliRunner invoke with flag, assert underlying check function not entered (patch `_maybe_check_for_update` itself, or assert `get_highest_release` not called).
- `PCSWITCHER_SKIP_VERSION_CHECK` env set -> skip: `monkeypatch.setenv`, assert skip.
- `self` subcommand -> skip: CliRunner invoke `["self", "update", ...]` with everything else mocked, assert the version-check path isn't entered (this is a `ctx.invoked_subcommand` behavior — best tested at the CliRunner level, since it depends on Click's context wiring, not just the internal function's own logic).
- check raises -> warn + continue: `get_highest_release` raises `RuntimeError`, assert command still proceeds and a warning is printed.

### 7. Docs to update

- **`README.md`** — add `--no-version-check` to the "Available Commands" section (`README.md:120-136` area) and a short paragraph near "Self-update pc-switcher" (`README.md:135`) describing the new startup check + prompt + `PCSWITCHER_SKIP_VERSION_CHECK` env var, alongside the existing GitHub rate-limit troubleshooting note (`README.md:150-166`, which already documents `--version`/`self update`/sync as GitHub-API-calling commands — the startup check adds a fourth, on every command, and belongs in that same troubleshooting section since it shares the exact rate-limit failure mode).
- **`docs/adr/adr-004-dynamic-versioning-github-releases.md`** — this ADR is scoped to *how versions are assigned* (dynamic versioning from git tags), not to CLI runtime behavior; the startup check doesn't change versioning semantics. `[ASSUMED]` recommend **not** amending ADR-004; it is cited in CONTEXT.md as "canonical reference" for the version-comparison model this feature *consumes*, not a doc that itself needs updating. If the planner disagrees, the appropriate place would be a new "Implementation Rules" bullet, but this is a discretionary call, not a locked decision.
- No `docs/system/core.md` / spec-driven doc reference was found describing self-update CLI behavior in spec form (only `README.md` and `tests/self-update-test-playbook.md` — the latter is a manual test doc, not a source-of-truth doc, but should get a short new scenario added for the startup-prompt-and-upgrade flow if the planner wants manual-playbook coverage matching the existing self-update entries there).

## Common Pitfalls

### Pitfall 1: Unpatched `os.execvp` kills the test process

**What goes wrong:** Calling the real `os.execvp` inside a pytest run replaces the running Python process (the test runner itself) with a new `pc-switcher` invocation — the test process never returns, pytest hangs or the run terminates abnormally with no useful traceback.

**Why it happens:** `execvp`/`execve` family calls do not fork; they replace the calling process's image in-place.

**How to avoid:** Always `patch("pcswitcher.cli.os.execvp")` in every test that exercises the "yes + upgrade" branch, with no exceptions.

**Warning signs:** A test run that hangs indefinitely or a CI job that dies without a pytest summary.

### Pitfall 2: `console.is_terminal` timing under module-level `Console()`

**What goes wrong:** Assuming a module-level `Console()` object caches its TTY-ness at import time, and therefore can't reflect `CliRunner`'s stdout/stdin isolation in tests.

**Why it happens:** Rich's `Console.file` (and thus `is_terminal`) resolves `sys.stdout` dynamically on each access by default, not once at construction — this is non-obvious and easy to assume wrong.

**How to avoid:** `[VERIFIED this session via live probe]` — no special handling needed; the existing module-level `console` in `cli.py` already "just works" correctly for both `CliRunner`-based skip tests (real dynamic resolution -> non-interactive) and explicit `is_interactive` mocking (for forcing the interactive branch).

**Warning signs:** N/A — verified working as-is; flagged here only so the planner doesn't add unneeded console-reconstruction workarounds.

### Pitfall 3: Losing existing exact-string test assertions during the `_install_and_verify` extraction

**What goes wrong:** Refactoring `update()`'s tail into a shared helper accidentally changes the exact text of `"Successfully updated to version {X}"` or `"Already at version {X}"`.

**Why it happens:** Both strings are asserted via substring match in `tests/integration/test_self_update.py:146,194` (an integration test, so it won't run in the fast unit suite and a break here is easy to miss during normal `uv run pytest` iteration).

**How to avoid:** Keep the success-message `console.print` call in `update()` itself (not inside the shared helper), and preserve the substrings verbatim. Run `tests/integration/test_self_update.py` (or at minimum grep the strings) before considering this task done, even though it needs VM infra to actually execute.

**Warning signs:** Integration test failures on `assert "Successfully updated" in stdout` / `assert "Already at version" in stdout` after a merge.

## Open Questions

1. **What if `os.execvp` itself raises (e.g., the freshly-`uv tool install`-ed binary is somehow not on `$PATH`)?**
   - What we know: `os.execvp` only returns to the caller on failure (raising `OSError`); on success it never returns.
   - What's unclear: CONTEXT.md's locked decisions describe the failure mode of *the upgrade itself* (`_install_and_verify` raising) but not a failure of the *re-exec* step after a successful, verified install.
   - Recommendation: wrap the `execvp` call in `try/except OSError` and fall back to printing a warning + continuing on the *old* process (same "never block" philosophy as the rest of the feature) — this is a natural, low-risk extension of the existing locked behavior, not a new decision, so it's safe for the planner to include without re-opening CONTEXT.md.

## Sources

### Primary (HIGH confidence)
- `src/pcswitcher/cli.py` (read in full this session) — `main()`, `_version_callback`, `update`, `_run_uv_tool_install`, `_verify_installed_version`, `_resolve_target_version`
- `src/pcswitcher/version.py` (read in full) — `get_this_version`, `get_highest_release`, `Version`/`Release` comparisons
- `src/pcswitcher/confirmer.py`, `src/pcswitcher/logger.py` (read in full) — `is_interactive`, `TerminalUIConfirmer`
- `src/pcswitcher/config_sync.py` (partial read) — established `Prompt.ask` idiom
- `tests/unit/cli/test_self_update.py`, `tests/unit/cli/test_commands.py`, `tests/unit/cli/test_config_sync.py`, `tests/unit/test_version.py` — existing test patterns
- Live probes this session: `uv run python -c "import typer, click; ..."` (version check), throwaway `CliRunner` script confirming `is_terminal`/`stdin.isatty()` behavior

### Secondary (MEDIUM confidence)
- Context7 `/fastapi/typer` — `docs/tutorial/commands/context.md`, `docs/tutorial/options/version.md`: `ctx.invoked_subcommand`, `is_eager` ordering `[CITED]`

### Tertiary (LOW confidence)
- I/O buffering/flush behavior across `os.execvp` — standard CPython semantics, not verified against this repo's specific Rich/Console buffering this session `[ASSUMED]`

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
| - | ----- | ------- | ------------- |
| A1 | `sys.stdout.flush()`/`sys.stderr.flush()` before `execvp` is necessary to avoid losing the pre-exec "restarting" message | Focus Q3 | Low — worst case a cosmetic message is dropped; upgrade itself is unaffected |
| A2 | ADR-004 should NOT be amended for this feature | Focus Q7 / Docs | Low — a doc-placement judgment call, easily corrected in review if the planner disagrees |

## Metadata

**Confidence breakdown:**
- Callback/skip mechanics: HIGH — verified against installed Typer/Click version and live probes, cross-checked with Context7 docs
- Extraction plan for `_install_and_verify`: HIGH — read exact existing code and exact existing test assertions
- Testing approach: HIGH — verified CliRunner TTY behavior directly this session
- Re-exec flush timing: LOW/ASSUMED — not independently verified this session

**Research date:** 2026-07-19

**Valid until:** Not time-sensitive — based on this repo's current code, not an external fast-moving dependency; re-check only if `cli.py`/`version.py`/`confirmer.py` change materially before implementation.
