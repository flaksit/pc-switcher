#!/usr/bin/env bash

set -euo pipefail

# create-vm.sh
# Creates a single Hetzner Cloud VM and installs Ubuntu 24.04 with btrfs filesystem
# using Hetzner's rescue mode and installimage tool.
#
# See docs/testing-infrastructure.md for the full provisioning flow diagram.
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

# Colors for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly CYAN='\033[0;36m'
readonly NC='\033[0m' # No Color

# Log prefix will be set after VM_NAME is known
LOG_PREFIX=""

log_step() { echo -e "${GREEN}==>${NC}${LOG_PREFIX} $*"; }
log_info() { echo -e "   ${LOG_PREFIX} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC}${LOG_PREFIX} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC}${LOG_PREFIX} $*" >&2; }

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
    log_step "Checking prerequisites..."

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

    log_step "Creating VM '$vm_name'..."
    log_info "Type: $SERVER_TYPE (2 vCPU, 4GB RAM)"
    log_info "Location: $LOCATION"

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

    log_step "Enabling rescue mode for '$vm_name'..."
    # Must explicitly specify SSH key for rescue mode access
    hcloud server enable-rescue "$vm_name" --type "linux64" --ssh-key "$SSH_KEY_NAME" --quiet
    log_info "Rescue mode enabled"
}

# Reboot VM
reboot_vm() {
    local vm_name="$1"

    log_step "Rebooting '$vm_name' into rescue mode..."
    hcloud server reboot "$vm_name"
}

# Common SSH options
readonly SSH_OPTS="-o ConnectTimeout=10 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes"

# Run SSH command and prefix all output with VM name
run_ssh() {
    local vm_ip="$1"
    shift
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "root@$vm_ip" "$@" 2>&1 | while IFS= read -r line; do
        echo -e "   ${LOG_PREFIX} $line"
    done
    return "${PIPESTATUS[0]}"
}

# Wait for SSH to become available
wait_for_ssh() {
    local vm_name="$1"
    local timeout="$2"

    log_step "Waiting for SSH on '$vm_name' (timeout: ${timeout}s)..."

    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")
    log_info "VM IP: $vm_ip"

    local elapsed=0
    local attempt=0
    while ((elapsed < timeout)); do
        ((++attempt))  # pre-increment to avoid exit code 1 when attempt=0
        # shellcheck disable=SC2086
        if ssh $SSH_OPTS "root@$vm_ip" "echo SSH ready" &> /dev/null; then
            log_info "SSH is ready on $vm_ip (after ${elapsed}s, attempt $attempt)"
            return 0
        fi
        sleep 5
        ((elapsed += 5))
        if ((attempt % 6 == 0)); then
            log_info "Still waiting for SSH... (${elapsed}s elapsed)"
        fi
    done

    log_error "SSH did not become available within ${timeout}s"
    log_error "Attempting verbose SSH for diagnostics..."
    # Run one verbose attempt to help diagnose
    # shellcheck disable=SC2086
    ssh -v $SSH_OPTS "root@$vm_ip" "echo SSH ready" 2>&1 | tail -30 || true
    return 1
}

# Run installimage to install Ubuntu 24.04 with btrfs
run_installimage() {
    local vm_name="$1"

    log_step "Running installimage to install Ubuntu 24.04 with btrfs..."

    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")

    # Debug: Check if we're actually in rescue mode
    log_info "Verifying rescue mode environment..."
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "root@$vm_ip" "hostname; cat /etc/os-release 2>/dev/null | head -3 || true; which installimage || echo 'installimage not in PATH'; ls -la /root/.oldroot/nfs/install/installimage 2>/dev/null || echo 'installimage not at expected path'" 2>&1 | while IFS= read -r line; do
        echo "   ${LOG_PREFIX} [DEBUG] $line"
    done

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

    log_info "Creating installimage config at /tmp/installimage.conf..."
    # shellcheck disable=SC2086
    # Write to /tmp first, then use -c to copy to /autosetup
    ssh $SSH_OPTS "root@$vm_ip" "cat > /tmp/installimage.conf" <<< "$config"

    # Verify config was written
    # shellcheck disable=SC2086
    log_info "Verifying config was written..."
    ssh $SSH_OPTS "root@$vm_ip" "ls -la /tmp/installimage.conf && head -3 /tmp/installimage.conf"

    log_info "Running installimage (this may take 5-10 minutes)..."
    # Use -a (automatic mode) with -c (config file) to bypass interactive prompts
    # Search functions.sh to understand what sets CANCELLED=true
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "root@$vm_ip" "
        export TERM=xterm
        echo '[DEBUG] Searching for what sets CANCELLED=true...'
        grep -n 'CANCELLED=' /root/.oldroot/nfs/install/*.sh 2>/dev/null || true
        echo '[DEBUG] Looking at validate_vars function...'
        grep -A 30 'validate_vars' /root/.oldroot/nfs/install/functions.sh 2>/dev/null | head -50 || true
        echo '[DEBUG] Checking if there is a dialog call in validate_vars...'
        grep -B5 -A10 'CANCELLED' /root/.oldroot/nfs/install/functions.sh 2>/dev/null | head -40 || true
        echo '[DEBUG] Running installimage...'
        /root/.oldroot/nfs/install/installimage -a -c /tmp/installimage.conf < /dev/null > /tmp/installimage.log 2>&1
        EXIT_CODE=\$?
        echo '[DEBUG] Exit code:' \$EXIT_CODE
        echo '[DEBUG] Full install log:'
        cat /tmp/installimage.log
        exit \$EXIT_CODE
    " 2>&1 | while IFS= read -r line; do
        echo -e "   ${LOG_PREFIX} $line"
    done
    local exit_code="${PIPESTATUS[0]}"
    if [[ "$exit_code" -ne 0 ]]; then
        log_error "installimage failed with exit code $exit_code"
        log_error "Fetching full log..."
        # shellcheck disable=SC2086
        ssh $SSH_OPTS "root@$vm_ip" "cat /tmp/installimage.log" 2>&1 | while IFS= read -r line; do
            echo -e "   ${LOG_PREFIX} [LOG] $line"
        done
        return 1
    fi

    log_info "Installation complete"
}

# Reboot into new system
reboot_into_system() {
    local vm_name="$1"

    log_step "Rebooting '$vm_name' into new system..."

    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")

    # Disable rescue mode first
    log_info "Disabling rescue mode..."
    hcloud server disable-rescue "$vm_name"

    # Reboot via SSH (rescue mode)
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "root@$vm_ip" "reboot" || true  # SSH connection will drop, so ignore error

    log_info "Waiting for system to reboot..."
    sleep 10
}

# Verify new system
verify_system() {
    local vm_name="$1"

    log_step "Verifying new system..."

    local vm_ip
    vm_ip=$(hcloud server ip "$vm_name")

    # Wait for SSH to come back up
    wait_for_ssh "$vm_name" "$TIMEOUT_REBOOT"

    # Verify btrfs filesystem
    log_info "Checking btrfs filesystem..."
    local btrfs_check
    # shellcheck disable=SC2086
    btrfs_check=$(ssh $SSH_OPTS "root@$vm_ip" "df -T / | tail -n1 | awk '{print \$2}'")

    if [[ "$btrfs_check" != "btrfs" ]]; then
        log_error "Root filesystem is not btrfs (got: $btrfs_check)"
        return 1
    fi

    # Verify subvolumes
    log_info "Checking subvolumes..."
    local subvols
    # shellcheck disable=SC2086
    subvols=$(ssh $SSH_OPTS "root@$vm_ip" "btrfs subvolume list / | awk '{print \$NF}' | sort")

    local expected_subvols=$'@\n@home\n@snapshots'

    if [[ "$subvols" != "$expected_subvols" ]]; then
        log_error "Subvolumes do not match expected configuration"
        echo "Expected:"
        echo "$expected_subvols"
        echo "Got:"
        echo "$subvols"
        return 1
    fi

    log_step "System verification successful"
    log_info "OS: Ubuntu 24.04 LTS"
    log_info "Filesystem: btrfs"
    log_info "Subvolumes: @, @home, @snapshots"
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

    log_step "Creating VM: $vm_name"

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
    wait_for_ssh "$vm_name" "$TIMEOUT_INSTALL"

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

    log_step "VM creation complete!"
    log_info "VM Name: $vm_name"
    log_info "IP Address: $vm_ip"
    log_info "SSH Access: ssh root@$vm_ip"
    echo ""
    log_info "Next steps:"
    log_info "  1. Configure the VM (users, packages, baseline snapshots)"
    log_info "  2. Test SSH connectivity"
}

# Run main function
main "$@"
