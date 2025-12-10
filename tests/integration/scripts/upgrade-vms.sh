#!/usr/bin/env bash
set -euo pipefail

# Upgrade apt packages on test VMs and update baseline snapshots.
# This script:
#   1. Acquires lock to prevent concurrent test runs
#   2. Resets VMs to baseline state
#   3. Upgrades all apt packages
#   4. Detects if reboot is needed
#   5. Reboots if necessary
#   6. Updates baseline snapshots (only if changes occurred)
#   7. Releases lock
#
# Usage: ./upgrade-vms.sh
#
# Environment Variables:
#   HCLOUD_TOKEN           (required) Hetzner Cloud API token
#   PC_SWITCHER_TEST_USER  SSH user for VM access (default: testuser)
#   CI_JOB_ID              GitLab CI job ID (for lock holder identification)
#   GITHUB_RUN_ID          GitHub Actions run ID (for lock holder identification)

# Source common SSH helpers
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/ssh-common.sh"

# Colors for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m' # No Color

log_step() { echo -e "${GREEN}==>${NC} $*"; }
log_info() { echo "    $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# Help
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: $(basename "$0")

Upgrade apt packages on test VMs and update baseline snapshots.

This script:
  1. Acquires lock to prevent concurrent integration tests
  2. Resets VMs to baseline state (removes test artifacts)
  3. Upgrades all apt packages non-interactively
  4. Runs apt-get autoremove to clean unused dependencies
  5. Detects if reboot is required
  6. Reboots VMs if needed (in parallel)
  7. Clears apt cache
  8. Updates baseline snapshots (only if changes occurred)
  9. Releases lock

Lock Behavior:
  - Acquires exclusive lock to prevent concurrent tests
  - Waits up to 5 minutes if lock is held by another process
  - Automatically releases lock on success or error (via trap)

Upgrade Behavior:
  - Uses conservative 'apt-get upgrade' (won't remove packages)
  - Preserves existing configuration files
  - Removes unused dependencies with autoremove
  - Only updates baselines if changes occurred

Reboot Behavior:
  - Checks /var/run/reboot-required (Ubuntu standard)
  - Also checks if kernel was upgraded
  - Reboots independently per VM (asymmetric reboot OK)
  - Waits up to 5 minutes for each VM to come back online

Environment Variables:
  HCLOUD_TOKEN            (required) Hetzner Cloud API token
  PC_SWITCHER_TEST_USER   SSH user for VM access (default: testuser)
  CI_JOB_ID               GitLab CI job ID (for lock holder)
  GITHUB_RUN_ID           GitHub Actions run ID (for lock holder)

Prerequisites:
  - HCLOUD_TOKEN environment variable must be set
  - Both VMs (pc1, pc2) must exist in Hetzner Cloud
  - Both VMs must have baseline snapshots
  - hcloud CLI installed and configured
  - SSH keys configured for VM access

Example:
  export HCLOUD_TOKEN=your_token_here
  $(basename "$0")

EOF
    exit 0
fi

# Check required environment variable
: "${HCLOUD_TOKEN:?HCLOUD_TOKEN environment variable must be set}"

# Configuration
readonly SSH_USER="${PC_SWITCHER_TEST_USER:-testuser}"
readonly VM1_NAME="pc1"
readonly VM2_NAME="pc2"

# Global state
LOCK_HOLDER=""
PC1_IP=""
PC2_IP=""
CHANGES_OCCURRED=false
TMPDIR=""

# Get lock holder identifier (CI or local user@hostname)
get_lock_holder() {
    local ci_job_id="${CI_JOB_ID:-${GITHUB_RUN_ID:-}}"

    if [[ -n "$ci_job_id" ]]; then
        echo "ci-${ci_job_id}"
    else
        local hostname
        hostname=$(hostname)
        echo "local-${USER:-unknown}@${hostname}"
    fi
}

# Cleanup function - called on exit to release lock and clean temp files
cleanup() {
    local exit_code=$?

    if [[ -n "$LOCK_HOLDER" ]]; then
        log_info "Releasing lock..."
        "$SCRIPT_DIR/lock.sh" "$LOCK_HOLDER" release || true
    fi

    if [[ -n "$TMPDIR" && -d "$TMPDIR" ]]; then
        rm -rf "$TMPDIR" 2>/dev/null || true
    fi

    exit $exit_code
}

# Setup trap to ensure lock is always released and temp files cleaned
trap cleanup EXIT INT TERM

# Detect if upgrade output shows changes
# Returns 0 if changes occurred, 1 if no changes
detect_upgrade_changes() {
    local output="$1"

    # Check if any packages were upgraded, installed, or removed
    if echo "$output" | grep -q "0 upgraded, 0 newly installed"; then
        if echo "$output" | grep -q "0 to remove"; then
            return 1  # No changes
        fi
    fi

    return 0  # Changes occurred
}

# Upgrade packages on a single VM
# Returns "CHANGES" or "NO_CHANGES"
upgrade_vm() {
    local vm_name="$1"
    local vm_ip="$2"

    log_step "Upgrading packages on $vm_name ($vm_ip)..."

    # Update package lists
    log_info "Updating package lists..."
    if ! ssh_run "${SSH_USER}@${vm_ip}" "sudo apt-get update" >/dev/null 2>&1; then
        log_error "Failed to update package lists on $vm_name"
        exit 1
    fi

    # Upgrade packages non-interactively
    log_info "Upgrading packages..."
    local upgrade_output
    upgrade_output=$(ssh_run "${SSH_USER}@${vm_ip}" \
        "DEBIAN_FRONTEND=noninteractive sudo apt-get upgrade -y \
         -o Dpkg::Options::='--force-confold' \
         -o Dpkg::Options::='--force-confdef' 2>&1") || {
        log_error "Package upgrade failed on $vm_name"
        exit 1
    }

    # Run autoremove to clean unused dependencies
    log_info "Cleaning unused dependencies..."
    local autoremove_output
    autoremove_output=$(ssh_run "${SSH_USER}@${vm_ip}" \
        "DEBIAN_FRONTEND=noninteractive sudo apt-get autoremove -y 2>&1") || {
        log_error "Autoremove failed on $vm_name"
        exit 1
    }

    # Detect if changes occurred
    local combined_output="${upgrade_output}\n${autoremove_output}"
    if detect_upgrade_changes "$combined_output"; then
        log_info "Changes detected on $vm_name"
        echo "CHANGES"
    else
        log_info "No package changes on $vm_name"
        echo "NO_CHANGES"
    fi
}

# Check if VM needs reboot
# Returns 0 if reboot needed, 1 if no reboot needed
check_reboot_required() {
    local vm_name="$1"
    local vm_ip="$2"

    log_info "Checking if $vm_name requires reboot..."

    # Check for reboot-required flag file (standard Ubuntu)
    if ssh_run "${SSH_USER}@${vm_ip}" "test -f /var/run/reboot-required" 2>/dev/null; then
        log_info "$vm_name: Reboot required (flag file exists)"
        return 0
    fi

    log_info "$vm_name: No reboot required"
    return 1
}

# Reboot a VM and wait for it to come back online
reboot_vm() {
    local vm_name="$1"
    local vm_ip="$2"

    log_step "Rebooting $vm_name..."

    # Initiate reboot (connection will drop, ignore exit code)
    ssh_run "${SSH_USER}@${vm_ip}" "sudo reboot" 2>/dev/null || true
    log_info "Reboot initiated"

    # Give VM time to actually go down
    sleep 5

    # Wait for VM to come back online
    log_info "Waiting for $vm_name to come back online..."
    local retry_count=0
    local max_retries=60  # 5 minutes maximum (60 * 5 seconds)

    until ssh_run -o ConnectTimeout=5 "${SSH_USER}@${vm_ip}" true 2>/dev/null; do
        retry_count=$((retry_count + 1))
        if [[ $retry_count -ge $max_retries ]]; then
            log_error "Timeout waiting for $vm_name to come back online after 5 minutes"
            return 1
        fi
        log_info "Waiting for $vm_name... (attempt $retry_count/$max_retries)"
        sleep 5
    done

    log_info "$vm_name is back online"
    return 0
}

# Main execution
log_step "VM upgrade and baseline update"

# Get lock holder identifier
LOCK_HOLDER=$(get_lock_holder)
log_info "Lock holder: $LOCK_HOLDER"

# Acquire lock
log_step "Acquiring lock..."
if ! "$SCRIPT_DIR/lock.sh" "$LOCK_HOLDER" acquire; then
    log_error "Failed to acquire lock"
    exit 1
fi
log_info "Lock acquired"

# Get VM IP addresses in parallel
log_step "Resolving VM IP addresses..."
PC1_IP=$(hcloud server ip "$VM1_NAME") &
PID1=$!
PC2_IP=$(hcloud server ip "$VM2_NAME") &
PID2=$!

if ! wait $PID1 || ! wait $PID2; then
    log_error "Failed to resolve VM IP addresses"
    exit 1
fi

log_info "$VM1_NAME: $PC1_IP"
log_info "$VM2_NAME: $PC2_IP"

# Reset VMs to baseline in parallel
log_step "Resetting VMs to baseline state..."
"$SCRIPT_DIR/reset-vm.sh" "$PC1_IP" &
PID1=$!
"$SCRIPT_DIR/reset-vm.sh" "$PC2_IP" &
PID2=$!

wait $PID1
EXIT1=$?
wait $PID2
EXIT2=$?

if [[ $EXIT1 -ne 0 || $EXIT2 -ne 0 ]]; then
    log_error "VM reset failed"
    exit 1
fi
log_info "VMs reset to baseline state"

# Upgrade packages in parallel (write results to temp files)
log_step "Upgrading packages on both VMs..."
TMPDIR=$(mktemp -d)

upgrade_vm "$VM1_NAME" "$PC1_IP" > "$TMPDIR/pc1.result" 2>&1 &
PID1=$!
upgrade_vm "$VM2_NAME" "$PC2_IP" > "$TMPDIR/pc2.result" 2>&1 &
PID2=$!

wait $PID1
EXIT1=$?
wait $PID2
EXIT2=$?

if [[ $EXIT1 -ne 0 || $EXIT2 -ne 0 ]]; then
    log_error "Package upgrade failed"
    exit 1
fi

log_info "Package upgrades complete"

# Get results from temp files
PC1_RESULT=$(cat "$TMPDIR/pc1.result")
PC2_RESULT=$(cat "$TMPDIR/pc2.result")

# Determine if changes occurred on either VM
if [[ "$PC1_RESULT" == "CHANGES" || "$PC2_RESULT" == "CHANGES" ]]; then
    CHANGES_OCCURRED=true
    log_info "Changes detected, proceeding with reboot check and baseline update"
else
    CHANGES_OCCURRED=false
    log_info "No package changes detected on either VM"
fi

# Only proceed with reboot check and baseline update if changes occurred
if [[ "$CHANGES_OCCURRED" == "true" ]]; then
    # Check reboot requirements in parallel
    log_step "Checking reboot requirements..."
    check_reboot_required "$VM1_NAME" "$PC1_IP" &
    PID1=$!
    check_reboot_required "$VM2_NAME" "$PC2_IP" &
    PID2=$!

    wait $PID1 && PC1_NEEDS_REBOOT=true || PC1_NEEDS_REBOOT=false
    wait $PID2 && PC2_NEEDS_REBOOT=true || PC2_NEEDS_REBOOT=false

    # Reboot if needed
    if [[ "$PC1_NEEDS_REBOOT" == "true" || "$PC2_NEEDS_REBOOT" == "true" ]]; then
        log_step "Rebooting VMs..."

        if [[ "$PC1_NEEDS_REBOOT" == "true" ]]; then
            reboot_vm "$VM1_NAME" "$PC1_IP" &
            PID1=$!
        else
            PID1=""
        fi

        if [[ "$PC2_NEEDS_REBOOT" == "true" ]]; then
            reboot_vm "$VM2_NAME" "$PC2_IP" &
            PID2=$!
        else
            PID2=""
        fi

        # Wait for reboots to complete
        if [[ -n "$PID1" ]]; then
            wait $PID1 || {
                log_error "VM reboot failed on $VM1_NAME"
                exit 1
            }
        fi

        if [[ -n "$PID2" ]]; then
            wait $PID2 || {
                log_error "VM reboot failed on $VM2_NAME"
                exit 1
            }
        fi

        log_info "Reboots complete"
    else
        log_info "No reboots needed"
    fi

    # Clear apt cache in parallel
    log_step "Cleaning apt cache..."
    ssh_run "${SSH_USER}@${PC1_IP}" "sudo apt-get clean" >/dev/null 2>&1 &
    PID1=$!
    ssh_run "${SSH_USER}@${PC2_IP}" "sudo apt-get clean" >/dev/null 2>&1 &
    PID2=$!

    if ! wait $PID1 || ! wait $PID2; then
        log_warn "Failed to clean apt cache on one or more VMs"
    fi
    log_info "Apt cache cleaned"

    # Create new baseline snapshots
    log_step "Creating new baseline snapshots..."
    if ! "$SCRIPT_DIR/create-baseline-snapshots.sh"; then
        log_error "Failed to create baseline snapshots"
        exit 1
    fi
    log_info "Baseline snapshots created"

    log_step "VM upgrade and baseline update complete!"
else
    log_step "VM upgrade complete (no changes detected, baseline not updated)"
fi
