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

1. Two test VMs available (pc1 and pc2) with SSH access between them

2. Install pc-switcher on both machines from the version you want to test:
   ```bash
   curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | VERSION=<version> bash
   ```

3. Initialize config on pc1:
   ```bash
   pc-switcher init
   # Edit ~/.config/pc-switcher/config.yaml
   # Set target_machine.hostname to pc2
   ```

4. Verify terminal supports 256 colors:
   ```bash
   tput colors  # Should be >= 256
   ```

## Visual Verification

Run all tests from pc1, syncing to pc2.

### 1. TUI Layout and Progress Bars

```bash
pc-switcher sync pc2
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

### 2. Log Level Colors

During sync, observe log messages. Each level should have a distinct color:

- [ ] `DEBUG` - dim/gray (if visible at your log level)
- [ ] `FULL` - cyan
- [ ] `INFO` - green
- [ ] `WARNING` - yellow
- [ ] `ERROR` - red
- [ ] `CRITICAL` - bold red

### 3. Interrupt Display

```bash
pc-switcher sync pc2
# Press Ctrl+C once during sync
```

- [ ] Yellow message: "Interrupt received, cleaning up..."
- [ ] Cleanup timeout displays
- [ ] Second Ctrl+C warning appears
- [ ] Exit is clean (no stack trace)

### 4. CLI Messages Outside TUI

**Version display:**
```bash
pc-switcher --version
```
- [ ] Shows "pc-switcher X.Y.Z" format

**Configuration error:**
```bash
mv ~/.config/pc-switcher/config.yaml ~/.config/pc-switcher/config.yaml.bak
pc-switcher sync pc2
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

### 5. Config Sync UI

To test config sync display, ensure pc2 config differs from pc1:

```bash
# On pc2, modify a setting
ssh pc2 "sed -i 's/INFO/WARNING/' ~/.config/pc-switcher/config.yaml"

# Run sync from pc1
pc-switcher sync pc2
```

**When configs differ:**
- [ ] Yellow-bordered panel with "Config Sync" title
- [ ] Configuration diff shows with syntax highlighting
- [ ] Lines starting with `-` and `+` are distinguishable
- [ ] Choices (a/k/x) displayed clearly

### 6. Terminal Size Adaptability

**Small terminal (80×24):**
- Resize terminal to minimum size
- Run `pc-switcher sync pc2`
- [ ] All elements visible (may be compressed)
- [ ] No horizontal scrolling needed
- [ ] No overlapping or broken layout

**Large terminal (120×40+):**
- Resize terminal to large size
- Run `pc-switcher sync pc2`
- [ ] Progress bars scale appropriately
- [ ] Layout uses space efficiently

### 7. Theme Compatibility

Test on your terminal's light and dark modes (if applicable):
- [ ] All text readable on dark background
- [ ] All text readable on light background
- [ ] Dim text visible (not invisible)
- [ ] Bold text stands out

## Summary Checklist

Before release, verify all pass:

**Visual Elements:**
- [ ] Progress bars animate smoothly
- [ ] Log level colors are distinct
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
- [ ] Config sync UI displays correctly

## Reporting Issues

If visual issues are found:

1. Note the specific element affected
2. Screenshot if possible
3. Record terminal: emulator, size (`tput cols`×`tput lines`), color support (`tput colors`)
4. Create GitHub issue with label "bug" and description
