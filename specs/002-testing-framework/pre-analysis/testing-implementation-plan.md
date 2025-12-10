# Testing Framework Implementation Plan

This document provides the detailed implementation plan for the testing framework infrastructure. Writing actual tests for specific features (e.g., 001-foundation) is out of scope and tracked separately in feature `003-foundation-tests`.

> **Note**: The script structure was refactored after this document was written. See tasks.md for the current structure:
> - `create-vm.sh` - creates a single VM (replaces provision-vms.sh VM creation + provision.sh)
> - `configure-vm.sh` - configures a single VM (unchanged)
> - `configure-hosts.sh` - configures both VMs (unchanged)
> - `create-baseline-snapshots.sh` - creates baseline snapshots (extracted from configure-vm.sh)
> - `provision-test-infra.sh` - orchestrator that calls all above scripts
> - `reset-vm.sh` - resets single VM (unchanged)
> - `lock.sh` - lock management (unchanged)
>
> The script implementations below are still useful as reference but need adaptation to the new structure.

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
└── infrastructure/                  # VM provisioning
    ├── README.md
    └── scripts/
        ├── create-vm.sh                # Create single VM via hcloud + install OS with btrfs
        ├── configure-vm.sh             # Configure single VM: testuser, SSH keys, services
        ├── configure-hosts.sh          # Configure both VMs: /etc/hosts, inter-VM SSH keys
        ├── create-baseline-snapshots.sh # Create baseline btrfs snapshots on both VMs
        ├── provision-test-infra.sh     # Orchestrator: calls above scripts in correct order
        ├── reset-vm.sh                 # Reset single VM to baseline snapshot
        └── lock.sh                     # Lock management via Hetzner Server Labels

docs/
├── testing-framework.md             # Architecture documentation (update)
├── testing-developer-guide.md       # Developer guide (create)
├── testing-ops-guide.md             # Operational guide (create)
└── testing-playbook.md              # Manual verification playbook (create, per FR-033)
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
# - integration_lock: Acquires lock via Hetzner Server Labels (survives VM reboot/reset)
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

## Contract Tests (FR-003a)

### tests/contract/test_executor_interface.py

Contract tests verify that MockExecutor and real executor implementations (LocalExecutor, RemoteExecutor) adhere to the same behavioral interface, ensuring mocks remain reliable representations of production behavior.

```python
# Contract test structure:
# - Define shared behavioral expectations as parameterized tests
# - Run same tests against MockExecutor and real executors
# - Verify consistent return types, error handling, and side effects

import pytest
from pcswitcher.executor import LocalExecutor, RemoteExecutor, MockExecutor

class ExecutorContractTests:
    """Base contract tests that all executor implementations must pass."""

    @pytest.fixture
    def executor(self):
        """Override in subclasses to provide specific executor."""
        raise NotImplementedError

    async def test_run_returns_result_with_exit_code(self, executor):
        """All executors must return result with exit_code attribute."""
        result = await executor.run("echo hello")
        assert hasattr(result, "exit_code")
        assert isinstance(result.exit_code, int)

    async def test_run_returns_result_with_stdout(self, executor):
        """All executors must return result with stdout attribute."""
        result = await executor.run("echo hello")
        assert hasattr(result, "stdout")
        assert isinstance(result.stdout, str)

    async def test_run_returns_result_with_stderr(self, executor):
        """All executors must return result with stderr attribute."""
        result = await executor.run("echo hello")
        assert hasattr(result, "stderr")
        assert isinstance(result.stderr, str)

    async def test_failed_command_returns_nonzero_exit(self, executor):
        """Failed commands must return non-zero exit code."""
        result = await executor.run("exit 1")
        assert result.exit_code != 0

    async def test_check_raises_on_failure(self, executor):
        """check=True must raise exception on non-zero exit."""
        with pytest.raises(Exception):  # Specific exception type TBD
            await executor.run("exit 1", check=True)


class TestMockExecutorContract(ExecutorContractTests):
    """Verify MockExecutor adheres to executor contract."""

    @pytest.fixture
    def executor(self):
        return MockExecutor()


class TestLocalExecutorContract(ExecutorContractTests):
    """Verify LocalExecutor adheres to executor contract."""

    @pytest.fixture
    def executor(self):
        return LocalExecutor()


# RemoteExecutor contract tests require VM infrastructure
# and are marked as integration tests
@pytest.mark.integration
class TestRemoteExecutorContract(ExecutorContractTests):
    """Verify RemoteExecutor adheres to executor contract."""

    @pytest.fixture
    async def executor(self, pc1_connection):
        from pcswitcher.executor import RemoteExecutor
        return RemoteExecutor(pc1_connection)
```

## Infrastructure Configuration

### tests/infrastructure/scripts/provision-vms.sh

Creates VMs via hcloud CLI if they don't exist, then runs full provisioning:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

show_help() {
    cat << EOF
Usage: $SCRIPT_NAME

Create and provision test VMs if they don't exist.

This script:
  1. Creates SSH key in Hetzner Cloud (if needed)
  2. Creates pc1 and pc2 VMs (if needed)
  3. Runs provision.sh on each VM to install btrfs
  4. Runs configure-hosts.sh to setup inter-VM networking

Environment:
  HCLOUD_TOKEN     Hetzner Cloud API token (required)
  SSH_PUBLIC_KEY   Path to SSH public key (default: ~/.ssh/id_ed25519.pub)

Examples:
  $SCRIPT_NAME
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    show_help
    exit 0
fi

: "${HCLOUD_TOKEN:?HCLOUD_TOKEN must be set}"
SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY:-$HOME/.ssh/id_ed25519.pub}"

# Create SSH key if needed
if ! hcloud ssh-key describe pc-switcher-test-key &>/dev/null; then
    echo "Creating SSH key..."
    hcloud ssh-key create --name pc-switcher-test-key \
        --public-key-from-file "$SSH_PUBLIC_KEY"
fi

# Track which VMs need provisioning
VMS_TO_PROVISION=()

# Create VMs if needed
for VM in pc1 pc2; do
    if hcloud server describe "$VM" &>/dev/null; then
        echo "VM $VM already exists, skipping creation"
    else
        echo "Creating VM $VM..."
        hcloud server create \
            --name "$VM" \
            --type cx23 \
            --image ubuntu-24.04 \
            --location fsn1 \
            --ssh-key pc-switcher-test-key
        VMS_TO_PROVISION+=("$VM")
    fi
done

# Run provisioning for newly created VMs
for VM in "${VMS_TO_PROVISION[@]}"; do
    echo "Provisioning $VM..."
    "$SCRIPT_DIR/provision.sh" "$VM"
done

# Configure inter-VM networking (if any VMs were provisioned)
if [[ ${#VMS_TO_PROVISION[@]} -gt 0 ]]; then
    echo "Configuring inter-VM networking..."
    "$SCRIPT_DIR/configure-hosts.sh"
fi

echo ""
echo "VMs ready:"
echo "  pc1: $(hcloud server ip pc1)"
echo "  pc2: $(hcloud server ip pc2)"
```

### tests/infrastructure/scripts/lock.sh

Uses Hetzner Server Labels to store lock state externally from the VMs. This ensures
the lock survives VM reboots and btrfs snapshot rollbacks.

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SERVER_NAME="pc1"  # Lock is stored on pc1's server object

show_help() {
    cat << EOF
Usage: $SCRIPT_NAME <holder> <acquire|release|status>

Manage integration test lock to prevent concurrent test runs.

The lock is stored as Hetzner Server Labels on $SERVER_NAME, which:
  - Survives VM reboots
  - Survives btrfs snapshot rollbacks
  - Is accessible via hcloud CLI from any machine

Arguments:
  holder    Identifier for lock holder (e.g., CI job ID or username)
  action    One of: acquire, release, status

Labels used:
  lock_holder    Lock holder identifier
  lock_acquired  ISO8601 timestamp when lock was acquired

Examples:
  $SCRIPT_NAME github-123456 acquire
  $SCRIPT_NAME \$USER release
  $SCRIPT_NAME "" status

Environment:
  HCLOUD_TOKEN   Hetzner Cloud API token (required)
EOF
}

# Handle help flags
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    show_help
    exit 0
fi

if [[ $# -lt 2 ]]; then
    show_help
    exit 1
fi

: "${HCLOUD_TOKEN:?HCLOUD_TOKEN must be set}"

LOCK_HOLDER="$1"
ACTION="$2"
TIMEOUT=300  # 5 minutes

get_current_lock() {
    # Returns "holder|timestamp" or empty string if no lock
    local holder timestamp
    holder=$(hcloud server describe "$SERVER_NAME" -o json | jq -r '.labels.lock_holder // empty')
    timestamp=$(hcloud server describe "$SERVER_NAME" -o json | jq -r '.labels.lock_acquired // empty')
    if [[ -n "$holder" ]]; then
        echo "${holder}|${timestamp}"
    fi
}

acquire_lock() {
    local end_time=$(($(date +%s) + TIMEOUT))

    while [ $(date +%s) -lt $end_time ]; do
        current=$(get_current_lock)

        if [[ -z "$current" ]]; then
            # No lock held, try to acquire
            local acquired_time
            acquired_time=$(date -Iseconds)
            hcloud server add-label "$SERVER_NAME" "lock_holder=$LOCK_HOLDER" --overwrite
            hcloud server add-label "$SERVER_NAME" "lock_acquired=$acquired_time" --overwrite

            # Verify we got the lock (check for race condition)
            sleep 1
            current=$(get_current_lock)
            current_holder="${current%%|*}"
            if [[ "$current_holder" == "$LOCK_HOLDER" ]]; then
                echo "Lock acquired by $LOCK_HOLDER at $acquired_time"
                return 0
            fi
            # Someone else got it, continue waiting
        fi

        # Show current holder while waiting
        current_holder="${current%%|*}"
        current_time="${current##*|}"
        echo "Lock held by $current_holder (since $current_time), waiting..."
        sleep 10
    done

    echo "ERROR: Failed to acquire lock after ${TIMEOUT}s" >&2
    current=$(get_current_lock)
    if [[ -n "$current" ]]; then
        echo "Current lock: holder=${current%%|*}, acquired=${current##*|}" >&2
    fi
    return 1
}

release_lock() {
    current=$(get_current_lock)

    if [[ -z "$current" ]]; then
        echo "No lock held"
        return 0
    fi

    current_holder="${current%%|*}"
    if [[ "$current_holder" == "$LOCK_HOLDER" ]]; then
        hcloud server remove-label "$SERVER_NAME" "lock_holder"
        hcloud server remove-label "$SERVER_NAME" "lock_acquired"
        echo "Lock released by $LOCK_HOLDER"
        return 0
    else
        echo "ERROR: Lock held by $current_holder, not $LOCK_HOLDER. Not releasing." >&2
        return 1
    fi
}

show_status() {
    current=$(get_current_lock)

    if [[ -z "$current" ]]; then
        echo "No lock held"
    else
        echo "Lock held by: ${current%%|*}"
        echo "Acquired at:  ${current##*|}"
    fi
}

case "$ACTION" in
    acquire) acquire_lock ;;
    release) release_lock ;;
    status) show_status ;;
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
  1. Validates baseline snapshots exist
  2. Cleans up test artifacts in /.snapshots/pc-switcher/
  3. Mounts top-level btrfs filesystem
  4. Replaces @ and @home with fresh snapshots from baseline
  5. Reboots the VM and waits for it to come back online
  6. Cleans up old subvolumes

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

# Validate baseline snapshots exist (FR-004)
echo "Validating baseline snapshots..."
VALIDATION_RESULT=$(ssh "$USER@$HOST" << 'VALIDATE'
    set -euo pipefail
    missing=()

    # Check baseline @ snapshot
    if ! sudo btrfs subvolume show /.snapshots/baseline/@ &>/dev/null; then
        missing+=("/.snapshots/baseline/@")
    fi

    # Check baseline @home snapshot
    if ! sudo btrfs subvolume show /.snapshots/baseline/@home &>/dev/null; then
        missing+=("/.snapshots/baseline/@home")
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "MISSING:${missing[*]}"
        exit 1
    fi
    echo "OK"
VALIDATE
) || true

if [[ "$VALIDATION_RESULT" == MISSING:* ]]; then
    missing_snapshots="${VALIDATION_RESULT#MISSING:}"
    echo "ERROR: Baseline snapshot missing: $missing_snapshots" >&2
    echo "" >&2
    echo "Run provisioning to create baseline snapshots:" >&2
    echo "  ./tests/infrastructure/scripts/provision-vms.sh" >&2
    exit 1
fi

if [[ "$VALIDATION_RESULT" != "OK" ]]; then
    echo "ERROR: Failed to validate baseline snapshots on $HOST" >&2
    exit 1
fi

echo "Baseline snapshots validated"

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
  server-name    Name of the Hetzner server (e.g., pc1)

Environment:
  HCLOUD_TOKEN   Hetzner Cloud API token (required)
  SSH_PUBLIC_KEY Path to SSH public key (default: ~/.ssh/id_ed25519.pub)

Examples:
  $SCRIPT_NAME pc1
  $SCRIPT_NAME pc2

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
PART /boot/efi esp 256M
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

PC1_IP=$(hcloud server ip pc1)
PC2_IP=$(hcloud server ip pc2)

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
    # Run on PRs to main (from main repo only - secrets unavailable for forks)
    # or on manual workflow dispatch
    if: |
      (github.event_name == 'pull_request' && github.base_ref == 'main' && github.event.pull_request.head.repo.full_name == github.repository) ||
      github.event.inputs.run_integration == 'true'
    needs: [lint, unit-tests]
    concurrency:
      group: pc-switcher-integration
      cancel-in-progress: false

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      # Check if secrets are available (FR-017a/FR-017b)
      - name: Check secrets availability
        id: secrets-check
        run: |
          if [[ -z "${{ secrets.HCLOUD_TOKEN }}" ]] || [[ -z "${{ secrets.HETZNER_SSH_PRIVATE_KEY }}" ]]; then
            echo "skip=true" >> $GITHUB_OUTPUT
            echo "::notice::Skipping integration tests: required secrets not available (fork PR or missing configuration)"
          else
            echo "skip=false" >> $GITHUB_OUTPUT
          fi

      - name: Skip notice for missing secrets
        if: steps.secrets-check.outputs.skip == 'true'
        run: |
          echo "::warning::Integration tests skipped: secrets not available"
          echo "This is expected for forked PRs. Unit tests still run."
          exit 0

      - uses: astral-sh/setup-uv@v6
        if: steps.secrets-check.outputs.skip != 'true'
        with:
          version: ${{ env.UV_VERSION }}

      - name: Install hcloud CLI
        if: steps.secrets-check.outputs.skip != 'true'
        run: |
          curl -fsSL https://github.com/hetznercloud/cli/releases/latest/download/hcloud-linux-amd64.tar.gz | tar -xz
          sudo mv hcloud /usr/local/bin/

      - name: Setup SSH key
        if: steps.secrets-check.outputs.skip != 'true'
        run: |
          mkdir -p ~/.ssh
          echo "${{ secrets.HETZNER_SSH_PRIVATE_KEY }}" > ~/.ssh/id_ed25519
          chmod 600 ~/.ssh/id_ed25519
          # Generate public key from private key for provisioning (FR-006a)
          ssh-keygen -y -f ~/.ssh/id_ed25519 > ~/.ssh/id_ed25519.pub

      - name: Provision VMs (if needed)
        if: steps.secrets-check.outputs.skip != 'true'
        run: ./tests/infrastructure/scripts/provision-vms.sh
        env:
          HCLOUD_TOKEN: ${{ secrets.HCLOUD_TOKEN }}
          SSH_PUBLIC_KEY: ~/.ssh/id_ed25519.pub

      - name: Add VM host keys to known_hosts
        if: steps.secrets-check.outputs.skip != 'true'
        run: |
          PC1_IP=$(hcloud server ip pc1)
          PC2_IP=$(hcloud server ip pc2)
          ssh-keyscan -H "$PC1_IP" >> ~/.ssh/known_hosts
          ssh-keyscan -H "$PC2_IP" >> ~/.ssh/known_hosts
        env:
          HCLOUD_TOKEN: ${{ secrets.HCLOUD_TOKEN }}

      - name: Reset VMs
        if: steps.secrets-check.outputs.skip != 'true'
        run: |
          PC1_IP=$(hcloud server ip pc1)
          PC2_IP=$(hcloud server ip pc2)
          ./tests/infrastructure/scripts/reset-vm.sh "$PC1_IP"
          ./tests/infrastructure/scripts/reset-vm.sh "$PC2_IP"
        env:
          HCLOUD_TOKEN: ${{ secrets.HCLOUD_TOKEN }}

      - name: Run integration tests
        if: steps.secrets-check.outputs.skip != 'true'
        run: |
          PC1_IP=$(hcloud server ip pc1)
          PC2_IP=$(hcloud server ip pc2)
          uv run pytest tests/integration -v -m integration --tb=short 2>&1 | tee pytest-output.log
        env:
          PC_SWITCHER_TEST_PC1_HOST: ${{ env.PC1_IP }}
          PC_SWITCHER_TEST_PC2_HOST: ${{ env.PC2_IP }}
          PC_SWITCHER_TEST_USER: testuser
          CI_JOB_ID: ${{ github.run_id }}
          HCLOUD_TOKEN: ${{ secrets.HCLOUD_TOKEN }}

      # Upload artifacts for debugging (FR-017c)
      - name: Upload test artifacts
        if: always() && steps.secrets-check.outputs.skip != 'true'
        uses: actions/upload-artifact@v4
        with:
          name: integration-test-logs
          path: |
            pytest-output.log
          retention-days: 14
```

## Required GitHub Secrets

| Secret | Description |
|--------|-------------|
| `HCLOUD_TOKEN` | Hetzner Cloud API token |
| `HETZNER_SSH_PRIVATE_KEY` | SSH private key for VM access (ed25519 format) |

Note: VM IP addresses are no longer stored as secrets. The workflow retrieves them dynamically via `hcloud server ip` after auto-provisioning.

## Implementation Order

Tasks can be implemented in parallel where noted.

### Phase 1: Infrastructure Scripts

**Can run in parallel:**
1. Create `tests/infrastructure/scripts/provision-vms.sh`
2. Create `tests/infrastructure/scripts/provision.sh`
3. Create `tests/infrastructure/scripts/configure-vm.sh`
4. Create `tests/infrastructure/scripts/configure-hosts.sh`
5. Create `tests/infrastructure/scripts/reset-vm.sh`
6. Create `tests/infrastructure/scripts/lock.sh`
7. Create `tests/infrastructure/README.md`

### Phase 2: Test Fixtures and Contract Tests

**Sequential (shared conftest first):**
8. Update `tests/conftest.py` with shared fixtures

**Then in parallel:**
9. Create `tests/unit/conftest.py`
10. Create `tests/integration/conftest.py`
11. Create `tests/integration/__init__.py`
12. Create `tests/contract/test_executor_interface.py` (FR-003a: MockExecutor vs LocalExecutor/RemoteExecutor parity)

### Phase 3: CI/CD & Documentation

**Can run in parallel:**
13. Create `.github/workflows/test.yml`
14. Update `pyproject.toml` with pytest configuration
15. Create `docs/testing-playbook.md` (visual verification + feature tour per FR-018-FR-020)
16. Create `docs/testing-developer-guide.md` (fixtures, SSH/btrfs patterns, troubleshooting per FR-021-FR-024)
17. Create `docs/testing-ops-guide.md` (secrets, env vars, cost monitoring, runbooks per FR-025-FR-029)
18. Update `docs/testing-framework.md` (architecture diagrams, design rationale per FR-030-FR-032)
