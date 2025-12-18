"""Performance test to measure run_command() overhead.

Measures the overhead of individual run_command() calls on RemoteExecutor,
excluding initial SSH connection setup (which happens once per connection).

Tests with multiple consecutive commands to see steady-state overhead.

These are performance benchmarks, not functional tests. Run with:
    pytest tests/integration/test_executor_overhead.py -m benchmark
"""

from __future__ import annotations

import logging
import time
from statistics import mean, stdev

import pytest

from pcswitcher.executor import BashLoginRemoteExecutor

logger = logging.getLogger(__name__)


@pytest.mark.benchmark
class TestRemoteExecutorOverhead:
    """Measure RemoteExecutor.run_command() overhead."""

    async def test_no_op_command_overhead(self, pc1_executor: BashLoginRemoteExecutor) -> None:
        """Time multiple no-op commands to measure steady-state overhead.

        Uses `:` (bash no-op) as a true no-op command with minimal overhead.
        Runs 20 commands and reports timing statistics.
        Running with login_shell=False to avoid extra bash login shell overhead.

        The first command includes SSH protocol overhead (negotiation, etc).
        Subsequent commands show the steady-state overhead of a single run_command() call.
        """
        num_runs = 20
        timings: list[float] = []

        # Warm-up: run once to establish SSH connection
        await pc1_executor.run_command(":", login_shell=False)

        # Run multiple times and measure
        for i in range(num_runs):
            start = time.perf_counter()
            result = await pc1_executor.run_command(":", login_shell=False)
            elapsed = time.perf_counter() - start

            timings.append(elapsed)
            assert result.success, f"Command {i} failed: {result.stderr}"

        # Calculate statistics
        min_time = min(timings)
        max_time = max(timings)
        avg_time = mean(timings)
        std_dev = stdev(timings) if len(timings) > 1 else 0.0

        # Log results for analysis
        logger.info("=" * 60)
        logger.info("No-op Command Overhead Statistics")
        logger.info("=" * 60)
        logger.info("Command: ':' (bash no-op)")
        logger.info(f"Runs: {num_runs}")
        logger.info(f"Min:  {min_time * 1000:.2f} ms")
        logger.info(f"Max:  {max_time * 1000:.2f} ms")
        logger.info(f"Mean: {avg_time * 1000:.2f} ms")
        logger.info(f"StdDev: {std_dev * 1000:.2f} ms")
        logger.info("Per-run timings (ms):")
        for i, t in enumerate(timings, 1):
            logger.info(f"  Run {i:2d}: {t * 1000:6.2f} ms")
        logger.info("=" * 60)

        # Basic assertions - should be reasonably fast
        assert avg_time < 1.0, f"Average overhead too high: {avg_time * 1000:.2f} ms"

    async def test_true_command_overhead(self, pc1_executor: BashLoginRemoteExecutor) -> None:
        """Time 'true' command for comparison.

        The 'true' builtin is even simpler than ':'.
        Running with login_shell=False to avoid extra bash login shell overhead.
        """
        num_runs = 10
        timings: list[float] = []

        # Warm-up
        await pc1_executor.run_command("true", login_shell=False)

        # Run multiple times and measure
        for _i in range(num_runs):
            start = time.perf_counter()
            result = await pc1_executor.run_command("true", login_shell=False)
            elapsed = time.perf_counter() - start

            timings.append(elapsed)
            assert result.success

        avg_time = mean(timings)
        min_time = min(timings)
        max_time = max(timings)

        logger.info("=" * 60)
        logger.info("'true' Command Overhead")
        logger.info("=" * 60)
        logger.info(f"Min:  {min_time * 1000:.2f} ms")
        logger.info(f"Max:  {max_time * 1000:.2f} ms")
        logger.info(f"Mean: {avg_time * 1000:.2f} ms")
        logger.info("=" * 60)

    async def test_master_connection_reuse(self, pc1_executor: BashLoginRemoteExecutor) -> None:
        """Verify SSH master connection is being reused across commands.

        If the master connection is working, subsequent commands should be
        significantly faster than the first (which establishes the connection).
        """
        # First command - may include connection setup
        start1 = time.perf_counter()
        result1 = await pc1_executor.run_command(":", login_shell=False)
        time1 = time.perf_counter() - start1
        assert result1.success

        # Second command - should use existing connection (reuse)
        start2 = time.perf_counter()
        result2 = await pc1_executor.run_command(":", login_shell=False)
        time2 = time.perf_counter() - start2
        assert result2.success

        # Third command - should also use reused connection
        start3 = time.perf_counter()
        result3 = await pc1_executor.run_command(":", login_shell=False)
        time3 = time.perf_counter() - start3
        assert result3.success

        logger.info("=" * 60)
        logger.info("Master Connection Reuse Test")
        logger.info("=" * 60)
        logger.info(f"Command 1: {time1 * 1000:.2f} ms (may include connection setup)")
        logger.info(f"Command 2: {time2 * 1000:.2f} ms (should reuse connection)")
        logger.info(f"Command 3: {time3 * 1000:.2f} ms (should reuse connection)")
        if time2 < time1:
            logger.info(f"✓ Connection reuse detected: cmd2 is {(time1 - time2) * 1000:.2f} ms faster than cmd1")
        else:
            logger.info("⚠ No obvious reuse benefit (cmd2 similar to cmd1)")
        logger.info("=" * 60)


@pytest.mark.benchmark
class TestExecutorWrapperOverhead:
    """Measure overhead of login_shell=True parameter vs bare RemoteExecutor."""

    async def test_direct_vs_wrapped_command_overhead(self, pc1_executor: BashLoginRemoteExecutor) -> None:
        """Compare direct RemoteExecutor vs login_shell=True overhead.

        RemoteExecutor with login_shell=True wraps commands in 'bash -l -c "..."'
        to get a login shell environment. This test measures the cost of that wrapper.
        """
        num_runs = 10
        direct_timings: list[float] = []
        wrapped_timings: list[float] = []

        # Warm up
        await pc1_executor.run_command(":", login_shell=False)

        # Test direct executor (login_shell=False)
        for _ in range(num_runs):
            start = time.perf_counter()
            result = await pc1_executor.run_command(":", login_shell=False)
            elapsed = time.perf_counter() - start
            direct_timings.append(elapsed)
            assert result.success

        # Test with login shell wrapper (login_shell=True)
        for _ in range(num_runs):
            start = time.perf_counter()
            result = await pc1_executor.run_command(":", login_shell=True)
            elapsed = time.perf_counter() - start
            wrapped_timings.append(elapsed)
            assert result.success

        # Calculate statistics
        direct_mean = mean(direct_timings)
        wrapped_mean = mean(wrapped_timings)
        direct_std = stdev(direct_timings) if len(direct_timings) > 1 else 0.0
        wrapped_std = stdev(wrapped_timings) if len(wrapped_timings) > 1 else 0.0
        wrapper_cost = wrapped_mean - direct_mean
        wrapper_cost_pct = (wrapper_cost / direct_mean * 100) if direct_mean > 0 else 0

        logger.info("=" * 70)
        logger.info("RemoteExecutor: login_shell=False vs login_shell=True Overhead")
        logger.info("=" * 70)
        logger.info("Direct RemoteExecutor (login_shell=False, bare SSH):")
        logger.info(f"  Mean:   {direct_mean * 1000:.2f} ms")
        logger.info(f"  StdDev: {direct_std * 1000:.2f} ms")
        logger.info("")
        logger.info("With Login Shell (login_shell=True, bash -l -c wrapper):")
        logger.info(f"  Mean:   {wrapped_mean * 1000:.2f} ms")
        logger.info(f"  StdDev: {wrapped_std * 1000:.2f} ms")
        logger.info("")
        logger.info("Login Shell Overhead:")
        logger.info(f"  Absolute: {wrapper_cost * 1000:.2f} ms")
        logger.info(f"  Relative: {wrapper_cost_pct:.1f}% slower")
        logger.info("=" * 70)
