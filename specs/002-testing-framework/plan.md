# Implementation Plan: Testing Framework

**Branch**: `002-testing-framework` | **Date**: 2025-12-05 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/002-testing-framework/spec.md`

## Summary

Implement the **testing framework infrastructure** for pc-switcher: VM provisioning and reset scripts, locking mechanism, CI/CD workflows, pytest fixtures, and documentation structure. The framework supports three tiers of testing: (1) fast unit tests safe for any machine, (2) VM-isolated integration tests for destructive btrfs/SSH operations, and (3) a manual playbook for visual verification.

**Scope Note**: This feature covers only the framework infrastructure and documentation. Writing actual tests for specific features (e.g., 001-foundation tests) is out of scope and tracked separately in feature `003-foundation-tests`.

## Technical Context

**Language/Version**: Python 3.14 (per ADR-003, existing in pyproject.toml)
**Primary Dependencies**:
- pytest >= 9.0.1 (existing)
- pytest-asyncio >= 1.3.0 (existing)
- asyncssh >= 2.21.1 (existing, for SSH connections)
- OpenTofu (Terraform-compatible, for Hetzner VM provisioning)
- hcloud CLI (Hetzner Cloud command-line tool)
**Storage**:
- OpenTofu state: Hetzner Storage Box (SSH/SCP access, synced via wrapper script)
- Test VM state: btrfs snapshots on VMs
**Testing**: pytest with asyncio mode, markers for integration tests
**Target Platform**: Ubuntu 24.04 LTS (both dev machines and test VMs), btrfs filesystem
**Project Type**: Single project (CLI tool with async operations)
**Performance Goals**:
- Unit tests: < 30 seconds full suite
- VM reset: < 30 seconds (btrfs snapshot rollback)
- Integration tests: 5-15 minutes full suite
**Constraints**:
- Test VMs must be isolated (no damage to dev machines)
- Concurrent test runs prevented via locking
- Infrastructure cost < EUR 10/month
**Scale/Scope**:
- 2 test VMs (pc1, pc2)
- ~62 existing test cases to extend
- Framework supports future test additions

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

### Reliability Without Compromise ✅
- **Data integrity**: Test VMs are isolated from production; btrfs snapshot reset ensures clean state before each test session
- **Conflict detection**: Lock mechanism prevents concurrent test runs; lock file stores holder identity and acquisition time (see research.md §5)
- **Rollback strategy**: Baseline snapshots created at provisioning time; reset restores VMs to known-good state; if reset fails, tests do not proceed (see research.md §4)

*Post-design validation*: Lock file format and reset flow documented in data-model.md with state transitions.

### Frictionless Command UX ✅
- **Single command**: `uv run pytest tests/unit tests/contract -v` for unit tests; `uv run pytest -m integration` for integration tests
- **Minimal intervention**: VM provisioning is automatic when VMs don't exist; reset is automatic before integration tests
- **Progressive feedback**: pytest provides test progress; CI preserves logs and artifacts for debugging

*Post-design validation*: Quickstart.md provides copy-paste commands for all scenarios.

### Well-supported tools and best practices ✅
- **Tools**: pytest (well-established), OpenTofu (actively maintained Terraform fork), Hetzner Cloud (reliable provider), GitHub Actions (standard CI)
- **Best practices**: Three-tier testing (unit/integration/e2e), contract tests for mock validation, infrastructure as code
- **DRY/YAGNI**: Minimal fixtures for VM command execution only; no test data generation fixtures until needed

*Post-design validation*: Research.md documents configuration patterns from official documentation and community best practices.

### Minimize SSD Wear ✅
- **Test VMs only**: All destructive operations happen on Hetzner Cloud VMs, not developer SSDs
- **Snapshot reset**: Uses btrfs snapshot rollback instead of full reprovisioning (no disk writes beyond snapshot metadata)
- **Developer machines**: Unit tests use mocks; no real btrfs operations on local disk

*Post-design validation*: No new local disk operations introduced. All btrfs operations scoped to Hetzner VMs.

### Throughput-Focused Syncing ✅
- **Not directly applicable**: This feature is about testing, not syncing
- **Test performance**: Unit tests < 30s target; btrfs snapshot reset ~20-30s (faster than VM reprovisioning)

*Post-design validation*: Performance targets documented in Technical Context section.

### Deliberate Simplicity ✅
- **Minimal components**: Two VMs mirror real sync architecture; single lock file for concurrency
- **Understandable flows**: Clear three-tier separation; existing conftest.py patterns extended
- **No over-engineering**: Using existing pytest infrastructure; simple bash scripts for VM management

*Post-design validation*: State diagrams in data-model.md document all entity lifecycles clearly.

### Up-to-date Documentation ✅
- **Documentation artifacts** (owners: this feature):
  - `docs/testing-framework.md` - Architecture (exists, needs updates)
  - `docs/testing-developer-guide.md` - Developer guide (create)
  - `docs/testing-ops-guide.md` - Operational guide (create)
  - `docs/testing-playbook.md` - Manual verification playbook (create)
- **ADR reference**: ADR-006 documents the testing decision

*Post-design validation*: All documentation deliverables identified in spec.md are tracked above.

## Project Structure

### Documentation (this feature)

```text
specs/002-testing-framework/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output (if applicable)
└── tasks.md             # Phase 2 output (/speckit.tasks command)
```

### Source Code (repository root)

```text
src/pcswitcher/          # Existing application code (no changes for this feature)

tests/
├── conftest.py          # Shared fixtures (extend with VM fixtures)
├── __init__.py
├── contract/            # Contract tests (existing)
│   └── test_job_interface.py
├── unit/                # Unit tests (existing structure)
│   ├── test_lock.py
│   └── test_jobs/
├── integration/         # Integration tests (create structure)
│   ├── __init__.py
│   ├── conftest.py      # Integration-specific fixtures (VM connections)
│   └── test_*.py        # Future integration test files (out of scope for this feature)
└── infrastructure/      # VM infrastructure (extend)
    ├── main.tf          # OpenTofu configuration for Hetzner VMs
    ├── variables.tf     # OpenTofu variables
    ├── outputs.tf       # OpenTofu outputs (VM IPs)
    └── scripts/
        ├── tofu-wrapper.sh   # State sync wrapper (create)
        ├── provision.sh      # VM provisioning (exists)
        ├── configure-vm.sh   # VM configuration (create)
        ├── configure-hosts.sh # /etc/hosts and SSH keys setup (create)
        ├── reset-vm.sh       # Btrfs snapshot reset (create)
        └── lock.sh           # Lock management (create)

.github/
└── workflows/
    ├── ci.yml           # Unit tests on every push
    └── integration.yml  # Integration tests on PR to main + manual trigger

docs/
├── testing-framework.md       # Architecture (update)
├── testing-developer-guide.md # Developer guide (create)
├── testing-ops-guide.md       # Operational guide (create)
└── testing-playbook.md        # Manual playbook (create)
```

**Structure Decision**: Single project structure maintained. Tests organized by category (unit, contract, integration). Infrastructure code for VM management lives in `tests/infrastructure/`. CI workflows in `.github/workflows/`.

## Complexity Tracking

> No constitution violations identified. The implementation follows existing patterns and uses proven tooling.

| Aspect | Decision | Rationale |
|--------|----------|-----------|
| VM isolation | Hetzner Cloud VMs | Safety requirement; can't test destructive btrfs ops locally |
| Snapshot reset | Btrfs snapshots at `/.snapshots/baseline/@` | Fast (< 30s) vs VM reprovisioning (minutes); matches docs/testing-framework.md |
| Lock mechanism | File-based on pc1 | Simple; matches existing lock module patterns |
| Lock scope | Test execution only | Provisioning is rare; protected by CI concurrency groups |
| CI concurrency | GitHub Actions concurrency group | Built-in feature; no external dependencies |
| State backend | Hetzner Storage Box | Cost-effective; avoids state drift between dev/CI |
