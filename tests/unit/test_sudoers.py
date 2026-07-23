"""Unit tests for the passwordless-sudo remediation text.

A validation error that only says "passwordless sudo is not available" leaves the user
to research sudoers syntax and the safe way to edit it, so the actionable parts of this
message are behaviour worth pinning: the drop-in path, the visudo invocation, the grant
line, and the verification command.
"""

from __future__ import annotations

from pcswitcher.sudoers import SUDOERS_DROPIN_PATH, passwordless_sudo_hint


class TestPasswordlessSudoHint:
    def test_names_every_required_binary(self) -> None:
        hint = passwordless_sudo_hint(("/usr/bin/apt-get", "/usr/bin/install"))

        assert "/usr/bin/apt-get" in hint
        assert "/usr/bin/install" in hint

    def test_uses_the_drop_in_not_etc_sudoers(self) -> None:
        """Editing /etc/sudoers directly would fight the distribution's own file."""
        hint = passwordless_sudo_hint(("/usr/bin/rsync",))

        assert SUDOERS_DROPIN_PATH in hint
        assert "/etc/sudoers " not in hint

    def test_directs_the_user_through_visudo(self) -> None:
        """A syntax error written by a plain editor can lock the user out of sudo."""
        hint = passwordless_sudo_hint(("/usr/bin/rsync",))

        assert f"visudo -f {SUDOERS_DROPIN_PATH}" in hint
        assert "visudo" in hint

    def test_includes_a_verification_command(self) -> None:
        hint = passwordless_sudo_hint(("/usr/bin/rsync",))

        assert "sudo -n true" in hint

    def test_substitutes_a_known_user_into_the_grant_line(self) -> None:
        hint = passwordless_sudo_hint(("/usr/bin/snap",), user="alice")

        assert "alice ALL=(ALL) NOPASSWD: /usr/bin/snap" in hint
        assert "YOUR_USER" not in hint

    def test_flags_the_placeholder_when_the_user_is_unknown(self) -> None:
        hint = passwordless_sudo_hint(("/usr/bin/snap",))

        assert "YOUR_USER ALL=(ALL) NOPASSWD: /usr/bin/snap" in hint
        assert "replacing YOUR_USER" in hint

    def test_says_a_broader_grant_is_acceptable(self) -> None:
        """ADR-013's entry is a lower bound; a machine may grant wider rights."""
        hint = passwordless_sudo_hint(("/usr/bin/rsync",))

        assert "broader" in hint.lower()
