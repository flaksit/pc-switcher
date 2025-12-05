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
4. **OpenTofu**: Install via `brew install opentofu` or from [releases](https://github.com/opentofu/opentofu/releases)

### First-Time Setup

```bash
# 1. Export required environment variables
export HCLOUD_TOKEN="your-api-token"
export STORAGE_BOX_HOST="uXXXXXX.your-storagebox.de"
export STORAGE_BOX_USER="uXXXXXX-sub1"  # Sub-account for pc-switcher

# 2. Provision test VMs (one-time, ~10 minutes)
cd tests/infrastructure
./scripts/tofu-wrapper.sh init
./scripts/tofu-wrapper.sh apply

# 3. Convert to btrfs and configure (one-time, ~5 minutes each)
./scripts/provision.sh pc-switcher-pc1
./scripts/provision.sh pc-switcher-pc2

# 4. Configure /etc/hosts and SSH keys for inter-VM communication
./scripts/configure-hosts.sh

# 5. Note the VM IPs from tofu output
export PC_SWITCHER_TEST_PC1_HOST=$(./scripts/tofu-wrapper.sh output -raw pc1_ip)
export PC_SWITCHER_TEST_PC2_HOST=$(./scripts/tofu-wrapper.sh output -raw pc2_ip)
```

The `tofu-wrapper.sh` script syncs OpenTofu state to/from the Hetzner Storage Box before and after each operation, ensuring CI and developers share the same state.

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

### Resetting VMs Between Test Runs

VMs are automatically reset before CI test runs. For local development:

```bash
cd tests/infrastructure
./scripts/reset-vm.sh pc1  # Reset pc1 to baseline
./scripts/reset-vm.sh pc2  # Reset pc2 to baseline
```

The reset script restores VMs to their baseline snapshots (`/.snapshots/baseline/@` and `/.snapshots/baseline/@home`) using btrfs snapshot operations. This is much faster than reprovisioning (~20-30 seconds vs ~10 minutes).

---

## For CI/CD: Automated Testing

### GitHub Actions Secrets Required

| Secret | Description |
|--------|-------------|
| `HCLOUD_TOKEN` | Hetzner Cloud API token |
| `HETZNER_SSH_PRIVATE_KEY` | SSH private key for VM access (ed25519) |
| `STORAGE_BOX_HOST` | Hetzner Storage Box hostname |
| `STORAGE_BOX_USER` | Storage Box SSH user (sub-account) |
| `STORAGE_BOX_SSH_KEY` | SSH private key for Storage Box access |

### Workflow Triggers

- **Unit tests**: Every push to any branch
- **Integration tests**: PRs to `main` branch only
- **Manual integration tests**: Via workflow dispatch on any branch

### Viewing Test Results

1. Go to Actions tab in GitHub
2. Select the workflow run
3. Expand the test job to see pytest output
4. Download artifacts for detailed logs

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
ssh testuser@$PC_SWITCHER_TEST_PC1_HOST "cat /tmp/pc-switcher-integration-test.lock"
```

To force-release a stuck lock:
```bash
ssh testuser@$PC_SWITCHER_TEST_PC1_HOST "rm -rf /tmp/pc-switcher-integration-test.lock*"
```

### "Baseline snapshot missing"

This means `/.snapshots/baseline/@` or `/.snapshots/baseline/@home` doesn't exist. Reprovision the VMs:
```bash
cd tests/infrastructure
./scripts/provision.sh pc-switcher-pc1
./scripts/provision.sh pc-switcher-pc2
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
cd tests/infrastructure
./scripts/tofu-wrapper.sh destroy

# VMs will be reprovisioned automatically on next integration test run
```

---

## Next Steps

- See [Testing Developer Guide](../../docs/testing-developer-guide.md) for writing tests
- See [Testing Ops Guide](../../docs/testing-ops-guide.md) for infrastructure management
- See [Testing Framework Architecture](../../docs/testing-framework.md) for design details
