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
# Installs from public ghcr.io container registry (no authentication needed)
install_pc_switcher() {
    info "Installing pc-switcher from GitHub Container Registry..."
    # Get version from package metadata or use latest
    # uv tool install from ghcr.io requires the full path
    uv tool install --from 'ghcr.io/flaksit/pc-switcher:latest' pc-switcher 2>/dev/null || \
        uv tool install 'pc-switcher' || \
        error "Failed to install pc-switcher"
    info "Installed pc-switcher"
}

# T114: Create config directory and copy default config.yaml
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
        # Try to get default config from repository at same version
        # For now, create inline (would be better to download from repo)
        cat > "${CONFIG_FILE}" << 'EOF'
# PC-Switcher Configuration File
# ~/.config/pc-switcher/config.yaml

# Logging Configuration
log_file_level: FULL
log_cli_level: INFO

# Module Configuration
sync_modules:
  btrfs_snapshots: true  # Required - cannot be disabled

# Btrfs Snapshots Module Configuration
btrfs_snapshots:
  subvolumes:
    - "@"
    - "@home"
    - "@root"
  snapshot_dir: "/.snapshots"
  keep_recent: 3
  max_age_days: 30
  min_free_threshold: 0.20

# Disk Configuration
disk:
  min_free: 0.20
  check_interval: 30
  reserve_minimum: 0.15
EOF
        info "Created default config file: ${CONFIG_FILE}"
        info "Edit this file to configure sync behavior"
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
    echo "================================================"
    echo "pc-switcher installed successfully!"
    echo "================================================"
    echo ""
    echo "Next steps:"
    echo "  1. List your btrfs subvolumes:"
    echo "     sudo btrfs subvolume list /"
    echo ""
    echo "  2. Edit ${CONFIG_FILE}"
    echo "     Update the 'subvolumes' list to match your layout"
    echo ""
    echo "  3. Verify your config:"
    echo "     pc-switcher --help"
    echo ""
    echo "  4. Perform your first sync:"
    echo "     pc-switcher sync <target-hostname>"
    echo ""
    echo "Important:"
    echo "  - Target machine must have btrfs filesystem"
    echo "  - Target must have matching subvolume layout"
    echo "  - 'pc-switcher sync' automatically installs pc-switcher on target"
    echo ""
}

main "$@"
