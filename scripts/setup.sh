#!/usr/bin/env bash
set -euo pipefail

# PC-Switcher Installation Script
# Installs pc-switcher and its dependencies on Ubuntu systems with btrfs filesystem

readonly REQUIRED_UV_VERSION="0.9.9"
readonly CONFIG_DIR="${HOME}/.config/pc-switcher"
readonly CONFIG_FILE="${CONFIG_DIR}/config.yaml"

error() {
    echo "ERROR: $1" >&2
    exit 1
}

info() {
    echo "INFO: $1"
}

# T111: Detect btrfs filesystem
check_btrfs() {
    local fs_type
    fs_type=$(stat -f -c %T /)
    if [[ "${fs_type}" != "btrfs" ]]; then
        error "Root filesystem is ${fs_type}, but btrfs is required. Aborting installation."
    fi
    info "Detected btrfs filesystem"
}

# T112: Check for uv installation, install if missing
check_uv() {
    if command -v uv &>/dev/null; then
        local current_version
        current_version=$(uv --version | awk '{print $2}')
        info "Found uv version ${current_version}"
    else
        info "uv not found, installing version ${REQUIRED_UV_VERSION}..."
        curl -LsSf https://astral.sh/uv/${REQUIRED_UV_VERSION}/install.sh | sh
        # Source the updated PATH
        export PATH="${HOME}/.local/bin:${PATH}"
        if ! command -v uv &>/dev/null; then
            error "Failed to install uv"
        fi
        info "Installed uv version ${REQUIRED_UV_VERSION}"
    fi
}

# T112a: Check for btrfs-progs, install if missing
check_btrfs_progs() {
    if dpkg -l btrfs-progs &>/dev/null; then
        info "Found btrfs-progs installed"
    else
        info "btrfs-progs not found, installing via apt-get..."
        sudo apt-get update
        sudo apt-get install -y btrfs-progs
        info "Installed btrfs-progs"
    fi
}

# T113: Install pc-switcher from GitHub Package Registry
install_pc_switcher() {
    info "Installing pc-switcher..."
    uv tool install pc-switcher
    info "Installed pc-switcher"
}

# T114: Create config directory and generate default config.yaml
create_default_config() {
    if [[ -d "${CONFIG_DIR}" ]]; then
        info "Config directory already exists: ${CONFIG_DIR}"
    else
        mkdir -p "${CONFIG_DIR}"
        info "Created config directory: ${CONFIG_DIR}"
    fi

    if [[ -f "${CONFIG_FILE}" ]]; then
        info "Config file already exists: ${CONFIG_FILE}"
    else
        cat > "${CONFIG_FILE}" << 'EOF'
# PC-Switcher Configuration
# See documentation for full configuration options

# Remote machine connection settings
remote:
  # SSH host for the target machine
  host: ""
  # SSH user (defaults to current user if empty)
  user: ""
  # SSH port (defaults to 22)
  port: 22

# Sync behavior settings
sync:
  # Paths to sync (relative to home directory)
  include_paths:
    - "Documents"
    - "Projects"
    - ".config"
    - ".local/share"
  # Paths to exclude from sync
  exclude_paths:
    - ".ssh/id_*"
    - ".config/tailscale"
    - ".cache"

# Logging configuration
logging:
  # Log level: DEBUG, INFO, WARNING, ERROR
  level: "INFO"
  # Log file location (empty for stdout only)
  file: ""

# Safety settings
safety:
  # Require confirmation before destructive operations
  confirm_destructive: true
  # Create backup snapshots before sync
  create_snapshots: true
EOF
        info "Created default config file: ${CONFIG_FILE}"
    fi
}

main() {
    info "Starting pc-switcher installation..."

    check_btrfs
    check_uv
    check_btrfs_progs
    install_pc_switcher
    create_default_config

    # T115: Success message
    echo ""
    echo "pc-switcher installed successfully"
    echo ""
    echo "Next steps:"
    echo "  1. Edit ${CONFIG_FILE} to configure your sync settings"
    echo "  2. Run 'pc-switcher --help' to see available commands"
}

main "$@"
