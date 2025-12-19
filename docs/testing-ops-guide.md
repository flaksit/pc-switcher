# PC-Switcher Test Infrastructure Operational Guide

This guide provides operational procedures for configuring and maintaining the pc-switcher test infrastructure.

**Audience**: DevOps engineers, sysadmins, repository maintainers

**Related Documentation**:
- [Testing Framework Architecture](testing-framework.md) - Design and architecture overview
- [Testing Infrastructure](testing-infrastructure.md) - VM provisioning flow and scripts
- [Testing Developer Guide](testing-developer-guide.md) - Writing integration tests
- [ADR-006: Testing Framework](adr/adr-006-testing-framework.md) - Architectural decision

---

## Overview

### Infrastructure Components

PC-switcher uses a persistent VM-based test infrastructure for integration testing:

- **Two Hetzner Cloud VMs** (pc1, pc2) running Ubuntu 24.04 LTS with btrfs
- **Btrfs baseline snapshots** for fast VM reset between test runs
- **Hetzner Server Labels** for lock-based concurrency control
- **GitHub Actions** for CI/CD automation

### Cost Expectations

With the specified configuration (CX23 VMs), expected costs are:

| Component | Cost |
|-----------|------|
| pc1 VM (CX23) | ~EUR 3.50/month |
| pc2 VM (CX23) | ~EUR 3.50/month |
| **Total** | **~EUR 7/month** |

VMs are expected to run persistently. Reset is performed via btrfs snapshot rollback (not VM reprovisioning). Manual destruction is acceptable during extended downtime to save costs.

---

## CI Workflow Configuration

### Workflow Overview

The repository uses three GitHub Actions workflows for testing:

| Workflow | File | Triggers | Purpose |
|----------|------|----------|---------|
| CI | `ci.yml` | Every push | Lint (basedpyright, ruff, codespell) and unit tests |
| Integration Tests | `integration-tests.yml` | PR ready for review | Full integration tests on Hetzner VMs |
| VM Maintenance | `vm-maintenance.yml` | Weekly (Monday 2am UTC) | Keep test VMs updated with OS patches |

### Integration Tests Trigger Strategy

Integration tests are expensive (they provision/use cloud VMs), so they don't run on every commit. Instead:

| PR State | Integration Tests |
|----------|-------------------|
| Draft PR | **Skipped** |
| Marked "Ready for review" | **Runs** |
| New commits to ready PR | **Runs** |
| Manual trigger | **Runs** (via `workflow_dispatch`) |

This is achieved via:

```yaml
# integration-tests.yml
on:
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened, ready_for_review]
  workflow_dispatch:

jobs:
  integration:
    if: github.event.pull_request.draft == false
    # ...
```

**Rationale**: Developers iterate on draft PRs without triggering expensive integration tests. When ready for review, tests run automatically. Subsequent commits to ready PRs also trigger tests.

### Path Filtering

Integration tests only run when relevant files change:

```yaml
paths:
  - '.github/workflows/integration-tests.yml'
  - 'src/**'
  - 'tests/integration/**'
  - 'install.sh'
  - 'pyproject.toml'
  - 'uv.lock'
```

Documentation-only changes skip integration tests.

### Branch Protection Configuration

The `main` branch requires these status checks before merging:

| Check | Workflow | Required |
|-------|----------|----------|
| Lint | CI | Yes |
| Unit Tests | CI | Yes |
| Integration Tests | Integration Tests | Yes |

**Configuration steps** (Settings > Branches > main):

1. Enable "Require status checks to pass before merging"
2. Enable "Require branches to be up to date before merging" (recommended)
3. Add required checks: `Lint`, `Unit Tests`, `Integration Tests`

**Note**: The merge queue feature is **not used**. Integration tests run directly on PR branches, gated by the draft/ready status.

### VM Maintenance Workflow

The `vm-maintenance.yml` workflow keeps test VMs updated with OS security patches:

- **Schedule**: Runs weekly on Monday at 2am UTC
- **Manual trigger**: Available via `workflow_dispatch`
- **Behavior**:
  - Checks if VMs exist before attempting upgrade
  - Runs `tests/integration/scripts/upgrade-vms.sh` on both VMs
  - Skips gracefully if VMs don't exist (with warning)

This ensures VMs stay patched without manual intervention. The baseline snapshots are **not** updated by this workflow - they preserve the original provisioned state for consistent test resets.

---

## CI Secrets Configuration

### About security of secrets in public GitHub repositories

#### Who Can See Secrets

GitHub Secrets in public repositories are **never visible to anyone** - not even repository administrators or collaborators. The secret values themselves are encrypted and hidden from all users through the GitHub interface and API.

#### Access During Workflow Execution

While secret values remain hidden, they can be accessed during GitHub Actions workflow execution with specific restrictions for public repositories:

- **Repository collaborators**: Can create and use secrets in workflows they author
- **Pull requests from forks**: Do **not** have access to secrets (except the read-only `GITHUB_TOKEN`), preventing attackers from exfiltrating secrets via malicious pull requests
- **Workflow logs**: Automatically mask all secret values, preventing accidental exposure in build output

#### Management Permissions

Only users with **admin access** to a repository can create, update, or view the list of secret names (though not their values) in the Settings interface. For organization-level secrets, admin access at the organization level is required.

#### Security Considerations

When using secrets in public repositories, be cautious about the `pull_request_target` event, which does provide access to secrets for fork pull requests. Additionally, avoid using third-party GitHub Actions directly by their tags or branches - fork them and use your own fork to prevent modified actions from capturing secrets.

### Required Secrets

GitHub repository requires these secrets for integration test automation:

| Secret Name | Purpose | Format |
|-------------|---------|--------|
| `HCLOUD_TOKEN` | Hetzner Cloud API access | API token string |
| `HETZNER_SSH_PRIVATE_KEY` | SSH access to test VMs | ed25519 private key (PEM format) |
| `SSH_AUTHORIZED_KEY_CI` | CI public key for VM access | ed25519 public key |
| `SSH_AUTHORIZED_KEY_*` | Developer public keys (one per developer/machine) | ed25519 public key |

**Note**: All `SSH_AUTHORIZED_KEY_*` secrets are automatically collected and injected into VMs during provisioning. This allows both CI and authorized developers to access the VMs.

### Creating HCLOUD_TOKEN

1. Log in to [Hetzner Cloud Console](https://console.hetzner.cloud/)
2. Navigate to your project (or create one named "pc-switcher-testing")
3. Go to Security > API Tokens
4. Click "Generate API Token"
5. Name: `pc-switcher-ci` (or similar)
6. Permissions: **Read & Write**
7. Copy the token (shown only once)

### Creating SSH Key Secrets

Generate an ed25519 SSH key pair specifically for CI:

```bash
ssh-keygen -t ed25519 -C "pc-switcher-ci@github" -f ~/.ssh/pc-switcher-ci -N ""
```

This creates:
- **Private key**: `~/.ssh/pc-switcher-ci` → add as `HETZNER_SSH_PRIVATE_KEY` secret
- **Public key**: `~/.ssh/pc-switcher-ci.pub` → add as `SSH_AUTHORIZED_KEY_CI` secret

**Format requirements**:
- Key type: ed25519 (required)
- Format: PEM (default for OpenSSH)
- No passphrase (CI cannot handle interactive prompts)

Remove the keys from your local machine after adding them as secrets:
```bash
rm ~/.ssh/pc-switcher-ci ~/.ssh/pc-switcher-ci.pub
```

### Adding Developer SSH Keys

For each developer who needs VM access, add their public key as a secret:

1. Get the developer's public key:
   ```bash
   cat ~/.ssh/id_ed25519.pub
   ```

2. Add as GitHub secret with name `SSH_AUTHORIZED_KEY_<NAME>`:
   - Example: `SSH_AUTHORIZED_KEY_JANFR_LAPTOP`
   - Example: `SSH_AUTHORIZED_KEY_JANFR_WORKSTATION`

The provisioning script automatically collects all `SSH_AUTHORIZED_KEY_*` secrets and injects them into the VMs' `authorized_keys` file.

### Adding Secrets to GitHub Repository

1. Navigate to repository on GitHub
2. Go to Settings > Secrets and variables > Actions
3. Click "New repository secret"
4. Add `HCLOUD_TOKEN`:
   - Name: `HCLOUD_TOKEN`
   - Value: Paste the Hetzner API token
5. Add `HETZNER_SSH_PRIVATE_KEY`:
   - Name: `HETZNER_SSH_PRIVATE_KEY`
   - Value: Paste the **entire private key file** including header/footer:
     ```text
     -----BEGIN OPENSSH PRIVATE KEY-----
     ... (key content) ...
     -----END OPENSSH PRIVATE KEY-----
     ```
6. Add `SSH_AUTHORIZED_KEY_CI`:
   - Name: `SSH_AUTHORIZED_KEY_CI`
   - Value: Paste the CI public key (single line starting with `ssh-ed25519`)
7. Add developer public keys:
   - Name: `SSH_AUTHORIZED_KEY_<DEVELOPER_NAME>`
   - Value: Developer's public key (single line starting with `ssh-ed25519`)

**Verification**: After adding secrets, trigger the integration test workflow. VMs will be provisioned with all authorized keys.

### Adding a New Developer

To grant VM access to a new developer:

1. Get their public key: `cat ~/.ssh/id_ed25519.pub`
2. Add as GitHub secret: `SSH_AUTHORIZED_KEY_<NAME>`
3. Delete existing VMs to force reprovisioning:
   ```bash
   hcloud server delete pc1
   hcloud server delete pc2
   ```
4. Trigger CI workflow: `gh workflow run test.yml`

No workflow file changes are needed - secrets are enumerated dynamically.

---

## VM Provisioning

**Important**: VM provisioning can only be performed by GitHub CI. This ensures all authorized SSH keys are properly configured from secrets. Local provisioning is blocked.

### CI-Only Provisioning Model

VMs are provisioned exclusively by GitHub Actions CI:

1. **First-time setup**: Trigger the integration test workflow manually or via a PR
2. **Reprovisioning**: Delete VMs via hcloud CLI, then trigger CI
3. **Adding developers**: Add their public key as a secret, delete VMs, trigger CI

Local developers use the VMs via SSH but cannot provision them. If VMs don't exist when running local tests, you'll see a clear error with instructions to trigger CI.

### Prerequisites (for managing infrastructure)

1. Hetzner Cloud account with API token
2. `hcloud` CLI installed locally (for VM management, not provisioning)

### Installing hcloud CLI

**Ubuntu/Debian**:
```bash
# Download latest release
wget https://github.com/hetznercloud/cli/releases/download/v1.42.0/hcloud-linux-amd64.tar.gz

# Extract and install
tar xzf hcloud-linux-amd64.tar.gz
sudo mv hcloud /usr/local/bin/
sudo chmod +x /usr/local/bin/hcloud

# Verify installation
hcloud version
```

**macOS (Homebrew)**:
```bash
brew install hcloud
```

**Configuration**:
```bash
# Set up Hetzner Cloud context
hcloud context create pc-switcher-testing

# Paste your HCLOUD_TOKEN when prompted
```

### Creating Hetzner Cloud Project

If you don't have a project yet:

1. Log in to [Hetzner Cloud Console](https://console.hetzner.cloud/)
2. Click "New Project"
3. Name: `pc-switcher-testing`
4. Create API token (see "CI Secrets Configuration" section above)

### SSH Key Setup

SSH keys are managed via GitHub Secrets (see "CI Secrets Configuration" section above).

All `SSH_AUTHORIZED_KEY_*` secrets are:
1. Collected by the CI workflow during provisioning
2. Injected into `testuser` account on both VMs
3. Included in the baseline snapshot for persistent access

### How Provisioning Works

Provisioning is triggered automatically by the CI workflow when VMs don't exist. The script:

1. Creates pc1 and pc2 VMs (CX23, Ubuntu 24.04, location: fsn1)
2. Runs OS installation with btrfs on each VM via Hetzner's `installimage`
3. Configures VMs:
   - Creates `testuser` account with sudo access
   - Injects all `SSH_AUTHORIZED_KEY_*` secrets for access
   - Sets up `/etc/hosts` entries for inter-VM communication
   - Generates and exchanges SSH keys for pc1↔pc2 communication
4. Creates baseline btrfs snapshots (`/.snapshots/baseline/@` and `/.snapshots/baseline/@home`)

**Duration**: Approximately 10-15 minutes for full provisioning.

**Idempotency**: The script skips provisioning if VMs already exist and are configured.

**Triggering provisioning**:
```bash
# Via GitHub CLI
gh workflow run test.yml

# Or push a PR to main branch
```

---

## Environment Variables

### Required for Integration Tests

These environment variables enable integration tests to connect to VMs:

| Variable | Description | Default |
|----------|-------------|---------|
| `PC_SWITCHER_TEST_PC1_HOST` | PC1 VM IP address or hostname | - |
| `PC_SWITCHER_TEST_PC2_HOST` | PC2 VM IP address or hostname | - |
| `PC_SWITCHER_TEST_USER` | SSH user on VMs | `testuser` |

### Optional: GitHub API Rate Limiting

| Variable | Description | Default |
|----------|-------------|---------|
| `GITHUB_TOKEN` | GitHub Personal Access Token for API calls | - (unauthenticated) |

**Why set GITHUB_TOKEN?**

The self-update feature queries the GitHub API to fetch release information. Without authentication:
- Rate limit: 60 requests/hour per IP address
- Integration tests running on shared VMs can exhaust this limit

With `GITHUB_TOKEN` set:
- Rate limit: 5,000 requests/hour
- Eliminates rate limit failures in integration tests

**Creating a token:**

1. Go to GitHub Settings > Developer settings > Personal access tokens > Tokens (classic)
2. Generate new token with **no special permissions** (public repo access is sufficient)
3. Copy the token and add as `GITHUB_TOKEN` environment variable or CI secret

**For CI:** Add `GITHUB_TOKEN` as a repository secret. GitHub Actions automatically provides `secrets.GITHUB_TOKEN` for workflows, but a separate PAT may be needed for higher rate limits.

**Setting for local runs**:

```bash
export PC_SWITCHER_TEST_PC1_HOST="<pc1-ip-address>"
export PC_SWITCHER_TEST_PC2_HOST="<pc2-ip-address>"
export PC_SWITCHER_TEST_USER="testuser"
```

**Finding VM IP addresses**:

```bash
hcloud server list
```

Output shows IP addresses for pc1 and pc2.

### Required for Infrastructure Management

| Variable | Description | Default |
|----------|-------------|---------|
| `HCLOUD_TOKEN` | Hetzner Cloud API token | - |

**Note**: `SSH_PUBLIC_KEY` is no longer used. SSH keys are now managed via GitHub Secrets (`SSH_AUTHORIZED_KEY_*`).

### CI-Specific Variables

These are automatically provided by GitHub Actions:

| Variable | Description | Default |
|----------|-------------|---------|
| `CI_JOB_ID` | CI job identifier for lock | `$USER` |
| `CI` | Indicates CI environment | - |

---

## Cost Monitoring

### Viewing Current Costs

**Hetzner Cloud Console**:

1. Log in to [Hetzner Cloud Console](https://console.hetzner.cloud/)
2. Select your project (e.g., "pc-switcher-testing")
3. Go to Billing section
4. View current month costs and cost breakdown

**CLI**:

```bash
# List all servers with their types and costs
hcloud server list

# View project info (includes resource counts)
hcloud context list
```

### Expected Monthly Cost Breakdown

| Resource | Quantity | Unit Cost | Total |
|----------|----------|-----------|-------|
| CX23 VMs | 2 | EUR 3.50/month | EUR 7.00/month |
| Traffic | Included | Included | EUR 0.00/month |
| **Total** | | | **EUR 7.00/month** |

**Note**: Hetzner bills by the hour (EUR 0.005/hour per CX23 VM). Monthly estimates assume 100% uptime.

### When to Destroy VMs

Consider destroying VMs in these scenarios:

- **Extended inactivity**: Repository inactive for >2 weeks
- **Budget constraints**: Need to reduce costs temporarily
- **Major infrastructure changes**: Switching cloud provider or VM configuration

**VMs are NOT destroyed** between test runs. Btrfs snapshot reset provides fast cleanup without reprovisioning.

### When to Reprovision VMs

Reprovisioning is needed when:

- **VMs were manually destroyed** for cost savings
- **Baseline snapshots are corrupted** beyond repair
- **OS upgrade required** (e.g., Ubuntu 26.04 LTS)
- **Infrastructure configuration changes** (e.g., different VM size)

### Destroying VMs (Manual)

```bash
# Destroy both VMs
hcloud server delete pc1
hcloud server delete pc2

# Verify deletion
hcloud server list
```

**Warning**: This is destructive. Baseline snapshots are lost. Re-provisioning is required before integration tests can run again.

### Reprovisioning After Destruction

Trigger the CI workflow to reprovision:

```bash
gh workflow run test.yml
```

The CI workflow detects missing VMs and provisions them from scratch (including OS installation and baseline snapshot creation).

---

## Runbooks

### Failed Provisioning Recovery

**Symptoms**:
- `provision-test-infra.sh` exits with error
- VMs partially created but not configured
- Baseline snapshots missing

**Diagnosis**:

1. Check VM existence:
   ```bash
   hcloud server list
   ```

2. Check provisioning logs (if running in CI):
   - Download artifacts from GitHub Actions workflow run
   - Review `provisioning.log`

3. Check SSH access:
   ```bash
   ssh testuser@<vm-ip>
   ```

**Recovery**:

1. Delete partially provisioned VMs:
   ```bash
   hcloud server delete pc1
   hcloud server delete pc2
   ```

2. Trigger CI workflow to reprovision:
   ```bash
   gh workflow run test.yml
   ```

3. If provisioning fails again:
   - Verify `HCLOUD_TOKEN` secret is valid
   - Verify `SSH_AUTHORIZED_KEY_*` secrets are configured
   - Check Hetzner Cloud status page for service issues
   - Review CI workflow logs in GitHub Actions

---

### Stuck Lock Cleanup

**Symptoms**:
- Integration tests fail with "Failed to acquire lock" error
- Lock holder shown is stale (old CI job or developer session)

**Diagnosis**:

1. Check lock status:
   ```bash
   hcloud server describe pc1 -o json | jq '.labels'
   ```

   Look for `lock_holder` and `lock_acquired` labels.

2. Determine if lock is stuck:
   - Check if CI job or developer session is still running
   - If holder is no longer active, lock is stuck

**Recovery**:

1. Manually release stuck lock:
   ```bash
   hcloud server remove-label pc1 lock_holder
   hcloud server remove-label pc1 lock_acquired
   ```

2. Verify lock cleared:
   ```bash
   hcloud server describe pc1 -o json | jq '.labels'
   ```

   Labels should be empty or not include lock fields.

3. Retry integration tests

**Prevention**:
- Ensure test fixtures properly release locks in cleanup
- Monitor for test crashes that skip cleanup
- Consider implementing lock timeout/expiration (future enhancement)

---

### Baseline Snapshot Corruption

**Symptoms**:
- VM reset fails with "Baseline snapshot missing" error
- Btrfs errors when attempting snapshot operations
- Integration tests fail immediately after reset

**Diagnosis**:

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

**Recovery**:

If baseline snapshots are missing or corrupted:

1. Destroy VMs:
   ```bash
   hcloud server delete pc1
   hcloud server delete pc2
   ```

2. Trigger CI to reprovision:
   ```bash
   gh workflow run test.yml
   ```

This recreates VMs with fresh baseline snapshots.

**Alternative (Advanced)**: Manually recreate baseline snapshots on running VMs:

1. SSH to VM
2. Run baseline snapshot creation script:
   ```bash
   # This script should exist on the VM from initial provisioning
   sudo /opt/pc-switcher-testing/create-baseline-snapshots.sh
   ```

3. Repeat for second VM

---

### VM Unreachable Scenarios

**Symptoms**:
- SSH connection timeout when accessing VMs
- Integration tests fail with "Connection refused" or "No route to host"
- VM reset script hangs

**Diagnosis**:

1. Check VM status:
   ```bash
   hcloud server list
   ```

   Status should be "running"

2. Ping VM:
   ```bash
   ping <vm-ip>
   ```

3. Check SSH service:
   ```bash
   ssh -v testuser@<vm-ip>
   ```

   Look for connection details in verbose output

**Recovery Options**:

#### Option 1: VM is powered off

```bash
hcloud server poweron pc1
hcloud server poweron pc2
```

#### Option 2: VM is running but SSH unreachable

1. Check firewall rules in Hetzner Cloud Console
2. Verify VM has public IP assigned
3. Reboot VM via Hetzner Console or CLI:
   ```bash
   hcloud server reboot pc1
   ```

#### Option 3: VM is completely unresponsive

1. Hard reset via Hetzner Console or CLI:
   ```bash
   hcloud server reset pc1
   ```

2. If hard reset fails, destroy and reprovision:
   ```bash
   hcloud server delete pc1 pc2
   gh workflow run test.yml
   ```

#### Option 4: Network configuration issue

- Verify `/etc/hosts` entries on VMs for inter-VM communication
- Check SSH keys for pc1↔pc2 communication
- Re-run configuration script:
  ```bash
  cd tests/infrastructure
  ./scripts/configure-hosts.sh
  ```

---

### CI Workflow Failures

**Symptoms**:
- GitHub Actions integration test job fails
- Error messages mention secrets, VMs, or SSH

**Common Scenarios**:

#### Secrets Not Configured

**Error**: "Skipping integration tests: secrets not available"

**Resolution**:
- Verify `HCLOUD_TOKEN` and `HETZNER_SSH_PRIVATE_KEY` are added to repository secrets
- Verify secret names match exactly (case-sensitive)
- For forked PRs: This is expected behavior (secrets unavailable to forks)

#### VMs Not Provisioned

**Error**: "VMs don't exist and provisioning is only allowed from GitHub CI"

**Resolution**:
1. Ensure all secrets are configured (`HCLOUD_TOKEN`, `HETZNER_SSH_PRIVATE_KEY`, `SSH_AUTHORIZED_KEY_*`)
2. Trigger the CI workflow: `gh workflow run test.yml`

#### VM Reset Timeout

**Error**: "Timeout waiting for VM to reboot after reset"

**Resolution**:
1. Check VM status in Hetzner Console
2. VMs may be overloaded or stuck
3. Manually reboot VMs:
   ```bash
   hcloud server reboot pc1
   hcloud server reboot pc2
   ```
4. Re-run CI workflow

#### SSH Connection Failures

**Error**: "Permission denied (publickey)" or "Connection timeout"

**Resolution**:
1. Verify your SSH public key is added as a `SSH_AUTHORIZED_KEY_*` secret
2. Delete VMs and reprovision to apply new keys:
   ```bash
   hcloud server delete pc1 pc2
   gh workflow run test.yml
   ```
3. Check VM firewall rules

#### Lock Acquisition Timeout

**Error**: "Failed to acquire lock after 5 minutes"

**Resolution**:
- Another test run is in progress (wait for completion)
- Lock is stuck (see "Stuck Lock Cleanup" runbook above)

---

## Maintenance Schedule

### Daily
- Monitor GitHub Actions workflow runs for failures

### Weekly
- Review Hetzner Cloud costs
- Check for stuck locks (if integration tests are failing frequently)

### Monthly
- Review and validate VM baseline snapshots are healthy
- Update `hcloud` CLI if new version available

### As Needed
- Update OS (when Ubuntu 24.04 LTS has security patches)
- Reprovision VMs (after major infrastructure changes)
- Update documentation (when procedures change)

---

## Emergency Contacts

- **Hetzner Cloud Support**: [https://docs.hetzner.com/general/others/support/](https://docs.hetzner.com/general/others/support/)
- **GitHub Actions Status**: [https://www.githubstatus.com/](https://www.githubstatus.com/)
- **Repository Maintainers**: See CODEOWNERS file

---

## Appendix: Provisioning Script Reference

### Script Locations

All infrastructure scripts are in `tests/integration/scripts/`:

| Script | Purpose |
|--------|---------|
| `provision-test-infra.sh` | Orchestrator (calls all other scripts) |
| `create-vm.sh` | Creates a single VM via hcloud CLI |
| `configure-vm.sh` | Configures a single VM (user, SSH, services) |
| `configure-hosts.sh` | Sets up inter-VM networking and SSH keys |
| `create-baseline-snapshots.sh` | Creates baseline btrfs snapshots |
| `reset-vm.sh` | Resets a VM to baseline via snapshot rollback |
| `lock.sh` | Lock operations (acquire/release) |

### Manual Script Usage

**Create single VM**:
```bash
./scripts/internal/create-vm.sh pc1
```

**Configure single VM**:
```bash
./scripts/internal/configure-vm.sh pc1 <vm-ip>
```

**Setup inter-VM networking**:
```bash
./scripts/internal/configure-hosts.sh <pc1-ip> <pc2-ip>
```

**Create baseline snapshots**:
```bash
./scripts/internal/create-baseline-snapshots.sh <pc1-ip> <pc2-ip>
```

**Reset VM**:
```bash
./scripts/reset-vm.sh pc1
```

**Lock operations**:
```bash
./scripts/internal/lock.sh acquire <holder-id>
./scripts/internal/lock.sh release <holder-id>
```

---

## Appendix: Security Considerations

### SSH Key Management

- **Never commit private keys to git**
- Use separate SSH keys for CI vs personal access
- Rotate keys periodically (recommended: annually)
- Revoke keys immediately if compromised

### API Token Security

- Store `HCLOUD_TOKEN` only in GitHub Secrets (never in code)
- Use separate API tokens for different purposes (CI, local dev, production)
- Rotate tokens if exposed
- Limit token permissions to minimum required (Read & Write for project resources)

### VM Access

- VMs should only be accessible via SSH (no password authentication)
- Test VMs should not contain sensitive data
- VMs should not have access to production systems
- Consider using Hetzner Cloud Firewall to restrict SSH access to known IPs (optional)

---

## Appendix: Troubleshooting Quick Reference

| Problem | Quick Fix |
|---------|-----------|
| Provisioning fails | Delete VMs, trigger CI: `gh workflow run test.yml` |
| Lock stuck | `hcloud server remove-label pc1 lock_holder lock_acquired` |
| VM unreachable | `hcloud server reboot pc1` |
| Baseline corrupt | Delete VMs, re-provision |
| CI secrets missing | Add to Settings > Secrets and variables > Actions |
| SSH permission denied | Add your public key as `SSH_AUTHORIZED_KEY_*` secret, reprovision |
| Reset timeout | Check VM status, manually reboot if needed |
| Integration tests skip | Check environment variables or CI secrets |
| GitHub API rate limit | Set `GITHUB_TOKEN` environment variable (see [Optional: GitHub API Rate Limiting](#optional-github-api-rate-limiting)) |

---

**Last Updated**: 2025-12-18
**Document Version**: 1.3 (Added CI Workflow Configuration section)
