# ADR-003 Considerations: Implementation Language and Framework Analysis

This document provides detailed analysis of technology options for implementing the PC-switcher CLI and orchestration system.

## Analysis Framework

Based on the requirements, the implementation needs to:
- Orchestrate complex operations **on both source and target machines** over SSH
- Sync multiple heterogeneous components (files, packages, Docker, VMs, k3s)
- Provide robust error handling and conflict detection
- Offer a smooth terminal-based UX
- Be maintainable by someone (you) who knows the stack
- Have excellent reliability (principle #1)

## Technology Analysis

### 1. Bash/Zsh Script

**Pros:**
- Zero dependencies - native to Ubuntu
- Direct system integration, fast execution
- Perfect for simple command orchestration
- You already know it well (maintainability)
- Ideal for calling existing tools (rsync, btrfs, apt, etc.)

**Cons:**
- **Complex orchestration becomes unwieldy**: Managing state across two machines, handling async operations, complex error handling
- **Poor error handling ergonomics**: Error propagation in pipelines is tricky (`set -e` pitfalls)
- **Limited data structures**: Tracking sync state, conflicts, file lists becomes messy
- **Testing is difficult**: Hard to unit test, mock SSH operations
- **No structured logging**: Would need external tools
- **String manipulation hell** for parsing complex outputs

**Verdict for PC-switcher:** Good for **individual sync operations** (e.g., a script to sync apt packages), but **poor fit for the overall orchestration layer** due to complexity.

### 2. Python

**Pros:**
- **Excellent SSH libraries**: `asyncssh` for remote execution
- **Rich ecosystem**: `rich`/`textual` for TUI, `click`/`typer` for CLI, `structlog` for logging
- **Robust error handling**: Try/except, proper exception types
- **Data structures**: Easy to model sync state, conflicts, file metadata
- **Testable**: `pytest`, mocking, fixtures
- **Standard on Ubuntu 24.04**: Python 3.12 included by default
- **You know it well**: Maintainability principle
- **subprocess module**: Can orchestrate bash commands when needed
- **JSON/YAML support**: Easy config file management

**Cons:**
- **Not a compiled binary**: Though you can use `pyinstaller` if desired
- **Slightly slower startup** than compiled languages (negligible for this use case)

**Verdict for PC-switcher:** **Strongest candidate for orchestration layer**. Can call bash scripts for specific operations while providing structure for complex coordination.

### 3. Go

**Pros:**
- **Single static binary**: Easy deployment
- **Excellent SSH libraries**: `golang.org/x/crypto/ssh`
- **Fast execution and compilation**
- **Good concurrency**: Could sync multiple components in parallel
- **Strong typing**: Catches errors at compile time
- **Cross-compilation**: Could build for different architectures

**Cons:**
- **Steep learning curve**: You don't know it at all
- **Time investment**: Learning + debugging + ecosystem familiarity
- **Might be overkill**: Benefits (performance, static binary) less critical for this use case
- **Verbose error handling**: `if err != nil` everywhere
- **Less mature TUI libraries** compared to Python

**Verdict for PC-switcher:** **Good technical fit, but poor pragmatic fit** due to learning curve vs. maintainability principle. Only consider if you want to learn Go anyway.

### 4. Ansible

**Pros:**
- **Designed for this domain**: Configuration management, remote execution
- **Idempotent operations**: Built-in
- **Declarative syntax**: Easier to reason about desired state
- **Extensive modules**: apt, systemd, file sync, docker
- **Inventory management**: Can model source/target machines
- **Dry-run mode**: Built-in

**Cons:**
- **Learning curve**: You don't know it
- **Designed for hub-to-spoke, not peer-to-peer**: Ansible assumes control machine → managed nodes. PC-switcher is peer-to-peer sync.
- **Complex state tracking**: Tracking conflicts, bi-directional checks might be awkward
- **YAML complexity**: Large playbooks become hard to maintain
- **Limited for custom orchestration logic**: You'd write Python plugins anyway for complex sync logic
- **Over-engineered for 2 machines**: Ansible shines at scale (10s-100s of machines)
- **Interactive UX harder**: Not designed for rich terminal UIs with progress bars, conflict prompts

**Verdict for PC-switcher:** **Interesting but mismatched**. Ansible is for "push configuration to many machines," not "orchestrate complex sync between two peers." You'd fight the tool's assumptions.

## Other Options Considered

### 5. Python + Bash Hybrid ⭐

**Approach:** Python for orchestration, bash scripts for individual operations

**Pros:**
- **Best of both worlds**: Python's structure + Bash's system integration
- **Incremental complexity**: Start simple, add Python structure as needed
- **Reuse existing knowledge**: Both familiar to you
- **Natural division**: Python coordinates, Bash executes
- **Easy testing**: Mock bash script calls in Python tests

**Example structure:**
```
pc-switcher
├── pcsync (Python CLI entry point)
├── pcswitcher/
│   ├── orchestrator.py (main sync logic)
│   ├── ssh.py (remote execution)
│   ├── conflict.py (conflict detection)
│   └── ui.py (TUI with rich/textual)
└── scripts/
    ├── sync-apt.sh
    ├── sync-docker.sh
    ├── sync-btrfs.sh
    └── detect-conflicts.sh
```

**Verdict:** **Excellent pragmatic choice**. Combines maintainability + flexibility.

### 6. Task + Scripts (Alternative orchestrator)

**Approach:** Use Taskfile.yml (Go-based task runner) + bash/python scripts

**Pros:**
- **Simple dependency management**: Define task dependencies
- **Cross-platform**: Works on any OS
- **No custom orchestration code**: Just define tasks

**Cons:**
- **Limited for complex logic**: Not designed for ssh orchestration, conflict detection
- **Would need Python/Bash anyway** for the actual logic
- **Another tool to learn**: Adds dependency for marginal benefit

**Verdict:** **Not a good fit** - too limited for PC-switcher's complexity.

## Recommendation

### **Primary Recommendation: Python (with bash scripts for specific operations)**

**Rationale:**

1. **Reliability (Principle #1)**: Python's exception handling, logging libraries, and testability directly support reliability. You can build comprehensive error handling and audit trails.

2. **Maintainability (Principle #6)**: You know Python well. Future you (or contributors) can maintain Python code more easily than learning a new language.

3. **UX Requirements**: Python has excellent TUI libraries:
   - `rich`: Beautiful terminal output, progress bars, tables
   - `textual`: Full TUI framework if needed later
   - `click`/`typer`: Excellent CLI frameworks

4. **Orchestration Fit**: PC-switcher needs:
   - SSH execution → `asyncssh`
   - State management → Python data structures + JSON/YAML
   - Conflict detection → Custom logic (easier in Python than Bash)
   - Multi-step workflows → Python's control flow

5. **Integration with System Tools**: Python's `subprocess` module can call:
   - `rsync` for file sync
   - `btrfs send/receive` for snapshots
   - `apt-mark showmanual` for package lists
   - `virsh` for VMs
   - `docker` CLI
   - `kubectl` for k3s

6. **Well-Supported (Principle #3)**: Python 3 is standard on Ubuntu, huge ecosystem, active development.

### **Implementation Strategy**

```
Phase 1: Pure Python orchestrator
- Main CLI with Click/Typer
- SSH orchestration with AsyncSSH
- Progress display with Rich
- Logging with structlog

Phase 2: Delegate to bash when appropriate
- Individual sync operations (apt, docker, etc.) as bash scripts
- Called from Python orchestrator
- Easier to iterate on sync logic independently

Phase 3: Add sophistication as needed
- Conflict detection algorithms in Python
- State management and rollback logic
- Advanced TUI with Textual if terminal UI isn't sufficient
```

### **Why NOT the Other Options**

- **Bash only**: Would become unmaintainable as complexity grows (especially SSH coordination, conflict detection)
- **Go**: Learning curve doesn't justify benefits for a 2-machine sync tool. Save Go for when you need its strengths (high-performance services, system tools).
- **Ansible**: Fighting tool assumptions (hub-spoke vs peer-to-peer), YAML complexity for custom logic, interactive UX limitations. You'd write Python plugins anyway.

### **Decision Factors**

**Choose Python if:**
- ✅ Reliability and maintainability are top priorities (they are)
- ✅ You value rapid development and iteration
- ✅ Complex orchestration logic needed (it is)
- ✅ You know Python well (you do)

**Only choose Go if:**
- ❌ Single-binary distribution is critical (it's not - Ubuntu has Python)
- ❌ You want to learn Go anyway (do you?)
- ❌ Performance is critical (it's not - network is bottleneck)

**Only choose Ansible if:**
- ❌ You're managing 10+ machines (you're not)
- ❌ Declarative config management is the primary need (it's not - orchestration is)
