#!/usr/bin/env bash

set -euo pipefail

# create-vm.sh
# Creates a single Hetzner Cloud VM and installs Ubuntu 24.04 with btrfs filesystem using installimage in rescue mode.
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
readonly TIMEOUT_INSTALL=300
readonly TIMEOUT_REBOOT=120

# Colors for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m' # No Color

# Helper functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
}

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
    log_info "Checking prerequisites..."

    # Check if hcloud CLI is installed
    if ! command -v hcloud &> /dev/null; then
        log_error "hcloud CLI not found. Install it with: brew install hcloud (macOS) or https://github.com/hetznercloud/cli"
        exit 1
    fi

    # Check if HCLOUD_TOKEN is set
    if [[ -z "${HCLOUD_TOKEN:-}" ]]; then
        log_error "HCLOUD_TOKEN environment variable is required"
        echo ""
        echo "Get your API token from: https://console.hetzner.cloud/"
        echo "Then set it with: export HCLOUD_TOKEN=your-token-here"
        exit 1
    fi

    log_info "Prerequisites OK"
}

# Check if VM already exists
vm_exists() {
    local vm_name="$1"
    hcloud server describe "$vm_name" &> /dev/null
}

# Create VM
create_vm() {
    local vm_name="$1"

    log_info "Creating VM '$vm_name'..."
    log_info "  Type: $SERVER_TYPE (2 vCPU, 4GB RAM)"
    log_info "  Location: $LOCATION"

    hcloud server create \
        --name "$vm_name" \
        --type "$SERVER_TYPE" \
        --location "$LOCATION" \
        --image "ubuntu-24.04" \
        --ssh-key "$SSH_KEY_NAME"

    log_info "VM '$vm_name' created"
}

# Enable rescue mode
enable_rescue_mode() {
    local vm_name="$1"

    log_info "Enabling rescue mode for '$vm_name'..."
    hcloud server enable-rescue "$vm_name" --type "linux64"
    log_info "Rescue mode enabled"
}

# Reboot VM
reboot_vm() {
    local vm_name="$1"

    log_info "Rebooting '$vm_name' into rescue mode..."
    hcloud server reboot "$vm_name"
}

# Wait for SSH to become available
wait_for_ssh() {
    local vm_name="$1"
    local timeout="$2"

    log_info "Waiting for SSH on '$vm_name' (timeout: ${timeout}s)..."

    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")

    local elapsed=0
    while ((elapsed < timeout)); do
        if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
               "root@$vm_ip" "echo SSH ready" &> /dev/null; then
            log_info "SSH is ready on $vm_ip"
            return 0
        fi
        sleep 5
        ((elapsed += 5))
    done

    log_error "SSH did not become available within ${timeout}s"
    return 1
}

# Run installimage to install Ubuntu 24.04 with btrfs
run_installimage() {
    local vm_name="$1"

    log_info "Running installimage to install Ubuntu 24.04 with btrfs..."

    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")

    # Create installimage config
    local config
    config=$(cat << EOF
DRIVE1 /dev/sda
USE_KERNEL_MODE_SETTING yes
HOSTNAME $vm_name
PART /boot/efi esp 128M
PART btrfs.1 btrfs all
SUBVOL btrfs.1 @ /
SUBVOL btrfs.1 @home /home
SUBVOL btrfs.1 @snapshots /.snapshots
IMAGE /root/.oldroot/nfs/install/../images/Ubuntu-2404-noble-amd64-base.tar.gz
EOF
)

    log_info "Creating installimage config..."
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "root@$vm_ip" \
        "cat > /tmp/installimage.conf" <<< "$config"

    log_info "Running installimage (this may take 5-10 minutes)..."
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "root@$vm_ip" \
        "installimage -a -c /tmp/installimage.conf"

    log_info "Installation complete"
}

# Reboot into new system
reboot_into_system() {
    local vm_name="$1"

    log_info "Rebooting '$vm_name' into new system..."

    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")

    # Disable rescue mode first
    log_info "Disabling rescue mode..."
    hcloud server disable-rescue "$vm_name"

    # Reboot via SSH (rescue mode)
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "root@$vm_ip" \
        "reboot" || true  # SSH connection will drop, so ignore error

    log_info "Waiting for system to reboot..."
    sleep 10
}

# Verify new system
verify_system() {
    local vm_name="$1"

    log_info "Verifying new system..."

    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")

    # Wait for SSH to come back up
    wait_for_ssh "$vm_name" "$TIMEOUT_REBOOT"

    # Verify btrfs filesystem
    log_info "Checking btrfs filesystem..."
    local btrfs_check
    btrfs_check=$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "root@$vm_ip" \
        "df -T / | tail -n1 | awk '{print \$2}'")

    if [[ "$btrfs_check" != "btrfs" ]]; then
        log_error "Root filesystem is not btrfs (got: $btrfs_check)"
        return 1
    fi

    # Verify subvolumes
    log_info "Checking subvolumes..."
    local subvols
    subvols=$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "root@$vm_ip" \
        "btrfs subvolume list / | awk '{print \$NF}' | sort")

    local expected_subvols=$'@\n@home\n@snapshots'

    if [[ "$subvols" != "$expected_subvols" ]]; then
        log_error "Subvolumes do not match expected configuration"
        echo "Expected:"
        echo "$expected_subvols"
        echo "Got:"
        echo "$subvols"
        return 1
    fi

    log_info "System verification successful"
    log_info "  OS: Ubuntu 24.04 LTS"
    log_info "  Filesystem: btrfs"
    log_info "  Subvolumes: @, @home, @snapshots"
}

# Main function
main() {
    # Parse arguments
    if [[ $# -eq 0 ]] || [[ "$1" == "-h" ]] || [[ "$1" == "--help" ]]; then
        show_help
        exit 0
    fi

    local vm_name="$1"

    log_info "=== Creating VM: $vm_name ==="

    # Check prerequisites
    check_prerequisites

    # Check if VM already exists
    if vm_exists "$vm_name"; then
        log_warn "VM '$vm_name' already exists - skipping creation"
        log_info "To recreate, first delete with: hcloud server delete $vm_name"
        exit 0
    fi

    # Create VM
    create_vm "$vm_name"

    # Wait for VM to be ready
    sleep 5
    wait_for_ssh "$vm_name" "$TIMEOUT_RESCUE"

    # Enable rescue mode
    enable_rescue_mode "$vm_name"

    # Reboot into rescue mode
    reboot_vm "$vm_name"

    # Wait for rescue mode to be ready
    sleep 10
    wait_for_ssh "$vm_name" "$TIMEOUT_RESCUE"

    # Run installimage
    run_installimage "$vm_name"

    # Reboot into new system
    reboot_into_system "$vm_name"

    # Verify system
    verify_system "$vm_name"

    # Get VM details
    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")

    log_info "=== VM Creation Complete ==="
    log_info "VM Name: $vm_name"
    log_info "IP Address: $vm_ip"
    log_info "SSH Access: ssh root@$vm_ip"
    log_info ""
    log_info "Next steps:"
    log_info "  1. Configure the VM (users, packages, baseline snapshots)"
    log_info "  2. Test SSH connectivity"
}

# Run main function
main "$@"
