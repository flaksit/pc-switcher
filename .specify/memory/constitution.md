<!-- Sync Impact Report
Version change: template -> 1.0.0
New principles:
- Reliability Without Compromise
- Frictionless Command UX
- Proven Tooling Only
- Minimize SSD Wear
- Throughput-Focused Syncing
- Deliberate Simplicity
- Up-to-date Documentation
Added sections:
- Core Principles (populated)
- Operational Constraints
- Development Workflow Standards
Removed sections:
- None
Templates requiring updates:
- ✅ .specify/templates/plan-template.md
- ✅ .specify/templates/spec-template.md
- ✅ .specify/templates/tasks-template.md
Follow-up TODOs:
- None
-->

# PC-switcher Constitution

## Core Principles

### Reliability Without Compromise
- Sync operations MUST guarantee data integrity through transactional snapshots or equivalent rollback plans before applying changes to target machines.
- Every run MUST detect conflicting modifications and halt with diagnostics that identify impacted items.
- Metadata and payload verification MUST occur pre- and post-transfer (checksums, permissions, ownership) with logged evidence.

*Rationale: Reliability is the top priority; regressions here halt delivery regardless of other gains.*

### Frictionless Command UX
- The primary entrypoint MUST remain a single command that orchestrates preparation, execution, and validation across machines.
- Automation MUST cover routine prerequisites and clean-up; manual intervention is limited to explicit confirmations or conflict resolution prompts.
- User messaging MUST provide progressive feedback, estimated completion, and actionable recovery guidance on failure.

*Rationale: Smooth operator experience enables trusted adoption and reduces human error.*

### Well-supported tools and best practices
- Dependencies MUST rely on actively maintained, well-supported tools
- Best practices and standards MUST be followed in architecture, implementation and operations
- DRY and YAGNI software design principles MUST be honored - Violations MUST be documented with a their motivation

*Rationale: Stable foundations limit unexpected maintenance overhead and security risk.*

### Minimize SSD Wear
- Implementation MUST minimize write amplification on NVMe SSDs via incremental transfers, deduplication, and avoidance of unnecessary temporary copies.
- Long-running sync stages MUST prefer RAM or ephemeral storage when staging data instead of repeated SSD writes.

*Rationale: Preserving drive health sustains reliability and cost efficiency.*

### Throughput-Focused Syncing
- Feature work MUST declare expected sync duration targets by asset category and design to meet or beat them.
- Safe parallelization and batching MUST be used to leverage network and disk bandwidth without compromising reliability.

*Rationale: Timely syncs keep switching practical and preserve user trust.*

### Deliberate Simplicity
- Architectures MUST favor minimal components, avoiding redundant services or fragile orchestration layers.
- Code paths MUST remain understandable by a single maintainer; complex flows demand diagrams and explicit ownership.

*Rationale: Simple systems are easier to reason about, maintain, and keep reliable.*

### Up-to-date Documentation
- Behavioral changes MUST update or create rationale, architecture, implementation, and user guides within the same change set.
- Documentation MUST remain navigable from high-level intent down to scripts, keeping references synchronized.
- Out-of-date documentation identified during reviews MUST block release until corrected.

*Rationale: Living documentation is essential for reliability, onboarding, and auditability.*

## Operational Constraints

- PC-switcher targets one active user context at a time; true concurrent edit scenarios remain out of scope except for conflict detection.
- Supported environments MUST run Ubuntu 24.04 LTS on Btrfs with a flat subvolume layout and 1+ Gb LAN connectivity during sync.
- Sync scope MUST include user and system state enumerated in `High level requirements.md` while explicitly excluding machine-specific secrets and caches.
- Acceptable workflow constraints (logouts, VM suspension, etc.) MUST be automated where possible and documented before enforcement.
- Configuration management stays external to this repository; solutions MUST be parameterized for multiple personal deployments.

## Development Workflow Standards

- Specs and plans MUST annotate the primary principles they satisfy and call out trade-offs when principles compete.
- Research and design deliverables MUST describe reliability mechanisms, disk-wear mitigation, and UX considerations before implementation begins.
- Performance budgets, conflict detection strategies, and documentation updates MUST appear as tracked tasks with owners.
- Every change set MUST include verification evidence (tests, measurements, or checklists) proving adherence to the seven principles.

## Governance

- This constitution governs project decisions; deviations require a recorded exemption approved by project maintainers.
- Amendments require: (1) written proposal referencing impacted principles, (2) updated documentation and templates, and (3) maintainer approval.
- Versioning follows semantic rules (major = principle overhaul, minor = new principle/section, patch = textual clarity). The constitution version MUST be updated with every amendment.
- Compliance reviews occur on every plan/spec/tasks generation and pull request; reviewers MUST confirm principle alignment and documentation updates before approval.

**Version**: 1.0.0 | **Ratified**: 2025-11-13 | **Last Amended**: 2025-11-13
