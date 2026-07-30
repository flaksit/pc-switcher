"""`PKG-FR-CREDENTIAL-PRIVACY`: a credential embedded in a URL is withheld from every route
out of the tool — the log, the confirmation prompt and the review's own text.
"""

from __future__ import annotations

import logging

from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.logger import CredentialRedactionFilter
from pcswitcher.redaction import redact_credentials

_SECRET_URL = "https://bearer:s3cr3t-token@esm.ubuntu.com/apps/ubuntu"
_SAFE_URL = "https://***@esm.ubuntu.com/apps/ubuntu"


class TestRedactCredentials:
    def test_the_whole_userinfo_goes_not_only_the_password(self) -> None:
        """A repository that authenticates with a bearer token puts the token where a
        username belongs, so keeping the username would keep the secret."""
        assert redact_credentials(_SECRET_URL) == _SAFE_URL
        assert redact_credentials("https://token-only@example.com/x") == "https://***@example.com/x"

    def test_a_url_without_a_credential_is_untouched(self) -> None:
        assert redact_credentials("https://cli.github.com/packages stable main") == (
            "https://cli.github.com/packages stable main"
        )

    def test_an_scp_style_target_is_untouched(self) -> None:
        """`folder_sync`'s rsync trace is full of these and none of them carries a secret."""
        assert redact_credentials("rsync -a alice@nomad:/home/alice/ /home/alice/") == (
            "rsync -a alice@nomad:/home/alice/ /home/alice/"
        )

    def test_it_is_idempotent(self) -> None:
        assert redact_credentials(redact_credentials(_SECRET_URL)) == _SAFE_URL

    def test_every_character_rfc_3986_allows_in_a_userinfo_is_matched(self) -> None:
        """Sub-delimiters `!$&'()*+,;=`, unreserved `-._~` and `:` are all legal in
        userinfo, so a generated password carrying one is still the secret."""
        for char in "!$&'()*+,;=-._~:":
            assert redact_credentials(f"https://user:pa{char}ss@example.test/repo") == (
                "https://***@example.test/repo"
            ), char

    def test_a_userinfo_of_nothing_but_legal_punctuation_is_matched(self) -> None:
        assert redact_credentials("https://!$&'()*+,;=-._~:@example.test/x") == "https://***@example.test/x"

    def test_a_percent_encoded_userinfo_is_matched(self) -> None:
        assert redact_credentials("https://us%40er:p%3Ass@example.test/x") == "https://***@example.test/x"

    def test_a_quoted_url_does_not_swallow_a_later_address(self) -> None:
        """`/` and whitespace are illegal in userinfo, which is what stops the match from
        running out of a shell command into the address after it."""
        line = "sudo sh -c 'echo deb https://ppa.example.test/ubuntu noble main' && mail ops@example.test"
        assert redact_credentials(line) == line

    def test_an_at_sign_in_a_query_string_is_left_alone(self) -> None:
        """`?` ends the authority, so nothing past it is a credential."""
        url = "https://example.test?notify=ops@example.test"
        assert redact_credentials(url) == url

    def test_a_credential_inside_a_longer_line_redacts_only_the_url(self) -> None:
        line = f"Types: deb\nURIs: {_SECRET_URL}\nSuites: noble\nSigned-By: /etc/apt/keyrings/k.gpg"
        assert _SECRET_URL not in redact_credentials(line)
        assert "Signed-By: /etc/apt/keyrings/k.gpg" in redact_credentials(line)


class TestCredentialRedactionFilter:
    @staticmethod
    def _record(msg: str, *args: object, **extra: str) -> logging.LogRecord:
        record = logging.LogRecord("pcswitcher", logging.DEBUG, __file__, 1, msg, args or None, None)
        record.__dict__.update(extra)
        return record

    def test_the_message_is_redacted(self) -> None:
        record = self._record(f"sudo apt-get install --assume-yes {_SECRET_URL}")

        assert CredentialRedactionFilter().filter(record)
        assert "s3cr3t-token" not in record.getMessage()

    def test_a_credential_arriving_through_args_is_redacted(self) -> None:
        """The format string is clean and the secret is in the argument — which is how a
        package manager's own output reaches the log."""
        record = self._record("stderr: %s", f"E: Failed to fetch {_SECRET_URL}")

        assert CredentialRedactionFilter().filter(record)
        assert "s3cr3t-token" not in record.getMessage()

    def test_structured_context_is_redacted(self) -> None:
        record = self._record("write failed", stderr=f"401 Unauthorized for {_SECRET_URL}")

        assert CredentialRedactionFilter().filter(record)
        assert "s3cr3t-token" not in record.__dict__["stderr"]


class TestItemDiffText:
    def test_every_string_the_user_reads_while_deciding_is_redacted(self) -> None:
        diff = ItemDiff(
            item_class=ItemClass.APT_SOURCE,
            diff_class=DiffClass.EXTRA_ON_TARGET,
            action=DiffAction.REMOVE,
            item_id="apt:source:vendor.sources",
            label=f"vendor.sources ({_SECRET_URL})",
            detail=f"nomad would stop getting software from {_SECRET_URL}",
            answer_hints=(f"delete {_SECRET_URL} from nomad", "leave it for now"),
        )

        assert "s3cr3t-token" not in diff.label
        assert diff.detail is not None and "s3cr3t-token" not in diff.detail
        assert diff.answer_hints is not None
        assert not any("s3cr3t-token" in hint for hint in diff.answer_hints)

    def test_the_item_id_is_left_alone(self) -> None:
        """It is the item's identity across runs and is what a recorded decision is keyed
        on, so rewriting it would make that decision unfindable."""
        diff = ItemDiff(
            item_class=ItemClass.APT_SOURCE,
            diff_class=DiffClass.EXTRA_ON_TARGET,
            action=DiffAction.REMOVE,
            item_id=f"apt:source:{_SECRET_URL}",
            label="vendor.sources",
        )

        assert diff.item_id == f"apt:source:{_SECRET_URL}"
