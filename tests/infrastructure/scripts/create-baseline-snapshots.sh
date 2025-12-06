#!/usr/bin/env bash
set -euo pipefail

# Create baseline btrfs snapshots on both test VMs
# These snapshots are used by reset-vm.sh to restore VMs to a known-good state

# Check for required environment variable
if [[ -z "${HCLOUD_TOKEN:-}" ]]; then
    echo "Error: HCLOUD_TOKEN environment variable is not set" >&2
    exit 1
fi

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
        echo "Error: Could not get IP address for VM: $vm_name" >&2
        return 1
    fi

    echo "$ip"
}

# Create baseline snapshots on a single VM
create_snapshots_on_vm() {
    local vm_name="$1"
    local vm_ip="$2"

    echo "Creating baseline snapshots on $vm_name ($vm_ip)..."

    # Create the baseline directory if it doesn't exist
    ssh -o StrictHostKeyChecking=no "${SSH_USER}@${vm_ip}" \
        "sudo mkdir -p ${SNAPSHOT_BASE_DIR}"

    # Check if snapshots already exist and delete them
    if ssh "${SSH_USER}@${vm_ip}" "sudo btrfs subvolume list / | grep -q '${ROOT_SNAPSHOT}'"; then
        echo "  Deleting existing root snapshot..."
        ssh "${SSH_USER}@${vm_ip}" "sudo btrfs subvolume delete ${ROOT_SNAPSHOT}"
    fi

    if ssh "${SSH_USER}@${vm_ip}" "sudo btrfs subvolume list / | grep -q '${HOME_SNAPSHOT}'"; then
        echo "  Deleting existing home snapshot..."
        ssh "${SSH_USER}@${vm_ip}" "sudo btrfs subvolume delete ${HOME_SNAPSHOT}"
    fi

    # Create new read-only snapshots
    echo "  Creating read-only snapshot of / at ${ROOT_SNAPSHOT}..."
    ssh "${SSH_USER}@${vm_ip}" "sudo btrfs subvolume snapshot -r / ${ROOT_SNAPSHOT}"

    echo "  Creating read-only snapshot of /home at ${HOME_SNAPSHOT}..."
    ssh "${SSH_USER}@${vm_ip}" "sudo btrfs subvolume snapshot -r /home ${HOME_SNAPSHOT}"

    echo "Successfully created baseline snapshots on $vm_name"
}

# Main execution
main() {
    echo "========================================="
    echo "Creating baseline snapshots on test VMs"
    echo "========================================="
    echo

    # Get VM IPs
    echo "Getting VM IP addresses..."
    vm1_ip=$(get_vm_ip "$VM1_NAME")
    vm2_ip=$(get_vm_ip "$VM2_NAME")
    echo "  $VM1_NAME: $vm1_ip"
    echo "  $VM2_NAME: $vm2_ip"
    echo

    # Create snapshots on both VMs
    create_snapshots_on_vm "$VM1_NAME" "$vm1_ip"
    echo
    create_snapshots_on_vm "$VM2_NAME" "$vm2_ip"
    echo

    echo "========================================="
    echo "All baseline snapshots created successfully!"
    echo "========================================="
}

main "$@"
