#!/usr/bin/env bash
# PC-switcher installation script
# Usage: curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash
# Or with version: curl -sSL ... | bash -s -- --version 0.1.0

set -euo pipefail

# Color output functions
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

error() {
    echo -e "${RED}ERROR: $*${NC}" >&2
}

success() {
    echo -e "${GREEN}SUCCESS: $*${NC}"
}

info() {
    echo -e "${BLUE}INFO: $*${NC}"
}

warn() {
    echo -e "${YELLOW}WARNING: $*${NC}"
}

# Parse command line arguments
INSTALL_VERSION=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --version)
            INSTALL_VERSION="$2"
            shift 2
            ;;
        -h|--help)
            echo "PC-switcher installation script"
            echo ""
            echo "Usage: $0 [--version VERSION]"
            echo ""
            echo "Options:"
            echo "  --version VERSION    Install specific version (e.g., 0.1.0)"
            echo "  -h, --help          Show this help message"
            echo ""
            echo "Without --version, installs the latest version from the main branch."
            exit 0
            ;;
        *)
            error "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Verify we're on Ubuntu 24.04 LTS
if [[ -f /etc/os-release ]]; then
    source /etc/os-release
    if [[ "${ID}" != "ubuntu" ]]; then
        error "This script is designed for Ubuntu. Detected: ${ID}"
        error "PC-switcher requires Ubuntu 24.04 LTS."
        exit 1
    fi
    if [[ "${VERSION_ID}" != "24.04" ]]; then
        warn "PC-switcher is designed for Ubuntu 24.04 LTS. Detected: ${VERSION_ID}"
        warn "Installation will continue, but functionality is not guaranteed."
        read -p "Continue anyway? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
else
    warn "Cannot detect OS version. PC-switcher requires Ubuntu 24.04 LTS."
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

info "Starting PC-switcher installation..."
echo ""

# Step 1: Check for and install uv if needed
info "Checking for uv package installer..."
if command -v uv &> /dev/null; then
    UV_VERSION=$(uv --version | awk '{print $2}')
    success "uv is already installed (version ${UV_VERSION})"
else
    info "uv not found. Installing uv..."
    if curl -LsSf https://astral.sh/uv/install.sh | sh; then
        success "uv installed successfully"
        # Source the environment to make uv available in current shell
        export PATH="$HOME/.local/bin:$PATH"
        if ! command -v uv &> /dev/null; then
            error "uv installation completed but uv command not found in PATH"
            error "Please add $HOME/.local/bin to your PATH and re-run this script"
            exit 1
        fi
    else
        error "Failed to install uv"
        error "Please install uv manually: https://docs.astral.sh/uv/"
        exit 1
    fi
fi

# Step 2: Check for btrfs-progs
info "Checking for btrfs-progs..."
if command -v btrfs &> /dev/null; then
    BTRFS_VERSION=$(btrfs --version | awk '{print $2}')
    success "btrfs-progs is already installed (${BTRFS_VERSION})"
else
    warn "btrfs-progs is not installed"
    info "PC-switcher requires btrfs filesystem and btrfs-progs for snapshot management"
    echo ""
    echo "To install btrfs-progs, run:"
    echo "  sudo apt update && sudo apt install -y btrfs-progs"
    echo ""
    read -p "Install btrfs-progs now? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        info "Installing btrfs-progs..."
        if sudo apt update && sudo apt install -y btrfs-progs; then
            success "btrfs-progs installed successfully"
        else
            error "Failed to install btrfs-progs"
            error "Please install it manually: sudo apt install btrfs-progs"
            exit 1
        fi
    else
        warn "Skipping btrfs-progs installation"
        warn "PC-switcher will not work without btrfs-progs and a btrfs filesystem"
    fi
fi

# Step 3: Install pc-switcher
info "Installing pc-switcher..."

# Determine install source
if [[ -n "${INSTALL_VERSION}" ]]; then
    INSTALL_SOURCE="git+https://github.com/flaksit/pc-switcher@v${INSTALL_VERSION}"
    info "Installing version ${INSTALL_VERSION} from GitHub..."
else
    INSTALL_SOURCE="git+https://github.com/flaksit/pc-switcher@main"
    info "Installing latest version from main branch..."
fi

# Install or upgrade using uv tool
if uv tool list | grep -q "^pc-switcher "; then
    info "pc-switcher is already installed. Upgrading..."
    if uv tool install --force "${INSTALL_SOURCE}"; then
        success "pc-switcher upgraded successfully"
    else
        error "Failed to upgrade pc-switcher"
        exit 1
    fi
else
    if uv tool install "${INSTALL_SOURCE}"; then
        success "pc-switcher installed successfully"
    else
        error "Failed to install pc-switcher"
        exit 1
    fi
fi

# Verify installation
if ! command -v pc-switcher &> /dev/null; then
    error "pc-switcher installed but command not found in PATH"
    error "uv tool install location: $(uv tool dir)"
    error "Please add the uv tool bin directory to your PATH"
    exit 1
fi

PC_SWITCHER_VERSION=$(pc-switcher --version 2>/dev/null || echo "unknown")
success "pc-switcher command is available: ${PC_SWITCHER_VERSION}"

# Step 4: Create log directory
LOG_DIR="$HOME/.local/share/pc-switcher/logs"
info "Setting up log directory: ${LOG_DIR}"

if [[ ! -d "${LOG_DIR}" ]]; then
    mkdir -p "${LOG_DIR}"
    success "Created log directory"
else
    info "Log directory already exists"
fi

# Final success message
echo ""
echo "================================================================"
success "PC-switcher installation complete!"
echo "================================================================"
echo ""
echo "Next steps:"
echo ""
echo "1. Create the default configuration file:"
echo "   pc-switcher init"
echo ""
echo "2. Review and customize your configuration:"
echo "   ~/.config/pc-switcher/config.yaml"
echo ""
echo "3. Ensure your system uses a btrfs filesystem"
echo "   Check with: df -T /"
echo ""
echo "4. Verify btrfs subvolumes match your config:"
echo "   sudo btrfs subvolume list /"
echo ""
echo "5. Run your first sync:"
echo "   pc-switcher sync <target-hostname>"
echo ""
echo "For help and documentation:"
echo "   pc-switcher --help"
echo "   https://github.com/flaksit/pc-switcher"
echo ""
