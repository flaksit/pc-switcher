#!/usr/bin/env bash
set -euo pipefail

# Reset a single VM to its baseline btrfs snapshot state
# This script restores the VM to the known-good baseline created during provisioning

# Usage
if [[ "$#" -ne 1 ]]; then
    cat >&2 <<EOF
Usage: $(basename "$0") <VM_HOST>

Reset a VM to its baseline btrfs snapshot state.

Arguments:
  VM_HOST         VM hostname or IP address to reset

Environment Variables:
  PC_SWITCHER_TEST_USER    SSH user for VM access (default: testuser)

Example:
  $(basename "$0") 192.168.1.100
  $(basename "$0") pc1

Prerequisites:
  - Baseline snapshots must exist (/.snapshots/baseline/@ and /.snapshots/baseline/@home)
  - Run provision-test-infra.sh if baseline snapshots are missing
EOF
    exit 1
fi

VM_HOST="$1"
SSH_USER="${PC_SWITCHER_TEST_USER:-testuser}"

# SSH connection helper with proper options
ssh_vm() {
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "${SSH_USER}@${VM_HOST}" "$@"
}

echo "========================================="
echo "Resetting VM: $VM_HOST"
echo "SSH User: $SSH_USER"
echo "========================================="
echo

# Step 1: Validate baseline snapshots exist
echo "Step 1/7: Validating baseline snapshots..."
if ! ssh_vm "sudo btrfs subvolume show /.snapshots/baseline/@ >/dev/null 2>&1"; then
    cat >&2 <<EOF
Error: Baseline snapshot /.snapshots/baseline/@ not found on $VM_HOST

The baseline snapshots are required to reset the VM. Please run:
  ./tests/infrastructure/scripts/provision-test-infra.sh

This will create the baseline snapshots needed for VM reset.
EOF
    exit 1
fi

if ! ssh_vm "sudo btrfs subvolume show /.snapshots/baseline/@home >/dev/null 2>&1"; then
    cat >&2 <<EOF
Error: Baseline snapshot /.snapshots/baseline/@home not found on $VM_HOST

The baseline snapshots are required to reset the VM. Please run:
  ./tests/infrastructure/scripts/provision-test-infra.sh

This will create the baseline snapshots needed for VM reset.
EOF
    exit 1
fi
echo "  ✓ Baseline snapshots validated"
echo

# Step 2: Clean up test artifacts
echo "Step 2/7: Cleaning up test artifacts..."
ssh_vm "sudo rm -rf /.snapshots/pc-switcher/test-* 2>/dev/null || true"
echo "  ✓ Test artifacts cleaned"
echo

# Step 3: Mount top-level btrfs filesystem
echo "Step 3/7: Mounting top-level btrfs filesystem..."
ssh_vm "sudo mkdir -p /mnt/btrfs"
ssh_vm "sudo mount -o subvolid=5 /dev/sda2 /mnt/btrfs 2>/dev/null || true"
echo "  ✓ Top-level btrfs mounted at /mnt/btrfs"
echo

# Step 4: Replace active subvolumes with fresh snapshots from baseline
echo "Step 4/7: Replacing active subvolumes with baseline snapshots..."
echo "  Moving @ to @_old..."
ssh_vm "sudo mv /mnt/btrfs/@ /mnt/btrfs/@_old"
echo "  Creating fresh snapshot of @ from baseline..."
ssh_vm "sudo btrfs subvolume snapshot /mnt/btrfs/.snapshots/baseline/@ /mnt/btrfs/@"

echo "  Moving @home to @home_old..."
ssh_vm "sudo mv /mnt/btrfs/@home /mnt/btrfs/@home_old"
echo "  Creating fresh snapshot of @home from baseline..."
ssh_vm "sudo btrfs subvolume snapshot /mnt/btrfs/.snapshots/baseline/@home /mnt/btrfs/@home"
echo "  ✓ Subvolumes replaced"
echo

# Step 5: Unmount and reboot
echo "Step 5/7: Unmounting and rebooting VM..."
ssh_vm "sudo umount /mnt/btrfs"
# Reboot may terminate SSH connection, so ignore exit code
ssh_vm "sudo reboot" || true
echo "  ✓ Reboot initiated"
echo

# Step 6: Wait for VM to come back online
echo "Step 6/7: Waiting for VM to come back online..."
echo "  (This typically takes 10-20 seconds)"

# Give the VM a moment to actually go down
sleep 5

# Poll until VM is accessible
RETRY_COUNT=0
MAX_RETRIES=60  # 5 minutes maximum (60 * 5 seconds)

until ssh -o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "${SSH_USER}@${VM_HOST}" true 2>/dev/null; do
    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [[ $RETRY_COUNT -ge $MAX_RETRIES ]]; then
        echo "  ✗ Timeout waiting for VM to come back online after 5 minutes" >&2
        echo "  Please check VM status manually" >&2
        exit 1
    fi
    echo "  Waiting for VM... (attempt $RETRY_COUNT/$MAX_RETRIES)"
    sleep 5
done

echo "  ✓ VM is back online"
echo

# Step 7: Clean up old subvolumes after reboot
echo "Step 7/7: Cleaning up old subvolumes..."
ssh_vm "sudo mkdir -p /mnt/btrfs"
ssh_vm "sudo mount -o subvolid=5 /dev/sda2 /mnt/btrfs"

echo "  Deleting @_old..."
ssh_vm "sudo btrfs subvolume delete /mnt/btrfs/@_old"

echo "  Deleting @home_old..."
ssh_vm "sudo btrfs subvolume delete /mnt/btrfs/@home_old"

ssh_vm "sudo umount /mnt/btrfs"
echo "  ✓ Old subvolumes deleted"
echo

echo "========================================="
echo "VM reset complete: $VM_HOST"
echo "========================================="
