"""Performance test to measure run_command() overhead.

Measures the overhead of individual run_command() calls on RemoteExecutor,
excluding initial SSH connection setup (which happens once per connection).

Tests with multiple consecutive commands to see steady-state overhead.

These are performance benchmarks, not functional tests. Run with:
    pytest tests/integration/test_executor_overhead.py -m benchmark
"""

from __future__ import annotations

import time
from statistics import mean, stdev

import asyncssh
import pytest

from pcswitcher.executor import RemoteExecutor


@pytest.mark.benchmark
class TestRemoteExecutorOverhead:
    """Measure RemoteExecutor.run_command() overhead."""

    async def test_no_op_command_overhead(self, pc1_executor) -> None:
        """Time multiple no-op commands to measure steady-state overhead.

        Uses `:` (bash no-op) as a true no-op command with minimal overhead.
        Runs 20 commands and reports timing statistics.

        The first command includes SSH protocol overhead (negotiation, etc).
        Subsequent commands show the steady-state overhead of a single run_command() call.
        """
        num_runs = 20
        timings: list[float] = []

        # Warm-up: run once to establish SSH connection
        await pc1_executor.run_command(":")

        # Run multiple times and measure
        for i in range(num_runs):
            start = time.perf_counter()
            result = await pc1_executor.run_command(":")
            elapsed = time.perf_counter() - start

            timings.append(elapsed)
            assert result.success, f"Command {i} failed: {result.stderr}"

        # Calculate statistics
        min_time = min(timings)
        max_time = max(timings)
        avg_time = mean(timings)
        std_dev = stdev(timings) if len(timings) > 1 else 0.0

        # Print results for analysis
        print("\n" + "=" * 60)
        print("No-op Command Overhead Statistics")
        print("=" * 60)
        print("Command: ':' (bash no-op)")
        print(f"Runs: {num_runs}")
        print(f"Min:  {min_time * 1000:.2f} ms")
        print(f"Max:  {max_time * 1000:.2f} ms")
        print(f"Mean: {avg_time * 1000:.2f} ms")
        print(f"StdDev: {std_dev * 1000:.2f} ms")
        print("\nPer-run timings (ms):")
        for i, t in enumerate(timings, 1):
            print(f"  Run {i:2d}: {t * 1000:6.2f} ms")
        print("=" * 60)

        # Basic assertions - should be reasonably fast
        assert avg_time < 1.0, f"Average overhead too high: {avg_time * 1000:.2f} ms"

    async def test_true_command_overhead(self, pc1_executor) -> None:
        """Time 'true' command for comparison.

        The 'true' builtin is even simpler than ':'.
        """
        num_runs = 10
        timings: list[float] = []

        # Warm-up
        await pc1_executor.run_command("true")

        # Run multiple times and measure
        for _i in range(num_runs):
            start = time.perf_counter()
            result = await pc1_executor.run_command("true")
            elapsed = time.perf_counter() - start

            timings.append(elapsed)
            assert result.success

        avg_time = mean(timings)
        min_time = min(timings)
        max_time = max(timings)

        print("\n" + "=" * 60)
        print("'true' Command Overhead")
        print("=" * 60)
        print(f"Min:  {min_time * 1000:.2f} ms")
        print(f"Max:  {max_time * 1000:.2f} ms")
        print(f"Mean: {avg_time * 1000:.2f} ms")
        print("=" * 60)

    async def test_master_connection_reuse(self, pc1_executor) -> None:
        """Verify SSH master connection is being reused across commands.

        If the master connection is working, subsequent commands should be
        significantly faster than the first (which establishes the connection).
        """
        # First command - may include connection setup
        start1 = time.perf_counter()
        result1 = await pc1_executor.run_command(":")
        time1 = time.perf_counter() - start1
        assert result1.success

        # Second command - should use existing connection (reuse)
        start2 = time.perf_counter()
        result2 = await pc1_executor.run_command(":")
        time2 = time.perf_counter() - start2
        assert result2.success

        # Third command - should also use reused connection
        start3 = time.perf_counter()
        result3 = await pc1_executor.run_command(":")
        time3 = time.perf_counter() - start3
        assert result3.success

        print("\n" + "=" * 60)
        print("Master Connection Reuse Test")
        print("=" * 60)
        print(f"Command 1: {time1 * 1000:.2f} ms (may include connection setup)")
        print(f"Command 2: {time2 * 1000:.2f} ms (should reuse connection)")
        print(f"Command 3: {time3 * 1000:.2f} ms (should reuse connection)")
        if time2 < time1:
            print(f"✓ Connection reuse detected: cmd2 is {(time1 - time2) * 1000:.2f} ms faster than cmd1")
        else:
            print("⚠ No obvious reuse benefit (cmd2 similar to cmd1)")
        print("=" * 60)


@pytest.mark.benchmark
class TestExecutorWrapperOverhead:
    """Measure overhead of login_shell=True parameter vs bare RemoteExecutor."""

    async def test_direct_vs_wrapped_command_overhead(self, pc1_connection: asyncssh.SSHClientConnection) -> None:
        """Compare direct RemoteExecutor vs login_shell=True overhead.

        RemoteExecutor with login_shell=True wraps commands in 'bash -l -c "..."'
        to get a login shell environment. This test measures the cost of that wrapper.
        """
        # Create executor
        executor = RemoteExecutor(pc1_connection)

        num_runs = 10
        direct_timings: list[float] = []
        wrapped_timings: list[float] = []

        # Warm up both modes
        await executor.run_command(":", login_shell=False)
        await executor.run_command(":", login_shell=True)

        # Test direct executor (login_shell=False)
        for _ in range(num_runs):
            start = time.perf_counter()
            result = await executor.run_command(":", login_shell=False)
            elapsed = time.perf_counter() - start
            direct_timings.append(elapsed)
            assert result.success

        # Test with login shell wrapper (login_shell=True)
        for _ in range(num_runs):
            start = time.perf_counter()
            result = await executor.run_command(":", login_shell=True)
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

        print("\n" + "=" * 70)
        print("RemoteExecutor: login_shell=False vs login_shell=True Overhead")
        print("=" * 70)
        print("Direct RemoteExecutor (login_shell=False, bare SSH):")
        print(f"  Mean:   {direct_mean * 1000:.2f} ms")
        print(f"  StdDev: {direct_std * 1000:.2f} ms")
        print()
        print("With Login Shell (login_shell=True, bash -l -c wrapper):")
        print(f"  Mean:   {wrapped_mean * 1000:.2f} ms")
        print(f"  StdDev: {wrapped_std * 1000:.2f} ms")
        print()
        print("Login Shell Overhead:")
        print(f"  Absolute: {wrapper_cost * 1000:.2f} ms")
        print(f"  Relative: {wrapper_cost_pct:.1f}% slower")
        print("=" * 70)

    async def test_direct_executor_performance(self, pc1_connection: asyncssh.SSHClientConnection) -> None:
        """Measure bare RemoteExecutor (direct SSH) performance in detail."""
        executor = RemoteExecutor(pc1_connection)

        num_runs = 20
        timings: list[float] = []

        # Warm up
        await executor.run_command(":")

        # Run multiple times
        for _ in range(num_runs):
            start = time.perf_counter()
            result = await executor.run_command(":")
            elapsed = time.perf_counter() - start
            timings.append(elapsed)
            assert result.success

        # Statistics
        min_time = min(timings)
        max_time = max(timings)
        avg_time = mean(timings)
        std_dev = stdev(timings) if len(timings) > 1 else 0.0

        print("\n" + "=" * 60)
        print("Direct RemoteExecutor (Bare SSH) Performance")
        print("=" * 60)
        print(f"Runs: {num_runs}")
        print(f"Min:  {min_time * 1000:.2f} ms")
        print(f"Max:  {max_time * 1000:.2f} ms")
        print(f"Mean: {avg_time * 1000:.2f} ms")
        print(f"StdDev: {std_dev * 1000:.2f} ms")
        print("\nPer-run timings (ms):")
        for i, t in enumerate(timings, 1):
            print(f"  Run {i:2d}: {t * 1000:6.2f} ms")
        print("=" * 60)
