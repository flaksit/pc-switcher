"""Configuration loading and validation for pc-switcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import ValidationError, validate

from pcswitcher.core.logging import LogLevel


@dataclass
class Configuration:
    """Parsed and validated configuration for pc-switcher.

    Attributes:
        log_file_level: Minimum level for file logging
        log_cli_level: Minimum level for terminal display
        sync_jobs: Job enable/disable flags
        job_configs: Per-job configuration sections
        disk: Disk space monitoring configuration
        config_path: Path to loaded config file
    """

    log_file_level: LogLevel
    log_cli_level: LogLevel
    sync_jobs: dict[str, bool]
    job_configs: dict[str, dict[str, Any]]
    disk: dict[str, Any]
    config_path: Path = field(default_factory=lambda: Path.home() / ".config" / "pc-switcher" / "config.yaml")


class ConfigError(Exception):
    """Raised when configuration is invalid."""

    pass


def load_config(path: Path | None = None) -> Configuration:
    """Load configuration from YAML file.

    Args:
        path: Path to config file. Defaults to ~/.config/pc-switcher/config.yaml

    Returns:
        Validated Configuration object

    Raises:
        ConfigError: If config file is invalid or missing required fields
        FileNotFoundError: If config file doesn't exist
    """
    if path is None:
        path = Path.home() / ".config" / "pc-switcher" / "config.yaml"

    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with path.open("r") as f:
        config_dict = yaml.safe_load(f)

    if config_dict is None:
        raise ConfigError(f"Configuration file is empty: {path}")

    # Validate structure
    validate_config_structure(config_dict, path)

    # Apply defaults
    config_dict = apply_defaults(config_dict)

    # Note: validate_required_jobs() disabled since btrfs_snapshots is now
    # orchestrator-level infrastructure, not a SyncJob in sync_jobs

    # Parse log levels
    log_file_level = _parse_log_level(config_dict.get("log_file_level", "FULL"))
    log_cli_level = _parse_log_level(config_dict.get("log_cli_level", "INFO"))

    # Extract job configs
    job_configs = {}
    for job_name in config_dict.get("sync_jobs", {}):
        if job_name in config_dict:
            job_configs[job_name] = config_dict[job_name]

    return Configuration(
        log_file_level=log_file_level,
        log_cli_level=log_cli_level,
        sync_jobs=config_dict.get("sync_jobs", {}),
        job_configs=job_configs,
        disk=config_dict.get("disk", {}),
        config_path=path,
    )


def validate_config_structure(config_dict: dict[str, Any], config_path: Path) -> None:
    """Validate that configuration has required fields and correct types.

    Args:
        config_dict: Configuration dictionary from YAML
        config_path: Path to config file (for error messages)

    Raises:
        ConfigError: If required fields are missing or types are incorrect
    """
    required_fields = ["sync_jobs"]

    for field_name in required_fields:
        if field_name not in config_dict:
            raise ConfigError(f"Missing required field '{field_name}' in {config_path}")

    # Validate sync_jobs is a dict
    if not isinstance(config_dict["sync_jobs"], dict):
        raise ConfigError(f"Field 'sync_jobs' must be a dictionary in {config_path}")

    # Validate sync_jobs values are boolean
    for job_name, enabled in config_dict["sync_jobs"].items():
        if not isinstance(enabled, bool):
            raise ConfigError(
                f"Job '{job_name}' in 'sync_jobs' must have boolean value (true/false) in {config_path}"
            )

    # Validate log levels if present
    if "log_file_level" in config_dict:
        _parse_log_level(config_dict["log_file_level"])  # Will raise if invalid

    if "log_cli_level" in config_dict:
        _parse_log_level(config_dict["log_cli_level"])  # Will raise if invalid


def validate_required_jobs(sync_jobs: dict[str, bool], config_path: Path) -> None:
    """Validate that required jobs are present and enabled.

    Note: btrfs_snapshots is now orchestrator-level infrastructure, not a SyncJob.
    It should NOT be listed in sync_jobs.

    Args:
        sync_jobs: Dictionary of job names to enabled flags
        config_path: Path to config file (for error messages)

    Raises:
        ConfigError: If btrfs_snapshots is incorrectly listed as a SyncJob
    """
    # btrfs_snapshots should NOT be in sync_jobs (it's infrastructure, not a SyncJob)
    if "btrfs_snapshots" in sync_jobs:
        raise ConfigError(
            f"'btrfs_snapshots' should not be listed in sync_jobs in {config_path}. "
            f"It is orchestrator-level infrastructure configured separately in the btrfs_snapshots section."
        )


def apply_defaults(config_dict: dict[str, Any]) -> dict[str, Any]:
    """Apply default values to configuration.

    Args:
        config_dict: Configuration dictionary from YAML

    Returns:
        Configuration dictionary with defaults applied
    """
    defaults = {
        "log_file_level": "FULL",
        "log_cli_level": "INFO",
        "disk": {
            "preflight_minimum": "20%",  # Pre-flight threshold with explicit units
            "runtime_minimum": "15%",    # Runtime threshold with explicit units
            "check_interval": 30,        # seconds
        },
    }

    # Apply top-level defaults
    for key, value in defaults.items():
        if key not in config_dict:
            config_dict[key] = value
        elif key == "disk" and isinstance(value, dict):
            # Merge disk defaults
            for disk_key, disk_value in value.items():
                if disk_key not in config_dict[key]:
                    config_dict[key][disk_key] = disk_value

    return config_dict


def validate_job_config(job_name: str, job_config: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate job configuration against JSON schema.

    Args:
        job_name: Name of the job
        job_config: Job configuration dictionary
        schema: JSON Schema (draft-07) for job config

    Raises:
        ConfigError: If job config doesn't match schema
    """
    try:
        validate(instance=job_config, schema=schema)
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration for job '{job_name}': {e.message}") from e


def generate_default_config() -> str:
    """Generate default configuration as YAML string with inline comments.

    Returns:
        YAML string with default configuration
    """
    config = """# PC-switcher configuration file
# See documentation for details: https://github.com/flaksit/pc-switcher

# Logging configuration
log_file_level: FULL  # Minimum level for file logs: DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL
log_cli_level: INFO   # Minimum level for terminal output: DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL

# Disk space monitoring
# Values MUST include explicit units: "nn%" for percentage, "nnGiB" for absolute
disk:
  preflight_minimum: "20%"     # Pre-flight check: free space required to start sync
  runtime_minimum: "15%"       # Runtime check: abort if space falls below during sync
  check_interval: 30           # Seconds between disk space checks

# Enabled sync jobs (in execution order)
sync_jobs:
  # dummy_success: false  # Example: Uncomment to enable test job
  # dummy_fail: false
  # NOTE: btrfs_snapshots is orchestrator-level infrastructure, not a SyncJob

# Job-specific configurations

# Btrfs snapshots configuration
btrfs_snapshots:
  subvolumes:             # Flat subvolume names from 'btrfs subvolume list /'
    - "@"                 # Root filesystem (usually mounted at /)
    - "@home"             # Home directory (usually mounted at /home)
    # - "@root"           # Root user home (uncomment if you have this subvolume)
  snapshot_dir: "/.snapshots"  # Directory for snapshots
  keep_recent: 3               # Number of recent snapshots to keep
  max_age_days: 7              # Delete snapshots older than this
"""
    return config


def get_enabled_jobs(sync_jobs: dict[str, bool]) -> list[str]:
    """Get list of enabled job names in configuration order.

    Args:
        sync_jobs: Dictionary of job names to enabled flags

    Returns:
        List of enabled job names in order
    """
    return [name for name, enabled in sync_jobs.items() if enabled]


def validate_job_names(sync_jobs: dict[str, bool], available_jobs: list[str]) -> None:
    """Validate that all jobs in config are known/available.

    Args:
        sync_jobs: Dictionary of job names to enabled flags
        available_jobs: List of known job names

    Raises:
        ConfigError: If unknown job name found in config
    """
    unknown_jobs = [name for name in sync_jobs if name not in available_jobs]

    if unknown_jobs:
        raise ConfigError(
            f"Unknown jobs in configuration: {', '.join(unknown_jobs)}. "
            f"Available jobs: {', '.join(available_jobs)}"
        )


def _parse_log_level(level_str: str) -> LogLevel:
    """Parse log level string to LogLevel enum.

    Args:
        level_str: Log level as string (e.g., "DEBUG", "INFO")

    Returns:
        LogLevel enum value

    Raises:
        ConfigError: If log level string is invalid
    """
    try:
        return LogLevel[level_str.upper()]
    except KeyError as e:
        valid_levels = ", ".join(level.name for level in LogLevel)
        raise ConfigError(f"Invalid log level '{level_str}'. Valid levels: {valid_levels}") from e
