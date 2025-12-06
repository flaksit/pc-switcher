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
# Lock is stored as labels on the pc1 server:
#   lock_holder: Identifier of the current lock holder
#   lock_acquired: ISO8601 timestamp when lock was acquired

set -euo pipefail

readonly SERVER_NAME="pc1"
readonly LOCK_TIMEOUT_SECONDS=300  # 5 minutes
readonly RETRY_INTERVAL_SECONDS=10

# Verify HCLOUD_TOKEN is set
: "${HCLOUD_TOKEN:?HCLOUD_TOKEN environment variable must be set}"

# Parse arguments
if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <holder> <acquire|release|status>" >&2
    echo "  holder: Lock holder identifier (or empty string for status)" >&2
    echo "  action: acquire, release, or status" >&2
    exit 1
fi

readonly HOLDER="$1"
readonly ACTION="$2"

# Get current lock holder from server labels
get_lock_holder() {
    hcloud server describe "$SERVER_NAME" -o json | jq -r '.labels.lock_holder // empty'
}

# Get lock acquisition timestamp from server labels
get_lock_timestamp() {
    hcloud server describe "$SERVER_NAME" -o json | jq -r '.labels.lock_acquired // empty'
}

# Set lock labels
set_lock() {
    local holder="$1"
    local timestamp
    timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    hcloud server add-label "$SERVER_NAME" "lock_holder=$holder" --overwrite
    hcloud server add-label "$SERVER_NAME" "lock_acquired=$timestamp" --overwrite
}

# Remove lock labels
remove_lock() {
    hcloud server remove-label "$SERVER_NAME" "lock_holder" 2>/dev/null || true
    hcloud server remove-label "$SERVER_NAME" "lock_acquired" 2>/dev/null || true
}

# Display lock status
status() {
    local holder
    local timestamp

    holder=$(get_lock_holder)
    timestamp=$(get_lock_timestamp)

    if [[ -z "$holder" ]]; then
        echo "Lock is not held"
        return 0
    fi

    echo "Lock held by: $holder"
    echo "Acquired at: $timestamp"
    return 0
}

# Acquire lock with timeout and retry
acquire() {
    if [[ -z "$HOLDER" ]]; then
        echo "Error: holder must not be empty for acquire operation" >&2
        exit 1
    fi

    local start_time
    local current_time
    local elapsed
    local current_holder

    start_time=$(date +%s)

    while true; do
        current_holder=$(get_lock_holder)

        # Lock is free - try to acquire it
        if [[ -z "$current_holder" ]]; then
            echo "Lock is free, attempting to acquire..."
            set_lock "$HOLDER"

            # Verify we got the lock (check for race condition)
            sleep 1
            current_holder=$(get_lock_holder)

            if [[ "$current_holder" == "$HOLDER" ]]; then
                echo "Successfully acquired lock as $HOLDER"
                return 0
            else
                echo "Race condition detected: lock acquired by $current_holder, retrying..."
            fi
        # Lock is held by us already
        elif [[ "$current_holder" == "$HOLDER" ]]; then
            echo "Lock already held by $HOLDER"
            return 0
        # Lock is held by someone else
        else
            local timestamp
            timestamp=$(get_lock_timestamp)
            echo "Lock held by $current_holder (since $timestamp), waiting..."
        fi

        # Check timeout
        current_time=$(date +%s)
        elapsed=$((current_time - start_time))

        if [[ $elapsed -ge $LOCK_TIMEOUT_SECONDS ]]; then
            echo "Error: Timeout waiting for lock (waited ${elapsed}s)" >&2
            echo "Lock is still held by: $current_holder" >&2
            exit 1
        fi

        # Wait before retry
        sleep "$RETRY_INTERVAL_SECONDS"
    done
}

# Release lock
release() {
    if [[ -z "$HOLDER" ]]; then
        echo "Error: holder must not be empty for release operation" >&2
        exit 1
    fi

    local current_holder
    current_holder=$(get_lock_holder)

    # Lock not held
    if [[ -z "$current_holder" ]]; then
        echo "Lock is not held, nothing to release"
        return 0
    fi

    # Lock held by us - release it
    if [[ "$current_holder" == "$HOLDER" ]]; then
        echo "Releasing lock held by $HOLDER"
        remove_lock
        echo "Lock released successfully"
        return 0
    fi

    # Lock held by someone else - error
    echo "Error: Cannot release lock held by $current_holder (requested by $HOLDER)" >&2
    exit 1
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
        echo "Error: Invalid action '$ACTION'. Must be one of: acquire, release, status" >&2
        exit 1
        ;;
esac
