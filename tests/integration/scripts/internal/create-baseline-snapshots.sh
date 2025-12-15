#!/usr/bin/env bash
set -euo pipefail

# Source common helpers
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

# Create baseline btrfs snapshots on both test VMs.
# These snapshots are used by reset-vm.sh to restore VMs to a known-good state between tests.
#
# See docs/testing-infrastructure.md for the full provisioning flow diagram.
#
# Usage: ./create-baseline-snapshots.sh
#
# Environment Variables:
#   HCLOUD_TOKEN    (required) Hetzner Cloud API token

# Help
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: $(basename "$0")

Create baseline btrfs snapshots on both test VMs (pc1, pc2).

These snapshots capture the clean state after provisioning and are used by
reset-vm.sh to restore VMs to a known-good state between test runs.

Snapshots created:
  /.snapshots/baseline/@      Read-only snapshot of root
  /.snapshots/baseline/@home  Read-only snapshot of /home

Environment Variables:
  HCLOUD_TOKEN    (required) Hetzner Cloud API token

Prerequisites:
  - Both VMs (pc1, pc2) must exist and be configured
  - hcloud CLI installed
EOF
    exit 0
fi

# Check for required environment variable
: "${HCLOUD_TOKEN:?HCLOUD_TOKEN environment variable must be set}"

# Configuration
readonly SSH_USER="testuser"
readonly VM1_NAME="pc1"
readonly VM2_NAME="pc2"
readonly SNAPSHOT_BASE_DIR="/.snapshots/baseline"
readonly ROOT_SNAPSHOT="${SNAPSHOT_BASE_DIR}/@"
readonly HOME_SNAPSHOT="${SNAPSHOT_BASE_DIR}/@home"

# Get VM IP address using hcloud CLI
get_vm_ip() {
    local vm_name="$1"
    local ip

    ip=$(hcloud server describe "$vm_name" -o json | jq -r '.public_net.ipv4.ip')

    if [[ -z "$ip" || "$ip" == "null" ]]; then
        log_error "Could not get IP address for VM: $vm_name"
        return 1
    fi

    echo "$ip"
}

# Create baseline snapshots on a single VM
# Key is already established by configure-hosts.sh, so we use ssh_run
# All operations consolidated into a single SSH call for performance
create_snapshots_on_vm() {
    local vm_name="$1"
    local vm_ip="$2"

    log_step "Creating baseline snapshots on $vm_name ($vm_ip)..."

    ssh_run "${SSH_USER}@${vm_ip}" 'sudo bash -s' << 'EOF'
set -euo pipefail

SNAPSHOT_BASE_DIR="/.snapshots/baseline"
OLD_SNAPSHOT_DIR="/.snapshots/old"
ROOT_SNAPSHOT="${SNAPSHOT_BASE_DIR}/@"
HOME_SNAPSHOT="${SNAPSHOT_BASE_DIR}/@home"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Helper: recursively delete a subvolume and its children
delete_subvol_recursive() {
    local path="$1"
    local child
    for child in $(btrfs subvolume list -o "$path" 2>/dev/null | awk '{print $NF}'); do
        # Convert relative path to absolute
        delete_subvol_recursive "$(dirname "$path")/$child"
    done
    btrfs subvolume delete "$path"
}

# Create directories if they don't exist
mkdir -p "$SNAPSHOT_BASE_DIR"
mkdir -p "$OLD_SNAPSHOT_DIR"

# Move existing baseline snapshots to old/ with timestamp (instead of deleting)
if btrfs subvolume show "$ROOT_SNAPSHOT" >/dev/null 2>&1; then
    echo "Archiving existing baseline: $ROOT_SNAPSHOT -> ${OLD_SNAPSHOT_DIR}/baseline_@_${TIMESTAMP}"
    mv "$ROOT_SNAPSHOT" "${OLD_SNAPSHOT_DIR}/baseline_@_${TIMESTAMP}"
fi
if btrfs subvolume show "$HOME_SNAPSHOT" >/dev/null 2>&1; then
    echo "Archiving existing baseline: $HOME_SNAPSHOT -> ${OLD_SNAPSHOT_DIR}/baseline_@home_${TIMESTAMP}"
    mv "$HOME_SNAPSHOT" "${OLD_SNAPSHOT_DIR}/baseline_@home_${TIMESTAMP}"
fi

# Create new read-only snapshots
echo "Creating read-only snapshot of / at $ROOT_SNAPSHOT..."
btrfs subvolume snapshot -r / "$ROOT_SNAPSHOT"

echo "Creating read-only snapshot of /home at $HOME_SNAPSHOT..."
btrfs subvolume snapshot -r /home "$HOME_SNAPSHOT"

# Rotate old baseline snapshots: keep 3 most recent, delete the rest
echo "Rotating old baseline snapshots (keeping 3 most recent)..."
for old_subvol in $(ls -1d ${OLD_SNAPSHOT_DIR}/baseline_@_* 2>/dev/null | sort -r | tail -n +4); do
    echo "Deleting old baseline: $old_subvol"
    delete_subvol_recursive "$old_subvol"
done
for old_subvol in $(ls -1d ${OLD_SNAPSHOT_DIR}/baseline_@home_* 2>/dev/null | sort -r | tail -n +4); do
    echo "Deleting old baseline: $old_subvol"
    delete_subvol_recursive "$old_subvol"
done

echo "Snapshots created successfully"
EOF
}

# Main execution
log_step "Getting VM IP addresses..."
vm1_ip=$(get_vm_ip "$VM1_NAME")
vm2_ip=$(get_vm_ip "$VM2_NAME")
log_info "$VM1_NAME: $vm1_ip"
log_info "$VM2_NAME: $vm2_ip"

# Create snapshots on both VMs
create_snapshots_on_vm "$VM1_NAME" "$vm1_ip"
create_snapshots_on_vm "$VM2_NAME" "$vm2_ip"

log_step "All baseline snapshots created successfully"
