"""Configuration loading and validation for pc-switcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from pcswitcher.models import ConfigError, LogLevel

__all__ = [
    "BtrfsConfig",
    "Configuration",
    "ConfigurationError",
    "DiskConfig",
]


@dataclass
class DiskConfig:
    """Disk space monitoring configuration."""

    preflight_minimum: str = "20%"  # Percentage or absolute (e.g., "50GiB")
    runtime_minimum: str = "15%"
    check_interval: int = 30  # Seconds


@dataclass
class BtrfsConfig:
    """Btrfs snapshot configuration."""

    subvolumes: list[str] = field(default_factory=lambda: ["@", "@home"])
    keep_recent: int = 3
    max_age_days: int | None = None  # None = no age limit


@dataclass
class Configuration:
    """Parsed and validated configuration from YAML file."""

    log_file_level: LogLevel = LogLevel.FULL
    log_cli_level: LogLevel = LogLevel.INFO
    sync_jobs: dict[str, bool] = field(default_factory=dict)  # job_name -> enabled
    disk: DiskConfig = field(default_factory=DiskConfig)
    btrfs_snapshots: BtrfsConfig = field(default_factory=BtrfsConfig)
    job_configs: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> Configuration:
        """Load and validate configuration from YAML file.

        Args:
            path: Path to config.yaml

        Returns:
            Validated Configuration instance

        Raises:
            ConfigurationError: If YAML is invalid or schema validation fails
        """
        errors: list[ConfigError] = []

        # Step 1: Load YAML with error handling for syntax
        try:
            with path.open() as f:
                data = yaml.safe_load(f)
        except FileNotFoundError:
            errors.append(
                ConfigError(
                    job=None,
                    path=str(path),
                    message=f"Configuration file not found: {path}",
                )
            )
            raise ConfigurationError(errors) from None
        except yaml.YAMLError as e:
            error_msg = str(e)
            # Extract line number if available (problem_mark exists on MarkedYAMLError)
            if hasattr(e, "problem_mark") and hasattr(e, "problem"):
                # Using hasattr check, so access is safe despite type system not knowing about it
                mark = e.problem_mark  # type: ignore[attr-defined]
                problem = e.problem  # type: ignore[attr-defined]
                if mark is not None and problem is not None:
                    error_msg = f"YAML syntax error at line {mark.line + 1}, column {mark.column + 1}: {problem}"
            errors.append(
                ConfigError(
                    job=None,
                    path=str(path),
                    message=error_msg,
                )
            )
            raise ConfigurationError(errors) from e

        # Handle empty file
        if data is None:
            data = {}

        # Step 2: Load schema from package resources
        schema = _load_schema()

        # Step 3: Validate against schema with jsonschema
        validator = jsonschema.Draft7Validator(schema)
        for error in validator.iter_errors(data):
            path_parts = list(error.absolute_path)
            path_str = ".".join(str(p) for p in path_parts) if path_parts else "root"
            errors.append(
                ConfigError(
                    job=None,
                    path=path_str,
                    message=error.message,
                )
            )

        if errors:
            raise ConfigurationError(errors)

        # Step 4: Parse log levels from strings to LogLevel enum
        log_file_level = LogLevel.FULL  # Default value
        log_cli_level = LogLevel.INFO  # Default value

        try:
            log_file_level = _parse_log_level(data.get("log_file_level", "FULL"))
        except ValueError as e:
            errors.append(
                ConfigError(
                    job=None,
                    path="log_file_level",
                    message=str(e),
                )
            )

        try:
            log_cli_level = _parse_log_level(data.get("log_cli_level", "INFO"))
        except ValueError as e:
            errors.append(
                ConfigError(
                    job=None,
                    path="log_cli_level",
                    message=str(e),
                )
            )

        if errors:
            raise ConfigurationError(errors)

        # Step 5: Apply defaults for missing fields and build dataclass instances
        sync_jobs = data.get("sync_jobs", {})

        # Parse disk config (key is disk_space_monitor in YAML, maps to disk in dataclass)
        disk_data = data.get("disk_space_monitor", {})
        disk_config = DiskConfig(
            preflight_minimum=disk_data.get("preflight_minimum", "20%"),
            runtime_minimum=disk_data.get("runtime_minimum", "15%"),
            check_interval=disk_data.get("check_interval", 30),
        )

        # Parse btrfs snapshots config
        btrfs_data = data.get("btrfs_snapshots", {})
        btrfs_config = BtrfsConfig(
            subvolumes=btrfs_data.get("subvolumes", ["@", "@home"]),
            keep_recent=btrfs_data.get("keep_recent", 3),
            max_age_days=btrfs_data.get("max_age_days"),
        )

        # Step 6: Extract job configs from top-level keys matching job names
        # Job configs are top-level keys except for the known global config keys
        global_keys = {
            "log_file_level",
            "log_cli_level",
            "sync_jobs",
            "disk_space_monitor",
            "btrfs_snapshots",
        }
        job_configs = {key: value for key, value in data.items() if key not in global_keys and isinstance(value, dict)}

        return cls(
            log_file_level=log_file_level,
            log_cli_level=log_cli_level,
            sync_jobs=sync_jobs,
            disk=disk_config,
            btrfs_snapshots=btrfs_config,
            job_configs=job_configs,
        )

    @classmethod
    def get_default_config_path(cls) -> Path:
        """Get the default config file path."""
        return Path.home() / ".config" / "pc-switcher" / "config.yaml"

    def get_job_config(self, job_name: str) -> dict[str, Any]:
        """Get job-specific config, returning empty dict if not specified."""
        return self.job_configs.get(job_name, {})


class ConfigurationError(Exception):
    """Raised when configuration loading or validation fails."""

    def __init__(self, errors: list[ConfigError]) -> None:
        self.errors = errors
        messages = [f"{e.path}: {e.message}" for e in errors]
        super().__init__("Configuration validation failed:\n" + "\n".join(messages))


def _load_schema() -> dict[str, Any]:
    """Load the config schema from package resources."""
    schema_path = Path(__file__).parent / "schemas" / "config-schema.yaml"
    with schema_path.open() as f:
        return yaml.safe_load(f)


def _parse_log_level(value: str) -> LogLevel:
    """Parse a log level string to LogLevel enum."""
    try:
        return LogLevel[value.upper()]
    except KeyError as e:
        valid_levels = ", ".join(level.name for level in LogLevel)
        raise ValueError(f"Invalid log level: {value}. Valid levels: {valid_levels}") from e
