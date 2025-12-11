#!/usr/bin/env bash
# Run integration tests locally with minimal setup.
#
# This script only requires HCLOUD_TOKEN to be set. It automatically:
# - Looks up VM IPs from Hetzner Cloud
# - Updates SSH known_hosts entries
# - Sets all required environment variables
# - Runs pytest with integration markers
#
# Usage:
#   ./local-integration-tests.sh                    # Run all integration tests
#   ./local-integration-tests.sh test_vm_connectivity.py  # Run specific test file
#   ./local-integration-tests.sh -k "test_ssh"     # Run tests matching pattern
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# Check HCLOUD_TOKEN
if [[ -z "${HCLOUD_TOKEN:-}" ]]; then
    log_info "HCLOUD_TOKEN not set, attempting to retrieve from pass..."
    if command -v pass &> /dev/null; then
        HCLOUD_TOKEN=$(pass show dev/pc-switcher/testing/hcloud_token_rw 2>/dev/null) || {
            log_error "HCLOUD_TOKEN not set and could not retrieve from pass"
            echo "Please set your Hetzner Cloud API token:" >&2
            echo "  export HCLOUD_TOKEN='your-token-here'" >&2
            echo "Or ensure you have the token set in pass 'dev/pc-switcher/testing/hcloud_token_rw'" >&2
            exit 1
        }
        export HCLOUD_TOKEN
        log_info "Successfully retrieved HCLOUD_TOKEN from pass"
    else
        log_error "HCLOUD_TOKEN not set and pass not available"
        echo "Please set your Hetzner Cloud API token:" >&2
        echo "  export HCLOUD_TOKEN='your-token-here'" >&2
        exit 1
    fi
fi

# Check hcloud CLI is available
if ! command -v hcloud &> /dev/null; then
    log_error "hcloud CLI not found"
    echo "Install it with:" >&2
    echo "  sudo apt-get install hcloud  # Ubuntu/Debian" >&2
    echo "  brew install hcloud          # macOS" >&2
    exit 1
fi

# Look up VM IPs
log_info "Looking up VM IPs from Hetzner Cloud..."
PC1_IP=$(hcloud server ip pc1 2>/dev/null) || {
    log_error "Failed to get IP for pc1. Is the VM running?"
    echo "Check with: hcloud server list" >&2
    exit 1
}
PC2_IP=$(hcloud server ip pc2 2>/dev/null) || {
    log_error "Failed to get IP for pc2. Is the VM running?"
    echo "Check with: hcloud server list" >&2
    exit 1
}

log_info "pc1: $PC1_IP"
log_info "pc2: $PC2_IP"

# Update SSH known_hosts
log_info "Updating SSH known_hosts..."
for ip in "$PC1_IP" "$PC2_IP"; do
    # Remove old entries (suppress errors if not found)
    ssh-keygen -R "$ip" 2>/dev/null || true
    # Add new entries
    ssh-keyscan -H "$ip" >> ~/.ssh/known_hosts 2>/dev/null
done

# Export environment variables
export PC_SWITCHER_TEST_PC1_HOST="$PC1_IP"
export PC_SWITCHER_TEST_PC2_HOST="$PC2_IP"
export PC_SWITCHER_TEST_USER="testuser"

log_info "Environment configured:"
log_info "  PC_SWITCHER_TEST_PC1_HOST=$PC_SWITCHER_TEST_PC1_HOST"
log_info "  PC_SWITCHER_TEST_PC2_HOST=$PC_SWITCHER_TEST_PC2_HOST"
log_info "  PC_SWITCHER_TEST_USER=$PC_SWITCHER_TEST_USER"

# Run tests
log_info "Running integration tests..."
cd "$PROJECT_ROOT"
exec uv run pytest tests/integration -v -m integration "$@"
