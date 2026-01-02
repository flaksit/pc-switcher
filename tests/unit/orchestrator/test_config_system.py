"""Unit tests for Configuration System (US-6).

Tests configuration loading, validation, defaults, and error handling
as specified in specs/001-foundation/spec.md User Story 6.

Test Coverage:
- FR-004: Jobs loaded in config order
- FR-011: Snapshots always active
- FR-028: Load from ~/.config/pc-switcher/config.yaml
- FR-029: YAML structure (global, sync_jobs, per-job)
- FR-030: Validate job configs against schemas
- FR-031: Apply defaults for missing values
- FR-032: Enable/disable jobs via sync_jobs
- FR-033: Clear error on syntax/validation failure
- US6-AS1: Load, validate, apply defaults
- US6-AS2: Independent file and CLI log levels
- US6-AS3: Enable/disable jobs via sync_jobs
- US6-AS4: Abort on missing required param
- US6-AS5: YAML syntax error handling
- Edge: Unknown job in config
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pcswitcher.config import Configuration, ConfigurationError
from pcswitcher.models import LogLevel


class TestConfigLoading:
    """Tests for basic configuration loading functionality."""

    def test_001_fr028_load_from_config_path(self, tmp_path: Path) -> None:
        """FR-028: Load configuration from ~/.config/pc-switcher/config.yaml.

        Verifies that Configuration.from_yaml() can load a config file from
        the specified path and parse basic structure.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
logging:
  file: DEBUG
  tui: INFO
"""
        )

        config = Configuration.from_yaml(config_file)

        assert config.logging.file == LogLevel.DEBUG.value
        assert config.logging.tui == LogLevel.INFO.value

    def test_001_fr028_get_default_config_path(self) -> None:
        """FR-028: Default config path is ~/.config/pc-switcher/config.yaml."""
        expected_path = Path.home() / ".config" / "pc-switcher" / "config.yaml"

        actual_path = Configuration.get_default_config_path()

        assert actual_path == expected_path

    def test_001_fr029_config_structure(self, tmp_path: Path) -> None:
        """FR-029: YAML structure includes global settings, sync_jobs, and per-job sections.

        Verifies that the config file supports:
        - Global settings (log levels)
        - sync_jobs section for enable/disable
        - Per-job configuration sections
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
# Global settings
logging:
  file: DEBUG
  tui: WARNING

# Job enable/disable
sync_jobs:
  dummy_success: true
  dummy_fail: false

# Per-job configuration
btrfs_snapshots:
  subvolumes: ["@", "@home"]
  keep_recent: 5
  max_age_days: 30

dummy_success:
  source_duration: 10
  target_duration: 10
"""
        )

        config = Configuration.from_yaml(config_file)

        # Global settings
        assert config.logging.file == LogLevel.DEBUG.value
        assert config.logging.tui == LogLevel.WARNING.value

        # sync_jobs section
        assert config.sync_jobs == {"dummy_success": True, "dummy_fail": False}

        # Per-job configuration
        # btrfs_snapshots and disk_space_monitor are special - they go into
        # dedicated dataclass fields, not job_configs
        assert config.btrfs_snapshots.subvolumes == ["@", "@home"]
        assert config.btrfs_snapshots.keep_recent == 5
        assert config.btrfs_snapshots.max_age_days == 30

        # Other jobs go into job_configs dict
        assert "dummy_success" in config.job_configs
        assert config.job_configs["dummy_success"]["source_duration"] == 10


class TestConfigValidation:
    """Tests for configuration validation against schemas."""

    def test_001_fr030_validate_job_configs(self, tmp_path: Path) -> None:
        """FR-030: Validate job configs against job schemas.

        Verifies that invalid job configuration (wrong type, out of range values)
        triggers validation errors.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
btrfs_snapshots:
  subvolumes: ["@home"]
  keep_recent: 0  # Invalid: must be >= 1
"""
        )

        with pytest.raises(ConfigurationError) as exc_info:
            Configuration.from_yaml(config_file)

        assert len(exc_info.value.errors) > 0
        # Verify error mentions the validation issue
        error_messages = [e.message for e in exc_info.value.errors]
        assert any("minimum" in msg.lower() or "0" in msg for msg in error_messages)

    def test_001_fr030_validate_job_config_types(self, tmp_path: Path) -> None:
        """FR-030: Validate job config parameter types.

        Verifies that wrong parameter types (e.g., string instead of integer)
        are caught during validation.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
disk_space_monitor:
  check_interval: "not-a-number"  # Invalid: must be integer
"""
        )

        with pytest.raises(ConfigurationError) as exc_info:
            Configuration.from_yaml(config_file)

        assert len(exc_info.value.errors) > 0
        error_messages = [e.message for e in exc_info.value.errors]
        assert any("type" in msg.lower() or "integer" in msg.lower() for msg in error_messages)

    def test_001_fr033_config_error_messages(self, tmp_path: Path) -> None:
        """FR-033: Clear error message on validation failure.

        Verifies that validation errors include:
        - Path to the invalid configuration
        - Description of what's wrong
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
btrfs_snapshots:
  subvolumes: []  # Invalid: minItems is 1
"""
        )

        with pytest.raises(ConfigurationError) as exc_info:
            Configuration.from_yaml(config_file)

        error = exc_info.value
        # Error message should be formatted with path and description
        assert "btrfs_snapshots" in str(error)
        assert len(error.errors) > 0
        # Check error has path and message fields
        assert error.errors[0].path
        assert error.errors[0].message

    def test_001_us6_as4_abort_on_missing_required_param(self, tmp_path: Path) -> None:
        """US6-AS4: Abort on missing required parameter.

        Verifies that when a job declares required config parameters
        and they are missing (with no defaults), the system refuses to run.
        """
        config_file = tmp_path / "config.yaml"
        # btrfs_snapshots requires 'subvolumes' parameter
        config_file.write_text(
            """
btrfs_snapshots:
  keep_recent: 3
  # Missing required 'subvolumes' parameter
"""
        )

        with pytest.raises(ConfigurationError) as exc_info:
            Configuration.from_yaml(config_file)

        error = exc_info.value
        assert len(error.errors) > 0
        # Error should mention the missing required field
        error_messages = [e.message for e in error.errors]
        assert any("required" in msg.lower() or "subvolumes" in msg.lower() for msg in error_messages)


class TestConfigDefaults:
    """Tests for default value application."""

    def test_001_fr031_apply_config_defaults(self, tmp_path: Path) -> None:
        """FR-031: Apply defaults for missing configuration values.

        Verifies that when optional config values are missing, the system
        applies reasonable defaults.
        """
        config_file = tmp_path / "config.yaml"
        # Minimal config - most values should get defaults
        config_file.write_text(
            """
# Only specify required values, let others default
btrfs_snapshots:
  subvolumes: ["@"]
"""
        )

        config = Configuration.from_yaml(config_file)

        # Logging defaults
        assert config.logging.file == LogLevel.DEBUG.value
        assert config.logging.tui == LogLevel.INFO.value
        assert config.logging.external == LogLevel.WARNING.value

        # sync_jobs defaults to empty dict
        assert config.sync_jobs == {}

        # Disk config uses defaults
        assert config.disk.preflight_minimum == "20%"
        assert config.disk.runtime_minimum == "15%"
        assert config.disk.warning_threshold == "25%"
        assert config.disk.check_interval == 30

        # Btrfs config uses defaults for unspecified values
        assert config.btrfs_snapshots.keep_recent == 3
        assert config.btrfs_snapshots.max_age_days is None

    def test_001_us6_as1_load_and_validate_config(self, tmp_path: Path) -> None:
        """US6-AS1: Load config, validate structure, apply defaults.

        Comprehensive test that configuration loading:
        1. Loads from file
        2. Validates structure
        3. Applies defaults for missing values
        4. Makes settings available via Configuration object
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
logging:
  tui: WARNING
  # file and external omitted - should use defaults

sync_jobs:
  dummy_success: true

btrfs_snapshots:
  subvolumes: ["@", "@home"]
  # keep_recent omitted - should default to 3

dummy_success:
  source_duration: 25
"""
        )

        config = Configuration.from_yaml(config_file)

        # Loaded values
        assert config.logging.tui == LogLevel.WARNING.value
        assert config.sync_jobs["dummy_success"] is True
        assert config.get_job_config("dummy_success")["source_duration"] == 25
        assert config.btrfs_snapshots.subvolumes == ["@", "@home"]

        # Applied defaults
        assert config.logging.file == LogLevel.DEBUG.value
        assert config.logging.external == LogLevel.WARNING.value
        assert config.btrfs_snapshots.keep_recent == 3


class TestLogLevels:
    """Tests for independent log level configuration."""

    def test_001_us6_as2_independent_log_levels(self, tmp_path: Path) -> None:
        """US6-AS2: Independent file and TUI log levels.

        Verifies that logging.file and logging.tui can be set independently
        and control different log outputs.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
logging:
  file: DEBUG
  tui: ERROR
"""
        )

        config = Configuration.from_yaml(config_file)

        assert config.logging.file == LogLevel.DEBUG.value
        assert config.logging.tui == LogLevel.ERROR.value
        # Verify they are independent
        assert config.logging.file != config.logging.tui

    def test_001_fr020_invalid_log_level(self, tmp_path: Path) -> None:
        """Test that invalid log level values are rejected.

        While not explicitly in requirements, this validates proper
        enum handling for log levels.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
logging:
  file: INVALID_LEVEL
"""
        )

        with pytest.raises(ConfigurationError) as exc_info:
            Configuration.from_yaml(config_file)

        # Should get validation error for invalid enum value
        assert len(exc_info.value.errors) > 0


class TestJobEnableDisable:
    """Tests for enabling/disabling jobs via sync_jobs."""

    def test_001_fr032_enable_disable_via_sync_jobs(self, tmp_path: Path) -> None:
        """FR-032: Enable/disable jobs via sync_jobs section.

        Verifies that jobs can be enabled or disabled via the sync_jobs
        configuration section.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
sync_jobs:
  dummy_success: true
  dummy_fail: false
"""
        )

        config = Configuration.from_yaml(config_file)

        assert config.sync_jobs["dummy_success"] is True
        assert config.sync_jobs["dummy_fail"] is False

    def test_001_us6_as3_enable_disable_jobs(self, tmp_path: Path) -> None:
        """US6-AS3: Jobs are enabled/disabled based on sync_jobs config.

        Verifies the acceptance scenario where dummy_success is enabled
        and dummy_fail is disabled.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
sync_jobs:
  dummy_success: true
  dummy_fail: false
"""
        )

        config = Configuration.from_yaml(config_file)

        # dummy_success should be enabled
        assert config.sync_jobs.get("dummy_success") is True

        # dummy_fail should be disabled
        assert config.sync_jobs.get("dummy_fail") is False

    def test_001_edge_unknown_job_in_config(self, tmp_path: Path) -> None:
        """Edge case: Unknown job name in sync_jobs is rejected.

        Verifies that the schema's additionalProperties: false for sync_jobs
        prevents unknown job names from being accepted.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
sync_jobs:
  unknown_job_name: true
  dummy_success: true
"""
        )

        with pytest.raises(ConfigurationError) as exc_info:
            Configuration.from_yaml(config_file)

        # Should fail schema validation due to unknown job
        assert len(exc_info.value.errors) > 0
        error_messages = str(exc_info.value)
        assert "unknown_job_name" in error_messages or "additional" in error_messages.lower()


class TestYAMLErrorHandling:
    """Tests for YAML syntax error handling."""

    def test_001_us6_as5_yaml_syntax_error_handling(self, tmp_path: Path) -> None:
        """US6-AS5: Clear error on YAML syntax error.

        Verifies that when config file has invalid YAML syntax, the system
        displays clear parse error with line number and exits.
        """
        config_file = tmp_path / "config.yaml"
        # Invalid YAML: improper indentation
        config_file.write_text(
            """
logging:
  file: DEBUG
    invalid_indent: true
"""
        )

        with pytest.raises(ConfigurationError) as exc_info:
            Configuration.from_yaml(config_file)

        error = exc_info.value
        assert len(error.errors) > 0
        # Error message should mention YAML or syntax
        error_msg = str(error)
        assert "yaml" in error_msg.lower() or "syntax" in error_msg.lower()

    def test_001_fr033_yaml_syntax_error_with_line_number(self, tmp_path: Path) -> None:
        """FR-033: YAML syntax error includes line number.

        Verifies that YAML parsing errors include the line number
        to help users locate the problem.
        """
        config_file = tmp_path / "config.yaml"
        # Invalid YAML: unclosed bracket
        config_file.write_text(
            """
logging:
  file: DEBUG
btrfs_snapshots:
  subvolumes: ["@", "@home"
"""
        )

        with pytest.raises(ConfigurationError) as exc_info:
            Configuration.from_yaml(config_file)

        error_msg = str(exc_info.value)
        # Should include line reference
        assert "line" in error_msg.lower() or "column" in error_msg.lower()

    def test_001_fr028_missing_config_file(self, tmp_path: Path) -> None:
        """FR-028: Clear error when config file doesn't exist.

        Verifies that attempting to load a non-existent config file
        produces a clear error message.
        """
        missing_file = tmp_path / "nonexistent.yaml"

        with pytest.raises(ConfigurationError) as exc_info:
            Configuration.from_yaml(missing_file)

        error = exc_info.value
        assert len(error.errors) > 0
        error_msg = str(error)
        assert "not found" in error_msg.lower()
        assert str(missing_file) in error_msg


class TestSnapshotsAlwaysActive:
    """Tests for snapshots being always active (FR-011)."""

    def test_001_fr011_snapshots_always_active(self, tmp_path: Path) -> None:
        """FR-011: Snapshots always active (config aspect).

        Verifies that btrfs_snapshots configuration is mandatory and cannot
        be disabled. The snapshot job itself should always run regardless of
        sync_jobs settings.

        Note: This test verifies the config side. The job execution side
        (that snapshots actually run) is tested in test_snapshot_job.py.
        """
        config_file = tmp_path / "config.yaml"
        # Valid config with btrfs_snapshots section
        config_file.write_text(
            """
btrfs_snapshots:
  subvolumes: ["@"]
"""
        )

        config = Configuration.from_yaml(config_file)

        # Snapshots config should be loaded
        assert config.btrfs_snapshots.subvolumes == ["@"]

        # Even if not in sync_jobs, snapshots should not be disableable
        # (sync_jobs only controls optional SyncJobs, not SystemJobs)
        assert "btrfs_snapshots" not in config.sync_jobs


class TestJobConfigAccess:
    """Tests for job-specific configuration access."""

    def test_001_get_job_config_existing(self, tmp_path: Path) -> None:
        """Test get_job_config() returns job-specific config when it exists."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
dummy_success:
  source_duration: 15
  target_duration: 20

btrfs_snapshots:
  subvolumes: ["@"]
"""
        )

        config = Configuration.from_yaml(config_file)

        dummy_config = config.get_job_config("dummy_success")
        assert dummy_config["source_duration"] == 15
        assert dummy_config["target_duration"] == 20

        # btrfs_snapshots is stored in dedicated field, not job_configs
        # Access it directly from the Configuration object
        assert config.btrfs_snapshots.subvolumes == ["@"]

    def test_001_get_job_config_missing(self, tmp_path: Path) -> None:
        """Test get_job_config() returns empty dict for unconfigured jobs."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
logging:
  file: INFO
"""
        )

        config = Configuration.from_yaml(config_file)

        # Job not configured - should return empty dict
        unconfigured_job_config = config.get_job_config("nonexistent_job")
        assert unconfigured_job_config == {}


class TestJobLoadOrder:
    """Tests for job loading order (FR-004)."""

    def test_001_fr004_jobs_loaded_in_config_order(self, tmp_path: Path) -> None:
        """FR-004: Jobs loaded in config order.

        Verifies that when multiple jobs are specified in sync_jobs,
        they maintain their order. This is important for deterministic
        execution order.

        Note: Python 3.7+ dicts maintain insertion order, and YAML parsers
        preserve key order, so this should work naturally.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
sync_jobs:
  dummy_success: true
  dummy_fail: false
"""
        )

        config = Configuration.from_yaml(config_file)

        # Verify sync_jobs maintains order
        job_names = list(config.sync_jobs.keys())
        assert job_names == ["dummy_success", "dummy_fail"]


class TestEmptyConfig:
    """Tests for empty or minimal configuration files."""

    def test_001_empty_config_file(self, tmp_path: Path) -> None:
        """Test that empty config file uses all defaults.

        Verifies that an empty config file is valid and uses defaults
        for all settings.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")

        config = Configuration.from_yaml(config_file)

        # Should use all defaults
        assert config.logging.file == LogLevel.DEBUG.value
        assert config.logging.tui == LogLevel.INFO.value
        assert config.logging.external == LogLevel.WARNING.value
        assert config.sync_jobs == {}
        assert config.disk.preflight_minimum == "20%"
        assert config.btrfs_snapshots.subvolumes == ["@", "@home"]

    def test_001_whitespace_only_config(self, tmp_path: Path) -> None:
        """Test that config file with only whitespace/comments is valid."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
# This is just a comment

# Another comment
"""
        )

        config = Configuration.from_yaml(config_file)

        # Should use all defaults
        assert config.logging.file == LogLevel.DEBUG.value
        assert config.logging.tui == LogLevel.INFO.value


class TestDiskSpaceConfig:
    """Tests for disk space monitor configuration."""

    def test_001_disk_space_defaults(self, tmp_path: Path) -> None:
        """Test that disk space monitoring uses correct defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")

        config = Configuration.from_yaml(config_file)

        assert config.disk.preflight_minimum == "20%"
        assert config.disk.runtime_minimum == "15%"
        assert config.disk.warning_threshold == "25%"
        assert config.disk.check_interval == 30

    def test_001_disk_space_custom_values(self, tmp_path: Path) -> None:
        """Test that custom disk space config values are applied."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
disk_space_monitor:
  preflight_minimum: "50GiB"
  runtime_minimum: "30GiB"
  warning_threshold: "60GiB"
  check_interval: 60
"""
        )

        config = Configuration.from_yaml(config_file)

        assert config.disk.preflight_minimum == "50GiB"
        assert config.disk.runtime_minimum == "30GiB"
        assert config.disk.warning_threshold == "60GiB"
        assert config.disk.check_interval == 60

    def test_001_disk_space_invalid_threshold_format(self, tmp_path: Path) -> None:
        """Test that invalid disk threshold format is rejected."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
disk_space_monitor:
  preflight_minimum: "invalid"
"""
        )

        with pytest.raises(ConfigurationError) as exc_info:
            Configuration.from_yaml(config_file)

        assert len(exc_info.value.errors) > 0

    def test_001_disk_space_check_interval_out_of_range(self, tmp_path: Path) -> None:
        """Test that check_interval out of valid range is rejected."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
disk_space_monitor:
  check_interval: 1  # Too low (minimum is 5)
"""
        )

        with pytest.raises(ConfigurationError) as exc_info:
            Configuration.from_yaml(config_file)

        assert len(exc_info.value.errors) > 0
