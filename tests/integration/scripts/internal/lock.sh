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
  acquire   Acquire locks on both VMs
  release   Release locks on both VMs (must be the current holder)
  status    Show current lock status for both VMs

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

# Get lock acquisition timestamp from server labels
get_server_lock_timestamp() {
    local server_name="$1"
    hcloud server describe "$server_name" -o json | jq -r '.labels.lock_acquired // empty'
}

# Set lock labels on a server
set_server_lock() {
    local server_name="$1"
    local holder="$2"
    local timestamp
    # Use format without colons (not valid in Hetzner labels)
    timestamp=$(date -u +"%Y%m%d-%H%M%S")

    hcloud server add-label "$server_name" "lock_holder=$holder" --overwrite
    hcloud server add-label "$server_name" "lock_acquired=$timestamp" --overwrite
}

# Remove lock labels from a server
remove_server_lock() {
    local server_name="$1"
    hcloud server remove-label "$server_name" "lock_holder" 2>/dev/null || true
    hcloud server remove-label "$server_name" "lock_acquired" 2>/dev/null || true
}

# Display lock status for all VMs
status() {
    local holders=()
    local timestamps=()

    # Gather status for all servers
    for server in "${SERVER_NAMES[@]}"; do
        holders+=("$(_get_server_lock_holder "$server")")
        timestamps+=("$(get_server_lock_timestamp "$server")")
    done

    # Check if any locks are held
    local any_held=false
    for holder in "${holders[@]}"; do
        [[ -n "$holder" ]] && any_held=true && break
    done

    if [[ "$any_held" == "false" ]]; then
        log_info "Locks are not held on any VM"
        return 0
    fi

    # Display status for each server
    log_info "Lock status:"
    for i in "${!SERVER_NAMES[@]}"; do
        local server="${SERVER_NAMES[$i]}"
        local holder="${holders[$i]}"
        local timestamp="${timestamps[$i]}"

        if [[ -n "$holder" ]]; then
            log_info "  $server: Held by $holder (acquired at $timestamp)"
        else
            log_info "  $server: Not held"
        fi
    done

    # Detect inconsistent state (different holders on different VMs)
    local first_holder=""
    local inconsistent=false
    for holder in "${holders[@]}"; do
        if [[ -n "$holder" ]]; then
            if [[ -z "$first_holder" ]]; then
                first_holder="$holder"
            elif [[ "$holder" != "$first_holder" ]]; then
                inconsistent=true
                break
            fi
        fi
    done

    if [[ "$inconsistent" == "true" ]]; then
        log_error "Inconsistent lock state detected! VMs are locked by different holders."
        return 1
    fi

    return 0
}

# Acquire locks on all VMs with timeout and retry
acquire() {
    if [[ -z "$HOLDER" ]]; then
        log_error "HOLDER must not be empty for acquire operation"
        exit 1
    fi

    # Check current holders for all servers
    local servers_to_lock=()

    # Check if any locks are held by others
    # Exit if so
    for server in "${SERVER_NAMES[@]}"; do
        local holder
        holder=$(_get_server_lock_holder "$server")
        
        if [[ -n "$holder" ]]; then
            if [[ "$holder" != "$HOLDER" ]]; then
                log_error "Lock on $server held by: $holder"
                exit 1
            fi
        else
            servers_to_lock+=("$server")
        fi
    done

    if [[ "${#servers_to_lock[@]}" -eq 0 ]]; then
        log_info "Locks already held by $HOLDER on all VMs"
        return 0
    fi

    # All or some are free --> acquire locks on servers where not held yet
    for server in "${servers_to_lock[@]}"; do
        set_server_lock "$server" "$HOLDER"
    done
}

# Release our locks on all VMs
release() {
    if [[ -z "$HOLDER" ]]; then
        log_error "HOLDER must not be empty for release operation"
        exit 1
    fi

    local any_held_by_others=false
    local any_held_by_us=false

    # Check current holders
    for server in "${SERVER_NAMES[@]}"; do
        local holder
        holder=$(_get_server_lock_holder "$server")
        holders+=("$holder")

        if [[ -n "$holder" ]]; then
            if [[ "$holder" == "$HOLDER" ]]; then
                log_info "Releasing lock on $server held by us ($HOLDER)"
                remove_server_lock "$server"
                any_held_by_us=true
            else
                log_error "Cannot release lock on $server held by $holder"
                any_held_by_others=true
            fi
        fi
    done

    if [[ "$any_held_by_others" == "true" ]]; then
        log_error "Some locks are held by another process; cannot release all locks"
        exit 1
    elif [[ "$any_held_by_us" == "true" ]]; then
        log_step "Locks released successfully"
    else
        log_step "There are no locks; nothing to release"
    fi
}

# Clear: release all locks on all VMs, regardless of holder
clear() {
    log_info "Cleaning up: Releasing all locks on all VMs"
    for server in "${SERVER_NAMES[@]}"; do
        remove_server_lock "$server"
    done
    log_step "All locks released"
}

# Execute requested action
case "$ACTION" in
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
