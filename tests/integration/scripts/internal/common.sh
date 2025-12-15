#!/usr/bin/env bash
# Common helpers for test infrastructure scripts.
#
# This file provides:
# - Color definitions
# - Logging functions (standard and prefixed for parallel execution)
# - SSH connection handling with ControlMaster multiplexing
#
# Source it at the top of each script:
#   From scripts/: source "$SCRIPT_DIR/internal/common.sh"
#   From scripts/internal/: source "$SCRIPT_DIR/common.sh"
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
# Lock Helper Functions
# =============================================================================

readonly SERVER_NAMES=("pc1" "pc2")

# Get lock holder identifier for integration test locking, including optional name
# Returns: "ci-{job_id}-{name}" in CI, "{user}-{hostname}-{name}-{random}" for local runs
# Parameters:
#   $1: name - Descriptive name to include in the lock holder ID (optional
#              Formatted to be compatible with Hetzner Cloud labels (alphanumeric, dash, underscore only)
generate_lock_holder() {
    local name="$1"
    if [[ -n "$name" ]]; then
        name="-${name//[^a-zA-Z0-9_-]/_}"
    fi

    # Generate new unique ID
    local ci_job_id="${CI_JOB_ID:-${GITHUB_RUN_ID:-}}"

    if [[ -n "$ci_job_id" ]]; then
        echo "ci-${ci_job_id}${name}"
    else
        local hostname
        hostname=$(hostname)
        local user="${USER:-unknown}"
        # Add random suffix to ensure uniqueness per invocation
        local random
        random=$(openssl rand -hex 3)  # 6 hex characters
        echo "${user}-${hostname}${name}-${random}"
    fi
}


#
# Get current lock holder from server labels
#
# Parameters:
#   $1: server_name - Name of the Hetzner Cloud server
#
# Output: lock holder string or empty if not held
#
# Environment Variables:
#   HCLOUD_TOKEN    (required) Hetzner Cloud API token
#
_get_server_lock_holder() {
    local server_name="$1"
    hcloud server describe "$server_name" -o json | jq -r '.labels.lock_holder // empty'
}


#
# get_lock_holder - Get current lock holder of all test VMs
#
# Output:  lock holder string or empty if not held or inconsistent
#
# Returns: 0 if lock is held or not held, in a consistent state
#          1 if locks are held in inconsistent state: different holders on different VMs
#
get_lock_holder() {
    # Verify HCLOUD_TOKEN is set
    if [[ -z "${HCLOUD_TOKEN:-}" ]]; then
        log_error "HCLOUD_TOKEN environment variable must be set"
        return 1
    fi

    # Gather status for all servers
    local holders=()

    for server in "${SERVER_NAMES[@]}"; do
        holders+=("$(_get_server_lock_holder "$server")")
    done

    # Check if all holders are the same
    # Return 1 if any differ
    for holder in "${holders[@]:1}"; do
        [[ "$holder" != "${holders[0]}" ]] && return 1
    done

    # All are the same - Only echo when a holder is present
    if [[ -n "${holders[0]}" ]]; then
        echo "${holders[0]}"
    fi
    return 0
}


#
# acquire_lock - Acquire lock on the test-VMs and register cleanup
#
# If PCSWITCHER_LOCK_HOLDER environment variable is set,
# this function assumes the lock is already held by the parent process. This function
# checks the lock is effectively held and fails if not.
# Otherwise a new unique lock holder ID is generated that includes the given name,
# the lock is acquired and PCSWITCHER_LOCK_HOLDER is set.
#
# Parameters:
#   $1: name - Descriptive name for the script/function/program acquiring the lock (optional)
#
# Exits with: 1 if lock acquisition fails
#             2 if locks are in an inconsistent state
#             3 if PCSWITCHER_LOCK_HOLDER was set, but lock not held
#
# Environment Variables:
#   PCSWITCHER_LOCK_HOLDER - If set, indicates the lock is already held
#                            If not set, this function will acquire the lock and set it
#
acquire_lock() {
    local name="$1"
    if [[ -n "${PCSWITCHER_LOCK_HOLDER:-}" ]]; then
        # Called from parent with lock - verify it's actually held
        local current_holder="$(get_lock_holder)"
        local rc=$?
        if [[ $rc -ne 0 ]]; then
            log_error "Failed to verify existing lock holder"
            exit 2
        fi
        if [[ "${current_holder}" != "$PCSWITCHER_LOCK_HOLDER" ]]; then
            log_error "PCSWITCHER_LOCK_HOLDER is set, but lock is not held"
            exit 3
        fi
        log_info "Using inherited lock from parent (holder: $PCSWITCHER_LOCK_HOLDER)"
    else
        # Not called from parent with lock - acquire our own
        local holder=$(generate_lock_holder "$name")

        # Set up cleanup trap only if we own the lock
        cleanup_lock() {
            "$SCRIPT_DIR/internal/lock.sh" "$PCSWITCHER_LOCK_HOLDER" release 2>/dev/null || true
        }
        trap "cleanup_lock; $(trap -p EXIT | cut -f2 -d \')" EXIT
        trap "cleanup_lock; $(trap -p INT | cut -f2 -d \')" INT
        trap "cleanup_lock; $(trap -p TERM | cut -f2 -d \')" TERM

        # Acquire lock
        if ! "$SCRIPT_DIR/internal/lock.sh" "$holder" acquire; then
            log_error "Failed to acquire lock"
            exit 1
        fi
        export PCSWITCHER_LOCK_HOLDER="$holder"
        log_info "Lock acquired: $PCSWITCHER_LOCK_HOLDER"
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

# File transfers reusing the ControlMaster connection established by ssh_run.
#
# Usage: scp_run <source> <dest>
# Example: scp_run testuser@192.168.1.100:/tmp/file /tmp/file
scp_run() {
    local control_path="/tmp/pcswitcher-ssh-%C"
    scp -o BatchMode=yes \
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

