"""Tests for disk monitoring utilities."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pcswitcher.utils.disk import DiskMonitor, format_bytes


def test_check_free_space_with_percentage() -> None:
    """Test disk space check with percentage threshold (string format)."""
    monitor = DiskMonitor()

    # Use a real path that exists
    with tempfile.TemporaryDirectory() as tmpdir:
        # Very low threshold should pass
        is_sufficient, free_bytes, required_bytes = monitor.check_free_space(Path(tmpdir), "1%")
        assert is_sufficient
        assert free_bytes > 0
        assert required_bytes > 0

        # Very high threshold should fail
        is_sufficient, free_bytes, required_bytes = monitor.check_free_space(Path(tmpdir), "99%")
        assert not is_sufficient
        assert free_bytes > 0
        assert required_bytes > 0


def test_check_free_space_with_percentage_string() -> None:
    """Test disk space check with percentage string threshold."""
    monitor = DiskMonitor()

    with tempfile.TemporaryDirectory() as tmpdir:
        # Very low threshold should pass
        is_sufficient, free_bytes, required_bytes = monitor.check_free_space(Path(tmpdir), "1%")
        assert is_sufficient
        assert free_bytes > 0
        assert required_bytes > 0


def test_check_free_space_with_bytes() -> None:
    """Test disk space check with absolute GiB threshold."""
    monitor = DiskMonitor()

    with tempfile.TemporaryDirectory() as tmpdir:
        # Very small GiB requirement should pass
        is_sufficient, free_bytes, required_bytes = monitor.check_free_space(Path(tmpdir), "1GiB")
        assert is_sufficient
        assert free_bytes > 0
        assert required_bytes == 1024 * 1024 * 1024  # 1 GiB in bytes


def test_check_free_space_invalid_format() -> None:
    """Test that invalid min_free format raises ValueError."""
    monitor = DiskMonitor()

    with tempfile.TemporaryDirectory() as tmpdir:
        # Invalid string without proper unit suffix
        with pytest.raises(ValueError, match="Disk minimum must be specified with explicit units"):
            monitor.check_free_space(Path(tmpdir), "invalid")

        # Invalid percentage value (out of range)
        with pytest.raises(ValueError, match="Percentage.*must be between"):
            monitor.check_free_space(Path(tmpdir), "150%")

        # Bare float without units should fail
        with pytest.raises(ValueError, match="Bare numbers without units"):
            monitor.check_free_space(Path(tmpdir), 0.20)  # type: ignore[arg-type]

        # Bare int without units should fail
        with pytest.raises(ValueError, match="Bare numbers without units"):
            monitor.check_free_space(Path(tmpdir), 1024)  # type: ignore[arg-type]


def test_monitor_continuously_start_stop() -> None:
    """Test starting and stopping continuous monitoring."""
    monitor = DiskMonitor()
    callback_called = False

    def callback(free_bytes: float, required_bytes: float) -> None:
        nonlocal callback_called
        callback_called = True

    with tempfile.TemporaryDirectory() as tmpdir:
        # Start monitoring with very high threshold (will trigger callback)
        monitor.monitor_continuously(
            path=Path(tmpdir),
            interval=1,
            reserve_minimum="99%",  # 99% - should trigger warning
            callback=callback,
        )

        assert monitor.is_monitoring()

        # Stop monitoring
        monitor.stop_monitoring()
        assert not monitor.is_monitoring()


def test_monitor_continuously_already_running() -> None:
    """Test that starting monitoring twice raises error."""
    monitor = DiskMonitor()

    def callback(free_bytes: float, required_bytes: float) -> None:
        pass

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            monitor.monitor_continuously(
                path=Path(tmpdir),
                interval=1,
                reserve_minimum="20%",
                callback=callback,
            )

            # Try to start again
            with pytest.raises(RuntimeError, match="Monitor is already running"):
                monitor.monitor_continuously(
                    path=Path(tmpdir),
                    interval=1,
                    reserve_minimum="20%",
                    callback=callback,
                )
        finally:
            monitor.stop_monitoring()


def test_format_bytes() -> None:
    """Test byte formatting utility."""
    assert format_bytes(0) == "0.0 B"
    assert format_bytes(100) == "100.0 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1024 * 1024) == "1.0 MB"
    assert format_bytes(1024 * 1024 * 1024) == "1.0 GB"
    assert format_bytes(1024 * 1024 * 1024 * 1024) == "1.0 TB"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(1536 * 1024) == "1.5 MB"
