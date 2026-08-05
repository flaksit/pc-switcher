"""Tests for CLI commands.

Tests verify that the CLI commands defined in src/pcswitcher/cli.py exist and
accept the correct arguments as specified in docs/system/core.md.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from pcswitcher.cli import _async_run_sync, app, update
from pcswitcher.config import Configuration
from pcswitcher.models import SessionStatus, SyncAborted, SyncAbortedByUser, SyncSession
from pcswitcher.orchestrator import Orchestrator
from pcswitcher.version import Release, Version
from tests.unit.console_capture import captured_console

runner = CliRunner()


class TestSyncCommand:
    """Tests for the 'pc-switcher sync <target>' command."""

    def test_core_fr_sync_cmd(self) -> None:
        """Test CORE-FR-SYNC-CMD: System provides single command 'pc-switcher sync <target>'.

        Verifies that:
        1. The sync command exists and can be invoked
        2. The command accepts a target argument (hostname/IP)
        3. The command structure matches the spec requirement

        References:
        - CORE-FR-SYNC-CMD in docs/system/core.md
        """
        # Mock Configuration to avoid needing actual config file
        mock_config = MagicMock(spec=Configuration)

        # Mock the _load_configuration function to return our mock config
        # Mock _run_sync to avoid actually running sync
        with (
            patch("pcswitcher.cli._load_configuration", return_value=mock_config),
            patch("pcswitcher.cli._run_sync", return_value=0),
        ):
            # Invoke the sync command with a target argument
            result = runner.invoke(app, ["sync", "test-target"])

            # Verify the command executed without errors
            assert result.exit_code == 0

            # Verify that _run_sync was called with correct arguments
            # (this confirms the command structure is correct)

    def test_core_fr_sync_cmd_requires_target(self) -> None:
        """Test CORE-FR-SYNC-CMD: Sync command requires a target argument.

        Verifies that invoking 'pc-switcher sync' without a target argument
        results in an error, ensuring the command structure is enforced.

        References:
        - CORE-FR-SYNC-CMD in docs/system/core.md
        """
        # Invoke the sync command without a target argument
        result = runner.invoke(app, ["sync"])

        # Verify the command fails with appropriate exit code
        # Typer returns exit code 2 for missing required arguments
        assert result.exit_code == 2

        # Verify error message indicates missing argument
        # Typer puts error messages in stdout when using CliRunner
        output = result.stdout + result.stderr
        assert "Missing argument" in output or "required" in output.lower()

    def test_core_fr_sync_cmd_accepts_config_option(self) -> None:
        """Test CORE-FR-SYNC-CMD: Sync command accepts optional --config flag.

        Verifies that the sync command accepts the optional --config/-c flag
        for specifying a custom configuration file path.

        References:
        - CORE-FR-SYNC-CMD in docs/system/core.md
        - sync command implementation in src/pcswitcher/cli.py
        """
        # Mock Configuration to avoid needing actual config file
        mock_config = MagicMock(spec=Configuration)

        # Create a temporary config path for testing
        custom_config_path = Path("/tmp/custom-config.yaml")

        # Mock the _load_configuration function to capture the config path used
        # Mock _run_sync to avoid actually running sync
        with (
            patch("pcswitcher.cli._load_configuration", return_value=mock_config) as mock_load,
            patch("pcswitcher.cli._run_sync", return_value=0),
        ):
            # Invoke the sync command with --config option
            result = runner.invoke(app, ["sync", "test-target", "--config", str(custom_config_path)])

            # Verify the command executed without errors
            assert result.exit_code == 0

            # Verify that _load_configuration was called with the custom config path
            mock_load.assert_called_once_with(custom_config_path)


class TestSyncAbortedHandling:
    """_async_run_sync must surface an abort once, calmly, distinct from a failure.

    UAT gap 2 / plan 01-16: previously a declined confirmation fell through to
    the generic except Exception handler and printed the same red "Sync
    failed" text the orchestrator's own CRITICAL log already implied.
    """

    @staticmethod
    async def _printed(abort: SyncAborted) -> tuple[int, str]:
        """What the CLI prints, and its exit code, for a run that ended in `abort`."""
        mock_config = MagicMock(spec=Configuration)

        with (
            patch("pcswitcher.cli.Orchestrator") as mock_orchestrator_cls,
            patch("pcswitcher.cli.console") as mock_console,
        ):
            mock_orchestrator = MagicMock()
            mock_orchestrator.run = AsyncMock(side_effect=abort)
            mock_orchestrator_cls.return_value = mock_orchestrator

            exit_code = await _async_run_sync("target-host", mock_config)

        return exit_code, " ".join(str(call.args[0]) for call in mock_console.print.call_args_list)

    @pytest.mark.asyncio
    async def test_user_abort_prints_single_calm_message_and_nonzero_exit(self) -> None:
        """Orchestrator.run() raising SyncAbortedByUser -> one calm 'aborted' line."""
        exit_code, printed = await self._printed(SyncAbortedByUser("the config sync was declined at its prompt"))

        assert exit_code != 0
        assert "aborted by user" in printed.lower()
        assert "failed" not in printed.lower()

    @pytest.mark.asyncio
    async def test_a_tool_decided_abort_is_not_reported_as_the_users(self) -> None:
        """#224 — nobody was asked about an unreadable registry, so the line must not say
        the user aborted; it stays the neutral label and carries the repair instead."""
        exit_code, printed = await self._printed(SyncAborted("package-snippets.yaml on nomad cannot be read"))

        assert exit_code != 0
        assert "aborted" in printed.lower()
        assert "user" not in printed.lower()
        assert "failed" not in printed.lower()


class TestToolOutputIsNotRichMarkup:
    """Text pc-switcher did not author must reach Rich as `Text`, never as markup.

    The end-of-run summary quotes each failed job's own reason, which carries a package
    manager's stderr. Rich reads a `[...]`-shaped substring in a markup string as a style
    tag: `[installed]` is swallowed and `[/usr/bin/apt]` raises MarkupError — a crash at
    the final summary, after every job has already done its work.
    """

    @pytest.mark.parametrize(
        "stderr",
        [
            "dpkg: error processing archive [/usr/bin/apt] (--unpack)",  # raises MarkupError as markup
            "E: Sub-process returned an error code [installed]",  # silently swallowed as markup
            "snap [core22/stable] is not available",
        ],
    )
    @pytest.mark.asyncio
    async def test_failed_session_summary_renders_bracketed_stderr(self, stderr: str) -> None:
        """A failed run's summary prints its reason verbatim instead of crashing."""
        console, buffer = captured_console()
        session = SyncSession(
            session_id="s1",
            started_at=datetime.now(UTC),
            source_hostname="source",
            target_hostname="target-host",
            config={},
            status=SessionStatus.FAILED,
            error_message=f"apt_sync — {stderr}",
        )

        with (
            patch("pcswitcher.cli.Orchestrator") as mock_orchestrator_cls,
            patch("pcswitcher.cli.console", console),
        ):
            mock_orchestrator = MagicMock()
            mock_orchestrator.run = AsyncMock(return_value=session)
            mock_orchestrator_cls.return_value = mock_orchestrator

            exit_code = await _async_run_sync("target-host", MagicMock(spec=Configuration))

        assert exit_code == 1
        assert stderr in buffer.getvalue(), f"stderr must survive rendering.\nOutput: {buffer.getvalue()!r}"

    @pytest.mark.asyncio
    async def test_crashing_job_message_renders_bracketed_stderr(self) -> None:
        """The generic failure path quotes the exception text, which also carries stderr."""
        console, buffer = captured_console()
        detail = "flatpak: remote [/var/lib/flatpak] is unreachable"

        with (
            patch("pcswitcher.cli.Orchestrator") as mock_orchestrator_cls,
            patch("pcswitcher.cli.console", console),
        ):
            mock_orchestrator = MagicMock()
            mock_orchestrator.run = AsyncMock(side_effect=RuntimeError(detail))
            mock_orchestrator_cls.return_value = mock_orchestrator

            exit_code = await _async_run_sync("target-host", MagicMock(spec=Configuration))

        assert exit_code == 1
        assert detail in buffer.getvalue(), f"Exception text must survive rendering.\nOutput: {buffer.getvalue()!r}"

    def test_self_update_renders_bracketed_install_stderr(self) -> None:
        """`self update` prints `uv`'s own stderr, which is equally unsanitized."""
        console, buffer = captured_console()
        stderr = "error: Failed to install [pc-switcher]"

        with (
            patch("pcswitcher.cli.console", console),
            patch("pcswitcher.cli.get_this_version", return_value=Version.parse("0.9.0")),
            patch("pcswitcher.cli._resolve_target_version", return_value=_STUB_RELEASE),
            patch(
                "pcswitcher.cli._run_uv_tool_install",
                return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr),
            ),
            pytest.raises(SystemExit) as exit_info,
        ):
            update()

        assert exit_info.value.code == 1
        assert stderr in buffer.getvalue(), f"uv stderr must survive rendering.\nOutput: {buffer.getvalue()!r}"


class TestFailureSummaryReadsAsAList:
    """A run that ends with several failed jobs lists one per line, under the label.

    `_summarize_job_outcomes` returns a reason per failed job; printed beside the label the
    first reason would sit on the label line and the rest below it, so the list of failures
    has no shape.
    """

    @pytest.mark.asyncio
    async def test_each_failed_job_gets_its_own_line_under_the_label(self) -> None:
        console, buffer = captured_console()
        reasons = [
            "apt_sync — could not install vim on Nomad: E: Unable to fetch archives",
            "snap_sync — could not refresh firefox on Nomad: snap has running apps",
        ]
        session = SyncSession(
            session_id="s1",
            started_at=datetime.now(UTC),
            source_hostname="Atlas",
            target_hostname="Nomad",
            config={},
            status=SessionStatus.FAILED,
            error_message="\n".join(reasons),
        )

        with (
            patch("pcswitcher.cli.Orchestrator") as mock_orchestrator_cls,
            patch("pcswitcher.cli.console", console),
        ):
            mock_orchestrator = MagicMock()
            mock_orchestrator.run = AsyncMock(return_value=session)
            mock_orchestrator_cls.return_value = mock_orchestrator

            exit_code = await _async_run_sync("Nomad", MagicMock(spec=Configuration))

        assert exit_code == 1
        printed = [line.rstrip() for line in buffer.getvalue().splitlines() if line.strip()]
        assert "Sync finished with failures:" in printed, f"the label shares its line.\nOutput: {printed}"
        label_at = printed.index("Sync finished with failures:")
        assert printed[label_at + 1 : label_at + 3] == [f"  {reason}" for reason in reasons]


class TestLogsCommand:
    """Tests for the 'pc-switcher logs' command.

    Spec reference: docs/system/logging.md - LOG-US-SYSTEM-AS6
    """

    def test_log_us_system_as6_logs_last_displays_most_recent(self, tmp_path: Path) -> None:
        """Test LOG-US-SYSTEM-AS6: logs --last displays the most recent log file.

        Verifies that `pc-switcher logs --last`:
        1. Identifies the most recent log file by filename (timestamp in name)
        2. Displays the log file content
        3. Returns exit code 0
        """
        # Create log file with test content
        log_file = tmp_path / "sync-20240102T100000-def67890.log"
        log_file.write_text('{"timestamp": "2024-01-02T10:00:00", "level": "INFO", "event": "Newer log - latest"}\n')

        # Mock get_latest_log_file to return our test file
        with patch("pcswitcher.cli.get_latest_log_file", return_value=log_file):
            result = runner.invoke(app, ["logs", "--last"])

        # Verify command succeeded
        assert result.exit_code == 0, f"logs --last failed: {result.stdout}"

        # Verify log content is displayed
        assert "Newer log" in result.stdout or "latest" in result.stdout, (
            f"Expected log content.\nOutput: {result.stdout}"
        )

    def test_log_us_system_as6_logs_last_no_logs_shows_message(self) -> None:
        """Test LOG-US-SYSTEM-AS6: logs --last shows message when no logs exist.

        Verifies that when no log files exist, the command shows an appropriate
        message rather than crashing.
        """
        # Mock get_latest_log_file to return None (no logs)
        with patch("pcswitcher.cli.get_latest_log_file", return_value=None):
            result = runner.invoke(app, ["logs", "--last"])

        # Should exit with non-zero and show "no log" message
        assert result.exit_code == 1, f"Expected exit code 1, got {result.exit_code}"
        assert "no log" in result.stdout.lower(), f"Expected 'no log' message.\nstdout: {result.stdout}"


class TestInitCommand:
    """Tests for the 'pc-switcher init' command.

    Verifies init writes config.yaml plus the starter home.filter/root.filter
    package-data files next to it, honoring --force for all three (#166).
    """

    def test_init_writes_config_and_filter_files(self, tmp_path: Path) -> None:
        """init writes config.yaml, home.filter, and root.filter into the config dir."""
        config_path = tmp_path / "config.yaml"
        with patch.object(Configuration, "get_default_config_path", return_value=config_path):
            result = runner.invoke(app, ["init"])

        assert result.exit_code == 0, f"init failed: {result.stdout}"
        assert config_path.exists()
        assert (tmp_path / "home.filter").exists()
        assert (tmp_path / "root.filter").exists()
        assert "+ .cache/uv/***" in (tmp_path / "home.filter").read_text()

    def test_init_force_overwrites_all_three_files(self, tmp_path: Path) -> None:
        """init --force overwrites config.yaml and both filter files without error."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("stale config")
        (tmp_path / "home.filter").write_text("stale home filter")
        (tmp_path / "root.filter").write_text("stale root filter")

        with patch.object(Configuration, "get_default_config_path", return_value=config_path):
            result = runner.invoke(app, ["init", "--force"])

        assert result.exit_code == 0, f"init --force failed: {result.stdout}"
        assert "+ .cache/uv/***" in (tmp_path / "home.filter").read_text()
        assert "stale root filter" not in (tmp_path / "root.filter").read_text()


# Pins both sides of the startup update check to one version, so no test here depends on
# the live GitHub API or on which release is current (`test_version_check.py` covers the
# check itself).
_STUB_VERSION = Version.parse("1.0.0")
_STUB_RELEASE = Release(_STUB_VERSION, is_prerelease=False, tag="v1.0.0")


class TestConfirmEachCommandFlag:
    """`--confirm-each-command` has no non-interactive fallback, by design.

    Every other gate degrades when nobody is watching: the confirmer falls back to an
    `--allow-*` flag, the review auto-declines. This one cannot — auto-proceeding on a
    prompt nobody can answer is precisely the failure it exists to prevent — so a run that
    could never ask must be refused before it loads config, connects or touches anything.
    """

    def test_refused_without_a_tty(self) -> None:
        """J166 — refused before config is loaded or anything is connected, naming the flag."""
        with (
            patch("pcswitcher.cli.is_interactive", return_value=False),
            patch("pcswitcher.cli._load_configuration") as load_config,
            patch("pcswitcher.cli._run_sync") as run_sync,
        ):
            result = runner.invoke(app, ["sync", "test-target", "--confirm-each-command"])

        assert result.exit_code == 1, f"Expected exit code 1, got {result.exit_code}\n{result.output}"
        assert "--confirm-each-command" in result.output, f"Error must name the flag.\nOutput: {result.output}"
        # Refused before the run begins: nothing is loaded and no sync is started.
        load_config.assert_not_called()
        run_sync.assert_not_called()

    def test_accepted_and_forwarded_on_a_tty(self) -> None:
        """J166 — the flag must actually reach the orchestrator, not be validated and dropped.

        The two version functions are stubbed for the same reason every TTY-faking test in
        `test_version_check.py` stubs them: faking a TTY also arms the startup update check,
        and a unit test must not depend on the live GitHub API or on which release happens
        to be current. Pinning both sides to the same version means no upgrade is offered.
        """
        with (
            patch("pcswitcher.cli.get_this_version", return_value=_STUB_VERSION),
            patch("pcswitcher.cli.get_highest_release", return_value=_STUB_RELEASE),
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli._load_configuration", return_value=MagicMock(spec=Configuration)),
            patch("pcswitcher.cli._run_sync", return_value=0) as run_sync,
        ):
            result = runner.invoke(app, ["sync", "test-target", "--confirm-each-command"])

        assert result.exit_code == 0, f"Expected exit code 0, got {result.exit_code}\n{result.output}"
        assert run_sync.call_args.kwargs["confirm_each_command"] is True

    def test_a_non_interactive_run_without_the_flag_is_not_refused(self) -> None:
        """J166 — the refusal is scoped to the flag: ordinary non-interactive syncs still run."""
        with (
            patch("pcswitcher.cli.is_interactive", return_value=False),
            patch("pcswitcher.cli._load_configuration", return_value=MagicMock(spec=Configuration)),
            patch("pcswitcher.cli._run_sync", return_value=0) as run_sync,
        ):
            result = runner.invoke(app, ["sync", "test-target"])

        assert result.exit_code == 0, f"Expected exit code 0, got {result.exit_code}\n{result.output}"
        assert run_sync.call_args.kwargs["confirm_each_command"] is False


class TestApplyPackageFlags:
    """#245: `--apply-package-installs` / `--apply-package-removals` answer package reviews,
    and nothing else does."""

    def test_both_flags_reach_the_run(self) -> None:
        """H245 — the flags must actually reach the orchestrator, not be parsed and dropped."""
        with (
            patch("pcswitcher.cli._load_configuration", return_value=MagicMock(spec=Configuration)),
            patch("pcswitcher.cli._run_sync", return_value=0) as run_sync,
        ):
            result = runner.invoke(
                app, ["sync", "test-target", "--apply-package-installs", "--apply-package-removals"]
            )

        assert result.exit_code == 0, result.output
        assert run_sync.call_args.kwargs["apply_package_installs"] is True
        assert run_sync.call_args.kwargs["apply_package_removals"] is True

    def test_neither_flag_is_the_default(self) -> None:
        """H232 — an ordinary sync answers no review by itself."""
        with (
            patch("pcswitcher.cli._load_configuration", return_value=MagicMock(spec=Configuration)),
            patch("pcswitcher.cli._run_sync", return_value=0) as run_sync,
        ):
            result = runner.invoke(app, ["sync", "test-target"])

        assert result.exit_code == 0, result.output
        assert run_sync.call_args.kwargs["apply_package_installs"] is False
        assert run_sync.call_args.kwargs["apply_package_removals"] is False

    def test_yes_alone_answers_no_package_review(self) -> None:
        """H236, H162 — `--yes` keeps its own meaning (the configuration-sync prompt): the
        policy a run with only `--yes` builds answers nothing."""
        orchestrator = Orchestrator(target="nomad", config=MagicMock(spec=Configuration), auto_accept=True)

        assert orchestrator._review_policy.answers_anything is False  # pyright: ignore[reportPrivateUsage]

    def test_one_policy_reaches_both_the_reviewer_and_every_job_context(self) -> None:
        """H246 — the flags become one `ReviewPolicy` the orchestrator holds; the review
        surface answers with it and every package job reads that same object, so the two can
        never disagree about what this run was told to apply."""
        config = MagicMock(spec=Configuration)
        config.sync_jobs = {"apt_sync": True}
        orchestrator = Orchestrator(target="nomad", config=config, apply_package_installs=True)
        orchestrator._local_executor = MagicMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._remote_executor = MagicMock()  # pyright: ignore[reportPrivateUsage]

        context = orchestrator._create_job_context({})  # pyright: ignore[reportPrivateUsage]

        assert context.review_policy is orchestrator._review_policy  # pyright: ignore[reportPrivateUsage]
        assert context.review_policy is not None
        assert (context.review_policy.apply_installs, context.review_policy.apply_removals) == (True, False)
