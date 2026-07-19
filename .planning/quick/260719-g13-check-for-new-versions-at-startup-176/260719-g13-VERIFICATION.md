---
phase: 260719-g13-check-for-new-versions-at-startup-176
verified: 2026-07-19T00:00:00Z
status: passed
score: 5/5 must-haves verified
behavior_unverified: 0
overrides_applied: 0
---

# Quick Task 260719-g13: Check for new versions at startup (#176) Verification Report

**Task Goal:** On startup, pc-switcher checks GitHub for a newer STABLE release and, in an interactive terminal, offers to upgrade-and-restart; skippable via `--no-version-check` / env / non-TTY / `self` / bare invocation; never fatal on any failure.

**Verified:** 2026-07-19. **Status:** passed. **Re-verification:** No — initial verification.

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Real subcommand in interactive terminal with newer stable release prints current-vs-available and prompts y/N | VERIFIED | `src/pcswitcher/cli.py:718-721` prints `"A new stable version... {current} -> {latest}"` then `Prompt.ask("Upgrade now?", choices=["y","n"], default="n")`. Behavioral test: `test_update_available_yes_upgrades_and_reexecs` PASSED. |
| 2 | y installs via existing install+verify path then re-execs same argv; N continues unchanged | VERIFIED | `cli.py:725-743` calls `_install_and_verify(latest)`, sets guard env, flushes stdio, `os.execvp(sys.argv[0], sys.argv)`. N-path returns at `cli.py:723`. Tests `test_update_available_yes_upgrades_and_reexecs` (asserts `execvp` called with `(sys.argv[0], sys.argv)` and env var set) and `test_update_available_no_continues_without_reexec` (asserts `execvp` NOT called) both PASSED. |
| 3 | Check skipped for `--no-version-check`, `PCSWITCHER_SKIP_VERSION_CHECK`, non-TTY, `self`, bare invocation, `--version` | VERIFIED | `cli.py:705` single guard covers flag/env/TTY; `cli.py:116` guards `self`/bare via `ctx.invoked_subcommand not in (None, "self")`; `--version` is Typer's `is_eager` callback (`cli.py:101-103`) which raises `typer.Exit()` before the callback body runs. Unit tests cover flag, env, non-TTY, `self`. Bare invocation and `--version` are not covered by the unit suite but were independently confirmed live in this session: `runner.invoke(cli.app, [])` and `runner.invoke(cli.app, ["--version"])` both exit without calling `_maybe_check_for_update` (mocked and asserted `.called == False`). |
| 4 | Any check/upgrade/execvp failure warns and continues, never fatal | VERIFIED | `cli.py:708-713` (broad `except Exception`), `726-732` (`except UpdateFailedError` — warn, return, no `sys.exit`), `742-745` (`except OSError` on `execvp` — warn, falls through). Tests `test_check_raises_warns_and_continues`, `test_upgrade_fails_warns_and_continues`, `test_reexec_oserror_warns_and_continues` all PASSED, each asserting no exception propagates and a warning string is printed. |
| 5 | `self update` still emits exact existing strings (`Successfully updated`, `Already at version`, downgrade warning, dim-stderr detail) after extraction | VERIFIED | Exact strings preserved verbatim: `cli.py:674` `"[green]Already at version {current_display}[/green]"`, `cli.py:678` downgrade warning, `cli.py:685-687` `[bold red]Error:[/bold red] {e}` + `[dim]{e.detail}[/dim]`, `cli.py:690` `"[green]Successfully updated to version {target_display}[/green]"`. `tests/unit/cli/test_self_update.py` passes unchanged against the refactored `_run_uv_tool_install`/`_verify_installed_version`. The VM-gated `tests/integration/test_self_update.py` (source of the literal assertions at lines 146, 194) was not runnable in this session — string match confirmed by direct code read, not by executing that integration test. |

**Score:** 5/5 truths verified (0 present-but-behavior-unverified)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/pcswitcher/cli.py` | `UpdateFailedError`, `_install_and_verify`, `_maybe_check_for_update`, `--no-version-check` option + `ctx` on `main` | VERIFIED | All four present: class at `524`, function at `576`, function at `693`, option+ctx at `97-108`. Confirmed importable and callable via `python -c` symbol check. |
| `tests/unit/cli/test_version_check.py` | Covers every CONTEXT.md branch plus execvp-OSError fallback | VERIFIED | 13 tests, all PASSED. Covers upgrade-yes/no, already-up-to-date, ahead-of-stable, non-TTY, flag skip, env skip, check-raises, upgrade-fails, execvp-OSError, `--no-version-check` CliRunner, `self` guard (both directions). |
| `README.md` | Documents `--no-version-check`, startup check, `PCSWITCHER_SKIP_VERSION_CHECK` | VERIFIED | Lines 132-133 (flag in Available Commands), 136-138 (Startup version check subsection), 159 (rate-limit note extended to mention the startup check). |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `main` callback | `_maybe_check_for_update` | `ctx.invoked_subcommand not in (None, 'self')` guard | WIRED | `cli.py:116-117`; behaviorally confirmed both directions (`self` skips, `logs` invokes) via `test_self_subcommand_skips_version_check` / `test_non_self_subcommand_invokes_version_check`, plus live bare/`--version` checks in this session. |
| `_maybe_check_for_update` + `update` | `_install_and_verify(release)` | Shared helper, fork at call sites | WIRED | `_maybe_check_for_update` at `cli.py:727` (warn+return on failure), `update` at `cli.py:683` (print+exit on failure) — same helper, different `except` bodies. |
| Re-exec guard | `os.execvp(sys.argv[0], sys.argv)` | `PCSWITCHER_SKIP_VERSION_CHECK=1` set immediately before | WIRED | `cli.py:737-743`; test asserts env var == "1" and `execvp` called with exact argv. |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full unit test file | `uv run pytest tests/unit/cli/test_version_check.py -q` | 13 passed | PASS |
| Lint | `uv run ruff check src/pcswitcher/cli.py tests/unit/cli/test_version_check.py` | All checks passed | PASS |
| Format | `uv run ruff format --check .` | 88 files already formatted | PASS |
| Type check | `uv run basedpyright src/pcswitcher/cli.py tests/unit/cli/test_version_check.py` | 0 errors, 0 warnings, 0 notes | PASS |
| Symbol existence | `python -c "from pcswitcher import cli; ..."` | `UpdateFailedError`/`_install_and_verify`/`_maybe_check_for_update` all present and callable | PASS |
| Full unit+contract suite (regression) | `uv run pytest tests/unit tests/contract -q` | 593 passed | PASS |
| Bare invocation skip | `runner.invoke(cli.app, [])` with `_maybe_check_for_update` mocked | exit 2 (Typer help/usage), mock NOT called | PASS |
| `--version` skip | `runner.invoke(cli.app, ["--version"])` with `_maybe_check_for_update` mocked | exit 0, prints version, mock NOT called | PASS |
| Existing self-update unit + command suites (regression) | `uv run pytest tests/unit/cli -q` | 61 passed | PASS |

### Anti-Patterns Found

None. Grep for `TBD|FIXME|XXX|TODO|HACK|PLACEHOLDER|not yet implemented|coming soon` across `src/pcswitcher/cli.py` and `tests/unit/cli/test_version_check.py` returned no matches. No stub returns, no hardcoded-empty data flowing to output.

### Requirements Coverage

Not applicable — this is a quick task under `.planning/quick/`, not a roadmap phase; there is no ROADMAP.md success-criteria entry or `REQUIREMENTS.md` phase mapping for it. The PLAN.md frontmatter `must_haves` (verified above) constitute the full contract, tied to GitHub issue #176.

### Human Verification Required

None. All must-haves are either directly verified by passing unit tests or independently confirmed live in this session (bare invocation, `--version`). The one item resting on code-inspection rather than an executed test — `self update`'s exact strings, per the VM-gated integration test — is a static string-literal match (not a state-transition/cancellation invariant), fully visible via grep/read, so it does not require a behavioral test to reach VERIFIED.

### Gaps Summary

None found. All 5 must-have truths, all 3 artifacts, and all 3 key links verified against the actual codebase (not SUMMARY.md claims). Full regression suite (593 tests) green; lint, format, and type checks clean; no debt markers in touched files.

_Verified 2026-07-19 by Claude (gsd-verifier)._
