#!/usr/bin/env bash
# PC-switcher installation script
#
# User installation (via curl):
#   curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash
#   curl -sSL ... | VERSION=v0.2.0 bash    # Install specific release
#
# Developer installation (from git checkout):
#   ./install.sh                          # Install from local checkout
#   ./install.sh --ref main               # Install from specific ref

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
INSTALL_REF=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --ref)
            INSTALL_REF="$2"
            shift 2
            ;;
        -h|--help)
            echo "PC-switcher installation script"
            echo ""
            echo "User installation (via curl):"
            echo "  curl -sSL .../install.sh | bash                # Latest from main"
            echo "  curl -sSL .../install.sh | VERSION=v0.2.0 bash  # Specific version"
            echo ""
            echo "Developer installation (from git checkout):"
            echo "  ./install.sh                # Install from local checkout"
            echo "  ./install.sh --ref main     # Install from specific ref"
            echo ""
            echo "Options:"
            echo "  --ref REF      Install from specific git ref (branch, tag, or commit)"
            echo "  -h, --help     Show this help message"
            echo ""
            echo "Environment variables:"
            echo "  VERSION        Install specific release version (e.g., VERSION=v0.2.0)"
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
# Read OS info without sourcing to avoid polluting environment variables
if [[ -f /etc/os-release ]]; then
    OS_ID=$(grep -E '^ID=' /etc/os-release | cut -d= -f2 | tr -d '"')
    OS_VERSION_ID=$(grep -E '^VERSION_ID=' /etc/os-release | cut -d= -f2 | tr -d '"')

    if [[ "${OS_ID}" != "ubuntu" ]]; then
        error "This script is designed for Ubuntu. Detected: ${OS_ID}"
        error "PC-switcher requires Ubuntu 24.04 LTS."
        exit 1
    fi
    if [[ "${OS_VERSION_ID}" != "24.04" ]]; then
        warn "PC-switcher is designed for Ubuntu 24.04 LTS. Detected: ${OS_VERSION_ID}"
        warn "Installation can continue, but functionality is not guaranteed."
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

# Determine install source with priority:
# 1. Explicit --ref argument
# 2. VERSION env var (for curl installations)
# 3. Git workspace detection (for developer installations)
# 4. Default to main branch

INSTALL_SOURCE=""
INSTALL_MODE=""

if [[ -n "${INSTALL_REF}" ]]; then
    # Priority 1: Explicit --ref argument
    INSTALL_SOURCE="git+https://github.com/flaksit/pc-switcher@${INSTALL_REF}"
    INSTALL_MODE="ref '${INSTALL_REF}' from GitHub"
elif [[ -n "${VERSION:-}" ]]; then
    # Priority 2: VERSION env var (for curl | VERSION=vX.Y.Z bash)
    INSTALL_SOURCE="git+https://github.com/flaksit/pc-switcher@${VERSION}"
    INSTALL_MODE="version ${VERSION} from GitHub"
elif git rev-parse --is-inside-work-tree &>/dev/null; then
    # Priority 3: Running from git workspace - install local checkout
    GIT_ROOT=$(git rev-parse --show-toplevel)
    INSTALL_SOURCE="${GIT_ROOT}"
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "detached")
    INSTALL_MODE="local checkout (${CURRENT_BRANCH})"
else
    # Priority 4: Default to main branch
    INSTALL_SOURCE="git+https://github.com/flaksit/pc-switcher@main"
    INSTALL_MODE="main branch from GitHub"
fi

info "Installing from ${INSTALL_MODE}..."

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
