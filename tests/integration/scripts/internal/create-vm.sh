#!/usr/bin/env bash

set -euo pipefail

# Source common helpers
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

# create-vm.sh
# Creates a single Hetzner Cloud VM and installs Ubuntu 24.04 with btrfs filesystem
# using Hetzner's rescue mode and installimage tool.
#
# See docs/ops/testing-architecture.md for the full provisioning flow diagram.
#
# Usage:
#   ./create-vm.sh <VM_NAME>
#
# Arguments:
#   VM_NAME    Name for the VM (e.g., "pc1")
#
# Environment Variables:
#   HCLOUD_TOKEN           (required) Hetzner Cloud API token
#
# Prerequisites:
#   - SSH key 'pc-switcher-test-key' must already exist in Hetzner Cloud
#
# Example:
#   ./create-vm.sh pc1

# Configuration
readonly LOCATION="fsn1"
readonly SERVER_TYPE="cx23"
readonly SSH_KEY_NAME="pc-switcher-test-key"
readonly TIMEOUT_RESCUE=180
readonly TIMEOUT_INSTALL=600
readonly TIMEOUT_REBOOT=120

# Log prefix will be set after VM_NAME is known (for parallel execution clarity)
LOG_PREFIX=""

show_help() {
    cat << EOF
Usage: $0 <VM_NAME>

Creates a single Hetzner Cloud VM and installs Ubuntu 24.04 with btrfs filesystem.

Arguments:
  VM_NAME    Name for the VM (e.g., "pc1")

Environment Variables:
  HCLOUD_TOKEN       (required) Hetzner Cloud API token

Prerequisites:
  - SSH key 'pc-switcher-test-key' must already exist in Hetzner Cloud

Examples:
  $0 pc1
  $0 pc2

VM Specifications:
  - Server Type: CX23 (2 shared vCPUs, 4GB RAM)
  - Location: fsn1 (Falkenstein, Germany)
  - OS: Ubuntu 24.04 LTS
  - Filesystem: btrfs with @ (root), @home, and @snapshots subvolumes
  - Cost: ~€3.50/month

The script is idempotent - if the VM already exists, creation is skipped.
EOF
}

# Check prerequisites
check_prerequisites() {
    log_step_prefixed "Checking prerequisites..."

    # Check if hcloud CLI is installed
    if ! command -v hcloud &> /dev/null; then
        log_error_prefixed "hcloud CLI not found. Install it with: brew install hcloud (macOS) or https://github.com/hetznercloud/cli"
        exit 1
    fi

    # Check if HCLOUD_TOKEN is set
    if [[ -z "${HCLOUD_TOKEN:-}" ]]; then
        log_error_prefixed "HCLOUD_TOKEN environment variable is required"
        echo ""
        echo "Get your API token from: https://console.hetzner.cloud/"
        echo "Then set it with: export HCLOUD_TOKEN=your-token-here"
        exit 1
    fi

    log_info_prefixed "Prerequisites OK"
}

# Check if VM already exists
vm_exists() {
    local vm_name="$1"
    hcloud server describe "$vm_name" &> /dev/null
}

# Create VM
create_vm() {
    local vm_name="$1"

    log_step_prefixed "Creating VM '$vm_name'..."
    log_info_prefixed "Type: $SERVER_TYPE (2 vCPU, 4GB RAM)"
    log_info_prefixed "Location: $LOCATION"

    hcloud server create \
        --name "$vm_name" \
        --type "$SERVER_TYPE" \
        --location "$LOCATION" \
        --image "ubuntu-24.04" \
        --ssh-key "$SSH_KEY_NAME"

    log_info_prefixed "VM '$vm_name' created"
}

# Enable rescue mode
enable_rescue_mode() {
    local vm_name="$1"

    log_step_prefixed "Enabling rescue mode for '$vm_name'..."
    # Must explicitly specify SSH key for rescue mode access
    hcloud server enable-rescue "$vm_name" --type "linux64" --ssh-key "$SSH_KEY_NAME" --quiet
    log_info_prefixed "Rescue mode enabled"
}

# Reboot VM
reboot_vm() {
    local vm_name="$1"

    log_step_prefixed "Rebooting '$vm_name' into rescue mode..."
    hcloud server reboot "$vm_name"
}

# Run SSH command and prefix all output with VM name
# Uses ssh_run from common.sh for subsequent connections
run_ssh() {
    local vm_ip="$1"
    shift
    ssh_run "root@$vm_ip" "$@" 2>&1 | while IFS= read -r line; do
        echo -e "   ${LOG_PREFIX} $line"
    done
    return "${PIPESTATUS[0]}"
}

# Wait for SSH to become available after a phase transition
# This is a wrapper that looks up VM IP and calls the common wait_for_ssh.
# All phase transitions in create-vm.sh require REMOVE_KEY since the host key changes.
wait_for_ssh_vm() {
    local vm_name="$1"
    local timeout="$2"

    log_step_prefixed "Waiting for SSH on '$vm_name' (timeout: ${timeout}s)..."

    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")
    log_info_prefixed "VM IP: $vm_ip"

    # Use the common wait_for_ssh with REMOVE_KEY for phase transitions
    wait_for_ssh "root@$vm_ip" "$timeout" REMOVE_KEY
}

# Run installimage to install Ubuntu 24.04 with btrfs
run_installimage() {
    local vm_name="$1"

    log_step_prefixed "Running installimage to install Ubuntu 24.04 with btrfs..."

    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")

    # Create installimage config
    # NOTE: ESP partition requires minimum 256MB per installimage validation
    local config
    config=$(cat << 'CONFIGEOF'
DRIVE1 /dev/sda
USE_KERNEL_MODE_SETTING yes
PART /boot/efi esp 256M
PART btrfs.1 btrfs all
SUBVOL btrfs.1 @ /
SUBVOL btrfs.1 @home /home
SUBVOL btrfs.1 @snapshots /.snapshots
IMAGE /root/.oldroot/nfs/install/../images/Ubuntu-2404-noble-amd64-base.tar.gz
CONFIGEOF
)
    # Add hostname separately to avoid quoting issues
    config="HOSTNAME $vm_name
$config"

    log_info_prefixed "Writing config to /autosetup..."
    # Write config to /autosetup - installimage detects this and runs in automatic mode
    ssh_run "root@$vm_ip" "cat > /autosetup" <<< "$config"

    log_info_prefixed "Running installimage (this may take 5-10 minutes)..."
    # Use bash -i to get the installimage alias loaded from .bashrc
    ssh_run "root@$vm_ip" 'bash -i -c "installimage"' 2>&1 | while IFS= read -r line; do
        echo -e "   ${LOG_PREFIX} $line"
    done
    local exit_code="${PIPESTATUS[0]}"
    if [[ "$exit_code" -ne 0 ]]; then
        log_error_prefixed "installimage failed with exit code $exit_code"
        return 1
    fi

    log_info_prefixed "Installation complete"
}

# Reboot into new system
reboot_into_system() {
    local vm_name="$1"

    log_step_prefixed "Rebooting '$vm_name' into new system..."

    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")

    # Disable rescue mode first
    log_info_prefixed "Disabling rescue mode..."
    hcloud server disable-rescue "$vm_name"

    # Reboot via SSH (rescue mode)
    ssh_run "root@$vm_ip" "reboot" || true  # SSH connection will drop, so ignore error

    log_info_prefixed "Waiting for system to reboot..."
    sleep 10
}

# Verify new system
verify_system() {
    local vm_name="$1"

    log_step_prefixed "Verifying new system..."

    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")

    # Wait for SSH to come back up
    wait_for_ssh_vm "$vm_name" "$TIMEOUT_REBOOT"

    # Verify btrfs filesystem
    log_info_prefixed "Checking btrfs filesystem..."
    local btrfs_check
    btrfs_check=$(ssh_run "root@$vm_ip" "df -T / | tail -n1 | awk '{print \$2}'")

    if [[ "$btrfs_check" != "btrfs" ]]; then
        log_error_prefixed "Root filesystem is not btrfs (got: $btrfs_check)"
        return 1
    fi

    # Verify subvolumes
    log_info_prefixed "Checking subvolumes..."
    local subvols
    subvols=$(ssh_run "root@$vm_ip" "btrfs subvolume list / | awk '{print \$NF}' | sort")

    local expected_subvols=$'@\n@home\n@snapshots'

    if [[ "$subvols" != "$expected_subvols" ]]; then
        log_error_prefixed "Subvolumes do not match expected configuration"
        echo "Expected:"
        echo "$expected_subvols"
        echo "Got:"
        echo "$subvols"
        return 1
    fi

    log_step_prefixed "System verification successful"
    log_info_prefixed "OS: Ubuntu 24.04 LTS"
    log_info_prefixed "Filesystem: btrfs"
    log_info_prefixed "Subvolumes: @, @home, @snapshots"
}

# Main function
main() {
    # Parse arguments
    if [[ $# -eq 0 ]] || [[ "$1" == "-h" ]] || [[ "$1" == "--help" ]]; then
        show_help
        exit 0
    fi

    local vm_name="$1"

    # Set log prefix for parallel execution clarity
    LOG_PREFIX=" ${CYAN}[${vm_name}]${NC}"

    log_step_prefixed "Creating VM: $vm_name"

    # Check prerequisites
    check_prerequisites

    # Check if VM already exists
    if vm_exists "$vm_name"; then
        log_warn_prefixed "VM '$vm_name' already exists - skipping creation"
        log_info_prefixed "To recreate, first delete with: hcloud server delete $vm_name"
        exit 0
    fi

    # Create VM
    create_vm "$vm_name"

    # Wait for VM to be ready
    sleep 5
    wait_for_ssh_vm "$vm_name" "$TIMEOUT_INSTALL"

    # Enable rescue mode
    enable_rescue_mode "$vm_name"

    # Reboot into rescue mode
    reboot_vm "$vm_name"

    # Wait for rescue mode to be ready
    sleep 10
    wait_for_ssh_vm "$vm_name" "$TIMEOUT_RESCUE"

    # Run installimage
    run_installimage "$vm_name"

    # Reboot into new system
    reboot_into_system "$vm_name"

    # Verify system
    verify_system "$vm_name"

    # Get VM details
    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")

    log_step_prefixed "VM creation complete!"
    log_info_prefixed "VM Name: $vm_name"
    log_info_prefixed "IP Address: $vm_ip"
    log_info_prefixed "SSH Access: ssh root@$vm_ip"
    echo ""
    log_info_prefixed "Next steps:"
    log_info_prefixed "  1. Configure the VM (users, packages, baseline snapshots)"
    log_info_prefixed "  2. Test SSH connectivity"
}

# Run main function
main "$@"
