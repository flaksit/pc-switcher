#!/usr/bin/env bash
# Common helpers for test infrastructure scripts.
#
# This file provides:
# - Color definitions
# - Logging functions (standard and prefixed for parallel execution)
# - SSH connection handling with ControlMaster multiplexing
#
# Source it at the top of each script: source "$(dirname "$0")/common.sh"
#
# See docs/testing-infrastructure.md for the SSH host key management strategy.

# =============================================================================
# Color Definitions
# =============================================================================

readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly CYAN='\033[0;36m'
readonly NC='\033[0m' # No Color

# =============================================================================
# Logging Functions
# =============================================================================

# Standard logging functions (used by most scripts)
log_step() { echo -e "${GREEN}==>${NC} $*"; }
log_info() { echo "    $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# Logging with prefix (for parallel execution clarity)
# Usage: Set LOG_PREFIX before calling, e.g.: LOG_PREFIX=" ${CYAN}[pc1]${NC}"
log_step_prefixed() { echo -e "${GREEN}==>${NC}${LOG_PREFIX:-} $*"; }
log_info_prefixed() { echo -e "   ${LOG_PREFIX:-} $*"; }
log_warn_prefixed() { echo -e "${YELLOW}[WARN]${NC}${LOG_PREFIX:-} $*" >&2; }
log_error_prefixed() { echo -e "${RED}[ERROR]${NC}${LOG_PREFIX:-} $*" >&2; }

# =============================================================================
# Lock Holder ID Generation
# =============================================================================

# Get lock holder identifier for integration test locking
# Returns: "ci-{job_id}" in CI, "local-{user}-{hostname}-{random}" for local runs
# Format must be compatible with Hetzner Cloud labels (alphanumeric, dash, underscore only)
#
# CRITICAL: Lock holder ID must be UNIQUE PER INVOCATION to prevent concurrent runs
# on the same machine by the same user. For nested script calls (e.g., provision-test-infra.sh
# calling other scripts), the ID is passed via PCSWITCHER_LOCK_HOLDER environment variable.
get_lock_holder() {
    # If PCSWITCHER_LOCK_HOLDER is already set, reuse it (nested script call)
    if [[ -n "${PCSWITCHER_LOCK_HOLDER:-}" ]]; then
        echo "$PCSWITCHER_LOCK_HOLDER"
        return
    fi

    # Generate new unique ID
    local ci_job_id="${CI_JOB_ID:-${GITHUB_RUN_ID:-}}"

    if [[ -n "$ci_job_id" ]]; then
        echo "ci-${ci_job_id}"
    else
        local hostname
        hostname=$(hostname)
        local user="${USER:-unknown}"
        # Add random suffix to ensure uniqueness per invocation
        local random
        random=$(openssl rand -hex 3)  # 6 hex characters
        echo "local-${user}-${hostname}-${random}"
    fi
}

# =============================================================================
# SSH Helper Functions
# =============================================================================

# Phase transition: single SSH connection (removes old key, accepts new)
# Use for one-off connections when host key has legitimately changed.
#
# Usage: ssh_first <user@host> [ssh args...]
# Example: ssh_first root@192.168.1.100 "echo hello"
ssh_first() {
    local userhost="$1"
    local host="${userhost#*@}"
    shift
    # Remove old key since we expect the host key to have changed
    ssh-keygen -R "$host" 2>/dev/null || true
    ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes "$userhost" "$@"
}

# First connection: accepts new key if missing, verifies if present
# Use when key might not exist in known_hosts but shouldn't be removed if it does.
# Safe for both fresh environments (empty known_hosts) and existing environments.
#
# Usage: ssh_accept_new <user@host> [ssh args...]
# Example: ssh_accept_new testuser@192.168.1.100 "echo hello"
ssh_accept_new() {
    ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes "$@"
}

# Subsequent connections: normal SSH (verifies stored key)
# Use after key has been established by wait_for_ssh, ssh_first, or ssh_accept_new.
# Fails if host key is not in known_hosts or doesn't match.
#
# Uses SSH ControlMaster for connection multiplexing to reduce overhead.
# First call creates a master connection, subsequent calls reuse it.
#
# Usage: ssh_run <user@host> [ssh args...]
# Example: ssh_run testuser@192.168.1.100 "sudo btrfs subvolume list /"
ssh_run() {
    local control_path="/tmp/pcswitcher-ssh-%C"
    ssh -o BatchMode=yes \
        -o ControlMaster=auto \
        -o ControlPath="$control_path" \
        -o ControlPersist=60 \
        "$@"
}

# wait_for_ssh - Wait for SSH to become available with progress display
#
# Parameters:
#   $1: userhost - user@host format (required)
#   $2: timeout - Timeout in seconds (default: 120)
#   $3: REMOVE_KEY - Pass literal "REMOVE_KEY" to remove existing host key first
#                    (phase transition: removes old key, accepts new key)
#
# Output: Shows progress every 5 seconds: "Waiting for SSH... (15s / 120s)"
#
# Returns: 0 if SSH becomes available, 1 on timeout
#
# Examples:
#   wait_for_ssh "testuser@192.168.1.100" 300           # Normal wait
#   wait_for_ssh "root@$vm_ip" 120 REMOVE_KEY           # Phase transition
wait_for_ssh() {
    local userhost="$1"
    local timeout="${2:-120}"
    local remove_key_flag="${3:-}"

    local host="${userhost#*@}"

    # Phase transition: remove old key and accept new
    if [[ "$remove_key_flag" == "REMOVE_KEY" ]]; then
        ssh-keygen -R "$host" 2>/dev/null || true
    elif [[ -n "$remove_key_flag" ]]; then
        log_error "Invalid flag '$remove_key_flag'. Use 'REMOVE_KEY' or omit."
        return 1
    fi

    local start_time deadline elapsed
    start_time=$(date +%s)
    deadline=$((start_time + timeout))

    # Determine SSH options based on phase transition
    local ssh_opts="-o ConnectTimeout=5 -o BatchMode=yes"
    if [[ "$remove_key_flag" == "REMOVE_KEY" ]]; then
        ssh_opts="$ssh_opts -o StrictHostKeyChecking=accept-new"
    fi

    while (($(date +%s) < deadline)); do
        elapsed=$(($(date +%s) - start_time))

        # shellcheck disable=SC2086
        if ssh_run $ssh_opts "$userhost" true 2>/dev/null; then
            log_info "SSH ready after ${elapsed}s"
            return 0
        fi

        log_info "Waiting for SSH... (${elapsed}s / ${timeout}s)"
        sleep 5
    done

    log_error "SSH timeout after ${timeout}s"
    return 1
}
