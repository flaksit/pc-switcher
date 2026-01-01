# Implementation Review: 004-python-logging

## Review Summary
The implementation is **NOT COMPLETE**. There are functional issues and test coverage gaps relative to the spec.

## Findings
1. **Root `basicConfig` handler bypasses the new logging pipeline and breaks TUI formatting**. `logging.basicConfig()` installs a plain `StreamHandler` on the root logger before `setup_logging()` runs. External library logs will be emitted twice: once through the plain handler (unformatted, not respecting `logging.file`/`logging.tui` levels) and again through the queue/formatter pipeline. This violates FR-004 and FR-008 because logs are no longer routed solely through the configured handlers or preserved TUI format. Remove the `basicConfig` handler or reconfigure it to use the queue-based handlers only. Location: `src/pcswitcher/cli.py:107`.

2. **TUI output does not render colors/markup**. `RichFormatter` emits Rich markup tags (e.g., `[green]`) but the handler is a plain `logging.StreamHandler` writing to `sys.stderr`, which does not interpret Rich markup. This means users will see markup text rather than colored output, violating FR-008 and User Story 4 acceptance scenario #2 (colored ERROR output). Use a Rich-aware handler (e.g., `rich.logging.RichHandler` or `Console.print` integration) or emit ANSI codes instead. Location: `src/pcswitcher/logger.py:250`.

3. **Acceptance scenarios for external log filtering and per-destination levels lack tests**. The spec’s User Story 1/2 scenarios (e.g., external INFO suppressed when `external: WARNING`, external INFO shown only in file when `tui: WARNING`, and pcswitcher DEBUG shown only in file when `tui: INFO`) are not covered by tests. Add unit or integration tests that exercise external logger records and verify file vs TUI filtering behavior. Current coverage stops at formatter/unit setup tests. Locations to extend: `tests/unit/test_logging.py`, `tests/contract/test_logging_contract.py`.
