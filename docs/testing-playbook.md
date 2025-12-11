# PC-Switcher Manual Testing Playbook

## Purpose

This playbook provides structured procedures for manual verification of pc-switcher's Terminal User Interface (TUI) and feature functionality before releases. Use this to:

- **Visually verify** TUI elements that cannot be automated (colors, formatting, progress bars)
- **Feature tour** all major pc-switcher capabilities to ensure end-to-end functionality
- **Pre-release validation** before publishing new versions
- **Regression testing** after significant infrastructure changes

Manual testing complements the automated test suite (unit and integration tests) by validating the user experience and visual presentation that humans perceive.

## When to Use This Playbook

Run through this playbook:
- Before creating any release (alpha, beta, RC, or stable)
- After significant changes to the TUI or CLI
- After changes to logging, progress reporting, or event handling
- When testing on new terminal emulators or color schemes
- For accessibility verification

## Prerequisites

### System Requirements

- Ubuntu 24.04 LTS
- Python 3.14+ (installed automatically by uv)
- btrfs filesystem with root (`@`) and home (`@home`) subvolumes
- SSH access to a test target machine (or localhost for basic tests)
- Terminal emulator with:
  - 256-color support or better
  - Minimum 80 columns × 24 rows (recommended: 120×40)
  - UTF-8 encoding

### Installation

Install pc-switcher from the version/branch you want to test:

```bash
# Install from specific release
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | VERSION=0.1.0-alpha.1 bash

# OR install from specific branch for testing
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/001-foundation/install.sh | bash -s -- --ref 001-foundation

# Verify installation
pc-switcher --version
which pc-switcher
```

### Configuration Setup

```bash
# Initialize config
pc-switcher init

# Customize config for your test environment
nano ~/.config/pc-switcher/config.yaml

# Critical settings to verify:
# - btrfs_snapshots.subvolumes: ["@", "@home"] (match your system)
# - target_machine.hostname: <your-test-target>
# - target_machine.user: <ssh-user>
# - sync_jobs.dummy_success: true (for infrastructure testing)
```

### Terminal Configuration

For best results, use a terminal emulator with:
- **24-bit true color support**: gnome-terminal, konsole, kitty, alacritty
- **Good Rich library compatibility**: Most modern terminals work well
- **Proper font rendering**: Monospace font with Unicode support

Verify color support:
```bash
# Check if terminal supports 256 colors
tput colors

# Should output: 256 or higher
```

## Visual Verification

### Terminal Display Requirements

**Objective**: Verify the TUI renders correctly in your terminal environment.

#### Step 1: Check Terminal Capabilities

```bash
# Verify terminal size
tput cols  # Should be >= 80
tput lines # Should be >= 24

# Check color support
tput colors  # Should be >= 256

# Test Rich library rendering
uv run python -m rich.diagnose
```

**Expected**: Rich diagnose shows your terminal capabilities and renders test patterns correctly.

**Verify**:
- [ ] Color blocks render with distinct colors
- [ ] Box-drawing characters render correctly (no broken borders)
- [ ] Unicode symbols display properly
- [ ] No rendering artifacts or garbled text

#### Step 2: Verify Color Scheme

**Objective**: Ensure log level colors are distinct and readable.

```bash
# Run dummy sync to generate logs with various levels
# First, enable dummy job in config
nano ~/.config/pc-switcher/config.yaml
# Set: sync_jobs.dummy_success: true

# Run sync
pc-switcher sync localhost
```

**During sync, observe the live TUI log panel** (bottom section of display).

**Expected color mapping**:
- `DEBUG`: Dim/gray text (if visible at DEBUG log level)
- `FULL`: Cyan text
- `INFO`: Green text
- `WARNING`: Yellow text
- `ERROR`: Red text
- `CRITICAL`: Bold red text

**Verify**:
- [ ] Each log level has a distinct color
- [ ] Colors are readable on your terminal background
- [ ] Bold formatting is visible for CRITICAL level
- [ ] Dim formatting is visible for DEBUG level
- [ ] Colors match between file logs (when viewed with `pc-switcher logs`) and live display

### Progress Bar Rendering

**Objective**: Verify progress bars render smoothly and update correctly.

#### Step 1: Basic Progress Bar

```bash
# Run sync with dummy job (already enabled from previous step)
pc-switcher sync localhost
```

**Observe the middle section** (progress bars).

**Verify**:
- [ ] Progress bar renders as a filled bar (not ASCII characters)
- [ ] Bar fills from left to right smoothly
- [ ] Percentage updates in sync with bar fill
- [ ] Job name appears in cyan color before the bar
- [ ] No flickering or visual artifacts during updates
- [ ] Bar completes at 100%

#### Step 2: Multiple Concurrent Progress Bars

If testing with multiple jobs enabled:

```bash
# Enable both dummy jobs in config
# sync_jobs.dummy_success: true
# sync_jobs.dummy_fail: false (set to true to test failure handling)

pc-switcher sync localhost
```

**Verify**:
- [ ] Multiple progress bars stack vertically
- [ ] Each bar updates independently
- [ ] No visual interference between bars
- [ ] All bars are aligned properly

#### Step 3: Progress with Item Display

**Observe progress bars** showing current item being processed.

**Verify**:
- [ ] Current item appears after the job name (e.g., "dummy_success: Processing item 5/10")
- [ ] Item description updates without visual artifacts
- [ ] Long item names are handled gracefully (no line wrapping issues)

### Status Bar Elements

**Objective**: Verify the top status bar displays correct information.

#### Connection Status

```bash
# Start sync to see connection status
pc-switcher sync <target-hostname>
```

**Observe top-left corner** of the TUI.

**Verify**:
- [ ] "Connection: disconnected" shows in red before SSH connection
- [ ] "Connection: connected" shows in green after SSH established
- [ ] Latency displays in milliseconds (e.g., "connected (12.3ms)")
- [ ] Connection status updates reflect actual state

#### Step Progress

**Observe top-right corner** of the TUI.

**Verify**:
- [ ] "Step N/M" appears in cyan
- [ ] Step count increments as sync progresses
- [ ] Total steps (M) matches expected workflow phases

### Log Panel Display

**Objective**: Verify the log panel at the bottom renders correctly.

#### Step 1: Log Scrolling

```bash
# Run sync to generate logs
pc-switcher sync localhost
```

**Observe the bottom panel** ("Recent Logs").

**Verify**:
- [ ] Panel has blue border
- [ ] Panel title "Recent Logs" is visible
- [ ] Maximum of 10 lines display (default max_log_lines)
- [ ] Older logs scroll out as new logs arrive
- [ ] No visual artifacts during scrolling
- [ ] Panel height remains constant

#### Step 2: Log Message Formatting

**Examine individual log lines** in the panel.

**Expected format**:
```text
HH:MM:SS LEVEL    job_name (hostname) message [context]
```

**Verify**:
- [ ] Timestamp appears in dim gray (HH:MM:SS format)
- [ ] Log level is color-coded and left-aligned (8 characters wide)
- [ ] Job name appears in blue
- [ ] Hostname appears in magenta/purple within parentheses
- [ ] Message text is readable
- [ ] Context key-value pairs appear in dim gray (if present)
- [ ] All elements are properly spaced

#### Step 3: Empty State

```bash
# Start sync and immediately observe
pc-switcher sync localhost
```

**At startup**, before any logs are emitted:

**Verify**:
- [ ] Panel shows "[dim]No logs yet[/dim]" placeholder
- [ ] Placeholder is visible but dimmed

### CLI Output (Non-Live Display)

**Objective**: Verify CLI messages outside the live TUI are formatted correctly.

#### Configuration Errors

```bash
# Temporarily corrupt config to test error display
mv ~/.config/pc-switcher/config.yaml ~/.config/pc-switcher/config.yaml.backup
pc-switcher sync localhost
```

**Verify**:
- [ ] "Configuration error:" appears in bold red
- [ ] Error message is clear and helpful
- [ ] Suggestion to run `pc-switcher init` appears in cyan
- [ ] Error messages are properly formatted

```bash
# Restore config
mv ~/.config/pc-switcher/config.yaml.backup ~/.config/pc-switcher/config.yaml
```

#### Version Display

```bash
pc-switcher --version
```

**Verify**:
- [ ] Version displays as "pc-switcher X.Y.Z" (e.g., "pc-switcher 0.1.0-alpha.1")
- [ ] Format matches expected pattern
- [ ] No errors or warnings

#### Logs Command

```bash
# View latest log
pc-switcher logs --last

# List all logs
pc-switcher logs --list
```

**Verify for `logs --last`**:
- [ ] Log file path displays in bold
- [ ] Timestamps are formatted consistently
- [ ] Log levels are color-coded (same colors as TUI)
- [ ] JSON structure is parsed and displayed in human-readable format
- [ ] Context fields are shown in dim gray

**Verify for `logs --list`**:
- [ ] Log files listed with timestamps
- [ ] Directory path is shown
- [ ] File count is accurate
- [ ] "No log files found" message in yellow if directory is empty

### Interrupt Handling

**Objective**: Verify graceful handling of Ctrl+C interrupts.

#### Single Interrupt

```bash
# Start sync
pc-switcher sync localhost

# Press Ctrl+C once during execution
```

**Verify**:
- [ ] Yellow message: "Interrupt received, cleaning up..."
- [ ] Cleanup timeout message displays (30 seconds)
- [ ] Second Ctrl+C warning appears
- [ ] TUI updates stop gracefully
- [ ] No stack traces or errors
- [ ] Exit code is 130 (standard for SIGINT)

```bash
echo $?  # Should output: 130
```

#### Force Interrupt

```bash
# Start sync
pc-switcher sync localhost

# Press Ctrl+C once
# Immediately press Ctrl+C again
```

**Verify**:
- [ ] First interrupt message in yellow
- [ ] Second interrupt shows red "Force terminating!" message
- [ ] Process exits immediately
- [ ] No zombie processes remain

```bash
ps aux | grep pc-switcher  # Should show no running processes
```

## Feature Tour

### Core Sync Workflow

**Objective**: Verify the complete sync workflow executes correctly.

#### Step 1: Pre-Sync Validation

```bash
# Run sync with verbose logging
pc-switcher sync <target-hostname>
```

**During the pre-sync phase**, watch for:

**Verify**:
- [ ] Configuration schema validation passes
- [ ] SSH connection establishes successfully
- [ ] Version compatibility check occurs
- [ ] Lock acquisition succeeds on both machines
- [ ] Btrfs subvolume existence validation runs
- [ ] Disk space preflight check completes
- [ ] No errors during validation phase

#### Step 2: Pre-Sync Snapshots

**Continue observing** as sync enters snapshot creation phase.

**Verify**:
- [ ] Snapshot directory creation logged (if first run)
- [ ] Pre-sync snapshots created for each configured subvolume
- [ ] Snapshot names include timestamp and "pre-" prefix
- [ ] Snapshots are read-only
- [ ] Snapshot creation completes on both source and target

**Manually verify** on source machine:
```bash
sudo btrfs subvolume list -r /.snapshots/pc-switcher/
```

**Verify**:
- [ ] Pre-snapshot subvolumes exist
- [ ] Subvolumes are read-only (ro flag)

#### Step 3: Version Check and Installation

**If target has different version** (test by using a target without pc-switcher or with older version):

**Verify**:
- [ ] Version mismatch detected and logged
- [ ] Target installation/upgrade initiated
- [ ] Progress displayed for installation
- [ ] Installation completes successfully
- [ ] Versions match after installation

**If versions already match**:

**Verify**:
- [ ] Log message: "Target pc-switcher version matches source"
- [ ] Installation step skipped

#### Manual Self-Installation Verification (Release Testing Only)

This step verifies that the InstallOnTargetJob can install the newly released version
on a target machine. This cannot be tested automatically during development because
development versions don't have release tags on GitHub.

**Pre-requisites:**
- Two machines (source and target) with SSH access between them
- Target machine should have an older version of pc-switcher (or none)
- Testing a newly created release

**Test procedure:**

```bash
# On TARGET machine: Ensure pc-switcher is NOT installed or has an OLDER version
# Option A: Uninstall pc-switcher
uv tool uninstall pc-switcher 2>/dev/null || true
pc-switcher --version  # Should fail: command not found

# Option B: Install an older version (e.g., 0.1.0-alpha.1)
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | VERSION=0.1.0-alpha.1 bash
pc-switcher --version  # Should show 0.1.0-alpha.1

# On SOURCE machine: Install the NEW release version
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | VERSION=<NEW_VERSION> bash
pc-switcher --version  # Should show NEW_VERSION

# On SOURCE machine: Run sync to target
# The InstallOnTargetJob should automatically install/upgrade pc-switcher on target
pc-switcher sync <target-hostname>

# On TARGET machine: Verify pc-switcher was installed/upgraded
pc-switcher --version  # Should show NEW_VERSION (same as source)
```

**Expected behavior:**
- If target has no pc-switcher: InstallOnTargetJob installs the source version
- If target has older version: InstallOnTargetJob upgrades to source version
- If target has same version: InstallOnTargetJob skips installation (logged)
- If target has NEWER version: Validation fails with version conflict error

**Automated tests:**
The automated integration tests in `tests/integration/test_self_installation.py` test the
installation mechanism using the release version derived from the current dev version.
The tests that require "current version == released version" are skipped during
development but would run if you install a release and run tests locally.

#### Step 4: Configuration Sync

**Objective**: Verify the interactive configuration synchronization between source and target.

The config sync step compares source and target configurations and handles three scenarios:

**Scenario 1: Target has no configuration**

To test this scenario, ensure the target machine has no config file:
```bash
# On target machine
rm -f ~/.config/pc-switcher/config.yaml

# Run sync from source
pc-switcher sync <target-hostname>
```

**Verify UI elements**:
- [ ] Yellow-bordered panel appears with title "Config Sync"
- [ ] Message: "[yellow]Target has no configuration file.[/yellow]"
- [ ] Source configuration displayed with YAML syntax highlighting (monokai theme)
- [ ] Line numbers shown in config display
- [ ] Blue-bordered panel with title "Source Configuration"
- [ ] Prompt: "[bold]Apply this config to target?[/bold]"
- [ ] Choices: "y" or "n" displayed

**Test "y" (accept)**:
- [ ] "[green]Configuration copied to target.[/green]" appears
- [ ] Sync continues normally

**Test "n" (decline)**:
- [ ] "[red]Sync aborted: configuration required on target.[/red]" appears
- [ ] Sync terminates

**Scenario 2: Target configuration differs from source**

To test this scenario, modify the target config to differ from source:
```bash
# On target machine - modify a setting
nano ~/.config/pc-switcher/config.yaml
# Change log_level_file from INFO to WARNING (or any other change)

# Run sync from source
pc-switcher sync <target-hostname>
```

**Verify UI elements**:
- [ ] Yellow-bordered panel with "[yellow]Target configuration differs from source.[/yellow]"
- [ ] Configuration diff displayed with syntax highlighting (diff format)
- [ ] Blue-bordered panel with title "Configuration Diff"
- [ ] Lines starting with `-` show target values (to be removed)
- [ ] Lines starting with `+` show source values (to be added)
- [ ] Three choices displayed:
  - [ ] `a` - Accept config from source (overwrite target)
  - [ ] `k` - Keep current config on target
  - [ ] `x` - Abort sync
- [ ] Prompt: "[bold]Your choice[/bold]"

**Test "a" (accept source)**:
- [ ] "[green]Configuration copied to target.[/green]" appears
- [ ] Sync continues

**Test "k" (keep target)**:
- [ ] "[yellow]Keeping existing target configuration.[/yellow]" appears
- [ ] Sync continues with target's existing config

**Test "x" (abort)**:
- [ ] "[red]Sync aborted by user.[/red]" appears
- [ ] Sync terminates

**Scenario 3: Configurations match**

To test this scenario, ensure source and target configs are identical:
```bash
# Copy source config to target
scp ~/.config/pc-switcher/config.yaml <target>:~/.config/pc-switcher/

# Run sync
pc-switcher sync <target-hostname>
```

**Verify**:
- [ ] "[dim]Target config matches source, skipping config sync.[/dim]" appears
- [ ] No interactive prompt is shown
- [ ] Sync continues automatically

#### Step 5: Job Execution

**With dummy_success job enabled**:

**Verify**:
- [ ] Job starts with INFO log message
- [ ] Progress bar appears for the job
- [ ] Progress updates smoothly from 0% to 100%
- [ ] Job completes successfully
- [ ] Job completion logged

#### Step 6: Post-Sync Snapshots

**After all jobs complete**:

**Verify**:
- [ ] Post-sync snapshots created for each subvolume
- [ ] Snapshot names include timestamp and "post-" prefix
- [ ] Snapshots are read-only
- [ ] All post-snapshots created successfully

**Manually verify**:
```bash
sudo btrfs subvolume list -r /.snapshots/pc-switcher/
```

**Verify**:
- [ ] Both pre- and post-snapshot subvolumes exist
- [ ] Timestamps distinguish pre from post snapshots
- [ ] Session ID directories contain both snapshot types

#### Step 7: Sync Completion

**Verify**:
- [ ] Sync completes with success message
- [ ] Lock is released on both machines
- [ ] SSH connection closes gracefully
- [ ] Log file is written
- [ ] TUI stops cleanly

### Configuration Management

**Objective**: Verify configuration initialization and validation.

#### Initialize Configuration

```bash
# Remove existing config
mv ~/.config/pc-switcher/config.yaml ~/.config/pc-switcher/config.yaml.backup

# Initialize new config
pc-switcher init
```

**Verify**:
- [ ] Success message in green
- [ ] Config file created at ~/.config/pc-switcher/config.yaml
- [ ] Helpful reminder to customize subvolumes displayed
- [ ] File contains default settings

**Check config structure**:
```bash
cat ~/.config/pc-switcher/config.yaml
```

**Verify**:
- [ ] YAML is properly formatted
- [ ] All required sections present (logging, btrfs_snapshots, target_machine, disk_space_monitor, sync_jobs)
- [ ] Comments explain each setting
- [ ] Default values are sensible

#### Force Overwrite

```bash
# Try init again (should fail)
pc-switcher init

# Force overwrite
pc-switcher init --force
```

**Verify first attempt**:
- [ ] Warning in yellow: "Configuration file already exists"
- [ ] Suggestion to use --force
- [ ] Config file not modified

**Verify force overwrite**:
- [ ] Success message
- [ ] Config file replaced with defaults

```bash
# Restore original config
mv ~/.config/pc-switcher/config.yaml.backup ~/.config/pc-switcher/config.yaml
```

#### Configuration Validation

**Test invalid config**:
```bash
# Edit config to introduce error
nano ~/.config/pc-switcher/config.yaml
# Change log_level_file to invalid value like "INVALID"

# Try to run sync
pc-switcher sync localhost
```

**Verify**:
- [ ] "Configuration error:" in bold red
- [ ] Specific error message identifies the problem
- [ ] Error location (path/field) is shown
- [ ] No stack trace (clean error presentation)

**Fix the error** and restore valid config.

### Snapshot Management

**Objective**: Verify snapshot cleanup functionality.

#### Dry-Run Cleanup

```bash
# Run cleanup in dry-run mode (safe)
pc-switcher cleanup-snapshots --older-than 30d --dry-run
```

**Verify**:
- [ ] Dry-run mode is clearly indicated
- [ ] Snapshots that would be deleted are listed
- [ ] Recent snapshots are preserved (newest 3 sessions by default)
- [ ] No actual deletion occurs
- [ ] Summary shows how many would be deleted

#### List Snapshots

```bash
# List current snapshots
sudo btrfs subvolume list -r /.snapshots/pc-switcher/
```

**Verify**:
- [ ] All snapshots from test runs are present
- [ ] Snapshot naming follows pattern: `YYYYMMDDTHHMMSS-<session-id>/pre-<subvol>-<timestamp>`

#### Actual Cleanup (if testing on disposable system)

**WARNING**: Only run this on a test system.

```bash
# Create multiple sync sessions to have snapshots to clean up
pc-switcher sync localhost  # Run several times

# Delete old snapshots
pc-switcher cleanup-snapshots --older-than 1s --keep-recent 1
```

**Verify**:
- [ ] Deletion progress is displayed
- [ ] Old snapshots are removed
- [ ] Most recent session preserved
- [ ] Cleanup completes successfully

### Error Handling Display

**Objective**: Verify error messages are clear and helpful.

#### Job Failure

```bash
# Enable the dummy_fail job in config
nano ~/.config/pc-switcher/config.yaml
# Set: sync_jobs.dummy_fail: true

# Run sync
pc-switcher sync localhost
```

**Verify**:
- [ ] Job progresses to configured fail_at_percent
- [ ] ERROR or CRITICAL message logged when job fails
- [ ] Error message is descriptive
- [ ] Sync aborts gracefully
- [ ] Pre-sync snapshots remain available for recovery
- [ ] Post-sync snapshots are NOT created (due to failure)

#### SSH Connection Failure

```bash
# Use invalid hostname
pc-switcher sync nonexistent-host-xyz.invalid
```

**Verify**:
- [ ] Connection failure detected
- [ ] Clear error message about connection problem
- [ ] No stack trace (clean error)
- [ ] Graceful exit

#### Disk Space Failure

**Simulate low disk space** (if testing on VM or disposable system):

```bash
# Temporarily set very high disk_space_monitor.preflight_minimum in config
nano ~/.config/pc-switcher/config.yaml
# Set: disk_space_monitor.preflight_minimum: "95%"

# Run sync
pc-switcher sync localhost
```

**Verify**:
- [ ] Disk space check fails with CRITICAL log
- [ ] Error message specifies available vs. required space
- [ ] Sync aborts before creating snapshots
- [ ] Helpful message about how to resolve

**Restore config** to normal values.

### Background Monitoring

**Objective**: Verify disk space monitoring during sync.

```bash
# Ensure runtime monitoring is configured
nano ~/.config/pc-switcher/config.yaml
# Verify: disk_space_monitor.check_interval: 30
#         disk_space_monitor.runtime_minimum: "15%"

# Run a longer sync (if available) or observe logs
pc-switcher sync localhost
```

**Verify**:
- [ ] Disk space checks occur at configured intervals (check logs)
- [ ] No errors if space remains adequate
- [ ] Background monitoring doesn't block job execution

**If you can simulate running out of space** during sync (advanced testing):

**Verify**:
- [ ] CRITICAL error logged when space drops below threshold
- [ ] Sync aborts immediately
- [ ] Current job is terminated gracefully

### Log File Output

**Objective**: Verify structured logging to file.

#### Log File Creation

```bash
# Run sync
pc-switcher sync localhost

# Find the log file
ls -lt ~/.local/share/pc-switcher/logs/
```

**Verify**:
- [ ] Log file created with timestamp in name: `sync-YYYYMMDDTHHMMSS.log`
- [ ] File is in JSON Lines format (one JSON object per line)
- [ ] File is readable with correct permissions

#### Log File Contents

```bash
# View latest log
pc-switcher logs --last
```

**Verify**:
- [ ] All sync operations are logged
- [ ] Timestamps are accurate
- [ ] Log levels are appropriate for events
- [ ] Host (source/target) is identified for each event
- [ ] Job names are included
- [ ] Context fields provide useful detail
- [ ] No truncated or malformed JSON

#### Log Level Filtering (File)

```bash
# Set file log level to ERROR in config
nano ~/.config/pc-switcher/config.yaml
# Set: logging.log_level_file: "ERROR"

# Run sync
pc-switcher sync localhost

# View logs
pc-switcher logs --last
```

**Verify**:
- [ ] Only ERROR and CRITICAL level messages in log file
- [ ] INFO, WARNING, FULL, DEBUG messages filtered out
- [ ] Terminal UI still shows messages per log_level_cli setting

**Restore** log_level_file to default (INFO or FULL).

## Accessibility Considerations

### Screen Reader Compatibility

**Objective**: Ensure basic screen reader usability (if applicable to your users).

**Note**: Rich Live displays may not work well with screen readers due to dynamic updates. Consider testing with screen reader users or accessibility experts if this is a concern.

**Alternative approaches for accessibility**:
- Use `log_level_cli: FULL` for more verbose terminal output
- Disable TUI updates (if such option is added in future)
- Rely on file logs with `pc-switcher logs --last`

### Color Blindness

**Objective**: Verify information is not conveyed by color alone.

**Verify**:
- [ ] Log levels include text labels ("INFO", "ERROR", etc.) not just colors
- [ ] Progress bars show percentage numbers alongside visual bar
- [ ] Connection status uses text ("connected"/"disconnected") not just color
- [ ] Critical information is not color-only (text labels present)

### Terminal Size Adaptability

**Objective**: Verify TUI adapts to different terminal sizes.

#### Small Terminal (80×24)

```bash
# Resize terminal to 80×24
# Run sync
pc-switcher sync localhost
```

**Verify**:
- [ ] TUI renders without horizontal scrolling
- [ ] All elements are visible (may be compressed)
- [ ] No broken layouts or overlapping elements

#### Large Terminal (120×40)

```bash
# Resize terminal to 120×40 or larger
# Run sync
pc-switcher sync localhost
```

**Verify**:
- [ ] TUI uses available space efficiently
- [ ] Progress bars scale appropriately
- [ ] Log panel shows more lines if configured

### High Contrast Mode

**Objective**: Verify readability in high contrast terminal themes.

**Test with**:
- Light terminal backgrounds
- Dark terminal backgrounds
- High contrast system themes

**Verify**:
- [ ] All colors are readable on both light and dark backgrounds
- [ ] Dim text is still visible (not invisible)
- [ ] Bold text stands out sufficiently

## Checklist Summary

Use this checklist for final verification before release:

### Visual Verification Checklist
- [ ] Terminal capabilities verified (colors, size, unicode)
- [ ] All log level colors are distinct and readable
- [ ] Progress bars render smoothly without artifacts
- [ ] Status bar elements (connection, steps) display correctly
- [ ] Log panel scrolls and formats messages properly
- [ ] CLI error messages are formatted and helpful
- [ ] Interrupt handling displays correct messages
- [ ] TUI adapts to different terminal sizes
- [ ] Config sync UI displays correctly (panels, syntax highlighting, diff)

### Feature Tour Checklist
- [ ] Pre-sync validation completes successfully
- [ ] Pre-sync snapshots created correctly
- [ ] Version check and installation works
- [ ] Self-installation on target works (release testing only)
- [ ] Config sync interactive prompts work correctly
- [ ] Job execution displays progress accurately
- [ ] Post-sync snapshots created after success
- [ ] Sync completes and cleans up properly
- [ ] Configuration init/overwrite works
- [ ] Configuration validation catches errors
- [ ] Snapshot cleanup (dry-run) works correctly
- [ ] Job failure is handled gracefully
- [ ] SSH connection errors display clearly
- [ ] Disk space checks prevent issues
- [ ] Log files are created with correct structure
- [ ] Log level filtering works for file and CLI

### Accessibility Checklist
- [ ] Information not conveyed by color alone
- [ ] TUI works on small terminals (80×24)
- [ ] TUI works on large terminals
- [ ] Readable in light and dark themes
- [ ] Screen reader compatibility considered (if applicable)

## Reporting Issues

If you find any issues during manual testing:

1. **Document the issue**:
   - What you were testing (specific step/feature)
   - Expected behavior
   - Actual behavior
   - Terminal environment (emulator, size, color support)
   - PC-switcher version
   - Relevant log excerpts

2. **Reproduce**:
   - Try to reproduce the issue consistently
   - Note any specific conditions required

3. **Create an issue** on GitHub with:
   - Clear title describing the problem
   - Steps to reproduce
   - Screenshots if visual issue
   - Label as "bug" and "UI" (if UI-related)

4. **Severity assessment**:
   - Critical: Blocks release (data loss, crashes, core features broken)
   - High: Significant UX degradation
   - Medium: Minor visual issues, non-critical features
   - Low: Cosmetic issues, edge cases

## Notes

- This playbook evolves as features are added. Update it when new visual elements or features are implemented.
- Consider automating portions of this playbook as visual regression testing capabilities improve.
- Manual testing time estimate: 45-60 minutes for complete playbook.
- Focus on sections most relevant to your changes when doing rapid iteration.
