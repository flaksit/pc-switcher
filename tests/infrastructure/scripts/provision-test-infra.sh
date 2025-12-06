#!/usr/bin/env bash
set -euo pipefail

# Main orchestrator script for provisioning pc-switcher test infrastructure
# Creates two VMs, configures them, sets up networking, and creates baseline snapshots

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Help text
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: $(basename "$0")

Provisions the complete pc-switcher test infrastructure:
1. Creates two VMs (pc-switcher-pc1, pc-switcher-pc2) in parallel
2. Configures both VMs in parallel
3. Sets up inter-VM networking
4. Creates baseline snapshots

Prerequisites:
  - HCLOUD_TOKEN environment variable must be set
  - SSH_PUBLIC_KEY environment variable (optional, defaults to ~/.ssh/id_ed25519.pub)
  - hcloud CLI installed and configured
  - ssh available

The script is idempotent - can be run multiple times safely.

Example:
  export HCLOUD_TOKEN=your_token_here
  $(basename "$0")
EOF
    exit 0
fi

# Check prerequisites
echo "==> Checking prerequisites..."
: "${HCLOUD_TOKEN:?HCLOUD_TOKEN must be set}"
SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY:-$HOME/.ssh/id_ed25519.pub}"

if [[ ! -f "$SSH_PUBLIC_KEY" ]]; then
    echo "Error: SSH public key not found at $SSH_PUBLIC_KEY" >&2
    echo "Set SSH_PUBLIC_KEY environment variable to the correct path" >&2
    exit 1
fi

if ! command -v hcloud >/dev/null 2>&1; then
    echo "Error: hcloud CLI not found. Please install it first." >&2
    exit 1
fi

if ! command -v ssh >/dev/null 2>&1; then
    echo "Error: ssh not found. Please install openssh-client." >&2
    exit 1
fi

echo "Prerequisites check passed."
echo

# Create VMs in parallel
echo "==> Creating VMs in parallel..."
echo "    - pc-switcher-pc1"
echo "    - pc-switcher-pc2"
"$SCRIPT_DIR/create-vm.sh" pc-switcher-pc1 &
PID1=$!
"$SCRIPT_DIR/create-vm.sh" pc-switcher-pc2 &
PID2=$!

# Wait for both VM creation jobs
wait $PID1
wait $PID2
echo "VM creation completed."
echo

# Get VM IPs
echo "==> Retrieving VM IP addresses..."
PC1_IP=$(hcloud server ip pc-switcher-pc1)
PC2_IP=$(hcloud server ip pc-switcher-pc2)
echo "    pc-switcher-pc1: $PC1_IP"
echo "    pc-switcher-pc2: $PC2_IP"
echo

# Configure VMs in parallel
echo "==> Configuring VMs in parallel..."
echo "    - Installing packages and setting up btrfs on both VMs"
"$SCRIPT_DIR/configure-vm.sh" "$PC1_IP" "$(cat "$SSH_PUBLIC_KEY")" &
PID1=$!
"$SCRIPT_DIR/configure-vm.sh" "$PC2_IP" "$(cat "$SSH_PUBLIC_KEY")" &
PID2=$!

# Wait for both configuration jobs
wait $PID1
wait $PID2
echo "VM configuration completed."
echo

# Configure inter-VM networking
echo "==> Configuring inter-VM networking..."
"$SCRIPT_DIR/configure-hosts.sh"
echo "Inter-VM networking configured."
echo

# Create baseline snapshots
echo "==> Creating baseline snapshots..."
"$SCRIPT_DIR/create-baseline-snapshots.sh"
echo "Baseline snapshots created."
echo

echo "==> Test infrastructure provisioning complete!"
echo
echo "VMs ready for testing:"
echo "  pc-switcher-pc1: $PC1_IP"
echo "  pc-switcher-pc2: $PC2_IP"
echo
echo "To SSH into VMs:"
echo "  ssh root@$PC1_IP"
echo "  ssh root@$PC2_IP"
echo
echo "To reset VMs to baseline state:"
echo "  $SCRIPT_DIR/reset-vm.sh $PC1_IP"
echo "  $SCRIPT_DIR/reset-vm.sh $PC2_IP"
echo
echo "To destroy VMs (manual cleanup):"
echo "  hcloud server delete pc-switcher-pc1"
echo "  hcloud server delete pc-switcher-pc2"
