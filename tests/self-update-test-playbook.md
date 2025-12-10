# Test Playbook: `pc-switcher self update` Command

## Prerequisites

- Fresh Ubuntu 24.04 LTS machine (physical or VM)
- Network access to GitHub

## Current State Summary

| Item | Value |
|------|-------|
| Existing release | `v0.1.0-alpha.1` (prerelease, does NOT have self-update) |
| Branch with self-update feature | `32-self-update` |
| Main branch | Does NOT have self-update command yet |

---

## Phase 1: Pre-Merge Testing

These tests can be run before the feature is merged and released.

### Scenario 1.1: Install old version, upgrade to branch with self-update

**Goal**: Verify the self-update command exists in the feature branch.

```bash
# 1. Install the existing release (v0.1.0-alpha.1)
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | VERSION=0.1.0-alpha.1 bash

# 2. Verify installation
pc-switcher --version
# Expected: pc-switcher 0.1.0-alpha.1

# 3. Verify self update command does NOT exist yet
pc-switcher self update --help
# Expected: Error - "No such command 'self'" (v0.1.0-alpha.1 doesn't have it)

# 4. Upgrade to the 32-self-update branch (which has the self update command)
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash -s -- --ref 32-self-update

# 5. Verify upgraded version
pc-switcher --version
# Expected: pc-switcher 0.1.0-alpha.1+post.N.dev.0.HASH (development version from branch)

# 6. Verify self update command NOW exists
pc-switcher self update --help
# Expected: Shows help for the update command with usage info
```

### Scenario 1.2: Test `self update` finds latest release

```bash
# From Scenario 1.1's state (running 32-self-update branch)

# 1. Get current version
pc-switcher --version
# Note the dev version is "newer" than v0.1.0-alpha.1

# 2. Try updating to latest (without --prerelease, finds no stable releases)
pc-switcher self update
# Expected: Error - no stable releases found (only v0.1.0-alpha.1 exists, which is prerelease)

# 3. Try updating to latest with --prerelease
pc-switcher self update --prerelease
# Expected: "Already at version ..." or attempts downgrade to v0.1.0-alpha.1
#           (dev version is newer than the only prerelease)
```

### Scenario 1.3: Test `self update` with explicit version (downgrade)

```bash
# 1. Ensure you're on 32-self-update branch (from Scenario 1.1)
pc-switcher --version

# 2. "Downgrade" to the released version explicitly
pc-switcher self update 0.1.0-alpha.1
# Expected:
#   - "Warning: Downgrading from ... to 0.1.0-alpha.1"
#   - Downloads and installs v0.1.0-alpha.1
#   - "Successfully updated to 0.1.0-alpha.1"

# 3. Verify downgrade
pc-switcher --version
# Expected: pc-switcher 0.1.0-alpha.1

# 4. IMPORTANT: After downgrade, `self update` command is GONE!
pc-switcher self update --help
# Expected: Error - "No such command 'self'"
# This is correct - v0.1.0-alpha.1 doesn't have self-update feature.
```

### Scenario 1.4: Test version format acceptance (SemVer vs PEP 440)

```bash
# The self update command should accept both version formats

# 1. Reinstall from branch
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash -s -- --ref 32-self-update

# 2. Test SemVer format
pc-switcher self update 0.1.0-alpha.1
# Expected: Installs v0.1.0-alpha.1

# 3. Reinstall from branch again
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash -s -- --ref 32-self-update

# 4. Test PEP 440 format
pc-switcher self update 0.1.0a1
# Expected: Installs the same version (v0.1.0-alpha.1)

# Both formats should work identically
```

### Scenario 1.5: Test invalid version handling

```bash
# 1. Ensure you have self update command
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash -s -- --ref 32-self-update

# 2. Try invalid version format
pc-switcher self update not-a-version
# Expected: "Error: Invalid version format: not-a-version"

# 3. Try non-existent version
pc-switcher self update 99.99.99
# Expected: Error from uv about version/tag not found on GitHub
```

---

## Phase 2: Merge and Release

**Important**: The following critical paths cannot be tested until the feature is merged and a new release is created:

- Using `self update` (without arguments) to upgrade to a genuinely newer release
- Upgrading FROM a released version that has `self update` TO another released version
- The primary user workflow: running `pc-switcher self update` after a new version is announced

### Tasks

1. **Create PR** for branch `32-self-update` targeting `main`
   ```bash
   gh pr create --base main --head 32-self-update \
     --title "Add pc-switcher self update command" \
     --body "Closes #32"
   ```

2. **Review and merge** the PR

3. **Create a new release** with a version newer than `v0.1.0-alpha.1`
   - Suggested version: `v0.1.0-alpha.2` or `v0.2.0-alpha.1`
   - Tag format must be `v{semver}` (e.g., `v0.1.0-alpha.2`)
   ```bash
   gh release create v0.1.0-alpha.2 --title "v0.1.0-alpha.2" --prerelease --notes "Adds self-update command"
   ```

---

## Phase 3: Post-Release Testing

Execute these scenarios AFTER a new release containing the self-update feature is published.

**Assumption**: New release is `v0.1.0-alpha.2`. Adjust version numbers in commands below as needed.

### Scenario 3.1: Fresh install of new release with self-update

```bash
# 1. Clean slate
uv tool uninstall pc-switcher 2>/dev/null || true

# 2. Install the new release
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | VERSION=0.1.0-alpha.2 bash

# 3. Verify installation
pc-switcher --version
# Expected: pc-switcher 0.1.0-alpha.2

# 4. Verify self update command exists
pc-switcher self update --help
# Expected: Shows help for the update command
```

### Scenario 3.2: Upgrade from old release (without self-update) to new release

```bash
# 1. Clean slate
uv tool uninstall pc-switcher 2>/dev/null || true

# 2. Install the OLD release (which does NOT have self-update)
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | VERSION=0.1.0-alpha.1 bash

# 3. Verify old version
pc-switcher --version
# Expected: pc-switcher 0.1.0-alpha.1

# 4. Cannot use self-update (doesn't exist), must use install script
pc-switcher self update
# Expected: Error - "No such command 'self'"

# 5. Use install script to upgrade
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | VERSION=0.1.0-alpha.2 bash

# 6. Verify upgrade
pc-switcher --version
# Expected: pc-switcher 0.1.0-alpha.2

# 7. NOW self update is available for future upgrades
pc-switcher self update --help
# Expected: Shows help
```

### Scenario 3.3: Use `self update` to get latest (primary use case)

**This is the main user workflow that couldn't be tested before.**

```bash
# 1. Install the release with self-update
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | VERSION=0.1.0-alpha.2 bash

# 2. Verify version
pc-switcher --version
# Expected: pc-switcher 0.1.0-alpha.2

# 3a. Check for stable updates
pc-switcher self update
# Expected (if no stable release): Error about no stable releases found
# Expected (if stable release exists): Updates to latest stable

# 3b. Check for prerelease updates
pc-switcher self update --prerelease
# Expected (if this is latest prerelease): "Already at version 0.1.0-alpha.2"
# Expected (if newer prerelease exists): Updates to that version
```

### Scenario 3.4: Upgrade between two releases (both have self-update)

**This tests the full self-update lifecycle.**

Requires TWO releases that both have self-update (e.g., `v0.1.0-alpha.2` and `v0.1.0-alpha.3`).

```bash
# 1. Install older release with self-update
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | VERSION=0.1.0-alpha.2 bash

# 2. Verify
pc-switcher --version
# Expected: pc-switcher 0.1.0-alpha.2

# 3. Use self update to upgrade to specific newer version
pc-switcher self update 0.1.0-alpha.3
# Expected:
#   - "Updating pc-switcher from 0.1.0-alpha.2 to 0.1.0-alpha.3..."
#   - "Successfully updated to 0.1.0-alpha.3"

# 4. Verify upgrade
pc-switcher --version
# Expected: pc-switcher 0.1.0-alpha.3

# 5. Self update command still works (persists across upgrades)
pc-switcher self update --help
# Expected: Shows help
```

### Scenario 3.5: Downgrade using `self update`

```bash
# From Scenario 3.4's state (running v0.1.0-alpha.3)

# 1. Downgrade to older version
pc-switcher self update 0.1.0-alpha.2
# Expected:
#   - "Warning: Downgrading from 0.1.0-alpha.3 to 0.1.0-alpha.2"
#   - Proceeds with installation
#   - "Successfully updated to 0.1.0-alpha.2"

# 2. Verify downgrade
pc-switcher --version
# Expected: pc-switcher 0.1.0-alpha.2

# 3. Self update still works (v0.1.0-alpha.2 has the feature)
pc-switcher self update --help
# Expected: Shows help
```

---

## Utility Commands

### Clean slate reset

```bash
# Remove pc-switcher completely
uv tool uninstall pc-switcher

# Verify removal
which pc-switcher
# Expected: no output (command not found)

# Optional: Remove config and data
rm -rf ~/.config/pc-switcher
rm -rf ~/.local/share/pc-switcher
```

### Check available releases

```bash
gh release list --repo flaksit/pc-switcher
```

---

## Test Results Checklist

### Phase 1: Pre-Merge

| # | Scenario | Result | Notes |
|---|----------|--------|-------|
| 1.1 | Install old, upgrade to branch | ☐ | |
| 1.2 | Self update finds latest release | ☐ | |
| 1.3 | Explicit version downgrade | ☐ | |
| 1.4 | SemVer and PEP 440 format acceptance | ☐ | |
| 1.5 | Invalid version handling | ☐ | |

### Phase 2: Merge & Release

| Task | Done | Details |
|------|------|---------|
| PR created | ☐ | PR #___ |
| PR merged | ☐ | |
| New release created | ☐ | Version: v_______ |

### Phase 3: Post-Release

| # | Scenario | Result | Notes |
|---|----------|--------|-------|
| 3.1 | Fresh install of new release | ☐ | |
| 3.2 | Upgrade from old release via install script | ☐ | |
| 3.3 | Self update to get latest (no args) | ☐ | |
| 3.4 | Upgrade between two releases with self-update | ☐ | Requires 2 releases |
| 3.5 | Downgrade using self update | ☐ | |
