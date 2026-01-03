# PC-Switcher Manual Testing Playbook

## Purpose

This playbook verifies **visual and UX elements** that automated tests cannot check. The functional behavior (SSH connections, snapshots, config sync logic, etc.) is validated by the automated test suite.

**Focus areas:**
- Colors render correctly and are distinguishable
- Progress bars animate smoothly without artifacts
- TUI layout adapts to different terminal sizes
- Text is readable on your terminal theme

**Estimated time:** 20-30 minutes

## Prerequisites

1. Install pc-switcher from the version you want to test:
   ```bash
   curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | VERSION=<version> bash
   ```

2. Initialize config and enable the dummy job:
   ```bash
   pc-switcher init
   # Edit ~/.config/pc-switcher/config.yaml
   # Set: sync_jobs.dummy_success: true
   ```

3. Verify terminal supports 256 colors:
   ```bash
   tput colors  # Should be >= 256
   ```

## Visual Verification

### 1. Rich Library Rendering

```bash
uv run python -m rich.diagnose
```

**Pass criteria:**
- [ ] Color blocks show distinct colors
- [ ] Box-drawing characters render correctly (solid borders, no broken lines)
- [ ] Unicode symbols display properly
- [ ] No garbled text or rendering artifacts

### 2. TUI Layout and Progress Bars

```bash
pc-switcher sync localhost
```

**Observe the display during sync:**

**Status bar (top):**
- [ ] "Connection: disconnected" in red → "connected" in green
- [ ] Latency shows in milliseconds after connection
- [ ] "Step N/M" visible in cyan on the right

**Progress section (middle):**
- [ ] Progress bar fills smoothly from left to right
- [ ] Percentage updates match bar fill
- [ ] Job name appears in cyan before the bar
- [ ] No flickering during updates

**Log panel (bottom):**
- [ ] Blue border around panel
- [ ] "Recent Logs" title visible
- [ ] Logs scroll up as new ones arrive
- [ ] No visual artifacts during scrolling

### 3. Log Level Colors

During sync, observe log messages. Each level should have a distinct color:

- [ ] `DEBUG` - dim/gray (if visible at your log level)
- [ ] `FULL` - cyan
- [ ] `INFO` - green
- [ ] `WARNING` - yellow
- [ ] `ERROR` - red
- [ ] `CRITICAL` - bold red

### 4. Log Message Formatting

Individual log lines should follow this format:
```
HH:MM:SS LEVEL    job_name (hostname) message [context]
```

**Verify:**
- [ ] Timestamp in dim gray
- [ ] Level is color-coded and aligned
- [ ] Job name in blue, hostname in magenta
- [ ] Context key-value pairs in dim gray (if present)

### 5. Interrupt Display

```bash
pc-switcher sync localhost
# Press Ctrl+C once during sync
```

- [ ] Yellow message: "Interrupt received, cleaning up..."
- [ ] Cleanup timeout displays
- [ ] Second Ctrl+C warning appears
- [ ] Exit is clean (no stack trace)

### 6. CLI Messages Outside TUI

**Version display:**
```bash
pc-switcher --version
```
- [ ] Shows "pc-switcher X.Y.Z" format

**Configuration error:**
```bash
mv ~/.config/pc-switcher/config.yaml ~/.config/pc-switcher/config.yaml.bak
pc-switcher sync localhost
mv ~/.config/pc-switcher/config.yaml.bak ~/.config/pc-switcher/config.yaml
```
- [ ] "Configuration error:" in bold red
- [ ] Suggestion to run `pc-switcher init` in cyan

**Logs command:**
```bash
pc-switcher logs --last
```
- [ ] Log entries are color-coded (same colors as TUI)
- [ ] Timestamps and context fields readable

### 7. Config Sync UI (if testing with a target machine)

To test config sync display, ensure target config differs from source:

```bash
pc-switcher sync <target-hostname>
```

**When configs differ:**
- [ ] Yellow-bordered panel with "Config Sync" title
- [ ] Configuration diff shows with syntax highlighting
- [ ] Lines starting with `-` and `+` are distinguishable
- [ ] Choices (a/k/x) displayed clearly

### 8. Terminal Size Adaptability

**Small terminal (80×24):**
- Resize terminal to minimum size
- Run `pc-switcher sync localhost`
- [ ] All elements visible (may be compressed)
- [ ] No horizontal scrolling needed
- [ ] No overlapping or broken layout

**Large terminal (120×40+):**
- Resize terminal to large size
- Run `pc-switcher sync localhost`
- [ ] Progress bars scale appropriately
- [ ] Layout uses space efficiently

### 9. Theme Compatibility

Test on your terminal's light and dark modes (if applicable):
- [ ] All text readable on dark background
- [ ] All text readable on light background
- [ ] Dim text visible (not invisible)
- [ ] Bold text stands out

## Summary Checklist

Before release, verify all pass:

**Visual Elements:**
- [ ] Rich library renders correctly
- [ ] Progress bars animate smoothly
- [ ] Log level colors are distinct
- [ ] Log messages format correctly
- [ ] Status bar updates properly

**CLI Output:**
- [ ] Version displays correctly
- [ ] Error messages are formatted and helpful
- [ ] Logs command output is readable

**Adaptability:**
- [ ] Works on small terminals (80×24)
- [ ] Works on large terminals
- [ ] Readable on your terminal theme

**Interactions:**
- [ ] Interrupt handling shows correct messages
- [ ] Config sync UI displays correctly (if applicable)

## Reporting Issues

If visual issues are found:

1. Note the specific element affected
2. Screenshot if possible
3. Record terminal: emulator, size (`tput cols`×`tput lines`), color support (`tput colors`)
4. Create GitHub issue with label "bug" and description
