# First Release Playbook for PC-Switcher v0.1.0

## 📌 Versioning Convention

**Decision:** Use Semantic Versioning (SemVer) for all releases, including pre-releases.

**Format:**
- Alpha: `v0.1.0-alpha.1`, `v0.1.0-alpha.2`
- Beta: `v0.1.0-beta.1`, `v0.1.0-beta.2`
- RC: `v0.1.0-rc.1`, `v0.1.0-rc.2`
- Final: `v0.1.0`

**This first release:** `v0.1.0-alpha.1`

**References:**
- ADR-004: Dynamic Package Versioning from GitHub Releases
- GitHub Issue #34: Allow version tags with pre-release part

---

## ✅ PRE-RELEASE VERIFICATION (Completed)

All checks pass:
- ✅ 25/25 tests passing
- ✅ Linters pass (ruff + basedpyright)
- ✅ Package builds successfully
- ✅ CLI commands work correctly
- ✅ Install script configured for github.com/flaksit/pc-switcher
- ✅ Default config and schema files included in package
- ✅ Documentation complete

**Current state:**
- Branch: `001-foundation`
- No existing git tags (first release)

---

## 📋 RELEASE PROCESS

### Step 1: Clean up working directory

```bash
# Decide what to do with uncommitted changes
git status

# Option A: Commit if you want to keep the notes
git add "draft prompts.txt"
git commit -m "Update draft prompts"

# Option B: Discard if temporary
git restore "draft prompts.txt"

# Verify clean
git status
```

### Step 2: Create Pull Request

```bash
# Push branch if not already pushed
git push origin 001-foundation

# Create PR on GitHub
# Go to: https://github.com/flaksit/pc-switcher/compare/main...001-foundation
```

**PR Details:**
- **Title:** `Foundation Infrastructure (Features 1+2+3)`
- **Description:**

```markdown
Completes the foundation infrastructure for pc-switcher.

## Features Implemented

1. **Basic CLI & Infrastructure**
   - Command parser with Typer
   - YAML configuration with JSON schema validation
   - SSH connection management with asyncssh
   - Structured logging (file + console)
   - Rich terminal UI with progress bars
   - Modular job architecture

2. **Safety Infrastructure**
   - Pre-sync validation framework
   - Btrfs snapshot management (pre/post sync)
   - Snapshot cleanup command with age-based retention
   - Disk space monitoring

3. **Installation & Setup**
   - One-command installation script
   - Automatic uv and btrfs-progs installation
   - Config initialization command
   - Log directory setup

## Infrastructure

- Async orchestrator with graceful interrupt handling
- Job types: sync jobs, system jobs, background jobs
- Version compatibility checking
- Lock file management
- Comprehensive test suite (25 tests, all passing)
- Type checking with basedpyright
- Linting with ruff

## Testing

All quality checks pass:
- ✅ Tests: 25/25 passing
- ✅ Linters: 0 errors, 0 warnings
- ✅ Type checker: 0 errors
- ✅ Package builds successfully

## Documentation

- Architecture documentation
- Implementation guide
- ADRs for key decisions
- Feature specifications in specs/001-foundation/

## Next Steps

After merge:
1. Create v0.1.0 release (pre-release/alpha)
2. Test installation from GitHub
3. Begin work on feature sync jobs (user data, packages, etc.)

Closes #[issue-number-if-any]
```

**Review and merge:**
```bash
# After PR approval, merge it (you can do this via GitHub UI)
# Or from command line:
git checkout main
git pull origin main
git merge 001-foundation --no-ff
git push origin main
```

Or just
```bash
gh pr merge <pr-number-or-url> --merge
```

### Step 3: Create GitHub Release (with new tag)

Go to: <https://github.com/flaksit/pc-switcher/releases/new>

**GitHub UI Steps:**

1. **Choose a tag:** Type `v0.1.0-alpha.1` (will show "Create new tag: v0.1.0-alpha.1 on publish")

2. **Target:** `main` branch

3. **Release title:** `v0.1.0-alpha.1 - Foundation Infrastructure`

4. Click **"Generate release notes"** button

5. **Edit the generated notes** - add this summary at the top:

````markdown
# PC-switcher v0.1.0-alpha.1 - Foundation Infrastructure (Alpha)

🎉 First release! Complete foundation infrastructure for synchronizing Linux desktop machines.

**⚠️ Alpha Release**: This release contains the foundation infrastructure only. Actual sync functionality (user data, packages, Docker, VMs, etc.) is not yet implemented. The `sync` command works but uses dummy jobs to test the infrastructure.

## 🚀 Quick Start

```bash
# Install
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash

# Initialize
pc-switcher init

# Customize config (especially btrfs subvolumes!)
nano ~/.config/pc-switcher/config.yaml

# Test
pc-switcher --help
```

## 📦 What's Working

- ✅ CLI with sync, logs, init, cleanup-snapshots commands
- ✅ SSH connection management
- ✅ Btrfs snapshot management (pre/post sync + cleanup)
- ✅ Disk space monitoring with configurable thresholds
- ✅ Terminal UI with progress bars
- ✅ Structured logging (file + console)
- ✅ Configuration management with validation
- ✅ One-command installation with dependency setup

## 📋 Requirements

- Ubuntu 24.04 LTS
- Python 3.14+ (automatically installed via uv)
- btrfs filesystem
- SSH access between machines
- btrfs-progs

## 📖 Documentation

- [README](https://github.com/flaksit/pc-switcher/blob/main/README.md)
- [Architecture](https://github.com/flaksit/pc-switcher/blob/main/docs/architecture.md)
- [High-level Requirements](https://github.com/flaksit/pc-switcher/blob/main/docs/High%20level%20requirements.md)

````

---

[Generated release notes will appear below this summary]

6. Check **"Set as a pre-release"** ✅ (because this is alpha)

7. Click **"Publish release"**

The tag `v0.1.0-alpha.1` will be created automatically when you publish.

---

## 🧪 INSTALLATION AND TESTING

### Step 4: Test installation on clean system

Test on a VM or second machine:

```bash
# Test installing specific version
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | VERSION=0.1.0-alpha.1 bash

# Verify
pc-switcher --version  # Should show 0.1.0-alpha.1
which pc-switcher
```

### Step 5: Test core functionality

```bash
# Initialize config
pc-switcher init
cat ~/.config/pc-switcher/config.yaml

# Test help
pc-switcher --help
pc-switcher sync --help
pc-switcher logs --help
pc-switcher cleanup-snapshots --help

# Test logs
pc-switcher logs
ls ~/.local/share/pc-switcher/logs/
```

### Step 6: Test on btrfs system

```bash
# Verify btrfs
sudo btrfs --version
sudo btrfs subvolume list /

# Update config to match your subvolumes
nano ~/.config/pc-switcher/config.yaml

# Test snapshot cleanup (dry-run is safe)
pc-switcher cleanup-snapshots --older-than 30d --dry-run
```

### Step 7: Test sync command (dummy jobs)

```bash
# Enable dummy job in config
nano ~/.config/pc-switcher/config.yaml
# Set sync_jobs.dummy_success: true

# Run sync (replace with actual hostname)
pc-switcher sync test-hostname

# Expected: SSH connection, validation, snapshots, progress bars

# Check logs
pc-switcher logs --last
```

---

## ✅ POST-RELEASE CHECKLIST

- [ ] Step 1: Clean working directory
- [ ] Step 2: Create and merge PR
- [ ] Step 3: Create GitHub release with tag v0.1.0-alpha.1
- [ ] Step 4: Test installation from GitHub
- [ ] Step 5: Test core CLI functionality
- [ ] Step 6: Test btrfs snapshot cleanup
- [ ] Step 7: Test sync with dummy jobs
- [ ] Verify release shows as "Pre-release" on GitHub
- [ ] Verify tag v0.1.0-alpha.1 was created
- [ ] Verify installation script works: `curl -sSL ... | bash`
- [ ] Verify version parsing works: `pc-switcher --version`

---

## 📝 NOTES

**Version Strategy (per ADR-004):**
- Use Semantic Versioning (SemVer) for all releases
- First release: `v0.1.0-alpha.1` = Foundation infrastructure (alpha)
- Next release examples:
  - `v0.1.0-alpha.2` (if more alpha iterations)
  - `v0.1.0-beta.1` (when moving to beta)
  - `v0.1.0-rc.1` (release candidate)
  - `v0.2.0` (first real sync job implementation)
- Version format: MAJOR.MINOR.PATCH[-prerelease.number]

**Alpha Status:**
- This is a pre-release (alpha)
- Infrastructure is complete but sync jobs are dummy/test only
- Not production-ready - marked as pre-release on GitHub
- Version parsing now supports SemVer pre-release identifiers (see issue #34)

**Code Changes in This Release:**
- ADR-004 updated with SemVer pre-release conventions
- `version.py` regex fixed to support `-alpha`, `-beta`, `-rc` suffixes
- New tests added for pre-release version parsing (10 tests total)
- All tests pass, linters pass, type checking passes

**What's Next:**
- Feature 5: User Data Sync
- Feature 6: Package Management Sync
- Feature 7: System Configuration Sync
- Features 8-10: Docker, VMs, k3s
