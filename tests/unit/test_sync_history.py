"""Unit tests for the sync_history module."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from pcswitcher import sync_history
from pcswitcher.sync_history import (
    HISTORY_DIR,
    SyncRole,
    get_history_path,
    get_last_role,
    get_last_role_with_error,
    get_last_sync_state,
    get_record_role_command,
    parse_sync_state,
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

    def test_creates_history_file_and_directories(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role() should create the history file and parent directories."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.SOURCE)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        assert history_path.exists()
        assert history_path.parent.is_dir()

    def test_writes_source_role(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role(SOURCE) should write source role to file."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.SOURCE)
        assert get_last_role() == SyncRole.SOURCE

    def test_writes_target_role(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role(TARGET) should write target role to file."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.TARGET)
        assert get_last_role() == SyncRole.TARGET

    def test_overwrites_previous_role(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role() should overwrite previous role."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.SOURCE)
        assert get_last_role() == SyncRole.SOURCE
        record_role(SyncRole.TARGET)
        assert get_last_role() == SyncRole.TARGET

    def test_writes_peer_when_provided(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role(role, peer) writes last_peer alongside last_role."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.TARGET, peer="workstation-a")
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        data = json.loads(history_path.read_text())
        assert data["last_role"] == "target"
        assert data["last_peer"] == "workstation-a"

    def test_omits_peer_when_not_provided(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role(role) without peer does not add last_peer to a fresh file."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.SOURCE)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        data = json.loads(history_path.read_text())
        assert "last_peer" not in data

    def test_preserves_unrelated_keys_on_merge(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role must not erase unrelated keys present in the file."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(json.dumps({"last_role": "source", "custom_key": "preserved"}))
        record_role(SyncRole.TARGET, peer="laptop-b")
        data = json.loads(history_path.read_text())
        assert data["last_role"] == "target"
        assert data["last_peer"] == "laptop-b"
        assert data["custom_key"] == "preserved", "merge-preserving write must keep unrelated keys"


class TestParseSyncState:
    """Tests for parse_sync_state function (pure JSON parser for remote history)."""

    def test_exported_in_all(self) -> None:
        """parse_sync_state must be listed in __all__."""
        assert "parse_sync_state" in sync_history.__all__

    def test_valid_target_with_peer(self) -> None:
        """Returns (TARGET, peer) for valid JSON with target role and last_peer."""
        role, peer = parse_sync_state('{"last_role": "target", "last_peer": "pc1"}')
        assert role is SyncRole.TARGET
        assert peer == "pc1"

    def test_valid_source_with_peer(self) -> None:
        """Returns (SOURCE, peer) for valid JSON with source role and last_peer."""
        role, peer = parse_sync_state('{"last_role": "source", "last_peer": "laptop-b"}')
        assert role is SyncRole.SOURCE
        assert peer == "laptop-b"

    def test_valid_role_without_peer(self) -> None:
        """Returns (role, None) when last_peer key is absent."""
        role, peer = parse_sync_state('{"last_role": "source"}')
        assert role is SyncRole.SOURCE
        assert peer is None

    def test_malformed_json(self) -> None:
        """Returns (None, None) for non-JSON input."""
        assert parse_sync_state("not json at all") == (None, None)

    def test_non_dict_json(self) -> None:
        """Returns (None, None) when JSON root is not a dict."""
        assert parse_sync_state('["source", "target"]') == (None, None)

    def test_missing_last_role_key(self) -> None:
        """Returns (None, None) when last_role key is absent."""
        assert parse_sync_state('{"last_peer": "pc1"}') == (None, None)

    def test_invalid_role_value(self) -> None:
        """Returns (None, None) for an unrecognised last_role string."""
        assert parse_sync_state('{"last_role": "admin", "last_peer": "pc1"}') == (None, None)

    def test_non_string_peer_value(self) -> None:
        """Returns (role, None) when last_peer is present but not a string."""
        role, peer = parse_sync_state('{"last_role": "source", "last_peer": 42}')
        assert role is SyncRole.SOURCE
        assert peer is None

    def test_empty_string(self) -> None:
        """Returns (None, None) for an empty input string."""
        assert parse_sync_state("") == (None, None)


class TestGetLastSyncState:
    """Tests for get_last_sync_state function (reads local history file)."""

    def test_exported_in_all(self) -> None:
        """get_last_sync_state must be listed in __all__."""
        assert "get_last_sync_state" in sync_history.__all__

    def test_returns_none_none_when_file_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns (None, None) when no history file exists."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert get_last_sync_state() == (None, None)

    def test_returns_role_and_peer_from_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns (SyncRole.TARGET, peer) when history contains both fields."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text('{"last_role": "target", "last_peer": "pc1"}')
        role, peer = get_last_sync_state()
        assert role is SyncRole.TARGET
        assert peer == "pc1"

    def test_returns_none_none_for_corrupt_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns (None, None) when history file is not valid JSON."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text("corrupt{{")
        assert get_last_sync_state() == (None, None)

    def test_round_trips_with_record_role(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role followed by get_last_sync_state returns the persisted (role, peer)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        record_role(SyncRole.SOURCE, peer="workstation-a")
        role, peer = get_last_sync_state()
        assert role is SyncRole.SOURCE
        assert peer == "workstation-a"


class TestGetRecordRoleCommand:
    """Tests for get_record_role_command function."""

    def test_returns_command_for_source(self) -> None:
        """get_record_role_command(SOURCE) returns a merge-preserving shell command."""
        cmd = get_record_role_command(SyncRole.SOURCE)
        assert f"mkdir -p {HISTORY_DIR}" in cmd
        assert "python3" in cmd
        assert "last_role" in cmd
        assert SyncRole.SOURCE.value in cmd  # "source"

    def test_returns_command_for_target(self) -> None:
        """get_record_role_command(TARGET) returns a merge-preserving shell command."""
        cmd = get_record_role_command(SyncRole.TARGET)
        assert f"mkdir -p {HISTORY_DIR}" in cmd
        assert "python3" in cmd
        assert "last_role" in cmd
        assert SyncRole.TARGET.value in cmd  # "target"

    def test_command_includes_last_peer_when_provided(self) -> None:
        """Command string mentions last_peer when peer argument is given."""
        cmd = get_record_role_command(SyncRole.SOURCE, peer="workstation-a")
        assert "last_peer" in cmd
        assert "workstation-a" in cmd

    def test_command_omits_last_peer_when_not_provided(self) -> None:
        """Command string does not mention last_peer when peer is omitted."""
        cmd = get_record_role_command(SyncRole.SOURCE)
        assert "last_peer" not in cmd

    def test_remote_command_writes_last_peer(self, tmp_path: Path) -> None:
        """Executing the command writes last_peer into the remote history file."""
        cmd = get_record_role_command(SyncRole.TARGET, peer="laptop-b")
        result = subprocess.run(
            cmd,
            check=False,
            shell=True,
            env={**os.environ, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Remote command failed:\nstdout={result.stdout}\nstderr={result.stderr}"

        history_file = tmp_path / ".local" / "share" / "pc-switcher" / "sync-history.json"
        actual = json.loads(history_file.read_text())
        assert actual["last_role"] == "target"
        assert actual["last_peer"] == "laptop-b"

    def test_remote_command_is_merge_preserving(self, tmp_path: Path) -> None:
        """Executing the remote command preserves existing keys; also writes last_peer."""
        history_dir = tmp_path / ".local" / "share" / "pc-switcher"
        history_dir.mkdir(parents=True)
        history_file = history_dir / "sync-history.json"
        initial_data: dict[str, object] = {
            "last_role": "source",
            "custom_key": "must_survive",
        }
        history_file.write_text(json.dumps(initial_data))

        cmd = get_record_role_command(SyncRole.TARGET, peer="workstation-a")
        result = subprocess.run(
            cmd,
            check=False,
            shell=True,
            env={**os.environ, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Remote command failed:\nstdout={result.stdout}\nstderr={result.stderr}"

        actual = json.loads(history_file.read_text())
        assert actual["last_role"] == "target", "last_role should be updated to target"
        assert actual["last_peer"] == "workstation-a", "last_peer should be written"
        assert actual["custom_key"] == "must_survive", "unrelated keys must be preserved"

    def test_remote_command_creates_file_when_missing(self, tmp_path: Path) -> None:
        """Executing the remote command creates the file when no prior history exists."""
        cmd = get_record_role_command(SyncRole.SOURCE)
        result = subprocess.run(
            cmd,
            check=False,
            shell=True,
            env={**os.environ, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Remote command failed: {result.stderr}"

        history_file = tmp_path / ".local" / "share" / "pc-switcher" / "sync-history.json"
        assert history_file.exists()
        actual = json.loads(history_file.read_text())
        assert actual["last_role"] == "source"
