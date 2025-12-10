# ADR-002 Considerations: SSH Orchestration Analysis

This document provides detailed analysis, alternatives evaluation, and implementation guidance for ADR-002 (SSH Orchestration).

## Orchestration Requirements Analysis

Based on PC-switcher's High Level Requirements, the orchestration system must support:

### Command Execution
- Execute scripts and services on the target machine
- Run operations requiring elevated privileges (via sudo)
- Coordinate multi-step operations across both machines (connect → validate → sync → cleanup)
- Start/stop services and modify system state

### Bidirectional Communication
- **Source → Target**: Commands, control signals, data transfer initiation
- **Target → Source**: Progress updates, status messages, error reports, log output
- Real-time feedback during potentially long-running operations
- Graceful shutdown on Ctrl+C interrupt

### Progress/Status Reporting
- Real-time progress updates (terminal UI updates must occur within 1 second per SC-003)
- Log messages at multiple severity levels (debug, info, warning, error)
- Heartbeat indicators every 2-3 seconds for operations exceeding this duration
- Operation phase transitions (connecting, validating, syncing phases)
- Per-job progress tracking

### Error Handling & Recovery
- Pre-sync validation errors detected on target before full sync begins
- Runtime errors during sync operations with detailed context
- Network interruption detection and graceful recovery
- Cleanup operations on interrupt signals
- Error logs for troubleshooting

### Security & Authentication
- Secure authentication between machines (SSH key-based assumed)
- No additional credential management infrastructure
- Respect user's existing `~/.ssh/config` configuration (FR-004)

### Operational Constraints
- Machines must be reachable via SSH (LAN, VPN such as Tailscale, or other network)
- Manual trigger (not a persistent connection requirement)
- Single user workflow (no concurrent operations)
- Connection establishment within 5 seconds (SC-002)

## SSH Assessment

### Strengths

**1. Authentication & Security (Perfect Match)**
- SSH key-based authentication already configured between machines
- Respects user's `~/.ssh/config` (explicit requirement FR-004)
- Industry-standard security model, mature and well-audited
- Zero additional attack surface compared to custom solutions
- No need to manage separate tokens, API keys, or credentials

**2. Command Execution (Excellent)**
- Native support for remote command execution with full flexibility
- Handles stdout and stderr streams naturally
- Supports PTY allocation for interactive commands
- Built-in sudo support
- Execute commands as specific users without additional complexity

**3. Proven & Ubiquitous (Aligns with Project Principle: "Proven Tooling Only")**
- Pre-installed on every Linux system
- Extremely mature and stable (used in production for 25+ years)
- Extensive documentation and community knowledge
- Well-understood by software engineers
- Rich ecosystem of libraries and tools

**4. Simplicity (Aligns with Project Principle: "Deliberate Simplicity")**
- No additional services or daemons required on target
- No port management beyond SSH (typically port 22)
- Leverages existing infrastructure and mental models
- Minimal setup required beyond standard SSH configuration

**5. Error Handling & Network Resilience**
- SSH protocol handles connection errors clearly
- Distinct failure modes: connection failure vs. command failure vs. timeout
- Exit codes propagate naturally for command result signaling
- Built-in keepalive options for detecting network interruptions
- Well-understood error scenarios with documented recovery patterns

### Challenges & Solutions

**Challenge 1: Bidirectional Communication Pattern**

SSH's native model is request-response (command sent, output received at completion). PC-switcher requires:
- Target sending unsolicited progress messages during command execution
- Real-time streaming of progress (not batch output at command end)

**Solution: Streaming Output**
- Target writes progress to stdout/stderr with explicit flush
- SSH preserves line order and streams output in real-time
- Libraries like Python's `asyncssh` support reading stdout as stream
- This is the standard pattern for all remote automation tools

**Implementation example (target script):**
```python
import sys
import time

def log_progress(message, level="INFO"):
    # Explicit flush ensures real-time delivery, not buffered
    print(f"[{level}] {message}", flush=True)

log_progress("Starting docker sync...")
for module in modules:
    log_progress(f"Syncing {module}...")
    time.sleep(2)  # Simulating work
    log_progress(f"Completed {module}")
log_progress("Docker sync complete")
```

**Source-side handling (Python with paramiko):**
```python
import asyncio
import asyncssh
import sys

async def run_client():
    async with asyncssh.connect(target_host) as conn:
        async with conn.create_process('pc-switcher-target sync-docker') as process:
            async for line in process.stdout:
                terminal_ui.update_progress(line.strip())
            
            if process.exit_status != 0:
                print(f"Sync failed: {await process.stderr.read()}")
                sys.exit(1)

asyncio.run(run_client())
```

**Verdict**: SSH handles streaming output natively. This is not a real limitation—it's standard practice.

---

**Challenge 2: Persistent Connection Management**

For multi-phase operations (validate → sync packages → sync home → sync docker → cleanup), you need to decide:
- Keep one SSH connection open for entire operation?
- Create new SSH connection per operation?
- Connection pool with multiple persistent connections?

**Solution: SSH Multiplexing**
SSH multiplexing (`ControlMaster`) is designed exactly for this:
- One initial SSH connection is created
- Subsequent operations create new logical channels reusing the same physical connection
- Reduces connection overhead dramatically
- Standard feature in OpenSSH (configured in `~/.ssh/config` or command-line)

**Configuration (in `~/.ssh/config`):**
```
Host *
    ControlMaster auto
    ControlPath ~/.ssh/control-%h-%p-%r
    ControlPersist 10m
```

**Usage:**
```python
# First connection establishes physical TCP connection
async with asyncssh.connect(target_host) as conn:
    # Operations reuse the same connection
    result1 = await conn.run('phase 1 operation')
    output1 = result1.stdout

    result2 = await conn.run('phase 2 operation')
    output2 = result2.stdout
```

**Verdict**: SSH multiplexing is a proven pattern with zero complexity overhead.

---

**Challenge 3: Real-Time Progress Updates for Terminal UI**

Requirement: Terminal UI updates within 1 second of progress message (SC-003). Two-second heartbeat for long operations.

**Solution: Line-Buffered Output with Explicit Flush**

Remote script must:
1. Use unbuffered or line-buffered I/O
2. Explicitly flush after each message
3. Output one message per line (not accumulating in buffer)

**Python (recommended for PC-switcher):**
```python
#!/usr/bin/env python3
import sys
import subprocess

# Ensure stdout is line-buffered with flush
print("Starting sync...", flush=True)

result = subprocess.run(['rsync', '-av', '--progress', '/src/', '/dst/'],
                       stdout=sys.stdout,
                       stderr=sys.stderr)

print(f"Sync complete with exit code {result.returncode}", flush=True)
sys.exit(result.returncode)
```

**Bash (alternative):**
```bash
#!/bin/bash

# Flush after each print
flush() {
    echo "$1"
    tee /dev/null  # Forces flush
}

flush "Starting sync..."
rsync -av /src/ /dst/
flush "Sync complete"
```

**Source-side handling:**
```python
import asyncssh
import asyncio

async def run_sync():
    async with asyncssh.connect(target_host) as conn:
        async with conn.create_process('python3 /opt/pc-switcher/sync.py') as process:
            # Read lines as they're flushed (typically within 100ms)
            async for line in process.stdout:
                ui.update_progress(line.rstrip())

asyncio.run(run_sync())
```

**Verdict**: Achievable with standard buffering practices. No new technology required.

---

**Challenge 4: Graceful Interrupt Handling (Ctrl+C)**

Requirement: User presses Ctrl+C on source machine, operations on target must stop and cleanup.

**Solution: Signal Trapping + Explicit Cleanup Commands**

**Source-side signal handling:**
```python
import asyncio
import asyncssh
import signal
import sys

async def run_sync_safe():
    async with asyncssh.connect(target_host) as conn:
        try:
            async with conn.create_process('pc-switcher-target sync') as process:
                async for line in process.stdout:
                    print(line.rstrip())
        except asyncio.CancelledError:
            print("\n[!] Sync interrupted. Cleaning up...")
            # Send explicit cleanup command
            await conn.run('pkill -f "pc-switcher-target"')
            raise

def signal_handler(sig, frame):
    # Cancel the main task
    for task in asyncio.all_tasks():
        task.cancel()

signal.signal(signal.SIGINT, signal_handler)
try:
    asyncio.run(run_sync_safe())
except asyncio.CancelledError:
    sys.exit(1)
```

**Target-side cleanup support:**
```bash
#!/bin/bash
# pc-switcher-target cleanup

# Stop any running sync operations
pkill -f "rsync"
pkill -f "docker cp"

# Restore any partially-synced state
# (implementation depends on specific jobs)

echo "Cleanup complete"
exit 0
```

**Alternative: PTY allocation for signal propagation**

```python
# Allocate PTY so Ctrl+C signal propagates to remote process
# Allocate PTY so Ctrl+C signal propagates to remote process
async with conn.create_process('python3 /opt/pc-switcher/sync.py', term_type='xterm') as process:
```

**Verdict**: Standard pattern for SSH-based automation. Well-documented with multiple proven approaches.

---

**Challenge 5: Connection Establishment Time**

Requirement: Connection within 5 seconds (SC-002).

**Analysis:**
- SSH connection over local network typically completes in <2 seconds (including authentication)
- VPN connections may add slight overhead but typically still within budget
- DNS resolution might add 1-2 seconds if needed
- Initial key exchange negligible on low-latency networks

**Verdict**: SSH easily meets the 5-second requirement. Not a concern.

---

## Alternatives Analysis

### Alternative 1: Custom Protocol over TCP/UDP

**Approach**: Build custom binary protocol for orchestration commands and responses.

**Pros**:
- Full control over message format, serialization, and timing
- Could theoretically optimize for specific use cases
- Bidirectional by nature

**Cons**:
- **Requires custom daemon on target machine** (violates simplicity principle)
- Must implement own authentication mechanism (SSH already solves this)
- Must implement own encryption (SSH already solves this)
- Must implement own error handling and recovery
- Port management and firewall configuration complexity
- High development burden for low payoff
- **Violates "Proven Tooling Only" principle** - unproven custom code instead of battle-tested SSH
- Debugging requires custom tools and protocols
- No existing ecosystem or community knowledge to leverage

**Decision**: **REJECT**. Custom protocols add massive complexity with zero functional benefit. SSH already solves the core problems elegantly.

---

### Alternative 2: gRPC

**Approach**: Run gRPC server on target, source acts as gRPC client.

**Pros**:
- Modern RPC framework with good documentation
- Supports bidirectional streaming natively
- Protocol buffers for schema definition
- Well-defined service contracts

**Cons**:
- **Requires persistent gRPC daemon on target** (adds setup complexity)
- Authentication separate from existing SSH infrastructure (mTLS or custom tokens)
- Requires separate port management and firewall rules
- gRPC requires HTTP/2 support (more complexity)
- Overkill for manually-triggered point-to-point use case
- Over-engineered for what is fundamentally remote command execution
- gRPC libraries add significant dependencies

**Decision**: **REJECT**. gRPC is designed for distributed service-to-service communication, not point-to-point orchestration. Adds unnecessary complexity and requires running persistent service on target.

---

### Alternative 3: REST API

**Approach**: Run lightweight HTTP server on target (e.g., Python's `http.server` or Flask), source makes HTTP requests.

**Pros**:
- Simpler than gRPC
- Well-understood HTTP semantics
- Easy to test with curl/Postman
- Moderate development effort

**Cons**:
- **Still requires server process on target** (adds setup complexity)
- Polling model for progress (not true streaming) - less real-time feedback
- Authentication becomes your responsibility (JWT, API keys, etc.)
- Port and firewall management required
- Server lifecycle management (start/stop/restart)
- More fragile than SSH's built-in request-response model
- Over-engineered for command execution

**Decision**: **REJECT**. REST adds complexity without solving any SSH limitation. Forces you to manage a server process on the target.

---

### Alternative 4: Message Queue (RabbitMQ, NATS, Kafka)

**Approach**: Both machines connect to central message broker for pub/sub communication.

**Pros**:
- Decouples sender/receiver
- Could support pub/sub for multi-client scenarios
- Natural async message flow

**Cons**:
- **Requires separate broker infrastructure** (where to host?)
- Massive overkill for two-machine, single-user, synchronous workflow
- Introduces additional latency (message brokering overhead)
- Doesn't fit manual-trigger operational model
- Requires running broker constantly (even when not syncing)
- Adds operational complexity and failure modes
- Over-engineered by several orders of magnitude

**Decision**: **REJECT**. Message queues are designed for distributed systems with many independent producers/consumers. PC-switcher is point-to-point orchestration—fundamentally different problem space.

---

### Alternative 5: Configuration Management (Ansible, SaltStack, Chef)

**Approach**: Use existing config management tools (Ansible, Salt, Chef) for orchestration.

**Pros**:
- Proven tools for complex multi-step remote operations
- Built-in support for idempotent operations
- Rich module ecosystem for common tasks
- Handles error cases and retries

**Cons**:
- Heavy dependencies (Ansible requires Python on target)
- Over-engineered for PC-switcher's needs
- Adds learning curve (playbook syntax, YAML, handlers)
- Less control over real-time progress feedback
- Overkill if only using 10% of features
- Not "proven tooling" for the application domain (designed for config management, not sync orchestration)
- Ansible inventory/group management complexity for single-target scenario

**Verdict**: **CONDITIONAL REJECTION**. Ansible is powerful and reliable, but over-engineered. However, valuable patterns to steal:
- Use idempotent operations (safe to re-run)
- Use explicit handlers for error scenarios
- Structure code as reusable modules
- Think in terms of state, not just script execution

---

## Recommended SSH Implementation Patterns

### Pattern 1: Persistent Connection with Multiplexing

```python
import asyncio
import asyncssh
import signal
import sys

class SyncOrchestrator:
    def __init__(self, target_host):
        self.target_host = target_host
        self.conn = None

    async def connect(self):
        self.conn = await asyncssh.connect(self.target_host)

    async def execute_phase(self, phase_name, command):
        """Execute a sync phase and stream output in real-time"""
        print(f"[*] Starting {phase_name}...")
        
        async with self.conn.create_process(command) as process:
            async for line in process.stdout:
                print(f"  {line.rstrip()}")
            
            if process.exit_status != 0:
                error_output = await process.stderr.read()
                print(f"[!] {phase_name} failed: {error_output}")
                raise RuntimeError(f"{phase_name} failed with exit code {process.exit_status}")

        print(f"[✓] {phase_name} complete")

    async def run_sync(self):
        """Execute full multi-phase sync"""
        try:
            await self.connect()
            await self.execute_phase("Validation", "pc-switcher-target validate")
            await self.execute_phase("Docker", "pc-switcher-target sync-docker")
            await self.execute_phase("Packages", "pc-switcher-target sync-packages")
            await self.execute_phase("Home", "pc-switcher-target sync-home /home")
        except (RuntimeError, OSError, asyncssh.Error) as e:
            print(f"[!] Sync failed: {e}")
            return False
        finally:
            if self.conn:
                self.conn.close()
        return True

    async def cleanup(self):
        """Graceful cleanup on interrupt"""
        if self.conn:
            try:
                await self.conn.run('pc-switcher-target cleanup')
            except:
                pass

async def main():
    orchestrator = SyncOrchestrator("my-target-machine")
    
    # Setup signal handling for graceful shutdown
    loop = asyncio.get_running_loop()
    stop = asyncio.Future()
    loop.add_signal_handler(signal.SIGINT, stop.set_result, None)
    
    sync_task = asyncio.create_task(orchestrator.run_sync())
    
    done, pending = await asyncio.wait(
        [sync_task, stop], 
        return_when=asyncio.FIRST_COMPLETED
    )
    
    if stop in done:
        print("\n[!] Interrupt received. Cleaning up...")
        await orchestrator.cleanup()
        sync_task.cancel()
    
    sys.exit(0 if await sync_task else 1)

# Usage
if __name__ == "__main__":
    asyncio.run(main())
```

### Pattern 2: Target-Side Script Structure

```bash
#!/bin/bash
# pc-switcher-target: Entry point for target operations

set -euo pipefail

log_info() {
    echo "[INFO] $*" >&2
}

log_error() {
    echo "[ERROR] $*" >&2
}

validate() {
    log_info "Validating target system..."

    # Check Docker
    if ! command -v docker &> /dev/null; then
        log_error "Docker not installed"
        exit 1
    fi

    # Check rsync
    if ! command -v rsync &> /dev/null; then
        log_error "rsync not installed"
        exit 1
    fi

    log_info "Validation passed"
}

sync_docker() {
    log_info "Starting docker sync..."

    # Implementation here
    log_info "Syncing docker images..."
    docker image list | while read -r line; do
        log_info "  Found image: $line"
    done

    log_info "Docker sync complete"
}

cleanup() {
    log_info "Cleaning up..."
    # Kill any remaining sync processes
    pkill -f "rsync" || true
    log_info "Cleanup complete"
}

trap cleanup EXIT

case "${1:-}" in
    validate)
        validate
        ;;
    sync-docker)
        sync_docker
        ;;
    cleanup)
        cleanup
        ;;
    *)
        echo "Usage: $0 {validate|sync-docker|cleanup}"
        exit 1
        ;;
esac
```

### Pattern 3: Progress Reporting in Target Script

```python
#!/usr/bin/env python3
import sys
import time
import subprocess

def log_progress(message, level="INFO"):
    """Output progress message with guaranteed flush"""
    print(f"[{level}] {message}", flush=True, file=sys.stderr)

def run_sync():
    log_progress("Starting synchronization...")

    # Validate
    log_progress("Validating target system...")
    time.sleep(1)  # Simulating validation work
    log_progress("Validation passed")

    # Docker sync
    log_progress("Syncing Docker images...")
    result = subprocess.run(
        ["python3", "-u", "sync-docker.py"],  # -u for unbuffered
        stdout=sys.stdout,
        stderr=sys.stderr
    )
    if result.returncode != 0:
        log_progress("Docker sync failed", level="ERROR")
        return False

    # Packages sync
    log_progress("Syncing packages...")
    result = subprocess.run(
        ["python3", "-u", "sync-packages.py"],
        stdout=sys.stdout,
        stderr=sys.stderr
    )
    if result.returncode != 0:
        log_progress("Package sync failed", level="ERROR")
        return False

    log_progress("All sync operations complete")
    return True

if __name__ == "__main__":
    success = run_sync()
    sys.exit(0 if success else 1)
```

## Conclusion

SSH is the optimal choice for PC-switcher orchestration:

1. **Perfect alignment with project principles**: Proven, simple, reliable
2. **Meets all functional requirements**: Command execution, progress streaming, error handling, signal management
3. **Leverages existing infrastructure**: Respects ~/.ssh/config, uses pre-installed OpenSSH
4. **No significant limitations**: All identified "challenges" have standard, proven solutions
5. **Superior to all alternatives**: Every alternative adds complexity without providing benefit

The project should confidently proceed with SSH as the orchestration mechanism and focus remaining design effort on:
- Job architecture and coordination logic
- Terminal UI implementation for progress visualization
- Error handling patterns and validation workflows
- Target-side script structure and reusability
