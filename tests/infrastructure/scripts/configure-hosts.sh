#!/usr/bin/env bash
set -euo pipefail

# Configure /etc/hosts and SSH keys for inter-VM communication
#
# Usage: ./configure-hosts.sh
#
# Environment Variables:
#   HCLOUD_TOKEN    (required) Hetzner Cloud API token
#
# Prerequisites:
#   - Both VMs (pc1, pc2) must exist and be accessible via hcloud
#   - hcloud CLI installed and configured

# Help
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: $(basename "$0")

Configure /etc/hosts and SSH keys for inter-VM communication.

This script:
  1. Updates /etc/hosts on both VMs with each other's IP addresses
  2. Generates SSH keypairs on both VMs (if not present)
  3. Exchanges public keys between VMs (authorized_keys)
  4. Configures known_hosts for passwordless SSH between VMs

Environment Variables:
  HCLOUD_TOKEN    (required) Hetzner Cloud API token

Prerequisites:
  - Both VMs (pc1, pc2) must exist and be accessible
  - hcloud CLI installed
EOF
    exit 0
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

# Check prerequisites
: "${HCLOUD_TOKEN:?HCLOUD_TOKEN environment variable must be set}"

# Configuration
readonly VM1="pc1"
readonly VM2="pc2"

log_step "Fetching VM IP addresses..."
PC1_IP=$(hcloud server ip "$VM1")
PC2_IP=$(hcloud server ip "$VM2")

if [[ -z "$PC1_IP" ]] || [[ -z "$PC2_IP" ]]; then
    log_error "Failed to get VM IP addresses"
    exit 1
fi

log_info "$VM1: $PC1_IP"
log_info "$VM2: $PC2_IP"

# Function to update /etc/hosts on a VM
update_hosts() {
    local vm_name="$1"
    local pc1_ip="$2"
    local pc2_ip="$3"

    log_info "Updating /etc/hosts on $vm_name..."
    hcloud server ssh "$vm_name" <<EOF
# Remove old pc1/pc2 entries
sudo sed -i '/\spc1$/d' /etc/hosts
sudo sed -i '/\spc2$/d' /etc/hosts

# Add new entries
echo "$pc1_ip pc1" | sudo tee -a /etc/hosts >/dev/null
echo "$pc2_ip pc2" | sudo tee -a /etc/hosts >/dev/null

echo "Updated /etc/hosts:"
grep -E '\spc[12]$' /etc/hosts
EOF
}

# Function to generate SSH keypair if not exists
generate_ssh_key() {
    local vm_name="$1"

    log_info "Generating SSH keypair on $vm_name if needed..."
    hcloud server ssh "$vm_name" <<'EOF'
if [[ ! -f ~/.ssh/id_ed25519 ]]; then
    ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -C "testuser@$(hostname)"
    echo "Generated new SSH keypair"
else
    echo "SSH keypair already exists"
fi
EOF
}

# Function to get public key from VM
get_pubkey() {
    local vm_name="$1"
    hcloud server ssh "$vm_name" 'cat ~/.ssh/id_ed25519.pub'
}

# Function to add public key to authorized_keys
add_authorized_key() {
    local vm_name="$1"
    local pubkey="$2"

    log_info "Adding public key to $vm_name authorized_keys..."
    hcloud server ssh "$vm_name" <<EOF
mkdir -p ~/.ssh
chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

# Remove old key if present, then add new one
grep -v "testuser@" ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.tmp || true
echo "$pubkey" >> ~/.ssh/authorized_keys.tmp
mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

echo "Updated authorized_keys"
EOF
}

# Function to add host key to known_hosts
add_known_host() {
    local vm_name="$1"
    local remote_hostname="$2"

    log_info "Adding $remote_hostname to $vm_name known_hosts..."
    hcloud server ssh "$vm_name" <<EOF
mkdir -p ~/.ssh
chmod 700 ~/.ssh

# Remove old entry if present
ssh-keygen -R "$remote_hostname" 2>/dev/null || true

# Scan and add host key
ssh-keyscan -H "$remote_hostname" >> ~/.ssh/known_hosts 2>/dev/null
echo "Added $remote_hostname to known_hosts"
EOF
}

# Update /etc/hosts on both VMs
log_step "Updating /etc/hosts on both VMs..."
update_hosts "$VM1" "$PC1_IP" "$PC2_IP"
update_hosts "$VM2" "$PC1_IP" "$PC2_IP"

# Generate SSH keypairs on both VMs
log_step "Generating SSH keypairs..."
generate_ssh_key "$VM1"
generate_ssh_key "$VM2"

# Get public keys
log_step "Fetching public keys..."
PC1_PUBKEY=$(get_pubkey "$VM1")
PC2_PUBKEY=$(get_pubkey "$VM2")

log_info "pc1 pubkey: ${PC1_PUBKEY:0:50}..."
log_info "pc2 pubkey: ${PC2_PUBKEY:0:50}..."

# Exchange public keys
log_step "Exchanging public keys..."
add_authorized_key "$VM1" "$PC2_PUBKEY"
add_authorized_key "$VM2" "$PC1_PUBKEY"

# Set up known_hosts for VM-to-VM SSH
log_step "Setting up known_hosts..."
add_known_host "$VM1" "pc2"
add_known_host "$VM2" "pc1"

log_step "Testing SSH connectivity..."
log_info "Testing pc1 -> pc2:"
hcloud server ssh "$VM1" 'ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 pc2 hostname'

log_info "Testing pc2 -> pc1:"
hcloud server ssh "$VM2" 'ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 pc1 hostname'

log_step "Configuration complete!"
log_info "- /etc/hosts updated on both VMs"
log_info "- SSH keypairs generated"
log_info "- Public keys exchanged"
log_info "- known_hosts configured"
log_info "- Inter-VM SSH tested successfully"
