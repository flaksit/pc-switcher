#!/usr/bin/env bash
set -euo pipefail

# Source common helpers
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/internal/common.sh"

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
  3. Pre-flight check - detects/recovers from interrupted previous resets
  4. Replaces active subvolumes with baseline snapshots
  5. Reboots the VM
  6. Waits for VM to come back online
  7. Cleans up old subvolumes

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

readonly VM_HOST="$1"
readonly SSH_USER="${PC_SWITCHER_TEST_USER:-testuser}"

# Acquire lock for VM operations (prevents concurrent access)
# Note: When called from conftest.py, lock is already held by pytest
# Lock acquisition is idempotent (succeeds if same holder already has lock)
export PCSWITCHER_LOCK_HOLDER=$(get_lock_holder)

# Set up cleanup trap
cleanup_lock() {
    "$SCRIPT_DIR/internal/lock.sh" "$PCSWITCHER_LOCK_HOLDER" release 2>/dev/null || true
}
trap cleanup_lock EXIT INT TERM

# Acquire lock (waits up to 5 minutes if held by another process)
if ! "$SCRIPT_DIR/internal/lock.sh" "$PCSWITCHER_LOCK_HOLDER" acquire 2>/dev/null; then
    log_error "Failed to acquire lock"
    exit 1
fi

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
if ! ssh_vm 'bash -s' << 'EOF'
set -euo pipefail
sudo btrfs subvolume show /.snapshots/baseline/@ >/dev/null 2>&1
sudo btrfs subvolume show /.snapshots/baseline/@home >/dev/null 2>&1
EOF
then
    log_error "Baseline snapshots not found on $VM_HOST"
    log_info "Run provision-test-infra.sh to create baseline snapshots"
    exit 1
fi
log_info "Baseline snapshots validated"

# Step 2: Clean up test artifacts
log_step "Cleaning up test artifacts..."
ssh_vm "sudo rm -rf /.snapshots/pc-switcher/test-* 2>/dev/null || true"
log_info "Test artifacts cleaned"

# Step 3: Pre-flight check - detect and recover from interrupted previous resets
log_step "Checking for interrupted previous reset..."
ssh_vm 'sudo bash -s' << 'EOF'
set -euo pipefail

# Mount top-level btrfs filesystem
mkdir -p /mnt/btrfs
if mountpoint -q /mnt/btrfs; then
    umount /mnt/btrfs
fi
mount -o subvolid=5 /dev/sda2 /mnt/btrfs

# Check current state
has_root=false
has_root_new=false
has_home=false
has_home_new=false

[ -d /mnt/btrfs/@ ] && has_root=true
[ -d /mnt/btrfs/@_new ] && has_root_new=true
[ -d /mnt/btrfs/@home ] && has_home=true
[ -d /mnt/btrfs/@home_new ] && has_home_new=true

# Check for any old snapshots in /.snapshots/old/ (or legacy locations at root)
latest_root_old=$(ls -1d /mnt/btrfs/@snapshots/old/@_* /mnt/btrfs/@_old_* /mnt/btrfs/@_old 2>/dev/null | sort -r | head -1 || true)
latest_home_old=$(ls -1d /mnt/btrfs/@snapshots/old/@home_* /mnt/btrfs/@home_old_* /mnt/btrfs/@home_old 2>/dev/null | sort -r | head -1 || true)

# Normal state: @ and @home exist, no _new (old snapshots are expected and kept)
if $has_root && $has_home && ! $has_root_new && ! $has_home_new; then
    echo "OK: Subvolumes in normal state"
    umount /mnt/btrfs
    exit 0
fi

# Leftover state: @ and @home exist, but _new also exists (cleanup was interrupted)
if $has_root && $has_home; then
    echo "WARN: Found leftover @_new subvolumes from interrupted reset, will clean up"
    umount /mnt/btrfs
    exit 0
fi

# Critical: @ is missing - previous reset was interrupted during swap
if ! $has_root; then
    echo "ERROR: @ subvolume is missing!"

    # Try to recover from most recent @_old_* or legacy @_old
    if [ -n "$latest_root_old" ]; then
        echo "RECOVERING: Restoring @ from $latest_root_old"
        mv "$latest_root_old" /mnt/btrfs/@
        has_root=true
    # Try to recover from @_new (very unlikely but possible)
    elif $has_root_new; then
        echo "RECOVERING: Restoring @ from @_new"
        mv /mnt/btrfs/@_new /mnt/btrfs/@
        has_root=true
    else
        echo "FATAL: Cannot recover @ subvolume - manual intervention required"
        echo "Consider deleting VMs and reprovisioning: hcloud server delete pc1 pc2"
        umount /mnt/btrfs
        exit 1
    fi
fi

# Critical: @home is missing
if ! $has_home; then
    echo "ERROR: @home subvolume is missing!"

    if [ -n "$latest_home_old" ]; then
        echo "RECOVERING: Restoring @home from $latest_home_old"
        mv "$latest_home_old" /mnt/btrfs/@home
        has_home=true
    elif $has_home_new; then
        echo "RECOVERING: Restoring @home from @home_new"
        mv /mnt/btrfs/@home_new /mnt/btrfs/@home
        has_home=true
    else
        echo "FATAL: Cannot recover @home subvolume - manual intervention required"
        umount /mnt/btrfs
        exit 1
    fi
fi

echo "Recovery complete, proceeding with reset"
umount /mnt/btrfs
EOF
log_info "Pre-flight check passed"

# Step 4: Mount filesystem and replace subvolumes with baseline snapshots
log_step "Replacing subvolumes with baseline snapshots..."
ssh_vm 'sudo bash -s' << 'EOF'
set -euo pipefail

# Helper: recursively delete a subvolume and its children
delete_subvol_recursive() {
    local path="$1"
    local child
    for child in $(btrfs subvolume list -o "$path" 2>/dev/null | awk '{print $NF}'); do
        delete_subvol_recursive "/mnt/btrfs/$child"
    done
    btrfs subvolume delete "$path"
}

# 1. Mount top-level btrfs filesystem
mkdir -p /mnt/btrfs
if mountpoint -q /mnt/btrfs; then
    umount /mnt/btrfs
fi
mount -o subvolid=5 /dev/sda2 /mnt/btrfs

# 2. Verify / is mounted from @ (bail out if not)
ROOT_SUBVOL=$(mount | grep ' on / ' | grep -o 'subvol=[^,)]*' | cut -d= -f2)
if [ "$ROOT_SUBVOL" != "/@" ]; then
    echo "ERROR: / is mounted from '$ROOT_SUBVOL', expected '/@'" >&2
    exit 1
fi

# 3. Verify /home is mounted from @home (bail out if not)
HOME_SUBVOL=$(mount | grep ' on /home ' | grep -o 'subvol=[^,)]*' | cut -d= -f2)
if [ "$HOME_SUBVOL" != "/@home" ]; then
    echo "ERROR: /home is mounted from '$HOME_SUBVOL', expected '/@home'" >&2
    exit 1
fi

# 4. Cleanup temporary snapshots from interrupted previous run
#    NOTE: We keep old snapshots in /.snapshots/old/ for investigation
#    Clean up: legacy snapshots at root level and @_new (temporary)
if [ -e /mnt/btrfs/@_old ]; then
    delete_subvol_recursive /mnt/btrfs/@_old  # Legacy naming
fi
if [ -e /mnt/btrfs/@_new ]; then
    delete_subvol_recursive /mnt/btrfs/@_new   # Temporary snapshot from interrupted reset
fi
if [ -e /mnt/btrfs/@home_old ]; then
    delete_subvol_recursive /mnt/btrfs/@home_old  # Legacy naming
fi
if [ -e /mnt/btrfs/@home_new ]; then
    delete_subvol_recursive /mnt/btrfs/@home_new   # Temporary snapshot from interrupted reset
fi
# Clean up legacy timestamped snapshots at root level (migrate to /.snapshots/old/)
for legacy in /mnt/btrfs/@_old_* /mnt/btrfs/@home_old_*; do
    [ -e "$legacy" ] && delete_subvol_recursive "$legacy"
done

# Ensure /.snapshots/old/ directory exists
mkdir -p /mnt/btrfs/@snapshots/old

# 5. Create BOTH new snapshots (prepare everything before swapping)
btrfs subvolume snapshot /.snapshots/baseline/@ /mnt/btrfs/@_new
btrfs subvolume snapshot /.snapshots/baseline/@home /mnt/btrfs/@home_new

# 6. Swap BOTH as fast as possible (back-to-back mv operations)
#    Store old snapshots in /.snapshots/old/ with timestamps for investigation
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
mv /mnt/btrfs/@ /mnt/btrfs/@snapshots/old/@_${TIMESTAMP}
mv /mnt/btrfs/@_new /mnt/btrfs/@
mv /mnt/btrfs/@home /mnt/btrfs/@snapshots/old/@home_${TIMESTAMP}
mv /mnt/btrfs/@home_new /mnt/btrfs/@home

# 7. Update the default subvolume. It is not used for booting, but keeping it 
#    blocks deletion of the old subvolume it might refer to.
btrfs subvolume set-default /mnt/btrfs/@
EOF
log_info "Subvolumes replaced with baseline snapshots"

# Step 5: Reboot
log_step "Rebooting VM..."
# Reboot may terminate SSH connection, so ignore exit code
ssh_vm "sudo reboot" || true
log_info "Reboot initiated"

# Step 6: Wait for VM to come back online
log_step "Waiting for VM to come back online..."

# Give the VM a moment to actually go down
sleep 5

# Wait for SSH (no REMOVE_KEY - host key doesn't change on reboot)
if ! wait_for_ssh "${SSH_USER}@${VM_HOST}" 300; then
    log_error "Timeout waiting for VM to come back online"
    log_info "Please check VM status manually"
    exit 1
fi

# Step 7: Clean up old subvolumes after reboot (keep 3 most recent for investigation)
log_step "Rotating old subvolumes (keeping 3 most recent)..."
ssh_vm 'sudo bash -s' << 'EOF'
set -euo pipefail

# Helper: recursively delete a subvolume and its children
delete_subvol_recursive() {
    local path="$1"
    local child
    for child in $(btrfs subvolume list -o "$path" 2>/dev/null | awk '{print $NF}'); do
        delete_subvol_recursive "/mnt/btrfs/$child"
    done
    btrfs subvolume delete "$path"
}

# Mount filesystem
mkdir -p /mnt/btrfs
if mountpoint -q /mnt/btrfs; then
    umount /mnt/btrfs
fi
mount -o subvolid=5 /dev/sda2 /mnt/btrfs

# Rotate old root snapshots in /.snapshots/old/: keep 3 most recent, delete the rest
# Timestamped names sort chronologically (YYYYMMDD_HHMMSS format)
for old_subvol in $(ls -1d /mnt/btrfs/@snapshots/old/@_* 2>/dev/null | sort -r | tail -n +4); do
    echo "Deleting old snapshot: $old_subvol"
    delete_subvol_recursive "$old_subvol"
done

# Rotate old home snapshots in /.snapshots/old/: keep 3 most recent, delete the rest
for old_subvol in $(ls -1d /mnt/btrfs/@snapshots/old/@home_* 2>/dev/null | sort -r | tail -n +4); do
    echo "Deleting old snapshot: $old_subvol"
    delete_subvol_recursive "$old_subvol"
done

# Unmount
umount /mnt/btrfs
EOF
log_info "Old subvolumes rotated"

log_step "VM reset complete: $VM_HOST"
