"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest

from pcswitcher.core.config import (
    ConfigError,
    apply_defaults,
    load_config,
    validate_module_config,
    validate_required_modules,
)
from pcswitcher.core.logging import LogLevel


def test_config_validates_btrfs_snapshots_present() -> None:
    """Test that config validation fails if btrfs_snapshots is missing."""
    config_path = Path("/tmp/test-config.yaml")

    with pytest.raises(ConfigError, match="Required module 'btrfs_snapshots' is missing"):
        validate_required_modules({"user_data": True}, config_path)


def test_config_validates_btrfs_snapshots_enabled() -> None:
    """Test that config validation fails if btrfs_snapshots is disabled."""
    config_path = Path("/tmp/test-config.yaml")

    with pytest.raises(ConfigError, match="Required module 'btrfs_snapshots' cannot be disabled"):
        validate_required_modules({"btrfs_snapshots": False}, config_path)


def test_config_validates_btrfs_snapshots_first() -> None:
    """Test that config validation fails if btrfs_snapshots is not first."""
    config_path = Path("/tmp/test-config.yaml")

    with pytest.raises(ConfigError, match="must be first in sync_modules"):
        validate_required_modules({"user_data": True, "btrfs_snapshots": True}, config_path)


def test_config_validates_btrfs_snapshots_success() -> None:
    """Test that config validation passes when btrfs_snapshots is first and enabled."""
    config_path = Path("/tmp/test-config.yaml")

    # Should not raise
    validate_required_modules({"btrfs_snapshots": True, "user_data": True}, config_path)


def test_apply_defaults_adds_disk_config() -> None:
    """Test that apply_defaults adds disk configuration with correct keys."""
    config_dict: dict = {"sync_modules": {"btrfs_snapshots": True}}

    result = apply_defaults(config_dict)

    assert "disk" in result
    assert result["disk"]["preflight_minimum"] == "20%"
    assert result["disk"]["runtime_minimum"] == "15%"
    assert result["disk"]["check_interval"] == 30


def test_apply_defaults_preserves_custom_disk_config() -> None:
    """Test that apply_defaults preserves user's custom disk settings."""
    config_dict: dict = {
        "sync_modules": {"btrfs_snapshots": True},
        "disk": {
            "min_free": 0.30,
            "reserve_minimum": 0.10,
        },
    }

    result = apply_defaults(config_dict)

    # User values preserved
    assert result["disk"]["min_free"] == 0.30
    assert result["disk"]["reserve_minimum"] == 0.10
    # Default for missing key added
    assert result["disk"]["check_interval"] == 30


def test_apply_defaults_adds_log_levels() -> None:
    """Test that apply_defaults adds default log levels."""
    config_dict: dict = {"sync_modules": {"btrfs_snapshots": True}}

    result = apply_defaults(config_dict)

    assert result["log_file_level"] == "FULL"
    assert result["log_cli_level"] == "INFO"


def test_validate_module_config_valid_schema() -> None:
    """Test that valid module config passes validation."""
    module_config = {
        "subvolumes": ["@", "@home"],
        "keep_recent": 3,
    }
    schema = {
        "type": "object",
        "properties": {
            "subvolumes": {"type": "array", "items": {"type": "string"}},
            "keep_recent": {"type": "integer"},
        },
    }

    # Should not raise
    validate_module_config("test_module", module_config, schema)


def test_validate_module_config_invalid_schema() -> None:
    """Test that invalid module config fails validation."""
    module_config = {
        "subvolumes": "not-an-array",  # Should be array
    }
    schema = {
        "type": "object",
        "properties": {
            "subvolumes": {"type": "array", "items": {"type": "string"}},
        },
    }

    with pytest.raises(ConfigError, match="Invalid configuration for module"):
        validate_module_config("test_module", module_config, schema)


def test_load_config_full_integration() -> None:
    """Test loading a complete config file."""
    config_content = """
log_file_level: DEBUG
log_cli_level: WARNING

disk:
  min_free: 0.25
  reserve_minimum: 0.12
  check_interval: 45

sync_modules:
  btrfs_snapshots: true
  dummy_success: false

btrfs_snapshots:
  subvolumes:
    - "@"
    - "@home"
"""

    with NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(config_content)
        config_path = Path(f.name)

    try:
        config = load_config(config_path)

        assert config.log_file_level == LogLevel.DEBUG
        assert config.log_cli_level == LogLevel.WARNING
        assert config.sync_modules == {"btrfs_snapshots": True, "dummy_success": False}
        assert config.disk["min_free"] == 0.25
        assert config.disk["reserve_minimum"] == 0.12
        assert config.disk["check_interval"] == 45
        assert config.module_configs["btrfs_snapshots"]["subvolumes"] == ["@", "@home"]
    finally:
        config_path.unlink()


def test_load_config_file_not_found() -> None:
    """Test that loading non-existent config file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_config(Path("/non/existent/path.yaml"))


def test_load_config_empty_file() -> None:
    """Test that loading empty config file raises ConfigError."""
    with NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("")  # Empty file
        config_path = Path(f.name)

    try:
        with pytest.raises(ConfigError, match="empty"):
            load_config(config_path)
    finally:
        config_path.unlink()


def test_load_config_missing_sync_modules() -> None:
    """Test that config without sync_modules raises ConfigError."""
    config_content = """
log_file_level: INFO
"""

    with NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(config_content)
        config_path = Path(f.name)

    try:
        with pytest.raises(ConfigError, match="Missing required field 'sync_modules'"):
            load_config(config_path)
    finally:
        config_path.unlink()


def test_load_config_invalid_log_level() -> None:
    """Test that invalid log level raises ConfigError."""
    config_content = """
log_file_level: INVALID_LEVEL
sync_modules:
  btrfs_snapshots: true
"""

    with NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(config_content)
        config_path = Path(f.name)

    try:
        with pytest.raises(ConfigError, match="Invalid log level"):
            load_config(config_path)
    finally:
        config_path.unlink()


def test_config_disk_keys_match_spec() -> None:
    """Test that disk config uses canonical key names from spec."""
    defaults = apply_defaults({"sync_modules": {"btrfs_snapshots": True}})

    # These are the canonical keys from the spec
    assert "preflight_minimum" in defaults["disk"]
    assert "runtime_minimum" in defaults["disk"]
    assert "check_interval" in defaults["disk"]

    # Old/incorrect keys should NOT be present
    assert "min_free" not in defaults["disk"]
    assert "reserve_minimum" not in defaults["disk"]
    assert "min_free_threshold" not in defaults["disk"]
