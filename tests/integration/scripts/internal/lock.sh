#!/usr/bin/env bash
# Integration test lock manager using Hetzner Server Labels
#
# Usage:
#   lock.sh <holder> <acquire|release|status>
#
# Arguments:
#   holder: Lock holder identifier (e.g., "github-123456", username, or "" for status)
#   action: acquire, release, or status
#
# Environment:
#   HCLOUD_TOKEN: Required for Hetzner API access
#
# Locks are stored as labels on both pc1 and pc2 servers:
#   lock_holder: Identifier of the current lock holder
#   lock_acquired: ISO8601 timestamp when lock was acquired

set -euo pipefail

# Source common helpers
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

# Help
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: $(basename "$0") <holder> <acquire|release|status>

Manage integration test lock using Hetzner Server Labels.

Arguments:
  holder    Lock holder identifier (e.g., "github-123456", username)
  action    One of: acquire, release, status

Actions:
  acquire   Acquire lock
  release   Release lock (must be the current holder)
  status    Show current lock status
  clear     Release all locks (regardless of holder)

Environment Variables:
  HCLOUD_TOKEN    (required) Hetzner Cloud API token

Examples:
  $(basename "$0") github-123456 acquire
  $(basename "$0") github-123456 release
  $(basename "$0") "" status
EOF
    exit 0
fi


# Verify HCLOUD_TOKEN is set
: "${HCLOUD_TOKEN:?HCLOUD_TOKEN environment variable must be set}"

# Parse arguments
if [[ $# -lt 2 ]]; then
    log_error "Expected 2 arguments, got $#"
    echo "Usage: $(basename "$0") <holder> <acquire|release|status>" >&2
    echo "Run with -h for help" >&2
    exit 1
fi

readonly HOLDER="$1"
readonly ACTION="$2"

get_lock_holder() {
    # Verify HCLOUD_TOKEN is set
    if [[ -z "${HCLOUD_TOKEN:-}" ]]; then
        log_error "HCLOUD_TOKEN environment variable must be set"
        return 1
    fi

    hcloud server describe "$LOCK_SERVER" -o json | jq -r '.labels.lock_holder // empty'
}

# Get lock acquisition timestamp from server labels
get_lock_timestamp() {
    hcloud server describe "$LOCK_SERVER" -o json | jq -r '.labels.lock_acquired // empty'
}

# Set lock labels
set_lock() {
    local holder="$1"
    local timestamp
    # Use format without colons (not valid in Hetzner labels)
    timestamp=$(date -u +"%Y%m%d-%H%M%SZ")

    hcloud server add-label "$LOCK_SERVER" "lock_holder=$holder"
    hcloud server add-label "$LOCK_SERVER" "lock_acquired=$timestamp"
}

# Remove lock labels
remove_lock() {
    hcloud server remove-label "$LOCK_SERVER" "lock_holder" 2>/dev/null || true
    hcloud server remove-label "$LOCK_SERVER" "lock_acquired" 2>/dev/null || true
}

# Display lock status
status() {
    local holder="$(get_lock_holder)"
    local timestamp="$(get_lock_timestamp)"

    if [[ -n "$holder" ]]; then
        log_info "Lock held by $holder (acquired at $timestamp)"
    else
        log_info "Lock not held"
    fi
}

# Acquire locks on all VMs with timeout and retry
acquire() {
    if [[ -z "$HOLDER" ]]; then
        log_error "HOLDER must not be empty for acquire operation"
        exit 1
    fi

    local holder=$(get_lock_holder)

    if [[ -n "$holder" ]]; then
        if [[ "$holder" == "$HOLDER" ]]; then
            log_info "Lock already held by $HOLDER"
        else
            log_error "Lock held by $holder"
            exit 1
        fi
    else
        set_lock "$HOLDER"
        log_info "Lock acquired by $HOLDER"
    fi
}

# Release our locks on all VMs
release() {
    if [[ -z "$HOLDER" ]]; then
        log_error "HOLDER must not be empty for release operation"
        exit 1
    fi

    local holder=$(get_lock_holder)

    if [[ -n "$holder" ]]; then
        if [[ "$holder" == "$HOLDER" ]]; then
            remove_lock
            log_step "Lock released successfully"
        else
            log_error "Cannot release lock held by other holder $holder"
            exit 1
        fi
    else
        log_info "Lock was not held; nothing to release"
    fi
}

# Clear: release lock regardless of holder
clear() {
    log_info "Cleaning up: Releasing lock"
    remove_lock
    log_step "Lock released"
}

# Execute requested action
case "$ACTION" in
    get_lock_holder)
        get_lock_holder
        ;;
    status)
        status
        ;;
    acquire)
        acquire
        ;;
    release)
        release
        ;;
    clear)
        clear
        ;;
    *)
        log_error "Invalid action '$ACTION'. Must be one of: acquire, release, cleanup, status"
        exit 1
        ;;
esac
