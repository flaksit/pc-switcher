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
  6. Installs Hetzner cloud server equivalents:
     - QEMU Guest Agent (for Hetzner Cloud integration)
     - Hetzner Cloud Utils (hc-utils)
     - fail2ban (SSH brute-force protection)
     - SSH hardening (disable password auth, only testuser allowed)
     - unattended-upgrades (with automatic reboot)
     - ufw firewall (SSH only by default)
  7. Creates baseline snapshots for test reset

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

# Get server ID
SERVER_ID=$(hcloud server list -o noheader -o columns=id,name | grep "$SERVER_NAME" | awk '{print $1}')
if [[ -z "$SERVER_ID" ]]; then
    echo "ERROR: Server '$SERVER_NAME' not found" >&2
    exit 1
fi

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
# Remove old host key if exists, because rescue mode uses a different host key
ssh-keygen -R "$SERVER_IP" 2>/dev/null || true
# Wait until we can connect via SSH (accept-new auto-adds the host key)
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

# Wait for new system and update known_hosts
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
