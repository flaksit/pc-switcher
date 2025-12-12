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
  acquire   Acquire locks on both VMs (waits up to 5 minutes if held by another)
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

readonly SERVER_NAMES=("pc1" "pc2")
readonly LOCK_TIMEOUT_SECONDS=0  # Fail immediately if locks not available
readonly RETRY_INTERVAL_SECONDS=10

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

# Get current lock holder from server labels
get_server_lock_holder() {
    local server_name="$1"
    hcloud server describe "$server_name" -o json | jq -r '.labels.lock_holder // empty'
}

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
        holders+=("$(get_server_lock_holder "$server")")
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
        log_error "holder must not be empty for acquire operation"
        exit 1
    fi

    local start_time
    local current_time
    local elapsed

    start_time=$(date +%s)

    while true; do
        # Check current holders for all servers
        local all_free=true
        local all_ours=true
        local we_hold_some=false
        local we_hold_all=true
        local holders=()

        for server in "${SERVER_NAMES[@]}"; do
            local holder
            holder=$(get_server_lock_holder "$server")
            holders+=("$holder")

            if [[ -n "$holder" ]]; then
                all_free=false
                if [[ "$holder" == "$HOLDER" ]]; then
                    we_hold_some=true
                else
                    all_ours=false
                    we_hold_all=false
                fi
            else
                we_hold_all=false
            fi
        done

        # Detect inconsistent state (different non-empty holders on different VMs)
        # This should never happen and requires manual intervention
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
            log_error "Inconsistent lock state detected! VMs are locked by different holders:"
            for i in "${!SERVER_NAMES[@]}"; do
                local server="${SERVER_NAMES[$i]}"
                local holder="${holders[$i]}"
                if [[ -n "$holder" ]]; then
                    log_error "  $server: $holder"
                fi
            done
            log_error "This requires manual intervention. Run 'lock.sh \"\" status' for details."
            exit 1
        fi

        # Detect partial lock state (we hold some but not all locks)
        # Only proceed if the locks we don't hold are FREE (not held by someone else)
        if [[ "$we_hold_some" == "true" && "$we_hold_all" == "false" ]]; then
            # Check if any VM is held by someone other than us
            local others_hold_locks=false
            for holder in "${holders[@]}"; do
                if [[ -n "$holder" && "$holder" != "$HOLDER" ]]; then
                    others_hold_locks=true
                    break
                fi
            done

            if [[ "$others_hold_locks" == "true" ]]; then
                # This should have been caught by inconsistent state check above
                # But adding explicit check for safety
                log_error "Partial lock conflict: we hold some VMs but others are held by different holder"
                exit 1
            fi

            # Safe partial lock cleanup: we hold some, others are free
            log_warn "Partial lock detected (likely from interrupted previous run), cleaning up..."
            for server in "${SERVER_NAMES[@]}"; do
                local holder
                holder=$(get_server_lock_holder "$server")
                if [[ "$holder" == "$HOLDER" ]]; then
                    remove_server_lock "$server"
                fi
            done
            # Continue to retry acquisition
            
        # Case 1: All locks are free - try to acquire
        elif [[ "$all_free" == "true" ]]; then
            log_info "All locks are free, attempting to acquire..."

            # Acquire locks sequentially in order
            for server in "${SERVER_NAMES[@]}"; do
                set_server_lock "$server" "$HOLDER"
            done

            # Verify we got all locks (check for race condition)
            sleep 1
            local verify_success=true
            for server in "${SERVER_NAMES[@]}"; do
                local current_holder
                current_holder=$(get_server_lock_holder "$server")
                if [[ "$current_holder" != "$HOLDER" ]]; then
                    log_warn "Race condition detected on $server: lock acquired by $current_holder, retrying..."
                    verify_success=false
                    break
                fi
            done

            if [[ "$verify_success" == "true" ]]; then
                log_step "Successfully acquired locks on all VMs as $HOLDER"
                return 0
            else
                # Race detected - release any locks we might have and retry
                for server in "${SERVER_NAMES[@]}"; do
                    remove_server_lock "$server"
                done
            fi

        # Case 2: All locks are held by us already
        elif [[ "$all_ours" == "true" ]]; then
            log_info "Locks already held by $HOLDER on all VMs"
            return 0

        # Case 3: Locks are held by someone else
        else
            local holder_list=""
            for i in "${!SERVER_NAMES[@]}"; do
                local server="${SERVER_NAMES[$i]}"
                local holder="${holders[$i]}"
                if [[ -n "$holder" ]]; then
                    local timestamp
                    timestamp=$(get_server_lock_timestamp "$server")
                    holder_list="$holder_list\n  $server: $holder (since $timestamp)"
                fi
            done
            log_info "Locks held by:$holder_list"
            log_info "Waiting..."
        fi

        # Check timeout
        current_time=$(date +%s)
        elapsed=$((current_time - start_time))

        if [[ $elapsed -ge $LOCK_TIMEOUT_SECONDS ]]; then
            log_error "Timeout waiting for locks (waited ${elapsed}s)"
            log_error "Current lock holders:"
            for i in "${!SERVER_NAMES[@]}"; do
                local server="${SERVER_NAMES[$i]}"
                local holder="${holders[$i]}"
                if [[ -n "$holder" ]]; then
                    log_error "  $server: $holder"
                else
                    log_error "  $server: (free)"
                fi
            done
            exit 1
        fi

        # Wait before retry
        sleep "$RETRY_INTERVAL_SECONDS"
    done
}

# Release locks on all VMs
release() {
    if [[ -z "$HOLDER" ]]; then
        log_error "holder must not be empty for release operation"
        exit 1
    fi

    local holders=()
    local any_held_by_us=false
    local any_held_by_others=false

    # Check current holders
    for server in "${SERVER_NAMES[@]}"; do
        local holder
        holder=$(get_server_lock_holder "$server")
        holders+=("$holder")

        if [[ -n "$holder" ]]; then
            if [[ "$holder" == "$HOLDER" ]]; then
                any_held_by_us=true
            else
                any_held_by_others=true
            fi
        fi
    done

    # No locks held
    if [[ "$any_held_by_us" == "false" && "$any_held_by_others" == "false" ]]; then
        log_info "Locks are not held on any VM, nothing to release"
        return 0
    fi

    # Some locks held by others
    if [[ "$any_held_by_others" == "true" ]]; then
        log_error "Cannot release locks held by another process:"
        for i in "${!SERVER_NAMES[@]}"; do
            local server="${SERVER_NAMES[$i]}"
            local holder="${holders[$i]}"
            if [[ -n "$holder" && "$holder" != "$HOLDER" ]]; then
                log_error "  $server: $holder"
            fi
        done
        exit 1
    fi

    # All locks (if any) are held by us - release them
    log_info "Releasing locks held by $HOLDER"
    for server in "${SERVER_NAMES[@]}"; do
        remove_server_lock "$server"
    done
    log_step "Locks released successfully"
    return 0
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
    *)
        log_error "Invalid action '$ACTION'. Must be one of: acquire, release, status"
        exit 1
        ;;
esac
