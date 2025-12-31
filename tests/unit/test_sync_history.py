"""Unit tests for the sync_history module."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcswitcher.sync_history import (
    HISTORY_DIR,
    HISTORY_PATH,
    SyncRole,
    get_history_path,
    get_last_role,
    get_last_role_with_error,
    get_record_role_command,
    record_role,
)


class TestHistoryPath:
    """Tests for history path utilities."""

    def test_get_history_path_returns_correct_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_history_path() should return path in .local/share/pc-switcher."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        expected = tmp_path / ".local/share/pc-switcher" / "sync-history.json"
        assert get_history_path() == expected


class TestGetLastRole:
    """Tests for get_last_role function."""

    def test_returns_none_when_no_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_last_role() should return None when history file doesn't exist."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert get_last_role() is None

    def test_returns_source_when_last_was_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_last_role() should return SOURCE when last role was source."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text('{"last_role": "source"}')
        assert get_last_role() == SyncRole.SOURCE

    def test_returns_target_when_last_was_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_last_role() should return TARGET when last role was target."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text('{"last_role": "target"}')
        assert get_last_role() == SyncRole.TARGET

    def test_returns_none_for_invalid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_last_role() should return None for invalid JSON."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text("not valid json")
        assert get_last_role() is None

    def test_returns_none_for_missing_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_last_role() should return None when last_role key is missing."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text('{"other_key": "value"}')
        assert get_last_role() is None

    def test_returns_none_for_invalid_role_value(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_last_role() should return None for invalid role value."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text('{"last_role": "invalid"}')
        assert get_last_role() is None


class TestGetLastRoleWithError:
    """Tests for get_last_role_with_error function."""

    def test_returns_no_error_for_missing_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing file should not be treated as an error."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        role, had_error = get_last_role_with_error()
        assert role is None
        assert had_error is False

    def test_returns_error_for_invalid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invalid JSON should be treated as an error."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text("not valid json")
        role, had_error = get_last_role_with_error()
        assert role is None
        assert had_error is True

    def test_returns_error_for_missing_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing key should be treated as an error."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text('{"other_key": "value"}')
        role, had_error = get_last_role_with_error()
        assert role is None
        assert had_error is True

    def test_returns_no_error_for_valid_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Valid source role should not be treated as an error."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text('{"last_role": "source"}')
        role, had_error = get_last_role_with_error()
        assert role == SyncRole.SOURCE
        assert had_error is False


class TestRecordRole:
    """Tests for record_role function."""

    def test_creates_history_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role() should create the history file."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.SOURCE)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        assert history_path.exists()

    def test_creates_parent_directories(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role() should create parent directories if needed."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.SOURCE)
        parent_dir = tmp_path / ".local/share/pc-switcher"
        assert parent_dir.is_dir()

    def test_writes_source_role(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role(SOURCE) should write source role to file."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.SOURCE)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        content = history_path.read_text()
        assert '"last_role": "source"' in content

    def test_writes_target_role(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role(TARGET) should write target role to file."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.TARGET)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        content = history_path.read_text()
        assert '"last_role": "target"' in content

    def test_overwrites_previous_role(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role() should overwrite previous role."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.SOURCE)
        assert get_last_role() == SyncRole.SOURCE
        record_role(SyncRole.TARGET)
        assert get_last_role() == SyncRole.TARGET

    def test_round_trip_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role(SOURCE) followed by get_last_role() should return SOURCE."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.SOURCE)
        assert get_last_role() == SyncRole.SOURCE

    def test_round_trip_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role(TARGET) followed by get_last_role() should return TARGET."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.TARGET)
        assert get_last_role() == SyncRole.TARGET


class TestSyncRole:
    """Tests for SyncRole enum."""

    def test_source_value(self) -> None:
        """SyncRole.SOURCE should have value 'source'."""
        assert SyncRole.SOURCE.value == "source"

    def test_target_value(self) -> None:
        """SyncRole.TARGET should have value 'target'."""
        assert SyncRole.TARGET.value == "target"


class TestGetRecordRoleCommand:
    """Tests for get_record_role_command function."""

    def test_returns_command_for_source(self) -> None:
        """get_record_role_command(SOURCE) should return valid shell command."""
        cmd = get_record_role_command(SyncRole.SOURCE)
        assert f"mkdir -p {HISTORY_DIR}" in cmd
        assert '"last_role": "source"' in cmd
        assert HISTORY_PATH in cmd

    def test_returns_command_for_target(self) -> None:
        """get_record_role_command(TARGET) should return valid shell command."""
        cmd = get_record_role_command(SyncRole.TARGET)
        assert f"mkdir -p {HISTORY_DIR}" in cmd
        assert '"last_role": "target"' in cmd
        assert HISTORY_PATH in cmd

    def test_command_creates_directory(self) -> None:
        """Command should include mkdir -p to create parent directories."""
        cmd = get_record_role_command(SyncRole.SOURCE)
        assert "mkdir -p" in cmd

    def test_command_uses_echo_redirect(self) -> None:
        """Command should use echo with redirect to write file."""
        cmd = get_record_role_command(SyncRole.SOURCE)
        assert "echo" in cmd
        assert ">" in cmd
