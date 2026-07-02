"""Unit tests for the reusable TerminalUIConfirmer (ADR-015 refinement).

Covers the two decision modes:
- interactive (TTY): pause the TUI, prompt, resume; return y/n outcome
- non-interactive (no TTY): fall back to the caller's --allow-* flag
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from pcswitcher.confirmer import TerminalUIConfirmer


def _mock_isatty(interactive: bool) -> MagicMock:
    """Create a mock for sys.stdin whose isatty() returns `interactive`."""
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = interactive
    return mock_stdin


def _make_confirmer() -> tuple[TerminalUIConfirmer, MagicMock, MagicMock]:
    """Build a confirmer wired to mock console + ui; return all three."""
    console = MagicMock()
    ui = MagicMock()
    return TerminalUIConfirmer(console, ui), console, ui


@pytest.mark.asyncio
class TestInteractive:
    """Interactive mode pauses the live display and honours the prompt answer."""

    async def test_yes_returns_true_and_toggles_ui(self) -> None:
        confirmer, _console, ui = _make_confirmer()
        with (
            patch("rich.prompt.Prompt.ask", return_value="y"),
            patch.object(sys, "stdin", _mock_isatty(True)),
        ):
            result = await confirmer.confirm(title="T", message="m", allow=False, allow_flag="--allow-x")
        assert result is True
        ui.stop.assert_called_once()
        ui.start.assert_called_once()

    async def test_no_returns_false(self) -> None:
        confirmer, _console, ui = _make_confirmer()
        with (
            patch("rich.prompt.Prompt.ask", return_value="n"),
            patch.object(sys, "stdin", _mock_isatty(True)),
        ):
            result = await confirmer.confirm(title="T", message="m", allow=False, allow_flag="--allow-x")
        assert result is False
        # UI is resumed even when the user declines.
        ui.start.assert_called_once()

    async def test_ui_resumed_when_prompt_raises(self) -> None:
        """If the prompt raises, the finally block still resumes the TUI."""
        confirmer, _console, ui = _make_confirmer()
        with (
            patch("rich.prompt.Prompt.ask", side_effect=KeyboardInterrupt),
            patch.object(sys, "stdin", _mock_isatty(True)),
            pytest.raises(KeyboardInterrupt),
        ):
            await confirmer.confirm(title="T", message="m", allow=False, allow_flag="--allow-x")
        ui.start.assert_called_once()


@pytest.mark.asyncio
class TestNonInteractive:
    """Without a TTY the --allow-* flag decides; the TUI is never paused."""

    async def test_allow_true_auto_approves(self) -> None:
        confirmer, console, ui = _make_confirmer()
        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await confirmer.confirm(title="T", message="m", allow=True, allow_flag="--allow-x")
        assert result is True
        ui.stop.assert_not_called()
        console.print.assert_not_called()

    async def test_allow_false_refuses_and_prints_hint(self) -> None:
        confirmer, console, ui = _make_confirmer()
        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await confirmer.confirm(title="T", message="m", allow=False, allow_flag="--allow-x")
        assert result is False
        ui.stop.assert_not_called()
        # The refusal prints the message and the flag hint for the user.
        console.print.assert_called()
        printed = " ".join(str(c.args[0]) for c in console.print.call_args_list if c.args)
        assert "--allow-x" in printed
