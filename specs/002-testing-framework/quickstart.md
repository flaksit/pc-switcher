# Testing Framework Quickstart

**Feature**: 002-testing-framework
**Date**: 2025-12-05

## For Developers: Running Unit Tests

Unit tests require no setup and can run on any machine:

```bash
# Run all unit and contract tests
uv run pytest tests/unit tests/contract -v

# Run with coverage
uv run pytest tests/unit tests/contract --cov=src/pcswitcher --cov-report=html

# Run specific test file
uv run pytest tests/unit/test_lock.py -v
```

Unit tests execute in < 30 seconds and use mocked executors.

---

## For Developers: Running Integration Tests

### Prerequisites

1. **Hetzner Cloud Account**: Required for test VM infrastructure
2. **SSH Key**: ed25519 key at `~/.ssh/id_ed25519` (or set `SSH_PUBLIC_KEY`)
3. **hcloud CLI**: Install via `brew install hcloud` or from [releases](https://github.com/hetznercloud/cli/releases)

### First-Time Setup

```bash
# 1. Export Hetzner API token
export HCLOUD_TOKEN="your-api-token"

# 2. Create VMs and provision with btrfs (one-time, ~15 minutes total)
cd tests/infrastructure
./scripts/provision-vms.sh

# 3. Note the VM IPs
export PC_SWITCHER_TEST_PC1_HOST=$(hcloud server ip pc1)
export PC_SWITCHER_TEST_PC2_HOST=$(hcloud server ip pc2)
```

The `provision-vms.sh` script creates VMs if they don't exist, installs Ubuntu with btrfs using Hetzner's installimage, configures the test user, and sets up inter-VM SSH access.

### Running Integration Tests

```bash
# Set environment variables (or add to shell profile)
export PC_SWITCHER_TEST_PC1_HOST="<pc1-ip>"
export PC_SWITCHER_TEST_PC2_HOST="<pc2-ip>"
export PC_SWITCHER_TEST_USER="testuser"

# Run integration tests
uv run pytest -m integration -v

# Run specific integration test
uv run pytest tests/integration/test_sync.py -v -m integration
```

### VM Reset Behavior

VMs are automatically reset to baseline by the `integration_session` pytest fixture before tests run. You do **not** need to manually reset VMs.

**Manual reset** is only needed when:
- VMs are corrupted from an aborted test run
- You want to reset without running the full test suite

```bash
# Manual reset (only if needed)
tests/infrastructure/scripts/reset-vm.sh $PC_SWITCHER_TEST_PC1_HOST
tests/infrastructure/scripts/reset-vm.sh $PC_SWITCHER_TEST_PC2_HOST
```

The reset script restores VMs to their baseline snapshots (`/.snapshots/baseline/@` and `/.snapshots/baseline/@home`) using btrfs snapshot operations. This is much faster than reprovisioning (~20-30 seconds vs ~10 minutes).

---

## For CI/CD: Automated Testing

### GitHub Actions Secrets Required

| Secret | Description |
|--------|-------------|
| `HCLOUD_TOKEN` | Hetzner Cloud API token |
| `HETZNER_SSH_PRIVATE_KEY` | SSH private key for VM access (ed25519 format) |

Note: VM IP addresses are retrieved dynamically via `hcloud server ip` after auto-provisioning. No need to store VM IPs as secrets.

### Workflow Triggers

- **Unit tests**: Every push to any branch
- **Integration tests**: PRs to `main` branch (from main repository only - forks are skipped with notice)
- **Manual integration tests**: Via workflow dispatch on any branch

### Fork PRs

Integration tests are automatically skipped for PRs from forked repositories because GitHub does not expose secrets to forks. Unit tests still run normally. A notice is displayed explaining the skip.

### Viewing Test Results

1. Go to Actions tab in GitHub
2. Select the workflow run
3. Expand the test job to see pytest output
4. Download artifacts for detailed logs (pytest output, provisioning logs)

---

## Troubleshooting

### "Skipping integration tests: VM environment not configured"

Set the required environment variables:
```bash
export PC_SWITCHER_TEST_PC1_HOST="<pc1-ip>"
export PC_SWITCHER_TEST_PC2_HOST="<pc2-ip>"
```

### "Failed to acquire lock after 300s"

Another test run is in progress. Check who holds the lock:
```bash
./tests/infrastructure/scripts/lock.sh "" status
# Or directly via hcloud:
hcloud server describe pc1 -o json | jq '.labels'
```

To force-release a stuck lock:
```bash
hcloud server remove-label pc1 lock_holder
hcloud server remove-label pc1 lock_acquired
```

### "Baseline snapshot missing"

This means `/.snapshots/baseline/@` or `/.snapshots/baseline/@home` doesn't exist. Reprovision the VMs:
```bash
cd tests/infrastructure
./scripts/provision.sh pc1
./scripts/provision.sh pc2
```

### SSH Connection Refused

1. Check VM is running: `hcloud server list`
2. Check SSH key: `ssh -i ~/.ssh/id_ed25519 testuser@<vm-ip>`
3. Check firewall: VMs only allow SSH on port 22

### VMs Not Reachable After Reset

Wait 20-30 seconds after reset for reboot to complete. Then verify:
```bash
ssh testuser@$PC_SWITCHER_TEST_PC1_HOST "hostname"
```

---

## Cost Management

Test VMs cost ~EUR 7/month total when running continuously. To reduce costs:

```bash
# Destroy VMs when not needed
hcloud server delete pc1
hcloud server delete pc2

# VMs will be reprovisioned automatically on next integration test run
```

---

## Next Steps

- See [Testing Developer Guide](../../docs/testing-developer-guide.md) for writing tests
- See [Testing Ops Guide](../../docs/testing-ops-guide.md) for infrastructure management
- See [Testing Framework Architecture](../../docs/testing-framework.md) for design details
