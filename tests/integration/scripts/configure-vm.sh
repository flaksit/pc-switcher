#!/usr/bin/env bash
set -euo pipefail

# Source common helpers
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

# VM Configuration Script
# Configures a single VM after btrfs installation: creates testuser, injects SSH keys,
# hardens SSH, and configures firewall.
#
# See docs/testing-infrastructure.md for the full provisioning flow diagram.
#
# Usage: ./configure-vm.sh <VM_HOST> <SSH_AUTHORIZED_KEYS> [VM_NAME]
#
# Arguments:
#   VM_HOST              IP address or hostname of the target VM
#   SSH_AUTHORIZED_KEYS  SSH public keys (newline-separated) for testuser
#   VM_NAME              (optional) Display name for logs (defaults to VM_HOST)

# Help
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: $(basename "$0") <VM_HOST> <SSH_AUTHORIZED_KEYS> [VM_NAME]

Configure a single VM after btrfs installation.

This script:
  1. Installs required packages (btrfs-progs, qemu-guest-agent, fail2ban, ufw)
  2. Creates testuser with sudo access
  3. Injects SSH public keys for testuser
  4. Hardens SSH (disables root login, password auth)
  5. Configures firewall (ufw)
  6. Creates snapshot directories

Arguments:
  VM_HOST              IP address or hostname of the target VM
  SSH_AUTHORIZED_KEYS  SSH public keys (newline-separated) for testuser
  VM_NAME              (optional) Display name for logs (defaults to VM_HOST)

Example:
  $(basename "$0") 192.168.1.100 "\$(cat ~/.ssh/id_ed25519.pub)" pc1
EOF
    exit 0
fi

if [[ "$#" -lt 2 ]]; then
    echo "Error: Expected at least 2 arguments, got $#" >&2
    echo "Usage: $(basename "$0") <VM_HOST> <SSH_AUTHORIZED_KEYS> [VM_NAME]" >&2
    echo "Run with -h for help" >&2
    exit 1
fi

readonly VM_HOST="$1"
readonly SSH_AUTHORIZED_KEYS="$2"
readonly VM_NAME="${3:-$VM_HOST}"  # Use VM_NAME if provided, otherwise fall back to VM_HOST

# Log prefix includes VM name for parallel execution clarity
readonly LOG_PREFIX=" ${CYAN}[${VM_NAME}]${NC}"

# Run SSH command and prefix all output with VM name
# Key is already established by create-vm.sh, so we use ssh_run
run_ssh() {
    ssh_run root@"${VM_HOST}" "$@" 2>&1 | while IFS= read -r line; do
        echo -e "   [${VM_NAME}] $line"
    done
    return "${PIPESTATUS[0]}"
}

log_step_prefixed "Configuring VM at ${VM_HOST}..."

log_info_prefixed "Installing required packages..."
run_ssh << 'EOF'
set -euo pipefail
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    btrfs-progs \
    qemu-guest-agent \
    fail2ban \
    ufw \
    sudo
EOF

log_info_prefixed "Creating testuser..."
run_ssh << 'EOF'
set -euo pipefail
useradd -m -s /bin/bash testuser
echo "testuser ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/testuser
chmod 0440 /etc/sudoers.d/testuser
mkdir -p /home/testuser/.ssh
chmod 700 /home/testuser/.ssh
EOF

log_info_prefixed "Injecting SSH keys for testuser..."
ssh_run root@"${VM_HOST}" "printf '%s\n' '${SSH_AUTHORIZED_KEYS}' > /home/testuser/.ssh/authorized_keys"

log_info_prefixed "Configuring SSH hardening and services..."
run_ssh << 'EOF'
set -euo pipefail

chmod 600 /home/testuser/.ssh/authorized_keys
chown -R testuser:testuser /home/testuser/.ssh

# SSH hardening
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-hardening.conf << 'SSHEOF'
PermitRootLogin no
PasswordAuthentication no
AllowUsers testuser
SSHEOF
systemctl restart ssh

# fail2ban
systemctl enable fail2ban
systemctl start fail2ban

# ufw firewall
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw --force enable

# Snapshot directories
mkdir -p /.snapshots/baseline
mkdir -p /.snapshots/pc-switcher

# qemu-guest-agent
systemctl enable qemu-guest-agent
systemctl start qemu-guest-agent
EOF

log_step_prefixed "Configuration of ${VM_HOST} completed successfully"
log_info_prefixed "SSH access: ssh testuser@${VM_HOST}"
