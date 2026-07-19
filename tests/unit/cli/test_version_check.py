"""Tests for the startup version check (#176).

Covers `_maybe_check_for_update` (direct-call style, mirroring
test_self_update.py) and the Typer-wiring cases that depend on Click's
context handling (`--no-version-check`, the `self`-subcommand guard;
CliRunner style, mirroring test_commands.py).

CRITICAL: every test that reaches the "yes -> upgrade" branch patches
`pcswitcher.cli.os.execvp` - an unpatched execvp replaces the pytest
process and silently kills the run.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from collections.abc import Callable, Generator
from unittest.mock import MagicMock, patch

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from pcswitcher import cli
from pcswitcher.version import Release, Version

runner = CliRunner()

CURRENT_VERSION = Version.parse("1.0.0")
HIGHER_RELEASE = Release(Version.parse("1.1.0"), is_prerelease=False, tag="v1.1.0")
SAME_RELEASE = Release(Version.parse("1.0.0"), is_prerelease=False, tag="v1.0.0")
LOWER_RELEASE = Release(Version.parse("0.9.0"), is_prerelease=False, tag="v0.9.0")

_SKIP_ENV_VAR = "PCSWITCHER_SKIP_VERSION_CHECK"


@pytest.fixture(autouse=True)
def _isolate_skip_env_var() -> Generator[None]:  # pyright: ignore[reportUnusedFunction]
    """Guarantee PCSWITCHER_SKIP_VERSION_CHECK doesn't leak across tests.

    The "yes -> upgrade" code path writes directly to `os.environ` (the
    re-exec guard) rather than through `monkeypatch.setenv`. Calling
    `monkeypatch.delenv` afterwards to clean that up is a footgun: monkeypatch
    would record the mutated value as the "original" and restore it at
    teardown, re-leaking it into the next test. Plain dict manipulation here
    avoids that entirely.
    """
    os.environ.pop(_SKIP_ENV_VAR, None)
    yield
    os.environ.pop(_SKIP_ENV_VAR, None)


def _capture_console() -> Console:
    """Build a Rich console that force-renders into an in-memory buffer.

    force_terminal ensures Rich's own markup renders, not `console.is_terminal`
    for the TTY gate - that gate is controlled separately via
    `patch("pcswitcher.cli.is_interactive", ...)` so tests are deterministic
    regardless of StringIO's own terminal detection.
    """
    return Console(file=io.StringIO(), force_terminal=True)


def _subprocess_side_effect(version_stdout: str) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Route `uv tool install` and `pc-switcher --version` subprocess.run calls to success results."""

    def _side_effect(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "uv":
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Installed", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=version_stdout, stderr="")

    return _side_effect


class TestMaybeCheckForUpdateUpgradeFlow:
    """update available -> yes -> upgrade + re-exec; and the decline path."""

    def test_update_available_yes_upgrades_and_reexecs(self) -> None:
        """Accepting the prompt installs the release, sets the guard env var, and re-execs."""
        console = _capture_console()
        with (
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli.get_this_version", return_value=CURRENT_VERSION),
            patch("pcswitcher.cli.get_highest_release", return_value=HIGHER_RELEASE),
            patch("pcswitcher.cli.Prompt.ask", return_value="y"),
            patch("pcswitcher.cli.subprocess.run", side_effect=_subprocess_side_effect("pc-switcher 1.1.0")),
            patch("pcswitcher.cli.os.execvp") as mock_execvp,
        ):
            cli._maybe_check_for_update(console, no_version_check=False)  # pyright: ignore[reportPrivateUsage]

        mock_execvp.assert_called_once_with(sys.argv[0], sys.argv)
        assert os.environ[_SKIP_ENV_VAR] == "1"

    def test_update_available_no_continues_without_reexec(self) -> None:
        """Declining the prompt returns without installing or re-executing."""
        console = _capture_console()
        with (
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli.get_this_version", return_value=CURRENT_VERSION),
            patch("pcswitcher.cli.get_highest_release", return_value=HIGHER_RELEASE),
            patch("pcswitcher.cli.Prompt.ask", return_value="n") as mock_prompt,
            patch("pcswitcher.cli.os.execvp") as mock_execvp,
        ):
            cli._maybe_check_for_update(console, no_version_check=False)  # pyright: ignore[reportPrivateUsage]

        mock_prompt.assert_called_once()
        mock_execvp.assert_not_called()


class TestMaybeCheckForUpdateNoPromptCases:
    """already up to date -> no prompt; non-TTY -> skip."""

    def test_already_up_to_date_no_prompt(self) -> None:
        """When latest stable <= current, the prompt is never shown."""
        console = _capture_console()
        with (
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli.get_this_version", return_value=CURRENT_VERSION),
            patch("pcswitcher.cli.get_highest_release", return_value=SAME_RELEASE),
            patch("pcswitcher.cli.Prompt.ask") as mock_prompt,
        ):
            cli._maybe_check_for_update(console, no_version_check=False)  # pyright: ignore[reportPrivateUsage]

        mock_prompt.assert_not_called()

    def test_current_ahead_of_stable_no_prompt(self) -> None:
        """Running a dev build ahead of the latest stable release never prompts."""
        console = _capture_console()
        with (
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli.get_this_version", return_value=CURRENT_VERSION),
            patch("pcswitcher.cli.get_highest_release", return_value=LOWER_RELEASE),
            patch("pcswitcher.cli.Prompt.ask") as mock_prompt,
        ):
            cli._maybe_check_for_update(console, no_version_check=False)  # pyright: ignore[reportPrivateUsage]

        mock_prompt.assert_not_called()

    def test_non_tty_skips_check_entirely(self) -> None:
        """When not fully interactive, the check never even fetches releases."""
        console = _capture_console()
        with (
            patch("pcswitcher.cli.is_interactive", return_value=False),
            patch("pcswitcher.cli.get_highest_release") as mock_get_highest,
        ):
            cli._maybe_check_for_update(console, no_version_check=False)  # pyright: ignore[reportPrivateUsage]

        mock_get_highest.assert_not_called()


class TestMaybeCheckForUpdateSkipConditions:
    """`--no-version-check` (function-level) and `PCSWITCHER_SKIP_VERSION_CHECK` env skip."""

    def test_no_version_check_flag_skips(self) -> None:
        """Passing no_version_check=True short-circuits before any fetch."""
        console = _capture_console()
        with (
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli.get_highest_release") as mock_get_highest,
        ):
            cli._maybe_check_for_update(console, no_version_check=True)  # pyright: ignore[reportPrivateUsage]

        mock_get_highest.assert_not_called()

    def test_env_var_set_skips(self) -> None:
        """PCSWITCHER_SKIP_VERSION_CHECK being set skips the check entirely."""
        os.environ[_SKIP_ENV_VAR] = "1"
        console = _capture_console()
        with (
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli.get_highest_release") as mock_get_highest,
        ):
            cli._maybe_check_for_update(console, no_version_check=False)  # pyright: ignore[reportPrivateUsage]

        mock_get_highest.assert_not_called()

    def test_help_request_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`<cmd> --help` skips the check so help renders without blocking on the prompt (WR-01, #176)."""
        console = _capture_console()
        monkeypatch.setattr(cli.sys, "argv", ["pc-switcher", "sync", "--help"])
        with (
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli.get_highest_release") as mock_get_highest,
        ):
            cli._maybe_check_for_update(console, no_version_check=False)  # pyright: ignore[reportPrivateUsage]

        mock_get_highest.assert_not_called()


class TestMaybeCheckForUpdateFailureFallbacks:
    """Failure handling, scoped to the disk-mutation boundary.

    Before `uv` touches the install (check error, `uv` not launchable) → warn +
    continue. Once `uv` has run (upgrade failed, or succeeded but re-exec failed)
    → exit rather than keep running a now-stale process (#176).
    """

    def test_check_raises_warns_and_continues(self) -> None:
        """A RuntimeError from get_highest_release (offline/rate-limit/API) is swallowed."""
        console = _capture_console()
        with (
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli.get_this_version", return_value=CURRENT_VERSION),
            patch("pcswitcher.cli.get_highest_release", side_effect=RuntimeError("offline")),
            patch("pcswitcher.cli.Prompt.ask") as mock_prompt,
            patch("pcswitcher.cli.os.execvp") as mock_execvp,
        ):
            cli._maybe_check_for_update(console, no_version_check=False)  # pyright: ignore[reportPrivateUsage]

        mock_prompt.assert_not_called()
        mock_execvp.assert_not_called()
        assert "warning" in console.file.getvalue().lower()  # pyright: ignore[reportAttributeAccessIssue]

    def test_uv_not_launchable_warns_and_continues(self) -> None:
        """`uv` missing from PATH raises before any disk write, so the command still runs (#176).

        subprocess.run raising FileNotFoundError means `uv` never executed - the
        on-disk install is untouched, so the current process is not stale and we
        warn + continue rather than exit.
        """
        console = _capture_console()
        with (
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli.get_this_version", return_value=CURRENT_VERSION),
            patch("pcswitcher.cli.get_highest_release", return_value=HIGHER_RELEASE),
            patch("pcswitcher.cli.Prompt.ask", return_value="y"),
            patch("pcswitcher.cli.subprocess.run", side_effect=FileNotFoundError("uv not found")),
            patch("pcswitcher.cli.os.execvp") as mock_execvp,
        ):
            # Must return normally (no typer.Exit) so the invoking command still runs.
            cli._maybe_check_for_update(console, no_version_check=False)  # pyright: ignore[reportPrivateUsage]

        mock_execvp.assert_not_called()
        assert "warning" in console.file.getvalue().lower()  # pyright: ignore[reportAttributeAccessIssue]

    def test_upgrade_fails_after_install_exits_with_recovery_hint(self) -> None:
        """A non-zero `uv` install already touched disk: exit (not continue) with a recovery hint (#176)."""
        console = _capture_console()
        failed_install = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="install exploded")
        with (
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli.get_this_version", return_value=CURRENT_VERSION),
            patch("pcswitcher.cli.get_highest_release", return_value=HIGHER_RELEASE),
            patch("pcswitcher.cli.Prompt.ask", return_value="y"),
            patch("pcswitcher.cli.subprocess.run", return_value=failed_install),
            patch("pcswitcher.cli.os.execvp") as mock_execvp,
            pytest.raises(typer.Exit) as exc_info,
        ):
            cli._maybe_check_for_update(console, no_version_check=False)  # pyright: ignore[reportPrivateUsage]

        assert exc_info.value.exit_code == 1
        mock_execvp.assert_not_called()
        output = console.file.getvalue()  # pyright: ignore[reportAttributeAccessIssue]
        assert "self update" in output  # recovery hint present

    def test_reexec_oserror_exits_asking_rerun(self) -> None:
        """A verified install then an execvp OSError must exit, asking to re-run, not continue stale (#176)."""
        console = _capture_console()
        with (
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli.get_this_version", return_value=CURRENT_VERSION),
            patch("pcswitcher.cli.get_highest_release", return_value=HIGHER_RELEASE),
            patch("pcswitcher.cli.Prompt.ask", return_value="y"),
            patch("pcswitcher.cli.subprocess.run", side_effect=_subprocess_side_effect("pc-switcher 1.1.0")),
            patch("pcswitcher.cli.os.execvp", side_effect=OSError("no such file")) as mock_execvp,
            pytest.raises(typer.Exit) as exc_info,
        ):
            cli._maybe_check_for_update(console, no_version_check=False)  # pyright: ignore[reportPrivateUsage]

        assert exc_info.value.exit_code == 1
        mock_execvp.assert_called_once()
        assert "re-run" in console.file.getvalue().lower()  # pyright: ignore[reportAttributeAccessIssue]


class TestMainCallbackWiring:
    """Typer/Click context wiring: --no-version-check flag and the `self`-subcommand guard."""

    def test_no_version_check_flag_short_circuits_before_fetch(self) -> None:
        """`pc-switcher --no-version-check logs` never calls get_highest_release."""
        with (
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli.get_highest_release") as mock_get_highest,
            patch("pcswitcher.cli.get_logs_directory", return_value=MagicMock(exists=lambda: False)),
        ):
            result = runner.invoke(cli.app, ["--no-version-check", "logs"])

        assert result.exit_code == 0, result.stdout
        mock_get_highest.assert_not_called()

    def test_self_subcommand_skips_version_check(self) -> None:
        """`pc-switcher self update` never calls _maybe_check_for_update (avoids a circular prompt)."""
        with (
            patch("pcswitcher.cli._maybe_check_for_update") as mock_check,
            patch("pcswitcher.cli._get_current_version_or_exit", return_value=SAME_RELEASE.version),
            patch("pcswitcher.cli._resolve_target_version", return_value=SAME_RELEASE),
        ):
            result = runner.invoke(cli.app, ["self", "update"])

        assert result.exit_code == 0, result.stdout
        mock_check.assert_not_called()

    def test_non_self_subcommand_invokes_version_check(self) -> None:
        """Mirror assertion: a real subcommand (e.g. `logs`) DOES call _maybe_check_for_update."""
        with (
            patch("pcswitcher.cli._maybe_check_for_update") as mock_check,
            patch("pcswitcher.cli.get_logs_directory", return_value=MagicMock(exists=lambda: False)),
        ):
            result = runner.invoke(cli.app, ["logs"])

        assert result.exit_code == 0, result.stdout
        mock_check.assert_called_once()


class TestSelfUpdateFailureModes:
    """`self update` failure handling that the startup path shares (#176)."""

    def test_uv_missing_shows_clean_error_not_traceback(self) -> None:
        """A missing `uv` binary (OSError) yields a clean error + exit 1, not a raw traceback (WR-02, #176)."""
        with (
            patch("pcswitcher.cli._get_current_version_or_exit", return_value=CURRENT_VERSION),
            patch("pcswitcher.cli._resolve_target_version", return_value=HIGHER_RELEASE),
            patch("pcswitcher.cli.subprocess.run", side_effect=FileNotFoundError("uv not found")),
        ):
            result = runner.invoke(cli.app, ["self", "update"])

        assert result.exit_code == 1
        # Exited cleanly via sys.exit, not by letting the OSError escape as a traceback.
        assert isinstance(result.exception, SystemExit)
        assert "Could not run the upgrade" in result.stdout
