#!/usr/bin/env bash
set -euo pipefail

# Source common SSH helpers
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/ssh-common.sh"

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

# Colors for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m' # No Color

log_step() { echo -e "${GREEN}==>${NC} $*"; }
log_info() { echo -e "    $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

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
create_snapshots_on_vm() {
    local vm_name="$1"
    local vm_ip="$2"

    log_step "Creating baseline snapshots on $vm_name ($vm_ip)..."

    # Create the baseline directory if it doesn't exist
    ssh_run "${SSH_USER}@${vm_ip}" "sudo mkdir -p ${SNAPSHOT_BASE_DIR}"

    # Check if snapshots already exist and delete them
    if ssh_run "${SSH_USER}@${vm_ip}" "sudo btrfs subvolume list / | grep -q '${ROOT_SNAPSHOT}'"; then
        log_info "Deleting existing root snapshot..."
        ssh_run "${SSH_USER}@${vm_ip}" "sudo btrfs subvolume delete ${ROOT_SNAPSHOT}"
    fi

    if ssh_run "${SSH_USER}@${vm_ip}" "sudo btrfs subvolume list / | grep -q '${HOME_SNAPSHOT}'"; then
        log_info "Deleting existing home snapshot..."
        ssh_run "${SSH_USER}@${vm_ip}" "sudo btrfs subvolume delete ${HOME_SNAPSHOT}"
    fi

    # Create new read-only snapshots
    log_info "Creating read-only snapshot of / at ${ROOT_SNAPSHOT}..."
    ssh_run "${SSH_USER}@${vm_ip}" "sudo btrfs subvolume snapshot -r / ${ROOT_SNAPSHOT}"

    log_info "Creating read-only snapshot of /home at ${HOME_SNAPSHOT}..."
    ssh_run "${SSH_USER}@${vm_ip}" "sudo btrfs subvolume snapshot -r /home ${HOME_SNAPSHOT}"

    log_info "Snapshots created successfully"
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
