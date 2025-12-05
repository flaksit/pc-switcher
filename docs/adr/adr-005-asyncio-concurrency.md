# ADR-005: Use Asyncio for Concurrency

Status: Accepted
Date: 2025-12-02

## TL;DR
Use Python's `asyncio` library for all concurrency, I/O operations, and job execution to ensure responsiveness and proper resource cleanup.

## Implementation Rules
- All `Job` interface methods (`validate`, `pre_sync`, `sync`, `post_sync`) must be `async def`.
- Use `asyncssh` for all remote SSH operations.
- Use `asyncio.create_subprocess_exec` (or similar async wrappers) for local subprocesses.
- Use `asyncio.TaskGroup` for managing concurrent tasks to ensure structured concurrency.
- Signal handlers (SIGINT, SIGTERM) must trigger cancellation of the active `asyncio.Task`.
- **Forbidden**: Blocking calls (e.g., `time.sleep`, synchronous `subprocess.run`, blocking socket I/O) in the main event loop.

## Context
The initial synchronous implementation of the orchestrator and jobs revealed critical gaps in handling user interruptions and timeouts. Specifically:
- Jobs running in the main thread could not be interrupted during blocking operations (e.g., `time.sleep`, `subprocess.run`).
- There was no reliable mechanism to force-kill child processes or remote SSH commands upon abort.
- Signal handling was "toothless," setting a flag that was only checked between operations, leading to poor responsiveness (up to seconds or minutes of delay).
- Requirements for "no orphaned processes" (FR-027) and immediate abort (FR-003) could not be met.

## Decision
- **Adopt `asyncio`** as the core concurrency model for the application.
- **The `Job` interface** must be fully asynchronous.
- **Use `asyncssh`** to allow cancellable remote operations.
- **Implement structured concurrency** using `asyncio.TaskGroup` to manage the lifecycle of the orchestrator, jobs, and background monitors (e.g., disk monitor).

## Consequences
**Positive**:
- **Immediate Cancellation**: `asyncio.Task.cancel()` propagates `CancelledError` immediately to awaiting coroutines, allowing instant response to interrupts.
- **Resource Cleanup**: `try...finally` blocks and context managers in async functions ensure resources (connections, processes) are cleaned up even during cancellation.
- **Concurrency**: Enables running background tasks (like disk monitoring) concurrently with the main job without threads.

**Negative**:
- **Complexity**: Asynchronous code introduces complexity in control flow and requires a different mental model compared to synchronous code.
- **Refactoring Cost**: Requires a rewrite of the existing synchronous `Job` implementations and the `Orchestrator`.
- **Testing**: Testing async code requires specific tools (`pytest-asyncio`) and can be harder to debug.

## References
- [Async vs Sync Concurrency Analysis](docs/adr/considerations/async-vs-sync-concurrency-analysis.md)
