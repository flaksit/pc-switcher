#!/usr/bin/env bash
set -euo pipefail

# Source common SSH helpers
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/ssh-common.sh"

# Reset a single VM to its baseline btrfs snapshot state
# This script restores the VM to the known-good baseline created during provisioning
#
# Usage: ./reset-vm.sh <VM_HOST>
#
# Arguments:
#   VM_HOST    VM hostname or IP address to reset

# Help
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: $(basename "$0") <VM_HOST>

Reset a VM to its baseline btrfs snapshot state.

This script:
  1. Validates baseline snapshots exist
  2. Cleans up test artifacts
  3. Replaces active subvolumes with baseline snapshots
  4. Reboots the VM
  5. Cleans up old subvolumes

Arguments:
  VM_HOST    VM hostname or IP address to reset

Environment Variables:
  PC_SWITCHER_TEST_USER    SSH user for VM access (default: testuser)

Examples:
  $(basename "$0") 192.168.1.100
  $(basename "$0") pc1

Prerequisites:
  - Baseline snapshots must exist (/.snapshots/baseline/@ and /.snapshots/baseline/@home)
  - Run provision-test-infra.sh if baseline snapshots are missing
EOF
    exit 0
fi

if [[ "$#" -ne 1 ]]; then
    echo "Error: Expected 1 argument, got $#" >&2
    echo "Usage: $(basename "$0") <VM_HOST>" >&2
    echo "Run with -h for help" >&2
    exit 1
fi

# Colors for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m' # No Color

log_step() { echo -e "${GREEN}==>${NC} $*"; }
log_info() { echo "    $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

readonly VM_HOST="$1"
readonly SSH_USER="${PC_SWITCHER_TEST_USER:-testuser}"

# SSH connection helper
# First connection uses ssh_accept_new (test runner may have empty known_hosts)
# After first connection, key is stored and subsequent calls verify it
ssh_vm() {
    ssh_run "${SSH_USER}@${VM_HOST}" "$@"
}

log_step "Resetting VM: $VM_HOST"
log_info "SSH User: $SSH_USER"

# Establish SSH connection (accept new key if not in known_hosts)
# Test runner may have empty known_hosts or correct key from provisioning
log_info "Establishing SSH connection..."
ssh_accept_new "${SSH_USER}@${VM_HOST}" true

# Step 1: Validate baseline snapshots exist
log_step "Validating baseline snapshots..."
if ! ssh_vm "sudo btrfs subvolume show /.snapshots/baseline/@ >/dev/null 2>&1"; then
    log_error "Baseline snapshot /.snapshots/baseline/@ not found on $VM_HOST"
    log_info "Run provision-test-infra.sh to create baseline snapshots"
    exit 1
fi

if ! ssh_vm "sudo btrfs subvolume show /.snapshots/baseline/@home >/dev/null 2>&1"; then
    log_error "Baseline snapshot /.snapshots/baseline/@home not found on $VM_HOST"
    log_info "Run provision-test-infra.sh to create baseline snapshots"
    exit 1
fi
log_info "Baseline snapshots validated"

# Step 2: Clean up test artifacts
log_step "Cleaning up test artifacts..."
ssh_vm "sudo rm -rf /.snapshots/pc-switcher/test-* 2>/dev/null || true"
log_info "Test artifacts cleaned"

# Step 3: Mount top-level btrfs filesystem
log_step "Mounting top-level btrfs filesystem..."
ssh_vm "sudo mkdir -p /mnt/btrfs"
# Unmount first in case it's already mounted from a previous run
ssh_vm "sudo umount /mnt/btrfs 2>/dev/null || true"
# Mount without error suppression - fail loudly if mount fails
ssh_vm "sudo mount -o subvolid=5 /dev/sda2 /mnt/btrfs"
log_info "Top-level btrfs mounted at /mnt/btrfs"

# Step 4: Replace active subvolumes with fresh snapshots from baseline
log_step "Replacing active subvolumes with baseline snapshots..."
log_info "Moving @ to @_old..."
ssh_vm "sudo mv /mnt/btrfs/@ /mnt/btrfs/@_old"
log_info "Creating fresh snapshot of @ from baseline..."
ssh_vm "sudo btrfs subvolume snapshot /mnt/btrfs/.snapshots/baseline/@ /mnt/btrfs/@"

log_info "Moving @home to @home_old..."
ssh_vm "sudo mv /mnt/btrfs/@home /mnt/btrfs/@home_old"
log_info "Creating fresh snapshot of @home from baseline..."
ssh_vm "sudo btrfs subvolume snapshot /mnt/btrfs/.snapshots/baseline/@home /mnt/btrfs/@home"
log_info "Subvolumes replaced"

# Step 5: Unmount and reboot
log_step "Unmounting and rebooting VM..."
ssh_vm "sudo umount /mnt/btrfs"
# Reboot may terminate SSH connection, so ignore exit code
ssh_vm "sudo reboot" || true
log_info "Reboot initiated"

# Step 6: Wait for VM to come back online
log_step "Waiting for VM to come back online..."
log_info "(This typically takes 10-20 seconds)"

# Give the VM a moment to actually go down
sleep 5

# Poll until VM is accessible (key is already in known_hosts, just verify connection)
RETRY_COUNT=0
MAX_RETRIES=60  # 5 minutes maximum (60 * 5 seconds)

until ssh_run -o ConnectTimeout=5 "${SSH_USER}@${VM_HOST}" true 2>/dev/null; do
    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [[ $RETRY_COUNT -ge $MAX_RETRIES ]]; then
        log_error "Timeout waiting for VM to come back online after 5 minutes"
        log_info "Please check VM status manually"
        exit 1
    fi
    log_info "Waiting for VM... (attempt $RETRY_COUNT/$MAX_RETRIES)"
    sleep 5
done

log_info "VM is back online"

# Step 7: Clean up old subvolumes after reboot
log_step "Cleaning up old subvolumes..."
ssh_vm "sudo mkdir -p /mnt/btrfs"
# Unmount first in case mount wasn't properly cleaned up
ssh_vm "sudo umount /mnt/btrfs 2>/dev/null || true"
ssh_vm "sudo mount -o subvolid=5 /dev/sda2 /mnt/btrfs"

log_info "Deleting @_old..."
ssh_vm "sudo btrfs subvolume delete /mnt/btrfs/@_old"

log_info "Deleting @home_old..."
ssh_vm "sudo btrfs subvolume delete /mnt/btrfs/@home_old"

ssh_vm "sudo umount /mnt/btrfs"
log_info "Old subvolumes deleted"

log_step "VM reset complete: $VM_HOST"
