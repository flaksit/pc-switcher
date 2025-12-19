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
#   VM_HOST    VM hostname (known to ssh) or IP address to reset

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

# Acquire lock
acquire_lock "reset-vm-$VM_HOST"

# SSH connection helper
# First connection uses ssh_accept_new (test runner may have empty known_hosts)
# After first connection, key is stored and subsequent calls verify it
ssh_vm() {
    ssh_run "${SSH_USER}@${VM_HOST}" "$@"
}

log_step_prefixed "Resetting VM: $VM_HOST"
log_info_prefixed "SSH User: $SSH_USER"

# Establish SSH connection (accept new key if not in known_hosts)
# Test runner may have empty known_hosts or correct key from provisioning
log_info_prefixed "Establishing SSH connection..."
ssh_accept_new "${SSH_USER}@${VM_HOST}" true

# Step 1: Validate baseline snapshots exist
log_step_prefixed "Validating baseline snapshots..."
if ! ssh_vm 'bash -s' << 'EOF'
set -euo pipefail
sudo btrfs subvolume show /.snapshots/baseline/@ >/dev/null 2>&1
sudo btrfs subvolume show /.snapshots/baseline/@home >/dev/null 2>&1
EOF
then
    log_error_prefixed "Baseline snapshots not found on $VM_HOST"
    log_info_prefixed "Run provision-test-infra.sh to create baseline snapshots"
    exit 1
fi
log_info_prefixed "Baseline snapshots validated"

# Step 2: Clean up test artifacts (all subvolumes except baseline/* and old/*)
log_step_prefixed "Cleaning up test artifacts..."
ssh_vm 'sudo bash -s' << 'EOF'
set -euo pipefail

delete_subvol_recursive() {
    local path="$1"
    local child
    # btrfs subvolume list shows them as @snapshots/..., so we use sed to adjust that to /.snapshots/...
    for child in $(btrfs subvolume list -o "$path" 2>/dev/null | awk '{print $NF}' | sed 's/^@snapshots/\/.snapshots/'); do
        # verify that child starts with "/.snapshots" to avoid deleting wrong paths
        if [[ "$child" != /.snapshots* ]]; then
            echo "ERROR: Unexpected non-snapshot subvolume: '$child' of '$path', aborting deletion!" >&2
            exit 1
        fi
        delete_subvol_recursive "$child"
    done
    # Double-check this is really a subvolume and still exists before deleting
    if btrfs subvolume show "$path" >/dev/null 2>&1; then
        echo "Deleting subvolume: $path"
        btrfs subvolume delete "$path"
    fi
}

# List all subvolumes under @snapshots/, excluding baseline/* and old/*
for subvol_path in $(btrfs subvolume list / 2>/dev/null | awk '{print $NF}' | grep '^@snapshots/' | grep -v '^@snapshots/baseline/' | grep -v '^@snapshots/old/'); do
    # Convert @snapshots/... to /.snapshots/...
    abs_path=$(echo "$subvol_path" | sed 's/^@snapshots/\/.snapshots/')
    delete_subvol_recursive "$abs_path"
done
EOF
log_info_prefixed "Test artifacts cleaned"

# Step 3: Pre-flight check - detect and recover from interrupted previous resets
log_step_prefixed "Checking for interrupted previous reset..."
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

# Normal state: @ and @home exist, no _new (old snapshots are expected and kept)
if $has_root && $has_home && ! $has_root_new && ! $has_home_new; then
    echo "OK: Subvolumes in normal state"
    exit 0
fi

# Leftover state: @ and @home exist, but _new also exists (cleanup was interrupted)
if $has_root && $has_home; then
    echo "WARN: Found leftover @_new subvolumes from interrupted reset, will clean up"
    exit 0
fi

# Critical: @ is missing - previous reset was interrupted during swap
if ! $has_root; then
    echo "ERROR: @ subvolume is missing!"

    # Check for any old snapshots in /.snapshots/old/
    latest_root_old=$(ls -1d /.snapshots/old/@_* 2>/dev/null | sort -r | head -1 || true)

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
        exit 1
    fi
fi

# Critical: @home is missing
if ! $has_home; then
    echo "ERROR: @home subvolume is missing!"

    # Check for any old snapshots in /.snapshots/old/
    latest_home_old=$(ls -1d /.snapshots/old/@home_* 2>/dev/null | sort -r | head -1 || true)

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
        echo "Consider deleting VMs and reprovisioning: hcloud server delete pc1 pc2"
        exit 1
    fi
fi

echo "Recovery complete, proceeding with reset"
EOF
log_info_prefixed "Pre-flight check passed"

# Step 4: Replace subvolumes with baseline snapshots
log_step_prefixed "Replacing subvolumes with baseline snapshots..."
ssh_vm 'sudo bash -s' << 'EOF'
set -euo pipefail

# 1. Top-level btrfs filesystem is still mounted on /mnt/btrfs from pre-flight check

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
#    They should not contain nested subvolumes, because they are r/w snapshots
#    of the baseline snapshots which do not have nested subvolumes.
if [ -e /mnt/btrfs/@_new ]; then
    btrfs subvolume delete /mnt/btrfs/@_new
fi
if [ -e /mnt/btrfs/@home_new ]; then
    btrfs subvolume delete /mnt/btrfs/@home_new
fi

# 5. Create BOTH new snapshots (prepare everything before swapping)
btrfs subvolume snapshot /.snapshots/baseline/@ /mnt/btrfs/@_new
btrfs subvolume snapshot /.snapshots/baseline/@home /mnt/btrfs/@home_new

# 6. Swap BOTH as fast as possible (back-to-back mv operations)
#    Store old snapshots in /.snapshots/old/ with timestamps for investigation

# Ensure /.snapshots/old/ directory exists
mkdir -p /.snapshots/old

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
mv /mnt/btrfs/@ /mnt/btrfs/@snapshots/old/@_${TIMESTAMP}
mv /mnt/btrfs/@_new /mnt/btrfs/@
mv /mnt/btrfs/@home /mnt/btrfs/@snapshots/old/@home_${TIMESTAMP}
mv /mnt/btrfs/@home_new /mnt/btrfs/@home

# 7. Update the default subvolume. It is not used for booting, but keeping it 
#    blocks deletion of the old subvolume it might refer to.
btrfs subvolume set-default /mnt/btrfs/@
EOF
log_info_prefixed "Subvolumes replaced with baseline snapshots"

# Step 5: Reboot
log_step_prefixed "Rebooting VM..."
# Reboot may terminate SSH connection, so ignore exit code
ssh_vm "sudo reboot" || true
log_info_prefixed "Reboot initiated"

# Step 6: Wait for VM to come back online
log_step_prefixed "Waiting for VM to come back online..."

# Give the VM a moment to actually go down
sleep 5

# Wait for SSH (no REMOVE_KEY - host key doesn't change on reboot)
if ! wait_for_ssh "${SSH_USER}@${VM_HOST}" 300; then
    log_error_prefixed "Timeout waiting for VM to come back online"
    log_info_prefixed "Please check VM status manually"
    exit 1
fi

# Step 7: Clean up old subvolumes after reboot (keep 3 most recent for investigation)
log_step_prefixed "Rotating old subvolumes (keeping 3 most recent)..."
ssh_vm 'sudo bash -s' << 'EOF'
set -euo pipefail

# Helper: recursively delete a subvolume and its children
delete_subvol_recursive() {
    local path="$1"
    local child
    # btrfs subvolume list shows them as @snapshots/..., so we use sed to adjust that to /.snapshots/...
    for child in $(btrfs subvolume list -o "$path" 2>/dev/null | awk '{print $NF}' | sed 's/^@snapshots/\/.snapshots/'); do
        # verify that child starts with "/.snapshots" to avoid deleting wrong paths
        if [[ "$child" != /.snapshots* ]]; then
            echo "ERROR: Unexpected non-snapshot subvolume: '$child' of '$path', aborting deletion!" >&2
            exit 1
        fi
        delete_subvol_recursive "$child"
    done
    # Double-check this is really a subvolume and still exists before deleting
    if btrfs subvolume show "$path" >/dev/null 2>&1; then
        echo "Deleting subvolume: $path"
        btrfs subvolume delete "$path"
    fi
}


# Rotate old root snapshots in /.snapshots/old/: keep 3 most recent, delete the rest
# Timestamped names sort chronologically (YYYYMMDD_HHMMSS format)
for old_subvol in $(ls -1d /.snapshots/old/@_* 2>/dev/null | sort -r | tail -n +4); do
    # verify it is really a subvolume before deleting
    if btrfs subvolume show "$old_subvol" >/dev/null 2>&1; then
        echo "Deleting old snapshot: $old_subvol"
        delete_subvol_recursive "$old_subvol"
    else
        echo "Skipping non-subvolume: $old_subvol"
    fi
done

# Rotate old home snapshots in /.snapshots/old/: keep 3 most recent, delete the rest
for old_subvol in $(ls -1d /.snapshots/old/@home_* 2>/dev/null | sort -r | tail -n +4); do
    # verify it is really a subvolume before deleting
    if btrfs subvolume show "$old_subvol" >/dev/null 2>&1; then
        echo "Deleting old snapshot: $old_subvol"
        delete_subvol_recursive "$old_subvol"
    else
        echo "Skipping non-subvolume: $old_subvol"
    fi
done
EOF
log_info_prefixed "Old subvolumes rotated"

log_step_prefixed "VM reset complete: $VM_HOST"
