#!/usr/bin/env bash
set -euo pipefail

# Main orchestrator script for provisioning pc-switcher test infrastructure.
# Creates two VMs, configures them, sets up networking, and creates baseline snapshots.
#
# See docs/testing-infrastructure.md for the full provisioning flow diagram and details.
#
# IMPORTANT: This script can only be run from GitHub CI. Local provisioning is blocked
# to ensure all authorized SSH keys are properly configured from secrets.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/ssh-common.sh"

# Colors for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m' # No Color

log_step() { echo -e "${GREEN}==>${NC} $*"; }
log_info() { echo -e "    $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# Configuration
readonly SSH_KEY_NAME="pc-switcher-test-key"
readonly DEFAULT_SSH_PUBLIC_KEY="${HOME}/.ssh/id_ed25519.pub"

# Create or verify SSH key in Hetzner Cloud
ensure_ssh_key() {
    log_step "Ensuring SSH key '$SSH_KEY_NAME' exists in Hetzner Cloud..."

    # Get the fingerprint of our local public key in MD5 format (Hetzner uses MD5)
    local local_fingerprint
    local_fingerprint=$(ssh-keygen -lf "$SSH_PUBLIC_KEY" -E md5 | awk '{print $2}' | sed 's/^MD5://')
    log_info "Local key fingerprint: $local_fingerprint"

    if hcloud ssh-key describe "$SSH_KEY_NAME" &> /dev/null; then
        # Key exists, check if fingerprint matches
        local remote_fingerprint
        remote_fingerprint=$(hcloud ssh-key describe "$SSH_KEY_NAME" -o json | jq -r '.fingerprint')
        log_info "Remote key fingerprint: $remote_fingerprint"

        if [[ "$local_fingerprint" == "$remote_fingerprint" ]]; then
            log_info "SSH key '$SSH_KEY_NAME' already exists with matching fingerprint"
        else
            log_warn "SSH key '$SSH_KEY_NAME' exists but fingerprint doesn't match!"
            log_info "Deleting old key and creating new one..."
            hcloud ssh-key delete "$SSH_KEY_NAME"
            hcloud ssh-key create --name "$SSH_KEY_NAME" --public-key-from-file "$SSH_PUBLIC_KEY"
            log_info "SSH key recreated with new fingerprint"
        fi
    else
        log_info "Creating SSH key '$SSH_KEY_NAME'..."
        hcloud ssh-key create --name "$SSH_KEY_NAME" --public-key-from-file "$SSH_PUBLIC_KEY"
        log_info "SSH key created"
    fi
}

# Help text
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: $(basename "$0")

Provisions the complete pc-switcher test infrastructure:
1. Creates two VMs (pc1, pc2) in parallel
2. Configures both VMs in parallel
3. Sets up inter-VM networking
4. Creates baseline snapshots

Prerequisites:
  - HCLOUD_TOKEN environment variable must be set
  - SSH_AUTHORIZED_KEYS environment variable must contain all authorized public keys (newline-separated)
  - hcloud CLI installed and configured
  - ssh available
  - Must be run from GitHub CI (CI environment variable set)

The script is idempotent - skips if VMs already exist and are configured.

Example (CI only):
  export HCLOUD_TOKEN=your_token_here
  export SSH_AUTHORIZED_KEYS="\$(env | grep '^SSH_AUTHORIZED_KEY_' | sed 's/^[^=]*=//')"
  $(basename "$0")
EOF
    exit 0
fi

# Check if VM is configured (testuser can SSH)
# Uses ssh_first since this may be first connection after VM reprovisioning
check_vm_configured() {
    local vm_ip="$1"
    ssh_first "testuser@$vm_ip" "echo configured" 2>/dev/null && return 0
    return 1
}

# Check if VM has btrfs filesystem (requires root SSH access)
# Uses ssh_first since this may be first connection after VM reprovisioning
check_vm_has_btrfs() {
    local vm_ip="$1"
    local fs_type
    fs_type=$(ssh_first "root@$vm_ip" "df -T / 2>/dev/null | tail -n1 | awk '{print \$2}'" 2>/dev/null) || return 1
    [[ "$fs_type" == "btrfs" ]]
}

# Get VM IPs (empty if VM doesn't exist)
log_step "Checking existing infrastructure..."
PC1_IP=$(hcloud server ip pc1 2>/dev/null) || PC1_IP=""
PC2_IP=$(hcloud server ip pc2 2>/dev/null) || PC2_IP=""

# Early exit if VMs exist and are configured
if [[ -n "$PC1_IP" && -n "$PC2_IP" ]]; then
    log_info "Found existing VMs:"
    log_info "  pc1: $PC1_IP"
    log_info "  pc2: $PC2_IP"
    log_info "Checking if VMs are configured..."

    # Run checks in parallel to establish keys for both VMs (avoids short-circuit)
    check_vm_configured "$PC1_IP" &
    pid1=$!
    check_vm_configured "$PC2_IP" &
    pid2=$!
    wait $pid1 && pc1_configured=true || pc1_configured=false
    wait $pid2 && pc2_configured=true || pc2_configured=false

    if [[ "$pc1_configured" == "true" && "$pc2_configured" == "true" ]]; then
        log_info "VMs are already configured. Skipping provisioning."
        log_step "VMs ready for testing"
        exit 0
    fi

    # VMs exist but not configured - check if they have btrfs
    log_info "VMs not configured. Checking filesystem state..."

    # Run btrfs checks in parallel
    check_vm_has_btrfs "$PC1_IP" &
    pid1=$!
    check_vm_has_btrfs "$PC2_IP" &
    pid2=$!
    wait $pid1 && PC1_HAS_BTRFS=true || PC1_HAS_BTRFS=false
    wait $pid2 && PC2_HAS_BTRFS=true || PC2_HAS_BTRFS=false

    if [[ "$PC1_HAS_BTRFS" == "true" ]]; then
        log_info "  pc1: has btrfs filesystem"
    else
        log_warn "  pc1: does NOT have btrfs filesystem"
    fi

    if [[ "$PC2_HAS_BTRFS" == "true" ]]; then
        log_info "  pc2: has btrfs filesystem"
    else
        log_warn "  pc2: does NOT have btrfs filesystem"
    fi

    # If any VM doesn't have btrfs, we can't just configure - need full reprovision
    if [[ "$PC1_HAS_BTRFS" == "false" || "$PC2_HAS_BTRFS" == "false" ]]; then
        log_error "VMs exist but are in an incomplete state (missing btrfs filesystem)."
        log_error "This happens when VM creation was interrupted during the rescue/installimage phase."
        log_error ""
        log_error "To fix this, delete the VMs and re-run the workflow:"
        log_error "  hcloud server delete pc1"
        log_error "  hcloud server delete pc2"
        log_error ""
        log_error "Then trigger the workflow again to reprovision from scratch."
        log_error "See docs/testing-infrastructure.md for more details."
        exit 1
    fi

    log_info "Both VMs have btrfs. Will proceed with configuration only."
    SKIP_VM_CREATION=true
else
    SKIP_VM_CREATION=false
fi

# Provisioning needed - block if not CI
if [[ -z "$PC1_IP" || -z "$PC2_IP" ]]; then
    if [[ -z "${CI:-}" ]]; then
        log_error "VMs don't exist and provisioning is only allowed from GitHub CI."
        log_info "To provision VMs, trigger the integration test workflow:"
        log_info "  gh workflow run test.yml"
        log_info "Then wait for it to complete before running local tests."
        exit 1
    fi
fi

# Check prerequisites for provisioning
log_step "Checking prerequisites..."
: "${HCLOUD_TOKEN:?HCLOUD_TOKEN must be set}"
: "${SSH_AUTHORIZED_KEYS:?SSH_AUTHORIZED_KEYS must be set with all authorized public keys}"

if ! command -v hcloud >/dev/null 2>&1; then
    log_error "hcloud CLI not found. Please install it first."
    exit 1
fi

if ! command -v ssh >/dev/null 2>&1; then
    log_error "ssh not found. Please install openssh-client."
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    log_error "jq not found. Please install jq."
    exit 1
fi

# Count authorized keys
KEY_COUNT=$(echo "$SSH_AUTHORIZED_KEYS" | grep -c '^ssh-' || true)
log_info "Found $KEY_COUNT authorized SSH key(s)"

# Determine SSH public key path for Hetzner Cloud key
SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY:-$DEFAULT_SSH_PUBLIC_KEY}"
if [[ ! -f "$SSH_PUBLIC_KEY" ]]; then
    log_error "SSH public key not found at: $SSH_PUBLIC_KEY"
    log_info "Generate one with: ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519"
    exit 1
fi

log_info "Prerequisites check passed"

# Ensure SSH key exists in Hetzner Cloud (must be done once before parallel VM creation)
ensure_ssh_key

# Create VMs in parallel (skip if VMs already exist with btrfs)
if [[ "$SKIP_VM_CREATION" == "true" ]]; then
    log_step "Skipping VM creation (VMs already exist with btrfs filesystem)"
else
    log_step "Creating VMs in parallel..."
    log_info "- pc1"
    log_info "- pc2"
    "$SCRIPT_DIR/create-vm.sh" pc1 &
    PID1=$!
    "$SCRIPT_DIR/create-vm.sh" pc2 &
    PID2=$!

    # Wait for both VM creation jobs
    wait $PID1
    wait $PID2
    log_info "VM creation completed"

    # Get VM IPs (refresh after creation)
    log_step "Retrieving VM IP addresses..."
    PC1_IP=$(hcloud server ip pc1)
    PC2_IP=$(hcloud server ip pc2)
    log_info "pc1: $PC1_IP"
    log_info "pc2: $PC2_IP"
fi

# Configure VMs in parallel
log_step "Configuring VMs in parallel..."
log_info "Installing packages and setting up users on both VMs"
"$SCRIPT_DIR/configure-vm.sh" "$PC1_IP" "$SSH_AUTHORIZED_KEYS" "pc1" &
PID1=$!
"$SCRIPT_DIR/configure-vm.sh" "$PC2_IP" "$SSH_AUTHORIZED_KEYS" "pc2" &
PID2=$!

# Wait for both configuration jobs
wait $PID1
wait $PID2
log_info "VM configuration completed"

# Configure inter-VM networking
log_step "Configuring inter-VM networking..."
"$SCRIPT_DIR/configure-hosts.sh"
log_info "Inter-VM networking configured"

# Create baseline snapshots
log_step "Creating baseline snapshots..."
"$SCRIPT_DIR/create-baseline-snapshots.sh"
log_info "Baseline snapshots created"

log_step "Test infrastructure provisioning complete!"
echo ""
log_info "VMs ready for testing:"
log_info "  pc1: $PC1_IP"
log_info "  pc2: $PC2_IP"
echo ""
log_info "To SSH into VMs:"
log_info "  ssh testuser@$PC1_IP"
log_info "  ssh testuser@$PC2_IP"
echo ""
log_info "To reset VMs to baseline state:"
log_info "  (Note that this is done automatically by the integration tests.)"
log_info "  $SCRIPT_DIR/reset-vm.sh $PC1_IP"
log_info "  $SCRIPT_DIR/reset-vm.sh $PC2_IP"
echo ""
log_info "To destroy VMs (manual cleanup):"
log_info "  hcloud server delete pc1"
log_info "  hcloud server delete pc2"
