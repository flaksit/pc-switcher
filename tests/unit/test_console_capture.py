"""#250 — a captured console prints plain text on every machine that runs the suite.

Every test that reads a buffer as text rests on this. Without these, a console that starts
honouring the environment again only fails for the developers whose environment sets
`FORCE_COLOR`, and only as an assertion claiming the text was never printed.
"""

from __future__ import annotations

import pytest

from tests.unit.console_capture import PlainBuffer, captured_console


class TestStylingCannotComeFromTheEnvironment:
    @pytest.mark.parametrize("force_color", ["1", "3", "true"])
    def test_a_styled_message_reaches_the_buffer_as_its_characters(
        self, force_color: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rich reads `FORCE_COLOR` when the console is built, so it is set first."""
        monkeypatch.setenv("FORCE_COLOR", force_color)
        console, buffer = captured_console()

        console.print("[bold]Job outcomes:[/bold] [dim]apt_sync[/dim]")

        assert buffer.getvalue() == "Job outcomes: apt_sync\n"

    def test_a_terminal_console_is_styled_no_more_than_the_others(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`terminal=True` buys the is_terminal branches, not escape sequences."""
        monkeypatch.setenv("FORCE_COLOR", "3")
        console, buffer = captured_console(terminal=True)

        console.print("[bold]Install packages[/bold]")

        assert console.is_terminal is True
        assert buffer.getvalue() == "Install packages\n"

    def test_a_console_the_environment_still_reaches_is_reported_as_that(self) -> None:
        """The guard names the cause; an assertion on the message alone would report only
        that some expected text was missing."""
        buffer = PlainBuffer()
        buffer.write("\x1b[1mJob outcomes:\x1b[0m")

        with pytest.raises(AssertionError, match="captured_console"):
            _ = buffer.getvalue()


class TestNothingDependsOnTheTerminalRunningTheSuite:
    def test_width_is_pinned_against_the_environments_columns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COLUMNS", "37")
        console, _ = captured_console(width=100)

        assert console.width == 100

    def test_a_captured_console_is_not_a_terminal_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`FORCE_COLOR` makes Rich call even a `StringIO` console a terminal, which would
        send the interactive branches down a path CI never takes."""
        monkeypatch.setenv("FORCE_COLOR", "3")
        console, _ = captured_console()

        assert console.is_terminal is False
