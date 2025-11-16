#!/usr/bin/env bash
set -euo pipefail

# PC-Switcher Installation Script
# Installs pc-switcher and its dependencies on Ubuntu systems with btrfs filesystem
#
# Usage:
#   setup.sh                      # Install latest release, preserve config
#   setup.sh --version=0.1.0      # Install specific version, preserve config
#   setup.sh --sync-mode --version=0.1.0  # Called by pc-switcher sync, override config
#   setup.sh --upgrade            # Alias for latest with --sync-mode
#   setup.sh --force-config       # Override config with source version (explicit flag)

readonly REQUIRED_UV_VERSION="0.9.9"
readonly CONFIG_DIR="${HOME}/.config/pc-switcher"
readonly CONFIG_FILE="${CONFIG_DIR}/config.yaml"
readonly GITHUB_REPO="https://github.com/flaksit/pc-switcher"

# Installation mode flags
VERSION=""
SYNC_MODE=false
FORCE_CONFIG=false
UPGRADE_MODE=false

error() {
    echo "ERROR: $1" >&2
    exit 1
}

info() {
    echo "INFO: $1"
}

# Parse command-line arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --version=*)
                VERSION="${1#*=}"
                shift
                ;;
            --sync-mode)
                SYNC_MODE=true
                FORCE_CONFIG=true
                shift
                ;;
            --force-config)
                FORCE_CONFIG=true
                shift
                ;;
            --upgrade)
                UPGRADE_MODE=true
                SYNC_MODE=true
                FORCE_CONFIG=true
                shift
                ;;
            *)
                error "Unknown option: $1"
                ;;
        esac
    done
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

# T113: Install pc-switcher from GitHub repository
# Version can be specified via --version flag, otherwise uses main branch
install_pc_switcher() {
    local git_ref

    if [[ -n "${VERSION}" ]]; then
        info "Installing pc-switcher version ${VERSION}..."
        git_ref="git+${GITHUB_REPO}@v${VERSION}"
    else
        info "Installing pc-switcher from main branch..."
        git_ref="git+${GITHUB_REPO}@main"
    fi

    if uv tool install "${git_ref}"; then
        info "Installed pc-switcher from ${git_ref}"
    else
        error "Failed to install pc-switcher from ${git_ref}"
    fi
}

# T114: Create config directory and manage config.yaml
# Behavior depends on mode:
# - Standalone install: create if missing, preserve if exists
# - Sync mode: backup existing, copy from source (via stdin)
create_default_config() {
    if [[ -d "${CONFIG_DIR}" ]]; then
        info "Config directory exists: ${CONFIG_DIR}"
    else
        mkdir -p "${CONFIG_DIR}"
        info "Created config directory: ${CONFIG_DIR}"
    fi

    # If in sync mode and FORCE_CONFIG is true, expect config from stdin
    if [[ "${SYNC_MODE}" == true && "${FORCE_CONFIG}" == true ]]; then
        if [[ -f "${CONFIG_FILE}" ]]; then
            info "Backing up existing config: ${CONFIG_FILE}.bak"
            cp "${CONFIG_FILE}" "${CONFIG_FILE}.bak"
        fi

        # Read config from stdin (provided by pc-switcher sync command)
        if cat > "${CONFIG_FILE}"; then
            info "Updated config file from source machine: ${CONFIG_FILE}"
        else
            error "Failed to write config file. Restored from backup if available."
        fi
        return
    fi

    # Standalone install: preserve existing config, create if missing
    if [[ -f "${CONFIG_FILE}" ]]; then
        info "Preserving existing config: ${CONFIG_FILE}"
        return
    fi

    # Create default config
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
}

main() {
    parse_args "$@"

    if [[ "${SYNC_MODE}" == true ]]; then
        info "Running in sync mode (config will be synchronized from source)"
    elif [[ "${UPGRADE_MODE}" == true ]]; then
        info "Running in upgrade mode"
    else
        info "Running in standalone mode (existing config will be preserved)"
    fi

    if [[ -n "${VERSION}" ]]; then
        info "Target version: ${VERSION}"
    else
        info "Installing latest available version"
    fi

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

    # Different next steps for sync mode vs standalone
    if [[ "${SYNC_MODE}" == true ]]; then
        info "Installation complete. Config has been synchronized from source."
    else
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
    fi
}

main "$@"
