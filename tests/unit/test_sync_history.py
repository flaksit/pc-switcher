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
    UNKNOWN_GENERATION,
    SyncRole,
    get_history_path,
    get_last_role,
    get_last_role_with_error,
    get_record_role_command,
    get_target_generation,
    record_role,
    set_target_generation,
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

    def test_remote_command_is_merge_preserving(self, tmp_path: Path) -> None:
        """Executing the remote command preserves an existing target_generations map."""
        # Pre-seed sync-history.json with last_role and target_generations
        history_dir = tmp_path / ".local" / "share" / "pc-switcher"
        history_dir.mkdir(parents=True)
        history_file = history_dir / "sync-history.json"
        initial_data: dict[str, object] = {
            "last_role": "source",
            "target_generations": {"host-b": {"/home": 54321, "/root": 1234}},
        }
        history_file.write_text(json.dumps(initial_data))

        cmd = get_record_role_command(SyncRole.TARGET)
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
        assert actual["target_generations"] == {"host-b": {"/home": 54321, "/root": 1234}}, (
            "target_generations should be preserved unchanged"
        )

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


class TestGetTargetGeneration:
    """Tests for get_target_generation function."""

    def test_returns_none_when_no_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns None when sync-history.json does not exist."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert get_target_generation("host-b", "/home") is None

    def test_returns_none_for_old_format_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns None when the file has only last_role (pre-extension format)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text('{"last_role": "source"}')
        assert get_target_generation("host-b", "/home") is None

    def test_returns_none_for_missing_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns None when the target host has no entry in target_generations."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps({"last_role": "source", "target_generations": {"other-host": {"/home": 1}}})
        )
        assert get_target_generation("host-b", "/home") is None

    def test_returns_none_for_missing_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns None when the path has no entry for the given host."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(json.dumps({"target_generations": {"host-b": {"/root": 999}}}))
        assert get_target_generation("host-b", "/home") is None

    def test_round_trip_single_pair(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """set then get returns the same generation for a single (host, path) pair."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        set_target_generation("host-b", "/home", 54321)
        assert get_target_generation("host-b", "/home") == 54321

    def test_round_trip_multiple_pairs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """round-trip works independently for multiple (host, path) pairs."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        set_target_generation("host-b", "/home", 100)
        set_target_generation("host-b", "/root", 200)
        set_target_generation("host-c", "/home", 300)

        assert get_target_generation("host-b", "/home") == 100
        assert get_target_generation("host-b", "/root") == 200
        assert get_target_generation("host-c", "/home") == 300
        # Unset entries still return None
        assert get_target_generation("host-c", "/root") is None


class TestSetTargetGeneration:
    """Tests for set_target_generation function."""

    def test_creates_file_when_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Creates the history file and parent directories when they do not exist."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        assert not history_path.exists()
        set_target_generation("host-b", "/home", 42)
        assert history_path.exists()
        data = json.loads(history_path.read_text())
        assert data["target_generations"]["host-b"]["/home"] == 42

    def test_preserves_existing_last_role(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """set_target_generation does not erase an existing last_role value."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text('{"last_role": "source"}')
        set_target_generation("host-b", "/home", 99)
        data = json.loads(history_path.read_text())
        assert data["last_role"] == "source", "last_role must be preserved by set_target_generation"
        assert data["target_generations"]["host-b"]["/home"] == 99

    def test_preserves_other_targets(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Updating one target does not remove entries for other targets."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        set_target_generation("host-b", "/home", 10)
        set_target_generation("host-c", "/home", 20)
        # Updating host-b must not touch host-c
        set_target_generation("host-b", "/home", 11)
        assert get_target_generation("host-c", "/home") == 20

    def test_updates_existing_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Calling set_target_generation twice updates the value in place."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        set_target_generation("host-b", "/home", 100)
        set_target_generation("host-b", "/home", 200)
        assert get_target_generation("host-b", "/home") == 200


class TestRecordRoleMergePreserving:
    """Tests that record_role preserves target_generations (Pitfall 4 guard)."""

    def test_record_role_preserves_target_generations(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_role(SOURCE) must not erase a target_generations map written earlier."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Simulate FolderSyncJob writing a divergence marker before record_role is called
        set_target_generation("host-b", "/home", 54321)
        # Now record_role as source — this must preserve the marker
        record_role(SyncRole.SOURCE)
        assert get_last_role() == SyncRole.SOURCE
        assert get_target_generation("host-b", "/home") == 54321, (
            "record_role must not erase target_generations (Pitfall 4)"
        )

    def test_record_role_preserves_target_generations_on_role_switch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Switching role from SOURCE to TARGET must not erase target_generations."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        set_target_generation("host-b", "/home", 1000)
        record_role(SyncRole.SOURCE)
        record_role(SyncRole.TARGET)
        assert get_last_role() == SyncRole.TARGET
        assert get_target_generation("host-b", "/home") == 1000


class TestUnknownGenerationSentinel:
    """Tests for the UNKNOWN_GENERATION sentinel constant (added for CR-02/WR-02)."""

    def test_unknown_generation_is_negative_one_and_exported(self) -> None:
        """UNKNOWN_GENERATION must equal -1 and be exported in __all__."""
        assert UNKNOWN_GENERATION == -1
        assert "UNKNOWN_GENERATION" in sync_history.__all__

    def test_unknown_generation_sentinel_round_trips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """set then get with UNKNOWN_GENERATION returns UNKNOWN_GENERATION, not None.

        UNKNOWN_GENERATION is a valid stored value distinct from None (never synced).
        Callers use None vs UNKNOWN_GENERATION to distinguish "never synced" from
        "baseline could not be established last run" — the two require different handling:
        the former is fail-open (first sync), the latter is fail-closed (guard is active).
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        set_target_generation("host-b", "/home", UNKNOWN_GENERATION)
        result = get_target_generation("host-b", "/home")
        assert result == UNKNOWN_GENERATION
        # Confirm it is distinct from None (the "never synced" marker)
        assert result is not None
