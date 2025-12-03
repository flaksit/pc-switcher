"""Disk space monitoring utilities."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pcswitcher.executor import Executor
from pcswitcher.models import CommandResult

__all__ = [
    "DiskSpace",
    "check_disk_space",
    "parse_df_output",
    "parse_threshold",
]


@dataclass(frozen=True)
class DiskSpace:
    """Disk space information for a mount point."""

    total_bytes: int
    used_bytes: int
    available_bytes: int
    use_percent: int
    mount_point: str


def parse_df_output(output: str, mount_point: str) -> DiskSpace | None:
    """Parse `df -B1` output for a specific mount point.

    Args:
        output: Raw stdout from `df -B1` command
        mount_point: Mount point to search for (e.g., "/home")

    Returns:
        DiskSpace if mount point found, None otherwise
    """
    for line in output.strip().split("\n")[1:]:  # Skip header
        parts = line.split()
        if len(parts) >= 6 and parts[5] == mount_point:
            return DiskSpace(
                total_bytes=int(parts[1]),
                used_bytes=int(parts[2]),
                available_bytes=int(parts[3]),
                use_percent=int(parts[4].rstrip("%")),
                mount_point=parts[5],
            )
    return None


def parse_threshold(threshold: str) -> tuple[str, int]:
    """Parse threshold string to type and value.

    Supports two formats:
    - Percentage: "20%" → ("percent", 20)
    - Absolute: "50GiB" → ("bytes", 53687091200)

    Supported units: GiB, MiB, GB, MB

    Args:
        threshold: Threshold string like "20%" or "50GiB"

    Returns:
        Tuple of (type, value) where type is "percent" or "bytes"

    Raises:
        ValueError: If threshold format is invalid
    """
    if threshold.endswith("%"):
        return ("percent", int(threshold[:-1]))

    match = re.match(r"(\d+)(GiB|MiB|GB|MB)", threshold)
    if match:
        value, unit = match.groups()
        multipliers = {"GiB": 2**30, "MiB": 2**20, "GB": 10**9, "MB": 10**6}
        return ("bytes", int(value) * multipliers[unit])

    raise ValueError(f"Invalid threshold format: {threshold}")


async def check_disk_space(
    executor: Executor,
    mount_point: str,
) -> DiskSpace:
    """Check disk space for a mount point.

    Args:
        executor: Executor to run df command
        mount_point: Mount point to check (e.g., "/home")

    Returns:
        DiskSpace information

    Raises:
        RuntimeError: If df command fails or mount point not found
    """
    result: CommandResult = await executor.run_command(f"df -B1 {mount_point}")

    if not result.success:
        raise RuntimeError(f"df command failed: {result.stderr}")

    disk_space = parse_df_output(result.stdout, mount_point)
    if disk_space is None:
        raise RuntimeError(f"Mount point {mount_point} not found in df output")

    return disk_space
