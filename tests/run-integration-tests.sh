#!/usr/bin/env bash
# Run integration tests with test environment configured.
#
# This script can work in two modes:
# 1. Local mode: Requires HCLOUD_TOKEN, automatically looks up VM IPs and configures environment
# 2. CI mode: Uses pre-set environment variables (PC_SWITCHER_TEST_PC1_HOST, PC_SWITCHER_TEST_PC2_HOST, PC_SWITCHER_TEST_USER)
#
# Environment variables (optional - will be looked up if not set):
#   PC_SWITCHER_TEST_PC1_HOST - IP or hostname of pc1 test VM
#   PC_SWITCHER_TEST_PC2_HOST - IP or hostname of pc2 test VM
#   PC_SWITCHER_TEST_USER - SSH user for test VMs (defaults to "testuser")
#   HCLOUD_TOKEN - Hetzner Cloud API token (required if VM IPs not set)
#   GITHUB_TOKEN - GitHub API token (optional, for rate limit increase)
#
# Usage:
#   ./run-integration-tests.sh [--skip-reset] [pytest args...]
#
# Options:
#   --skip-reset  Skip resetting VMs to baseline (faster for iterative development)
#
# Examples:
#   ./run-integration-tests.sh                                           # Run all integration tests
#   ./run-integration-tests.sh tests/integration/test_vm_connectivity.py # Run specific test file
#   ./run-integration-tests.sh -k "test_ssh" -v                          # Run tests matching pattern
#   ./run-integration-tests.sh --skip-reset -k "test_ssh"                # Skip VM reset, run matching tests
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "Starting integration tests setup"

COMMON_SCRIPT="$SCRIPT_DIR/integration/scripts/internal/common.sh"
RESET_SCRIPT="$SCRIPT_DIR/integration/scripts/reset-vm.sh"

# Source common helpers (colors, logging, lock helpers)
source "$COMMON_SCRIPT"

# Help
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    echo "Usage: $0 [--skip-reset] [pytest args...]"
    echo ""
    echo "Options:"
    echo "  --skip-reset    Skip resetting VMs to baseline (faster for iterative development)"
    echo ""
    echo "All other arguments are forwarded to pytest."
    exit 0
fi

# Parse our own flags (before forwarding remaining args to pytest)
SKIP_RESET=false
PYTEST_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --skip-reset)
            SKIP_RESET=true
            ;;
        *)
            PYTEST_ARGS+=("$arg")
            ;;
    esac
done

# Check GITHUB_TOKEN
if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    if command -v pass &> /dev/null; then
        GITHUB_TOKEN=$(pass show dev/pc-switcher/github_token_public_ro 2>/dev/null) || {
            log_warn "GITHUB_TOKEN not set and could not retrieve from pass"
            echo "Please set your GitHub API token:"
            echo "  export GITHUB_TOKEN='your-token-here'"
            echo "Or ensure you have the token set in pass 'dev/pc-switcher/github_token_public_ro'"
            echo "Or ensure you have the token set in pass 'dev/pc-switcher/github_token_public_ro'"
            echo "Will continue with unauthenticated GitHub requests (60 requests/hour limit)."
        }
        export GITHUB_TOKEN
        log_info "Successfully retrieved GITHUB_TOKEN from pass"
    else
        log_warn "GITHUB_TOKEN not set and pass not available"
        echo "Please set your (read-only) GitHub token:"
        echo "  export GITHUB_TOKEN='your-token-here'"
        echo "Will continue with unauthenticated GitHub requests (60 requests/hour limit)."
    fi
fi

# Check if we need to look up VM IPs from Hetzner Cloud
NEED_LOOKUP_VM_IPS=false
if [[ -z "${PC_SWITCHER_TEST_PC1_HOST:-}" ]] || [[ -z "${PC_SWITCHER_TEST_PC2_HOST:-}" ]]; then
    NEED_LOOKUP_VM_IPS=true
fi

if [[ "$NEED_LOOKUP_VM_IPS" == "true" ]]; then
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
    if [[ -z "${PC_SWITCHER_TEST_PC1_HOST:-}" ]]; then
        PC1_IP=$(hcloud server ip pc1 2>/dev/null) || {
            log_error "Failed to get IP for pc1. Is the VM running?"
            echo "Check with: hcloud server list" >&2
            exit 1
        }
        log_info "pc1: $PC1_IP"
        export PC_SWITCHER_TEST_PC1_HOST="$PC1_IP"
    else
        log_info "Using pre-set PC_SWITCHER_TEST_PC1_HOST: $PC_SWITCHER_TEST_PC1_HOST"
    fi

    if [[ -z "${PC_SWITCHER_TEST_PC2_HOST:-}" ]]; then
        PC2_IP=$(hcloud server ip pc2 2>/dev/null) || {
            log_error "Failed to get IP for pc2. Is the VM running?"
            echo "Check with: hcloud server list" >&2
            exit 1
        }
        log_info "pc2: $PC2_IP"
        export PC_SWITCHER_TEST_PC2_HOST="$PC2_IP"
    else
        log_info "Using pre-set PC_SWITCHER_TEST_PC2_HOST: $PC_SWITCHER_TEST_PC2_HOST"
    fi

    # Update SSH known_hosts for newly looked up IPs
    log_info "Updating SSH known_hosts if needed..."
    for ip in "${PC1_IP:-}" "${PC2_IP:-}"; do
        if [[ -z "$ip" ]]; then
            continue
        fi
        NEW_KEYS=$(ssh-keyscan -H "$ip" 2>/dev/null)
        if [[ -n "$NEW_KEYS" ]]; then
            NEW_FPS=$(echo "$NEW_KEYS" | ssh-keygen -lf - | awk '{print $2}' | sort)
            OLD_FPS=$(ssh-keygen -F "$ip" 2>/dev/null | ssh-keygen -lf - | awk '{print $2}' | sort)

            if [[ "$NEW_FPS" != "$OLD_FPS" ]]; then
                ssh-keygen -R "$ip" 2>/dev/null || true
                echo "$NEW_KEYS" >> ~/.ssh/known_hosts
            fi
        fi
    done
else
    log_info "Using pre-set VM hosts:"
    log_info "  PC_SWITCHER_TEST_PC1_HOST=$PC_SWITCHER_TEST_PC1_HOST"
    log_info "  PC_SWITCHER_TEST_PC2_HOST=$PC_SWITCHER_TEST_PC2_HOST"
fi

# Set test user (use provided value or default to testuser)
if [[ -z "${PC_SWITCHER_TEST_USER:-}" ]]; then
    export PC_SWITCHER_TEST_USER="testuser"
    log_info "Using default PC_SWITCHER_TEST_USER: testuser"
else
    log_info "Using pre-set PC_SWITCHER_TEST_USER: $PC_SWITCHER_TEST_USER"
fi

log_info "Environment configured:"
log_info "  PC_SWITCHER_TEST_PC1_HOST=$PC_SWITCHER_TEST_PC1_HOST"
log_info "  PC_SWITCHER_TEST_PC2_HOST=$PC_SWITCHER_TEST_PC2_HOST"
log_info "  PC_SWITCHER_TEST_USER=$PC_SWITCHER_TEST_USER"

# =============================================================================
# VM Provisioning: Lock, Readiness Check, and Reset
# =============================================================================

# Acquire lock (cleanup trap is set up by acquire_lock)
log_step "Acquiring integration test lock..."
acquire_lock "pytest"

# Check VM readiness
log_info "Checking if test VMs are provisioned and ready..."

check_vm_ready() {
    local vm_host="$1"
    local user="$2"

    # Try to connect and check baseline snapshot exists
    if ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
        "${user}@${vm_host}" \
        "sudo btrfs subvolume show /.snapshots/baseline/@ >/dev/null 2>&1" 2>/dev/null; then
        return 0
    else
        return 1
    fi
}

# Check both VMs are ready
if ! check_vm_ready "$PC_SWITCHER_TEST_PC1_HOST" "$PC_SWITCHER_TEST_USER"; then
    log_error "pc1 test VM is not provisioned yet"
    log_info "Run the GitHub workflow 'Integration Tests' to provision test VMs"
    exit 1
fi

if ! check_vm_ready "$PC_SWITCHER_TEST_PC2_HOST" "$PC_SWITCHER_TEST_USER"; then
    log_error "pc2 test VM is not provisioned yet"
    log_info "Run the GitHub workflow 'Integration Tests' to provision test VMs"
    exit 1
fi

log_info "Test VMs are ready"

# Reset VMs to baseline (unless --skip-reset flag is set)
if [[ "$SKIP_RESET" == "false" ]]; then
    log_info "Resetting test VMs to baseline snapshots. This may take a few moments (typically 25-60 seconds)..."

    # Reset both VMs in parallel
    export LOG_PREFIX="pc1:" && "$RESET_SCRIPT" "$PC_SWITCHER_TEST_PC1_HOST" &
    PC1_RESET_PID=$!

    export LOG_PREFIX="pc2:" && "$RESET_SCRIPT" "$PC_SWITCHER_TEST_PC2_HOST" &
    PC2_RESET_PID=$!

    # Wait for both resets to complete
    if ! wait "$PC1_RESET_PID"; then
        log_error "pc1 reset failed"
        wait "$PC2_RESET_PID" || true  # Let pc2 finish
        exit 1
    fi

    if ! wait "$PC2_RESET_PID"; then
        log_error "pc2 reset failed"
        exit 1
    fi

    log_info "Test VMs reset complete"
else
    log_info "Skipping VM reset (--skip-reset flag)"
fi

# =============================================================================
# Run pytest
# =============================================================================

# Run pytest with all provided arguments
# Note: We don't use exec here because we need the EXIT trap to release the lock.
# We disable errexit temporarily to capture pytest's exit code.
log_info "Running pytest..."
cd "$PROJECT_ROOT"
set +e
uv run pytest -m "integration and not benchmark" -s "${PYTEST_ARGS[@]}"
pytest_exit_code=$?
set -e

exit $pytest_exit_code
