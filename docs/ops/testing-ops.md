# Testing Infrastructure Operations

This document provides operational procedures for setting up and maintaining the pc-switcher test infrastructure.

**Audience**: Repository maintainers, DevOps engineers

**Related Documentation**:
- [Testing Architecture](testing-architecture.md) - System design and component overview
- [CI/CD Configuration](ci-setup.md) - GitHub Actions workflow details
- [Testing Developer Guide](../dev/testing-guide.md) - Writing integration tests

## CI Secrets Configuration

### Security Model

GitHub Secrets in public repositories are encrypted and never visible to anyone, including administrators:
- **Pull requests from forks**: Cannot access secrets (prevents exfiltration)
- **Workflow logs**: Automatically mask all secret values
- **Management**: Only repository admins can create, update, or view secret names

**Security Warning**: Avoid `pull_request_target` event (provides secrets to forks). Fork any third-party GitHub Actions and use your own fork.

### Required Secrets

| Secret Name | Purpose | Format |
|-------------|---------|--------|
| `HCLOUD_TOKEN` | Hetzner Cloud API access | API token string |
| `HETZNER_SSH_PRIVATE_KEY` | SSH access to test VMs | ed25519 private key (PEM format) |
| `SSH_AUTHORIZED_KEY_CI` | CI public key for VM access | ed25519 public key |
| `SSH_AUTHORIZED_KEY_*` | Developer public keys (one per developer/machine) | ed25519 public key |

All `SSH_AUTHORIZED_KEY_*` secrets are automatically collected and injected into VMs during provisioning.

### Creating HCLOUD_TOKEN

1. Log in to [Hetzner Cloud Console](https://console.hetzner.cloud/)
2. Navigate to your project (or create one named "pc-switcher-testing")
3. Go to **Security > API Tokens**
4. Click **Generate API Token**
5. Name: `pc-switcher-ci`
6. Permissions: **Read & Write**
7. Copy the token (shown only once)

### Creating SSH Key Secrets

Generate an ed25519 SSH key pair for CI:

```bash
ssh-keygen -t ed25519 -C "pc-switcher-ci@github" -f ~/.ssh/pc-switcher-ci -N ""
```

This creates:
- **Private key**: `~/.ssh/pc-switcher-ci` - add as `HETZNER_SSH_PRIVATE_KEY`
- **Public key**: `~/.ssh/pc-switcher-ci.pub` - add as `SSH_AUTHORIZED_KEY_CI`

Remove local keys after adding as secrets:
```bash
rm ~/.ssh/pc-switcher-ci ~/.ssh/pc-switcher-ci.pub
```

### Adding Secrets to GitHub

1. Navigate to repository > **Settings > Secrets and variables > Actions**
2. Click **New repository secret**
3. Add `HCLOUD_TOKEN`: paste the Hetzner API token
4. Add `HETZNER_SSH_PRIVATE_KEY`: paste the **entire** private key including header/footer:
   ```text
   -----BEGIN OPENSSH PRIVATE KEY-----
   ... (key content) ...
   -----END OPENSSH PRIVATE KEY-----
   ```
5. Add `SSH_AUTHORIZED_KEY_CI`: paste the CI public key (single line starting with `ssh-ed25519`)

### Adding a New Developer

1. Get their public key: `cat ~/.ssh/id_ed25519.pub`
2. Add as GitHub secret: `SSH_AUTHORIZED_KEY_<NAME>` (e.g., `SSH_AUTHORIZED_KEY_JANFR_LAPTOP`)
3. Delete existing VMs to force reprovisioning:
   ```bash
   hcloud server delete pc1
   hcloud server delete pc2
   ```
4. Trigger CI workflow: `gh workflow run integration-tests.yml`

No workflow file changes needed - secrets are enumerated dynamically.

### Optional: GITHUB_TOKEN for Rate Limiting

Without authentication, GitHub API allows 60 requests/hour per IP. With `GITHUB_TOKEN`, this increases to 5,000 requests/hour.

Create a PAT with **no special permissions** (public repo access is sufficient) and add as repository secret if rate limit errors occur.

## VM Provisioning and Management

### Provisioning Model

**Important**: VM provisioning can only be performed by GitHub CI to ensure all authorized SSH keys are properly configured.

- **First-time setup**: Trigger integration test workflow
- **Reprovisioning**: Delete VMs via hcloud CLI, then trigger CI
- **Adding developers**: Add their public key as a secret, delete VMs, trigger CI

### Installing hcloud CLI

**Ubuntu/Debian**:
```bash
wget https://github.com/hetznercloud/cli/releases/latest/download/hcloud-linux-amd64.tar.gz
tar xzf hcloud-linux-amd64.tar.gz
sudo mv hcloud /usr/local/bin/
sudo chmod +x /usr/local/bin/hcloud
hcloud version
```

**macOS (Homebrew)**:
```bash
brew install hcloud
```

**Configuration**:
```bash
hcloud context create pc-switcher-testing
# Paste HCLOUD_TOKEN when prompted
```

### VM Management Commands

```bash
# List VMs and their IPs
hcloud server list

# Get specific VM IP
hcloud server ip pc1

# Power on/off
hcloud server poweron pc1
hcloud server poweroff pc1

# Reboot
hcloud server reboot pc1

# Hard reset (use when VM is unresponsive)
hcloud server reset pc1

# Destroy VMs
hcloud server delete pc1
hcloud server delete pc2
```

### Triggering Reprovisioning

After deleting VMs:
```bash
gh workflow run integration-tests.yml
```

Or push a non-draft PR targeting main.

## Cost Monitoring

### Expected Monthly Costs

| Resource | Quantity | Unit Cost | Total |
|----------|----------|-----------|-------|
| CX23 VMs | 2 | EUR 3.50/month | EUR 7.00/month |
| Traffic | Included | - | EUR 0.00 |
| **Total** | | | **EUR 7.00/month** |

Hetzner bills hourly (EUR 0.005/hour per CX23 VM).

### Viewing Current Costs

1. Log in to [Hetzner Cloud Console](https://console.hetzner.cloud/)
2. Select project "pc-switcher-testing"
3. Go to **Billing** section

### When to Destroy VMs

Consider destroying VMs to save costs when:
- Repository inactive for >2 weeks
- Budget constraints
- Major infrastructure changes planned

VMs are **not** destroyed between test runs. Btrfs snapshot reset provides fast cleanup.

### When to Reprovision VMs

Reprovisioning is needed when:
- VMs were manually destroyed
- Baseline snapshots are corrupted
- OS upgrade required (e.g., Ubuntu 26.04 LTS)
- Infrastructure configuration changes (e.g., different VM size)

## Lock Management

The test infrastructure uses Hetzner server labels for lock-based concurrency control, preventing multiple test runs from interfering with each other.

### Checking Lock Status

```bash
hcloud server describe pc1 -o json | jq '.labels'
```

Look for `lock_holder` and `lock_acquired` labels.

### Releasing Stuck Locks

If a CI job crashed or a developer session terminated without cleanup:

```bash
# Remove lock labels
hcloud server remove-label pc1 lock_holder
hcloud server remove-label pc1 lock_acquired

# Verify
hcloud server describe pc1 -o json | jq '.labels'
```

### Lock Operations (Manual)

```bash
# Acquire lock
./tests/integration/scripts/internal/lock.sh acquire <holder-id>

# Release lock
./tests/integration/scripts/internal/lock.sh release <holder-id>
```

## Runbooks

### Runbook: Failed Provisioning Recovery

**Symptoms**:
- CI workflow exits with provisioning error
- VMs partially created but not configured
- Baseline snapshots missing

**Steps**:

1. Check VM existence:
   ```bash
   hcloud server list
   ```

2. Check provisioning logs in GitHub Actions workflow artifacts

3. Delete partially provisioned VMs:
   ```bash
   hcloud server delete pc1
   hcloud server delete pc2
   ```

4. Trigger CI workflow to reprovision:
   ```bash
   gh workflow run integration-tests.yml
   ```

5. If provisioning fails again, verify:
   - `HCLOUD_TOKEN` secret is valid
   - `SSH_AUTHORIZED_KEY_*` secrets are configured
   - [Hetzner Cloud status page](https://status.hetzner.com/) for service issues

### Runbook: Stuck Lock Cleanup

**Symptoms**:
- Integration tests fail with "Failed to acquire lock"
- Lock holder shown is stale (old CI job or terminated session)

**Steps**:

1. Check lock status:
   ```bash
   hcloud server describe pc1 -o json | jq '.labels'
   ```

2. Verify lock holder is no longer active (check CI runs, ask developers)

3. Release stuck lock:
   ```bash
   hcloud server remove-label pc1 lock_holder
   hcloud server remove-label pc1 lock_acquired
   ```

4. Verify lock cleared:
   ```bash
   hcloud server describe pc1 -o json | jq '.labels'
   ```

5. Retry integration tests

### Runbook: Baseline Snapshot Corruption

**Symptoms**:
- VM reset fails with "Baseline snapshot missing"
- Btrfs errors during snapshot operations
- Integration tests fail immediately after reset

**Steps**:

1. SSH to VM:
   ```bash
   ssh testuser@<vm-ip>
   ```

2. Check baseline snapshots:
   ```bash
   sudo btrfs subvolume list / | grep baseline
   ```

   Expected output:
   ```text
   ID XXX gen XXX top level XXX path .snapshots/baseline/@
   ID XXX gen XXX top level XXX path .snapshots/baseline/@home
   ```

3. Check snapshot properties:
   ```bash
   sudo btrfs property get /.snapshots/baseline/@
   ```
   Should show `ro=true` (read-only)

4. If snapshots are missing or corrupted, reprovision:
   ```bash
   hcloud server delete pc1
   hcloud server delete pc2
   gh workflow run integration-tests.yml
   ```

### Runbook: VM Unreachable

**Symptoms**:
- SSH connection timeout
- "Connection refused" or "No route to host" errors
- VM reset script hangs

**Steps**:

1. Check VM status:
   ```bash
   hcloud server list
   ```
   Status should be "running"

2. Ping VM:
   ```bash
   ping <vm-ip>
   ```

3. Try verbose SSH:
   ```bash
   ssh -v testuser@<vm-ip>
   ```

**Recovery options**:

| Situation | Action |
|-----------|--------|
| VM is powered off | `hcloud server poweron pc1` |
| VM running but SSH unreachable | `hcloud server reboot pc1` |
| VM completely unresponsive | `hcloud server reset pc1` |
| Network configuration issue | Check `/etc/hosts` entries, re-run `configure-hosts.sh` |
| All else fails | Delete and reprovision |

### Runbook: CI Workflow Failures

**"Skipping integration tests: secrets not available"**
- Verify `HCLOUD_TOKEN` and `HETZNER_SSH_PRIVATE_KEY` are added to repository secrets
- For forked PRs: This is expected behavior (secrets unavailable)

**"VMs don't exist and provisioning is only allowed from GitHub CI"**
- Ensure all secrets are configured
- Trigger CI workflow: `gh workflow run integration-tests.yml`

**"Timeout waiting for VM to reboot after reset"**
- Check VM status in Hetzner Console
- Manually reboot: `hcloud server reboot pc1 pc2`
- Re-run CI workflow

**"Permission denied (publickey)"**
- Add your SSH public key as `SSH_AUTHORIZED_KEY_*` secret
- Delete VMs and reprovision:
  ```bash
  hcloud server delete pc1 pc2
  gh workflow run integration-tests.yml
  ```

**"Failed to acquire lock after 5 minutes"**
- Another test run is in progress (wait for completion)
- Lock is stuck (see "Stuck Lock Cleanup" runbook)

## Troubleshooting Quick Reference

| Problem | Quick Fix |
|---------|-----------|
| Provisioning fails | Delete VMs, trigger CI: `gh workflow run integration-tests.yml` |
| Lock stuck | `hcloud server remove-label pc1 lock_holder lock_acquired` |
| VM unreachable | `hcloud server reboot pc1` |
| Baseline corrupt | Delete VMs, reprovision |
| CI secrets missing | Add to Settings > Secrets and variables > Actions |
| SSH permission denied | Add public key as `SSH_AUTHORIZED_KEY_*` secret, reprovision |
| Reset timeout | Check VM status, manually reboot if needed |
| Integration tests skip | Check environment variables or CI secrets |
| GitHub API rate limit | Set `GITHUB_TOKEN` secret |

## Environment Variables

### Required for Integration Tests

| Variable | Description | Default |
|----------|-------------|---------|
| `PC_SWITCHER_TEST_PC1_HOST` | PC1 VM IP address or hostname | - |
| `PC_SWITCHER_TEST_PC2_HOST` | PC2 VM IP address or hostname | - |
| `PC_SWITCHER_TEST_USER` | SSH user on VMs | `testuser` |

### Setting for Local Runs

```bash
# Get VM IPs
hcloud server list

# Export environment
export PC_SWITCHER_TEST_PC1_HOST="<pc1-ip>"
export PC_SWITCHER_TEST_PC2_HOST="<pc2-ip>"
export PC_SWITCHER_TEST_USER="testuser"

# Run tests
./tests/local-pytest.sh tests/integration
```

## Maintenance Schedule

| Frequency | Task |
|-----------|------|
| **Daily** | Monitor GitHub Actions workflow runs (automated VM updates run at 2am UTC) |
| **Weekly** | Review Hetzner Cloud costs; check for stuck locks if tests failing |
| **Monthly** | Validate VM baseline snapshots are healthy; update hcloud CLI if needed |
| **As Needed** | Reprovision VMs after major changes; update documentation |

## Script Reference

All infrastructure scripts are in `tests/integration/scripts/`:

| Script | Purpose |
|--------|---------|
| `provision-test-infra.sh` | Orchestrator (calls all other scripts) |
| `reset-vm.sh` | Resets a VM to baseline via snapshot rollback |
| `upgrade-vms.sh` | Upgrades VMs and updates baseline snapshots |

Internal scripts (`tests/integration/scripts/internal/`):

| Script | Purpose |
|--------|---------|
| `create-vm.sh` | Creates a single VM via hcloud CLI |
| `configure-vm.sh` | Configures a single VM (user, SSH, services) |
| `configure-hosts.sh` | Sets up inter-VM networking and SSH keys |
| `create-baseline-snapshots.sh` | Creates baseline btrfs snapshots |
| `lock.sh` | Lock operations (acquire/release) |

## Security Considerations

### SSH Key Management
- **Never commit private keys to git**
- Use separate SSH keys for CI vs personal access
- Rotate keys periodically (recommended: annually)
- Revoke keys immediately if compromised

### API Token Security
- Store `HCLOUD_TOKEN` only in GitHub Secrets (never in code)
- Use separate API tokens for different purposes
- Rotate tokens if exposed

### VM Access
- VMs accessible only via SSH (no password authentication)
- Test VMs should not contain sensitive data
- Consider Hetzner Cloud Firewall to restrict SSH access to known IPs

## External Resources

- [Hetzner Cloud Console](https://console.hetzner.cloud/)
- [Hetzner Cloud Status](https://status.hetzner.com/)
- [Hetzner Support](https://docs.hetzner.com/general/others/support/)
- [GitHub Actions Status](https://www.githubstatus.com/)
