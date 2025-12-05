# Testing Framework Implementation Plan

This document provides the detailed implementation plan for the testing framework infrastructure. Writing actual tests for specific features (e.g., 001-foundation) is out of scope and tracked separately in feature `003-foundation-tests`.

## Test Directory Structure

```text
tests/
├── conftest.py                      # Shared fixtures
├── pytest.ini                       # Pytest configuration (if separate from pyproject.toml)
├── __init__.py
│
├── unit/                            # Fast tests, no VMs
│   ├── __init__.py
│   ├── conftest.py                  # Unit-specific fixtures
│   └── ...                          # Test files (out of scope for this feature)
│
├── contract/                        # Interface compliance (existing)
│   ├── __init__.py
│   └── test_job_interface.py
│
├── integration/                     # VM-required tests
│   ├── __init__.py
│   ├── conftest.py                  # VM fixtures
│   └── ...                          # Test files (out of scope for this feature)
│
├── infrastructure/                  # VM provisioning
│   ├── README.md
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── versions.tf
│   ├── cloud-config.yaml
│   └── scripts/
│       ├── tofu-wrapper.sh          # State sync to/from Storage Box
│       ├── provision.sh
│       ├── configure-vm.sh
│       ├── configure-hosts.sh
│       ├── reset-vm.sh
│       └── lock.sh
│
└── playbook/
    └── visual-verification.md
```

## Shared Fixtures (conftest.py)

### tests/conftest.py

Shared fixtures available to all test types:

```python
# Key fixtures:
# - mock_subprocess: Mocked asyncio.create_subprocess_shell
# - mock_job_context: JobContext with mocked executors
# - mock_event_bus: Mocked EventBus
# - temp_config_file: Path to temporary config file
# - valid_config_dict: Valid configuration dictionary
```

### tests/unit/conftest.py

Unit-specific fixtures:

```python
# Key fixtures:
# - mock_local_executor: LocalExecutor with mocked subprocess
# - mock_remote_executor: RemoteExecutor with mocked SSH
# - temp_config_with_content: Config file with valid YAML
```

### tests/integration/conftest.py

```python
# Key fixtures:
# - integration_lock: Acquires lock for test session
# - reset_vms: Resets VMs to baseline at session start
# - event_bus: Real EventBus instance
# - local_executor: Real LocalExecutor
# - ssh_connection: Real asyncssh connection to target
# - remote_executor: Real RemoteExecutor
# - test_session_id: Unique session ID for test isolation
# - cleanup_snapshots: Cleanup fixture for snapshot tests

# pytest markers:
# @pytest.mark.integration - marks tests requiring VMs
```

## Infrastructure Configuration

### tests/infrastructure/main.tf

```hcl
terraform {
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = ">= 1.57.0"
    }
  }
  # Local backend - state synced to/from Storage Box via tofu-wrapper.sh
}

provider "hcloud" {
  token = var.hcloud_token
}

resource "hcloud_ssh_key" "test_key" {
  name       = "pc-switcher-test-key"
  public_key = file(var.ssh_public_key_path)
}

resource "hcloud_server" "pc1" {
  name        = "pc-switcher-pc1"
  server_type = "cx23"
  image       = "ubuntu-24.04"
  location    = "fsn1"
  ssh_keys    = [hcloud_ssh_key.test_key.id]
  user_data   = file("${path.module}/cloud-config.yaml")

  labels = {
    project = "pc-switcher"
    role    = "pc1"
  }
}

resource "hcloud_server" "pc2" {
  name        = "pc-switcher-pc2"
  server_type = "cx23"
  image       = "ubuntu-24.04"
  location    = "fsn1"
  ssh_keys    = [hcloud_ssh_key.test_key.id]
  user_data   = file("${path.module}/cloud-config.yaml")

  labels = {
    project = "pc-switcher"
    role    = "pc2"
  }
}

output "pc1_ip" {
  value = hcloud_server.pc1.ipv4_address
}

output "pc2_ip" {
  value = hcloud_server.pc2.ipv4_address
}
```

### tests/infrastructure/cloud-config.yaml

Minimal cloud-config for initial SSH access. The actual btrfs and user configuration is done by `provision.sh` using Hetzner's installimage.

```yaml
#cloud-config
# Minimal config for initial boot - provision.sh does the real setup

# Disable password authentication for security
ssh_pwauth: false
```

Note: This cloud-config is intentionally minimal because `provision.sh` will reinstall the OS with btrfs using installimage, which wipes everything.

### tests/infrastructure/scripts/tofu-wrapper.sh

Wrapper script to sync OpenTofu state to/from Hetzner Storage Box:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(dirname "$SCRIPT_DIR")"
STATE_FILE="$INFRA_DIR/terraform.tfstate"
REMOTE_PATH="${STORAGE_BOX_PATH:-pc-switcher/test-infrastructure}/terraform.tfstate"

# Validate required environment variables
: "${STORAGE_BOX_HOST:?STORAGE_BOX_HOST must be set}"
: "${STORAGE_BOX_USER:?STORAGE_BOX_USER must be set}"

pull_state() {
    echo "Pulling state from Storage Box..."
    scp -q "${STORAGE_BOX_USER}@${STORAGE_BOX_HOST}:${REMOTE_PATH}" "$STATE_FILE" 2>/dev/null || true
}

push_state() {
    if [[ -f "$STATE_FILE" ]]; then
        echo "Pushing state to Storage Box..."
        ssh "${STORAGE_BOX_USER}@${STORAGE_BOX_HOST}" "mkdir -p $(dirname "$REMOTE_PATH")"
        scp -q "$STATE_FILE" "${STORAGE_BOX_USER}@${STORAGE_BOX_HOST}:${REMOTE_PATH}"
    fi
}

# Pull state before running tofu
pull_state

# Run tofu with all arguments
cd "$INFRA_DIR"
tofu "$@"
EXIT_CODE=$?

# Push state after running tofu (if state-modifying command)
case "${1:-}" in
    apply|destroy|import|state)
        push_state
        ;;
esac

exit $EXIT_CODE
```

### tests/infrastructure/scripts/lock.sh

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
LOCK_FILE="/tmp/pc-switcher-integration-test.lock"

show_help() {
    cat << EOF
Usage: $SCRIPT_NAME <holder> <acquire|release>

Manage integration test lock to prevent concurrent test runs.

Arguments:
  holder    Identifier for lock holder (e.g., CI job ID or username)
  action    One of: acquire, release

Examples:
  $SCRIPT_NAME github-123456 acquire
  $SCRIPT_NAME \$USER release

Lock file: $LOCK_FILE
EOF
}

# Handle help flags
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 2 ]]; then
    show_help
    [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && exit 0
    exit 1
fi

LOCK_HOLDER="$1"
ACTION="$2"

acquire_lock() {
    local max_wait=300
    local waited=0

    while true; do
        if mkdir "$LOCK_FILE" 2>/dev/null; then
            echo "$LOCK_HOLDER" > "$LOCK_FILE/holder"
            echo "Lock acquired by $LOCK_HOLDER"
            return 0
        fi

        if [[ $waited -ge $max_wait ]]; then
            echo "Failed to acquire lock after ${max_wait}s" >&2
            echo "Current holder: $(cat "$LOCK_FILE/holder" 2>/dev/null || echo 'unknown')" >&2
            return 1
        fi

        sleep 5
        waited=$((waited + 5))
    done
}

release_lock() {
    if [[ -d "$LOCK_FILE" ]]; then
        holder=$(cat "$LOCK_FILE/holder" 2>/dev/null || echo "unknown")
        if [[ "$holder" == "$LOCK_HOLDER" ]]; then
            rm -rf "$LOCK_FILE"
            echo "Lock released by $LOCK_HOLDER"
        else
            echo "Lock held by $holder, not releasing" >&2
        fi
    fi
}

case "$ACTION" in
    acquire) acquire_lock ;;
    release) release_lock ;;
    *) show_help; exit 1 ;;
esac
```

### tests/infrastructure/scripts/reset-vm.sh

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
USER="${PC_SWITCHER_TEST_USER:-testuser}"

show_help() {
    cat << EOF
Usage: $SCRIPT_NAME <hostname>

Reset a test VM to its baseline btrfs snapshot state.

This script:
  1. Cleans up test artifacts in /.snapshots/pc-switcher/
  2. Mounts top-level btrfs filesystem
  3. Replaces @ and @home with fresh snapshots from baseline
  4. Reboots the VM and waits for it to come back online
  5. Cleans up old subvolumes

Arguments:
  hostname    SSH hostname of the VM to reset (e.g., pc1, pc2)

Environment:
  PC_SWITCHER_TEST_USER    SSH user (default: testuser)

Examples:
  $SCRIPT_NAME pc1
  $SCRIPT_NAME pc2
EOF
}

# Handle help flags
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
    show_help
    [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && exit 0
    exit 1
fi

HOST="$1"

echo "Resetting VM: $HOST"

# Reset to baseline snapshots
ssh "$USER@$HOST" << 'EOF'
    set -euo pipefail

    # Clean up test artifacts (keep baseline snapshots)
    echo "Cleaning up test artifacts..."
    sudo rm -rf /.snapshots/pc-switcher/test-* 2>/dev/null || true

    # Mount top-level btrfs filesystem
    echo "Mounting top-level filesystem..."
    sudo mkdir -p /mnt/btrfs
    sudo mount -o subvolid=5 /dev/sda2 /mnt/btrfs

    # Replace active subvolumes with fresh snapshots from baseline
    echo "Replacing subvolumes with baseline snapshots..."
    sudo mv /mnt/btrfs/@ /mnt/btrfs/@_old
    sudo btrfs subvolume snapshot /mnt/btrfs/.snapshots/baseline/@ /mnt/btrfs/@
    sudo mv /mnt/btrfs/@home /mnt/btrfs/@home_old
    sudo btrfs subvolume snapshot /mnt/btrfs/.snapshots/baseline/@home /mnt/btrfs/@home

    # Unmount before reboot
    sudo umount /mnt/btrfs

    echo "Rebooting..."
    sudo reboot
EOF

# Wait for VM to come back
echo "Waiting for $HOST to come back online..."
sleep 15
until ssh -o ConnectTimeout=5 -o BatchMode=yes "$USER@$HOST" true 2>/dev/null; do
    sleep 5
done

# Clean up old subvolumes after reboot
echo "Cleaning up old subvolumes..."
ssh "$USER@$HOST" << 'EOF'
    set -euo pipefail
    sudo mkdir -p /mnt/btrfs
    sudo mount -o subvolid=5 /dev/sda2 /mnt/btrfs
    sudo btrfs subvolume delete /mnt/btrfs/@_old 2>/dev/null || true
    sudo btrfs subvolume delete /mnt/btrfs/@home_old 2>/dev/null || true
    sudo umount /mnt/btrfs
EOF

echo "VM $HOST is ready"
```

### tests/infrastructure/scripts/provision.sh

This script is run once per VM to wipe and install Ubuntu 24.04 with btrfs filesystem using Hetzner's installimage in rescue mode:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

show_help() {
    cat << EOF
Usage: $SCRIPT_NAME <server-name>

Provision a Hetzner VM with btrfs filesystem using installimage.

This script:
  1. Enables rescue mode on the server
  2. Reboots into rescue mode
  3. Runs installimage with btrfs configuration
  4. Configures subvolumes (@, @home, @snapshots)
  5. Creates testuser with SSH access and sudo
  6. Creates baseline snapshots for test reset

Arguments:
  server-name    Name of the Hetzner server (e.g., pc-switcher-pc1)

Environment:
  HCLOUD_TOKEN   Hetzner Cloud API token (required)
  SSH_PUBLIC_KEY Path to SSH public key (default: ~/.ssh/id_ed25519.pub)

Examples:
  $SCRIPT_NAME pc-switcher-pc1
  $SCRIPT_NAME pc-switcher-pc2

Note: This is a one-time operation. After provisioning, use reset-vm.sh
      for subsequent test runs.
EOF
}

# Handle help flags
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
    show_help
    [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && exit 0
    exit 1
fi

SERVER_NAME="$1"
SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY:-$HOME/.ssh/id_ed25519.pub}"

if [[ -z "${HCLOUD_TOKEN:-}" ]]; then
    echo "ERROR: HCLOUD_TOKEN environment variable is required" >&2
    exit 1
fi

echo "Provisioning server: $SERVER_NAME"

# Get server IP
SERVER_IP=$(hcloud server ip "$SERVER_NAME")
echo "Server IP: $SERVER_IP"

# Enable rescue mode
echo "Enabling rescue mode..."
hcloud server enable-rescue "$SERVER_NAME" --ssh-key pc-switcher-test-key

# Reboot into rescue
echo "Rebooting into rescue mode..."
hcloud server reboot "$SERVER_NAME"

# Wait for rescue mode
echo "Waiting for rescue mode (this may take a minute)..."
sleep 30
ssh-keygen -R "$SERVER_IP" 2>/dev/null || true
until ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new root@"$SERVER_IP" true 2>/dev/null; do
    sleep 5
done

echo "Connected to rescue mode"

# Create installimage config
cat << INSTALLCONFIG | ssh root@"$SERVER_IP" "cat > /autosetup"
DRIVE1 /dev/sda
USE_KERNEL_MODE_SETTING
HOSTNAME $SERVER_NAME
PART /boot/efi ext4 128M
PART btrfs.1 btrfs all
SUBVOL btrfs.1 @ /
SUBVOL btrfs.1 @home /home
SUBVOL btrfs.1 @snapshots /.snapshots
IMAGE /root/.oldroot/nfs/images/Ubuntu-2404-noble-amd64-base.tar.gz
INSTALLCONFIG

# Run installimage
echo "Running installimage (this takes several minutes)..."
ssh root@"$SERVER_IP" "installimage -a -c /autosetup"

# Reboot into new system
echo "Rebooting into new system..."
ssh root@"$SERVER_IP" "reboot" || true

# Wait for new system
echo "Waiting for system to come online..."
sleep 60
ssh-keygen -R "$SERVER_IP" 2>/dev/null || true
until ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new root@"$SERVER_IP" true 2>/dev/null; do
    sleep 10
done

echo "System online, configuring..."

# Copy and run the configuration script
SCRIPT_DIR="$(dirname "$0")"
scp "$SCRIPT_DIR/configure-vm.sh" root@"$SERVER_IP":/tmp/
ssh root@"$SERVER_IP" "bash /tmp/configure-vm.sh '$(cat "$SSH_PUBLIC_KEY")'"
ssh root@"$SERVER_IP" "rm /tmp/configure-vm.sh"

echo "Server $SERVER_NAME provisioned successfully"
echo ""
echo "IMPORTANT: After provisioning both VMs, run configure-hosts.sh to set up /etc/hosts"
```

### tests/infrastructure/scripts/configure-vm.sh

This script is copied to and executed on the VM by provision.sh:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Called by provision.sh with SSH public key as argument
SSH_PUBLIC_KEY="$1"

# Install required packages
apt-get update
apt-get install -y btrfs-progs qemu-guest-agent fail2ban ufw

# Enable QEMU Guest Agent
systemctl enable qemu-guest-agent
systemctl start qemu-guest-agent

# Configure fail2ban for SSH
cat > /etc/fail2ban/jail.local << 'FAIL2BAN'
[DEFAULT]
bantime = 10m
findtime = 10m
maxretry = 5
backend = systemd

[sshd]
enabled = true
port = ssh
banaction = iptables-multiport
FAIL2BAN
systemctl enable fail2ban
systemctl restart fail2ban

# SSH hardening
cat > /etc/ssh/sshd_config.d/99-hardening.conf << 'SSHCONF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
MaxAuthTries 2
AllowTcpForwarding no
X11Forwarding no
AllowAgentForwarding no
AuthorizedKeysFile .ssh/authorized_keys
AllowUsers testuser
SSHCONF
systemctl restart ssh

# Configure ufw firewall
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

# Create testuser with sudo and SSH access
useradd -m -s /bin/bash testuser
echo "testuser ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/testuser
chmod 440 /etc/sudoers.d/testuser

mkdir -p /home/testuser/.ssh
echo "$SSH_PUBLIC_KEY" > /home/testuser/.ssh/authorized_keys
chmod 700 /home/testuser/.ssh
chmod 600 /home/testuser/.ssh/authorized_keys
chown -R testuser:testuser /home/testuser/.ssh

# Create baseline snapshot directory
mkdir -p /.snapshots/baseline
mkdir -p /.snapshots/pc-switcher

# Clean up
apt-get autoremove -y
apt-get clean

# Create baseline snapshots for test reset
btrfs subvolume snapshot -r / /.snapshots/baseline/@
btrfs subvolume snapshot -r /home /.snapshots/baseline/@home

echo "VM configuration complete!"
```

### tests/infrastructure/scripts/configure-hosts.sh

Run this after both VMs are provisioned:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

show_help() {
    cat << EOF
Usage: $SCRIPT_NAME

Configure networking and SSH keys on both test VMs.

This script:
  1. Gets the IPs of both VMs from Hetzner
  2. Updates /etc/hosts on both VMs with pc1/pc2 entries
  3. Generates SSH keys for testuser on each VM (if not present)
  4. Exchanges SSH public keys so testuser can SSH between VMs

Environment:
  HCLOUD_TOKEN   Hetzner Cloud API token (required)

Run this after provisioning both VMs with provision.sh.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    show_help
    exit 0
fi

if [[ -z "${HCLOUD_TOKEN:-}" ]]; then
    echo "ERROR: HCLOUD_TOKEN environment variable is required" >&2
    exit 1
fi

PC1_IP=$(hcloud server ip pc-switcher-pc1)
PC2_IP=$(hcloud server ip pc-switcher-pc2)

echo "PC1 IP: $PC1_IP"
echo "PC2 IP: $PC2_IP"

# Configure /etc/hosts on both VMs
for HOST in "$PC1_IP" "$PC2_IP"; do
    echo "Configuring /etc/hosts on $HOST..."
    ssh testuser@"$HOST" << EOF
sudo sed -i '/pc1/d' /etc/hosts
sudo sed -i '/pc2/d' /etc/hosts
echo "$PC1_IP pc1" | sudo tee -a /etc/hosts > /dev/null
echo "$PC2_IP pc2" | sudo tee -a /etc/hosts > /dev/null
EOF
done

echo "Hosts configured"

# Generate SSH keys for testuser on each VM (if not present)
for HOST in "$PC1_IP" "$PC2_IP"; do
    echo "Generating SSH key on $HOST..."
    ssh testuser@"$HOST" << 'EOF'
if [[ ! -f ~/.ssh/id_ed25519 ]]; then
    ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
    echo "SSH key generated"
else
    echo "SSH key already exists"
fi
EOF
done

# Exchange SSH public keys between VMs
echo "Exchanging SSH keys..."

PC1_PUBKEY=$(ssh testuser@"$PC1_IP" cat ~/.ssh/id_ed25519.pub)
PC2_PUBKEY=$(ssh testuser@"$PC2_IP" cat ~/.ssh/id_ed25519.pub)

ssh testuser@"$PC2_IP" << EOF
grep -qF "$PC1_PUBKEY" ~/.ssh/authorized_keys 2>/dev/null || echo "$PC1_PUBKEY" >> ~/.ssh/authorized_keys
EOF

ssh testuser@"$PC1_IP" << EOF
grep -qF "$PC2_PUBKEY" ~/.ssh/authorized_keys 2>/dev/null || echo "$PC2_PUBKEY" >> ~/.ssh/authorized_keys
EOF

# Set up known_hosts for VM-to-VM SSH
echo "Configuring known_hosts for VM-to-VM SSH..."

PC1_HOSTKEY=$(ssh-keyscan -H "$PC1_IP" 2>/dev/null)
PC2_HOSTKEY=$(ssh-keyscan -H "$PC2_IP" 2>/dev/null)

ssh testuser@"$PC1_IP" << EOF
echo "$PC2_HOSTKEY" >> ~/.ssh/known_hosts
EOF

ssh testuser@"$PC2_IP" << EOF
echo "$PC1_HOSTKEY" >> ~/.ssh/known_hosts
EOF

echo ""
echo "Done! testuser can now:"
echo "  - SSH from pc1 to pc2: ssh pc2"
echo "  - SSH from pc2 to pc1: ssh pc1"
```

## GitHub Actions Workflow

### .github/workflows/test.yml

```yaml
name: Tests

on:
  push:
    branches: ['**']
  pull_request:
    branches: [main]
  workflow_dispatch:
    inputs:
      run_integration:
        description: 'Run integration tests'
        type: boolean
        default: false

env:
  UV_VERSION: "latest"

jobs:
  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          version: ${{ env.UV_VERSION }}
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run basedpyright
      - run: uv run codespell

  unit-tests:
    name: Unit Tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: astral-sh/setup-uv@v6
        with:
          version: ${{ env.UV_VERSION }}
      - run: uv run pytest tests/unit tests/contract -v --tb=short
      - name: Coverage
        run: uv run pytest tests/unit tests/contract --cov=src/pcswitcher --cov-report=xml
      - uses: codecov/codecov-action@v4
        with:
          files: coverage.xml
        if: always()

  integration-tests:
    name: Integration Tests
    runs-on: ubuntu-latest
    if: |
      github.event_name == 'pull_request' && github.base_ref == 'main' ||
      github.event.inputs.run_integration == 'true'
    needs: [lint, unit-tests]
    concurrency:
      group: pc-switcher-integration
      cancel-in-progress: false

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: astral-sh/setup-uv@v6
        with:
          version: ${{ env.UV_VERSION }}

      - uses: opentofu/setup-opentofu@v1
        with:
          tofu_version: "1.10.7"

      - name: Setup SSH keys
        run: |
          mkdir -p ~/.ssh
          echo "${{ secrets.HETZNER_SSH_PRIVATE_KEY }}" > ~/.ssh/id_ed25519
          chmod 600 ~/.ssh/id_ed25519
          echo "${{ secrets.STORAGE_BOX_SSH_KEY }}" > ~/.ssh/storagebox
          chmod 600 ~/.ssh/storagebox
          ssh-keyscan -H ${{ secrets.PC1_TEST_HOST }} >> ~/.ssh/known_hosts
          ssh-keyscan -H ${{ secrets.PC2_TEST_HOST }} >> ~/.ssh/known_hosts

      - name: Provision VMs (if needed)
        working-directory: tests/infrastructure
        run: |
          ./scripts/tofu-wrapper.sh init
          ./scripts/tofu-wrapper.sh apply -auto-approve
        env:
          TF_VAR_hcloud_token: ${{ secrets.HCLOUD_TOKEN }}
          STORAGE_BOX_HOST: ${{ secrets.STORAGE_BOX_HOST }}
          STORAGE_BOX_USER: ${{ secrets.STORAGE_BOX_USER }}

      - name: Reset VMs
        run: |
          ./tests/infrastructure/scripts/reset-vm.sh ${{ secrets.PC1_TEST_HOST }}
          ./tests/infrastructure/scripts/reset-vm.sh ${{ secrets.PC2_TEST_HOST }}

      - name: Run integration tests
        run: uv run pytest tests/integration -v -m integration --tb=short
        env:
          PC_SWITCHER_TEST_PC1_HOST: ${{ secrets.PC1_TEST_HOST }}
          PC_SWITCHER_TEST_PC2_HOST: ${{ secrets.PC2_TEST_HOST }}
          PC_SWITCHER_TEST_USER: testuser
          CI_JOB_ID: ${{ github.run_id }}
```

## Required GitHub Secrets

| Secret | Description |
|--------|-------------|
| `HCLOUD_TOKEN` | Hetzner Cloud API token |
| `HETZNER_SSH_PRIVATE_KEY` | SSH private key for VM access |
| `STORAGE_BOX_HOST` | Hetzner Storage Box hostname |
| `STORAGE_BOX_USER` | Storage Box SSH user (sub-account) |
| `STORAGE_BOX_SSH_KEY` | SSH private key for Storage Box access |
| `PC1_TEST_HOST` | IP/hostname of pc1 test VM |
| `PC2_TEST_HOST` | IP/hostname of pc2 test VM |

## Implementation Order

Tasks can be implemented in parallel where noted.

### Phase 1: Infrastructure Scripts

**Can run in parallel:**
1. Create `tests/infrastructure/scripts/tofu-wrapper.sh`
2. Create `tests/infrastructure/scripts/lock.sh`
3. Create `tests/infrastructure/scripts/reset-vm.sh`
4. Create `tests/infrastructure/scripts/provision.sh`
5. Create `tests/infrastructure/scripts/configure-vm.sh`
6. Create `tests/infrastructure/scripts/configure-hosts.sh`

### Phase 2: OpenTofu Configuration

**Can run in parallel:**
7. Create `tests/infrastructure/main.tf`
8. Create `tests/infrastructure/variables.tf`
9. Create `tests/infrastructure/outputs.tf`
10. Create `tests/infrastructure/versions.tf`
11. Create `tests/infrastructure/cloud-config.yaml`
12. Create `tests/infrastructure/README.md`

### Phase 3: Test Fixtures

**Sequential (shared conftest first):**
13. Update `tests/conftest.py` with shared fixtures

**Then in parallel:**
14. Create `tests/unit/conftest.py`
15. Create `tests/integration/conftest.py`
16. Create `tests/integration/__init__.py`

### Phase 4: CI/CD & Documentation

**Can run in parallel:**
17. Create `.github/workflows/test.yml`
18. Update `pyproject.toml` with pytest configuration
19. Create `tests/playbook/visual-verification.md`
20. Update `docs/testing-developer-guide.md`
21. Update `docs/testing-ops-guide.md`
22. Update `docs/testing-framework.md`
