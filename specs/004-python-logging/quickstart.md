# Quickstart: Standard Python Logging Integration

**Branch**: `004-python-logging`
**Date**: 2025-12-31

## Configuration

Add the `logging` section to your `~/.config/pc-switcher/config.yaml`:

```yaml
logging:
  file: DEBUG      # Log level floor for file output (default: DEBUG)
  tui: INFO        # Log level floor for TUI output (default: INFO)
  external: WARNING  # Additional floor for external libraries (default: WARNING)
```

### Log Levels

| Level | Value | When to Use |
|-------|-------|-------------|
| `DEBUG` | 10 | Internal diagnostics, development only |
| `FULL` | 15 | Operational details (file-level sync info) |
| `INFO` | 20 | High-level operations (job start/complete) |
| `WARNING` | 30 | Unexpected but non-fatal conditions |
| `ERROR` | 40 | Recoverable errors |
| `CRITICAL` | 50 | Unrecoverable errors, sync must abort |

### Defaults

If `logging` section is omitted, the system uses:
- `file: DEBUG` - All log levels written to file
- `tui: INFO` - Only INFO and above shown in terminal
- `external: WARNING` - External library logs (asyncssh, etc.) filtered to WARNING+

## Common Configurations

### Debug SSH Connection Issues

```yaml
logging:
  file: DEBUG
  tui: INFO
  external: DEBUG  # Show asyncssh debug logs
```

### Quiet Mode (Errors Only)

```yaml
logging:
  file: DEBUG     # Still log everything to file
  tui: ERROR      # Only show errors in terminal
  external: ERROR
```

### Verbose Mode (Everything)

```yaml
logging:
  file: DEBUG
  tui: FULL
  external: FULL
```

## Log Output

### File Format (JSON Lines)

Location: `~/.local/share/pc-switcher/logs/sync-<timestamp>-<session>.log`

```json
{"timestamp": "2025-12-31T14:30:22.123456", "level": "INFO", "job": "btrfs", "host": "source", "message": "Creating snapshot", "subvolume": "@home"}
{"timestamp": "2025-12-31T14:30:22.456789", "level": "FULL", "job": "btrfs", "host": "source", "message": "Snapshot created", "path": "/.snapshots/pc-switcher/..."}
```

### TUI Format (Rich Console)

```
14:30:22 [INFO    ] [btrfs] (laptop-01) Creating snapshot subvolume=@home
14:30:22 [FULL    ] [btrfs] (laptop-01) Snapshot created path=/.snapshots/...
```

## Filtering Behavior

The 3-setting model works as follows:

1. **pcswitcher logs** (from `pcswitcher.*` loggers):
   - File output: respects `file` level
   - TUI output: respects `tui` level

2. **External library logs** (asyncssh, etc.):
   - Must pass `external` level **AND** the destination's level
   - Example: With `file: DEBUG`, `tui: INFO`, `external: WARNING`:
     - asyncssh WARNING → appears in both file and TUI
     - asyncssh INFO → blocked by `external: WARNING`
     - pcswitcher DEBUG → appears in file, not TUI

## Programmatic Usage

```python
import structlog

# Get a logger for your module
log = structlog.get_logger("pcswitcher.jobs.btrfs")

# Basic logging
log.info("Starting sync", target="laptop-02")
log.warning("Slow connection", latency_ms=250)
log.error("Transfer failed", file="/path/to/file", reason="permission denied")

# FULL level (operational details)
log.log(15, "File transferred", path="/home/user/doc.txt", bytes=1024)
# Or with helper method:
log.full("File transferred", path="/home/user/doc.txt", bytes=1024)

# Bind context for all subsequent logs
bound_log = log.bind(job="btrfs", host="source")
bound_log.info("Operation complete")  # Includes job and host automatically
```

## Troubleshooting

### Invalid Log Level in Config

If config contains an invalid log level:

```
Configuration validation failed:
logging.file: Invalid log level: VERBOSE. Valid levels: DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL
```

**Fix**: Use a valid level name or integer value.

### External Library Logs Not Appearing

Check that `external` is set low enough:

```yaml
logging:
  external: DEBUG  # Show all external library logs
```

### Performance with High Volume Logging

For large syncs with FULL level, logs are queued asynchronously. If TUI seems slow:

```yaml
logging:
  tui: INFO  # Reduce TUI volume
  file: FULL  # Keep detailed file logs
```
