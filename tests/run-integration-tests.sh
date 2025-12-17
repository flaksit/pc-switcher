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
#   ./run-integration-tests.sh tests/integration -m integration          # Run all integration tests
#   ./run-integration-tests.sh tests/integration/test_vm_connectivity.py # Run specific test file
#   ./run-integration-tests.sh -k "test_ssh" -v                          # Run tests matching pattern
#   ./run-integration-tests.sh tests/integration/test_executor_overhead.py::TestRemoteExecutorOverhead::test_no_op_command_overhead -s
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
NEED_LOOKUP=false
if [[ -z "${PC_SWITCHER_TEST_PC1_HOST:-}" ]] || [[ -z "${PC_SWITCHER_TEST_PC2_HOST:-}" ]]; then
    NEED_LOOKUP=true
fi

if [[ "$NEED_LOOKUP" == "true" ]]; then
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

LOCK_SCRIPT="$SCRIPT_DIR/integration/scripts/internal/lock.sh"
RESET_SCRIPT="$SCRIPT_DIR/integration/scripts/reset-vm.sh"

# Generate lock holder ID
generate_lock_holder_id() {
    local ci_job_id="${CI_JOB_ID:-${GITHUB_RUN_ID:-}}"
    if [[ -n "$ci_job_id" ]]; then
        echo "ci-${ci_job_id}-pytest"
    else
        local hostname
        hostname=$(hostname)
        local user="${USER:-unknown}"
        # Add random suffix to ensure uniqueness per invocation
        local random_suffix
        random_suffix=$(openssl rand -hex 3)  # 6 hex characters
        echo "${user}-${hostname}-pytest-${random_suffix}"
    fi
}

# Acquire lock
log_info "Acquiring integration test lock..."
LOCK_HOLDER=$(generate_lock_holder_id)
if ! "$LOCK_SCRIPT" acquire "$LOCK_HOLDER"; then
    log_error "Failed to acquire integration test lock"
    exit 1
fi
export PCSWITCHER_LOCK_HOLDER="$LOCK_HOLDER"
log_info "Lock acquired with holder ID: $LOCK_HOLDER"

# Set up cleanup trap to release lock on exit/error
cleanup_lock() {
    if [[ -n "${PCSWITCHER_LOCK_HOLDER:-}" ]]; then
        log_info "Releasing lock: $PCSWITCHER_LOCK_HOLDER"
        "$LOCK_SCRIPT" release "$PCSWITCHER_LOCK_HOLDER" 2>/dev/null || true
    fi
}
trap cleanup_lock EXIT INT TERM

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

# Reset VMs to baseline (unless skip flag is set)
if [[ -z "${PC_SWITCHER_SKIP_RESET:-}" ]]; then
    log_info "Resetting test VMs to baseline snapshots..."

    # Reset both VMs in parallel
    "$RESET_SCRIPT" "$PC_SWITCHER_TEST_PC1_HOST" &
    PC1_RESET_PID=$!

    "$RESET_SCRIPT" "$PC_SWITCHER_TEST_PC2_HOST" &
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
    log_info "Skipping VM reset (PC_SWITCHER_SKIP_RESET is set)"
fi

# =============================================================================
# Run pytest
# =============================================================================

# Run pytest with all provided arguments
log_info "Running pytest..."
cd "$PROJECT_ROOT"
exec uv run pytest -m "integration and not benchmark" -v -s "$@"
