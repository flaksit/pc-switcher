#!/usr/bin/env bash
# Common SSH helpers for test infrastructure scripts.
#
# This file provides consistent SSH connection handling across all infrastructure scripts.
# Source it at the top of each script: source "$(dirname "$0")/ssh-common.sh"
#
# See docs/testing-infrastructure.md for the SSH host key management strategy.

# Phase transition: wait for SSH to become available (removes old key, accepts new)
# Use when host key has legitimately changed (e.g., VM reinstalled).
#
# Usage: wait_for_ssh <host> [timeout_seconds] [user]
# Example: wait_for_ssh 192.168.1.100 120 root
wait_for_ssh() {
    local host="$1"
    local timeout="${2:-120}"
    local user="${3:-root}"

    # Remove old key since we expect the host key to have changed
    ssh-keygen -R "$host" 2>/dev/null || true

    local deadline
    deadline=$(($(date +%s) + timeout))

    while (($(date +%s) < deadline)); do
        if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -o BatchMode=yes "${user}@${host}" true 2>/dev/null; then
            return 0
        fi
        sleep 5
    done
    return 1
}

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
# Usage: ssh_run <user@host> [ssh args...]
# Example: ssh_run testuser@192.168.1.100 "sudo btrfs subvolume list /"
ssh_run() {
    ssh -o ConnectTimeout=10 -o BatchMode=yes "$@"
}
