# Testing Framework Research

**Feature**: 002-testing-framework
**Date**: 2025-12-05
**Status**: Complete

## Summary

Research for the testing framework implementation. All technical decisions have been validated against best practices and existing patterns in the codebase.

---

## 1. pytest-asyncio Configuration

### Decision: Use `asyncio_mode = "auto"` with function-scoped event loops

### Rationale
- Auto mode automatically detects async test functions without requiring explicit `@pytest.mark.asyncio` markers
- Function-scoped loops provide highest test isolation (default behavior)
- Aligns with existing codebase patterns in `tests/conftest.py`

### Configuration

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

### Alternatives Considered
- **Strict mode**: Requires explicit markers on all async tests; rejected for verbosity
- **Module/session-scoped loops**: Rejected for reduced isolation between tests

---

## 2. pytest Markers and Test Selection

### Decision: Default exclusion of integration tests via `-m "not integration"` in addopts

### Rationale
- FR-008a requires integration tests NOT run by default
- Configuration-based exclusion is simpler than hook-based
- Can be overridden with `-m integration` or `-m ""`

### Configuration

```toml
# pyproject.toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = [
    "--strict-markers",
    "-v",
    "-m", "not integration",
]
markers = [
    "integration: Integration tests (require VM infrastructure)",
    "slow: Tests that take >5 seconds",
]
```

### Skip Behavior for Missing Environment

```python
# tests/integration/conftest.py
import os
import pytest

def pytest_collection_modifyitems(config, items):
    """Skip integration tests if VM environment not configured."""
    vm_host = os.getenv("PC_SWITCHER_TEST_PC1_HOST")

    if not vm_host:
        skip_msg = "Skipping integration tests: VM environment not configured"
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(pytest.mark.skip(reason=skip_msg))
```

### Alternatives Considered
- **Custom CLI option (`--run-integration`)**: More explicit but deviates from standard pytest patterns
- **Hook-based exclusion in root conftest.py**: More complex; chosen approach is simpler

---

## 3. OpenTofu/Hetzner Cloud Configuration

### Decision: Use OpenTofu with Hetzner Storage Box for state persistence

### Rationale
- OpenTofu is actively maintained Terraform fork (per constitution: well-supported tools)
- Local state alone would be ephemeral in CI runners; remote state needed for shared access
- Hetzner Storage Box (existing infrastructure) provides cost-effective SSH/SCP-accessible storage
- Hetzner CX23 VMs meet cost constraint (< EUR 10/month)

### Configuration Structure

```hcl
# tests/infrastructure/main.tf
terraform {
  required_version = ">= 1.6"
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.45.0"
    }
  }
  # Local backend - state synced to/from Storage Box via wrapper script
}

provider "hcloud" {
  token = var.hcloud_token
}

resource "hcloud_server" "pc1" {
  name        = "pc-switcher-pc1"
  server_type = "cx23"  # 2 vCPU, 4GB RAM, ~EUR 3.50/month
  image       = "ubuntu-24.04"
  location    = "fsn1"
  ssh_keys    = [hcloud_ssh_key.test_key.id]
}

resource "hcloud_server" "pc2" {
  name        = "pc-switcher-pc2"
  server_type = "cx23"
  image       = "ubuntu-24.04"
  location    = "fsn1"
  ssh_keys    = [hcloud_ssh_key.test_key.id]
}
```

### State Management
- State file: `tests/infrastructure/terraform.tfstate` (gitignored locally)
- Remote storage: Hetzner Storage Box at `pc-switcher/test-infrastructure/terraform.tfstate`
- Wrapper script `tofu-wrapper.sh` syncs state before/after tofu operations:
  1. Pull state from Storage Box via SCP
  2. Run tofu command
  3. Push updated state back to Storage Box
- Single source of truth prevents CI/developer state drift

### Alternatives Considered
- **Pure local state**: Would be lost in CI runners; causes drift between dev/CI
- **Hetzner Object Storage (S3)**: More expensive than existing Storage Box
- **Smaller VM types**: CX11 (2GB RAM) may be insufficient for btrfs operations

---

## 4. btrfs Snapshot Management

### Decision: Baseline snapshots created at provisioning; reset via mv + snapshot + reboot

### Rationale
- Read-only baseline snapshots preserve known-good state
- mv + snapshot approach is simpler and avoids set-default complexity
- Reboot required to activate new root subvolume (~10-20 seconds)

### Reset Flow

Per docs/testing-framework.md, the reset procedure is:

```bash
#!/bin/bash
# tests/infrastructure/scripts/reset-vm.sh

# 1. Delete test artifacts (preserving baseline snapshots)
ssh root@$VM "rm -rf /.snapshots/pc-switcher/test-* 2>/dev/null || true"

# 2. Mount top-level btrfs filesystem
ssh root@$VM "mount -o subvolid=5 /dev/sda2 /mnt/btrfs"

# 3. Replace active subvolumes with fresh snapshots from baseline
ssh root@$VM "mv /mnt/btrfs/@ /mnt/btrfs/@_old"
ssh root@$VM "btrfs subvolume snapshot /mnt/btrfs/.snapshots/baseline/@ /mnt/btrfs/@"
ssh root@$VM "mv /mnt/btrfs/@home /mnt/btrfs/@home_old"
ssh root@$VM "btrfs subvolume snapshot /mnt/btrfs/.snapshots/baseline/@home /mnt/btrfs/@home"

# 4. Unmount and reboot
ssh root@$VM "umount /mnt/btrfs"
ssh root@$VM "reboot" || true
sleep 20
# Wait for VM to come back online

# 5. Clean up old subvolumes after reboot
ssh root@$VM "mount -o subvolid=5 /dev/sda2 /mnt/btrfs"
ssh root@$VM "btrfs subvolume delete /mnt/btrfs/@_old"
ssh root@$VM "btrfs subvolume delete /mnt/btrfs/@home_old"
ssh root@$VM "umount /mnt/btrfs"
```

### Snapshot Layout

```text
/.snapshots/
├── baseline/
│   ├── @           # Read-only baseline of root (created at provisioning)
│   └── @home       # Read-only baseline of home (created at provisioning)
└── pc-switcher/
    └── test-*      # Test artifacts (deleted on reset)
```

Active subvolumes (`@` and `@home`) are at the btrfs top level, not under `/.snapshots/`.

### Alternatives Considered
- **Hetzner VM snapshots**: Slow restore (minutes); rejected for fast iteration
- **btrfs set-default approach**: More complex; mv + snapshot is simpler
- **LVM snapshots**: Would require different filesystem; rejected for btrfs consistency

---

## 5. Lock Mechanism

### Decision: File-based lock on pc1 VM at `/tmp/pc-switcher-integration-test.lock`

### Rationale
- Simple implementation matching existing lock module patterns
- Lock file contains holder identity and timestamp for debugging
- `/tmp` location cleared on reboot (automatic cleanup)

### Lock Scope

The lock protects **integration test execution only**, not provisioning:

- **Integration tests**: Must acquire lock before reset and release after tests complete
- **Provisioning**: Protected by CI concurrency groups for CI runs; local concurrent provisioning is explicitly unsupported (documented constraint)
- **VM reset**: Part of test session lifecycle, covered by the test execution lock

This avoids over-complicating the provisioning workflow while ensuring test isolation.

### Lock File Format

```json
{
  "holder": "CI-job-12345",
  "acquired": "2025-12-05T10:30:00Z",
  "hostname": "github-runner-abc"
}
```

### Lock Script

```bash
#!/bin/bash
# tests/infrastructure/scripts/lock.sh

LOCK_FILE="/tmp/pc-switcher-integration-test.lock"
HOLDER="$1"
ACTION="$2"
TIMEOUT=300  # 5 minutes

case "$ACTION" in
  acquire)
    # Try to acquire lock with timeout
    end_time=$(($(date +%s) + TIMEOUT))
    while [ $(date +%s) -lt $end_time ]; do
      if mkdir "$LOCK_FILE.d" 2>/dev/null; then
        echo "{\"holder\": \"$HOLDER\", \"acquired\": \"$(date -Iseconds)\"}" > "$LOCK_FILE"
        echo "Lock acquired by $HOLDER"
        exit 0
      fi
      current_holder=$(cat "$LOCK_FILE" 2>/dev/null | jq -r .holder)
      echo "Lock held by $current_holder, waiting..."
      sleep 10
    done
    echo "ERROR: Failed to acquire lock after ${TIMEOUT}s" >&2
    exit 1
    ;;
  release)
    rm -rf "$LOCK_FILE.d" "$LOCK_FILE"
    echo "Lock released by $HOLDER"
    ;;
esac
```

### Alternatives Considered
- **Database-based lock**: Over-engineering for two users (dev + CI)
- **GitHub Actions artifact lock**: Doesn't prevent local dev conflicts
- **Flock-based lock**: Requires persistent SSH connection
- **Lock for all operations**: Adds complexity to provisioning without significant benefit (provisioning is rare)

---

## 6. GitHub Actions CI/CD

### Decision: Two workflows - ci.yml (unit tests on push) and integration.yml (integration tests on PR + manual)

### Rationale
- Separation allows different concurrency controls
- Unit tests should never be blocked by integration test queue
- Manual trigger enables testing feature branches before PR

### Concurrency Configuration

```yaml
# .github/workflows/integration.yml
concurrency:
  group: pc-switcher-integration
  cancel-in-progress: false  # Queue instead of cancel
```

### Secret Handling
- `HCLOUD_TOKEN`: Hetzner API token (repository secret)
- `HETZNER_SSH_PRIVATE_KEY`: SSH key for VM access (repository secret)
- Secrets not available to forked PRs (FR-017b compliance)

### Workflow Structure

```yaml
# Unit tests: every push
on:
  push:
    branches: ["**"]
  pull_request:

# Integration tests: PRs to main + manual
on:
  pull_request:
    branches: [main]
  workflow_dispatch:
```

### Alternatives Considered
- **Single workflow with conditional jobs**: Harder to reason about concurrency
- **Reusable workflows**: Adds complexity without benefit for single repository

---

## 7. VM Fixtures for Integration Tests

### Decision: Minimal fixtures providing RemoteExecutor-like interface via asyncssh

### Rationale
- FR-034 requires minimal fixtures for VM command execution
- Reuse existing executor patterns from `src/pcswitcher/executor.py`
- Fixtures handle connection lifecycle; tests focus on assertions

### Fixture Design

```python
# tests/integration/conftest.py
import pytest_asyncio
import asyncssh
import os

@pytest_asyncio.fixture
async def pc1_connection():
    """SSH connection to pc1 test VM."""
    host = os.environ["PC_SWITCHER_TEST_PC1_HOST"]
    user = os.environ.get("PC_SWITCHER_TEST_USER", "testuser")

    async with asyncssh.connect(host, username=user, known_hosts=None) as conn:
        yield conn

@pytest_asyncio.fixture
async def pc1_executor(pc1_connection):
    """Executor for running commands on pc1."""
    from pcswitcher.executor import RemoteExecutor
    return RemoteExecutor(pc1_connection)
```

### Alternatives Considered
- **Custom VMExecutor class**: Adds abstraction without benefit; RemoteExecutor sufficient
- **Parameterized fixtures for both VMs**: Complex; simpler to have explicit pc1/pc2 fixtures

---

## References

- [pytest-asyncio documentation](https://pytest-asyncio.readthedocs.io/)
- [OpenTofu Registry - Hetzner Cloud Provider](https://search.opentofu.org/provider/opentofu/hcloud/latest)
- [btrfs-subvolume documentation](https://btrfs.readthedocs.io/en/latest/btrfs-subvolume.html)
- [GitHub Actions concurrency](https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions#concurrency)
- [pytest markers](https://docs.pytest.org/en/stable/how-to/mark.html)
