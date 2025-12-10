# ADR-002: SSH as Communication Channel between Source and Target

Status: Accepted
Date: 2025-11-14

## TL;DR
Use SSH as the communication channel for orchestration between source and target machines, with the user specifying target via SSH hostname (respecting `~/.ssh/config`).

## Implementation Rules

**Required Patterns:**
- Single persistent SSH connection with multiplexing (`ControlMaster`) for multi-phase operations
- Target-side scripts produce line-buffered progress output to stdout (explicit flush after each message)
- Source implementation using proven SSH libraries
- Signal handlers on source to catch `SIGINT` and send explicit cleanup commands to target before connection close
- Exit codes for success/failure signaling; separate stdout/stderr for logs and errors

**Forbidden Approaches:**
- Do not build custom protocols or daemons on target machine
- Do not poll for progress (use streaming stdout instead)
- Do not create new SSH connections per operation (reuse via multiplexing)
- Do not require persistent services running on target outside of sync operation

## Context

PC-switcher needs to communicate with the target machine to orchestrate multi-phase sync operations:
- Execute scripts and services on target (with elevated privileges)
- Receive real-time progress updates and log messages (target → source)
- Handle validation errors before sync begins
- Gracefully handle user interrupts (Ctrl+C) with cleanup

See `docs/adr/considerations/adr-002-ssh-communication-channel.md` for detailed analysis including:
- Requirements analysis (command execution, bidirectional communication, progress reporting, error handling, security)
- SSH strengths and challenges addressed
- Evaluation of alternatives (custom protocols, gRPC, REST APIs, message queues, Ansible)
- Implementation patterns with code examples

## Decision

- Use SSH as the communication channel for all orchestration commands and feedback between source and target
- Orchestration logic (command sequencing, error handling, phase management) lives in the source application
- Target exposes discrete, stateless scripts that SSH invokes; target sends feedback via stdout/stderr
- File syncing protocols (rsync, rclone, custom) are invoked through this SSH communication channel
- User provides target via `hostname` argument with support for SSH config aliases, custom ports, users, keys

**Rationale:**
- Aligns with project principles: proven tooling, deliberate simplicity
- Meets all functional requirements without additional infrastructure
- Zero additional services on target machine
- Respects existing SSH infrastructure and user mental models (FR-004)
- Exit codes and streaming output naturally support bidirectional communication and progress reporting

## Consequences

**Positive:**
- No additional daemons or services to install/manage on target
- Leverages pre-installed, universally available SSH
- Natural support for real-time progress streaming via stdout/stderr
- Respects user's existing `~/.ssh/config` authentication setup
- Debugging straightforward: commands can be tested manually via `ssh target 'command'`
- Graceful error handling via exit codes and signal trapping

**Negative:**
- Requires careful output buffering on target for real-time progress (must use explicit flush)
- All remote operations must be designed as discrete, stateless scripts (no persistent daemons)
- User must have SSH access to target (standard assumption for network-based sync)

## References
- FR-004: Support for SSH config specification
- High Level Requirements: Full orchestration and sync scope
- docs/adr/considerations/adr-002-ssh-communication-channel.md: Detailed analysis and alternatives evaluation
