#!/usr/bin/env bash
set -euo pipefail

# Main orchestrator script for provisioning pc-switcher test infrastructure
# Creates two VMs, configures them, sets up networking, and creates baseline snapshots
#
# IMPORTANT: This script can only be run from GitHub CI. Local provisioning is blocked
# to ensure all authorized SSH keys are properly configured from secrets.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Help text
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: $(basename "$0")

Provisions the complete pc-switcher test infrastructure:
1. Creates two VMs (pc1, pc2) in parallel
2. Configures both VMs in parallel
3. Sets up inter-VM networking
4. Creates baseline snapshots

Prerequisites:
  - HCLOUD_TOKEN environment variable must be set
  - SSH_AUTHORIZED_KEYS environment variable must contain all authorized public keys (newline-separated)
  - hcloud CLI installed and configured
  - ssh available
  - Must be run from GitHub CI (CI environment variable set)

The script is idempotent - skips if VMs already exist and are configured.

Example (CI only):
  export HCLOUD_TOKEN=your_token_here
  export SSH_AUTHORIZED_KEYS="\$(env | grep '^SSH_AUTHORIZED_KEY_' | sed 's/^[^=]*=//')"
  $(basename "$0")
EOF
    exit 0
fi

# Check if VM is configured (testuser can SSH)
check_vm_configured() {
    local vm_ip="$1"
    ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        "testuser@$vm_ip" "echo configured" 2>/dev/null && return 0
    return 1
}

# Get VM IPs (empty if VM doesn't exist)
echo "==> Checking existing infrastructure..."
PC1_IP=$(hcloud server ip pc1 2>/dev/null) || PC1_IP=""
PC2_IP=$(hcloud server ip pc2 2>/dev/null) || PC2_IP=""

# Early exit if VMs exist and are configured
if [[ -n "$PC1_IP" && -n "$PC2_IP" ]]; then
    echo "    Found existing VMs:"
    echo "      pc1: $PC1_IP"
    echo "      pc2: $PC2_IP"
    echo ""
    echo "    Checking if VMs are configured..."

    if check_vm_configured "$PC1_IP" && check_vm_configured "$PC2_IP"; then
        echo "    VMs are already configured. Skipping provisioning."
        echo ""
        echo "VMs ready for testing:"
        echo "  pc1: $PC1_IP"
        echo "  pc2: $PC2_IP"
        exit 0
    fi

    echo "    VMs exist but are not fully configured. Will reconfigure."
    echo ""
fi

# Provisioning needed - block if not CI
if [[ -z "$PC1_IP" || -z "$PC2_IP" ]]; then
    if [[ -z "${CI:-}" ]]; then
        echo "Error: VMs don't exist and provisioning is only allowed from GitHub CI." >&2
        echo "" >&2
        echo "To provision VMs, trigger the integration test workflow:" >&2
        echo "  gh workflow run test.yml" >&2
        echo "" >&2
        echo "Then wait for it to complete before running local tests." >&2
        exit 1
    fi
fi

# Check prerequisites for provisioning
echo "==> Checking prerequisites..."
: "${HCLOUD_TOKEN:?HCLOUD_TOKEN must be set}"
: "${SSH_AUTHORIZED_KEYS:?SSH_AUTHORIZED_KEYS must be set with all authorized public keys}"

if ! command -v hcloud >/dev/null 2>&1; then
    echo "Error: hcloud CLI not found. Please install it first." >&2
    exit 1
fi

if ! command -v ssh >/dev/null 2>&1; then
    echo "Error: ssh not found. Please install openssh-client." >&2
    exit 1
fi

# Count authorized keys
KEY_COUNT=$(echo "$SSH_AUTHORIZED_KEYS" | grep -c '^ssh-' || true)
echo "    Found $KEY_COUNT authorized SSH key(s)"

echo "Prerequisites check passed."
echo

# Create VMs in parallel
echo "==> Creating VMs in parallel..."
echo "    - pc1"
echo "    - pc2"
"$SCRIPT_DIR/create-vm.sh" pc1 &
PID1=$!
"$SCRIPT_DIR/create-vm.sh" pc2 &
PID2=$!

# Wait for both VM creation jobs
wait $PID1
wait $PID2
echo "VM creation completed."
echo

# Get VM IPs (refresh after potential creation)
echo "==> Retrieving VM IP addresses..."
PC1_IP=$(hcloud server ip pc1)
PC2_IP=$(hcloud server ip pc2)
echo "    pc1: $PC1_IP"
echo "    pc2: $PC2_IP"
echo

# Configure VMs in parallel
echo "==> Configuring VMs in parallel..."
echo "    - Installing packages and setting up users on both VMs"
"$SCRIPT_DIR/configure-vm.sh" "$PC1_IP" "$SSH_AUTHORIZED_KEYS" &
PID1=$!
"$SCRIPT_DIR/configure-vm.sh" "$PC2_IP" "$SSH_AUTHORIZED_KEYS" &
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
echo "  pc1: $PC1_IP"
echo "  pc2: $PC2_IP"
echo
echo "To SSH into VMs:"
echo "  ssh testuser@$PC1_IP"
echo "  ssh testuser@$PC2_IP"
echo
echo "To reset VMs to baseline state:"
echo "  $SCRIPT_DIR/reset-vm.sh $PC1_IP"
echo "  $SCRIPT_DIR/reset-vm.sh $PC2_IP"
echo
echo "To destroy VMs (manual cleanup):"
echo "  hcloud server delete pc1"
echo "  hcloud server delete pc2"
