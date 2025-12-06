#!/usr/bin/env bash
set -euo pipefail

# VM Configuration Script
# Configures a single VM after btrfs installation with testuser setup,
# SSH key injection, and baseline services.

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <VM_HOST> <SSH_AUTHORIZED_KEYS>" >&2
    echo "  VM_HOST: IP address or hostname of the target VM" >&2
    echo "  SSH_AUTHORIZED_KEYS: SSH public keys (newline-separated) for testuser" >&2
    exit 1
fi

VM_HOST="$1"
SSH_AUTHORIZED_KEYS="$2"

echo "Configuring VM at ${VM_HOST}..."

# SSH into VM and configure
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@"${VM_HOST}" << 'EOF'
set -euo pipefail

echo "Installing required packages..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    btrfs-progs \
    qemu-guest-agent \
    fail2ban \
    ufw \
    sudo

echo "Creating testuser..."
useradd -m -s /bin/bash testuser

echo "Configuring sudo access for testuser..."
echo "testuser ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/testuser
chmod 0440 /etc/sudoers.d/testuser

echo "Setting up SSH key for testuser..."
mkdir -p /home/testuser/.ssh
chmod 700 /home/testuser/.ssh
EOF

# Inject SSH public keys (done separately to handle variable expansion)
# Using printf to preserve newlines in multi-key input
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@"${VM_HOST}" "printf '%s\n' '${SSH_AUTHORIZED_KEYS}' > /home/testuser/.ssh/authorized_keys"

ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@"${VM_HOST}" << 'EOF'
set -euo pipefail

chmod 600 /home/testuser/.ssh/authorized_keys
chown -R testuser:testuser /home/testuser/.ssh

echo "Configuring SSH hardening..."
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-hardening.conf << 'SSHEOF'
PermitRootLogin no
PasswordAuthentication no
AllowUsers testuser
SSHEOF

echo "Restarting SSH service..."
systemctl restart sshd

echo "Configuring fail2ban..."
systemctl enable fail2ban
systemctl start fail2ban

echo "Configuring ufw firewall..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw --force enable

echo "Creating snapshot directories..."
mkdir -p /.snapshots/baseline
mkdir -p /.snapshots/pc-switcher

echo "Enabling qemu-guest-agent..."
systemctl enable qemu-guest-agent
systemctl start qemu-guest-agent

echo "VM configuration complete!"
EOF

echo "Configuration of ${VM_HOST} completed successfully."
echo "You can now SSH as: ssh testuser@${VM_HOST}"
