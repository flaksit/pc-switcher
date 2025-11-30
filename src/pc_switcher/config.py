import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field, ValidationError
import jsonschema

# Default configuration path
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "pc-switcher" / "config.yaml"


class JobConfig(BaseModel):
    """Base configuration for all jobs."""

    enabled: bool = True


class GlobalConfig(BaseModel):
    """Global configuration settings."""

    log_file_level: str = "FULL"
    log_cli_level: str = "INFO"
    sync_jobs: Dict[str, bool] = Field(default_factory=dict)


class Config(BaseModel):
    """Root configuration object."""

    global_settings: GlobalConfig = Field(default_factory=GlobalConfig)
    jobs: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, config_path: Path = DEFAULT_CONFIG_PATH) -> "Config":
        """Load configuration from a YAML file."""
        if not config_path.exists():
            # Return default config if file doesn't exist
            return cls()

        try:
            with open(config_path, "r") as f:
                raw_config = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"Failed to parse config file: {e}")

        # Extract global settings
        global_data = {
            "log_file_level": raw_config.get("log_file_level", "FULL"),
            "log_cli_level": raw_config.get("log_cli_level", "INFO"),
            "sync_jobs": raw_config.get("sync_jobs", {}),
        }

        # Extract job-specific settings (everything else)
        job_data = {k: v for k, v in raw_config.items() if k not in ["log_file_level", "log_cli_level", "sync_jobs"]}

        try:
            return cls(global_settings=GlobalConfig(**global_data), jobs=job_data)
        except ValidationError as e:
            raise ValueError(f"Invalid configuration: {e}")


def validate_job_config(config: Dict[str, Any], schema: Dict[str, Any]) -> None:
    """Validate a job's configuration against its schema."""
    try:
        jsonschema.validate(instance=config, schema=schema)
    except jsonschema.ValidationError as e:
        raise ValueError(f"Job configuration validation failed: {e.message}")
