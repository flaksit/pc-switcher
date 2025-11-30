# Plan Review: Foundation Infrastructure Complete (Detail Review)

**Reviewer**: GitHub Copilot
**Date**: 2025-11-30
**Status**: Review complete

## Overall Assessment

The plan demonstrates excellent architectural thinking and comprehensive coverage of the foundation infrastructure requirements. The design artifacts are thorough, with strong alignment to the project constitution and spec. However, several gaps and inconsistencies remain from previous reviews that need resolution before implementation can proceed confidently. The plan is largely complete but requires targeted fixes to ensure correctness and consistency.

---

## Completeness Assessment

### Strengths
- **Architecture Coverage**: `architecture.md` provides extensive detail on component interactions, event flows, and class hierarchies, covering all major systems (CLI, Orchestrator, Jobs, Connection, Logging, UI).
- **Data Model**: `data-model.md` comprehensively defines all core entities, types, and relationships with proper validation rules.
- **Technical Research**: `research.md` thoroughly documents technology choices with implementation patterns and alternatives considered.
- **Contracts**: Both `config-schema.yaml` and `job-interface.md` provide clear, implementable contracts for configuration and job integration.
- **Developer Guidance**: `quickstart.md` offers practical setup and development patterns.
- **Constitution Alignment**: `plan.md` includes detailed constitution checks and project structure planning.

### Gaps Identified

#### 1. Self-Installation Implementation Details (Critical)
**Location**: Missing from `architecture.md`, `plan.md`, `research.md`
**Impact**: FR-005, FR-006, FR-007 require specific implementation details for target version checking and installation.

**Missing Elements**:
- Concrete flow for detecting target pc-switcher version (command to run on target)
- Exact `uv tool install` command with Git URL format
- Verification steps after installation/upgrade
- Error handling for installation failures
- Timing expectations (SC-004: <30 seconds)

**Recommendation**: Add a dedicated section in `research.md` or `architecture.md` detailing the self-installation workflow, including command sequences and success criteria.

#### 2. Disk Space Preflight Checks (Critical)
**Location**: `architecture.md` mentions `DiskSpaceMonitorJob` for runtime monitoring, but preflight checks (FR-016) are not detailed.
**Impact**: System must abort before any operations if disk space is insufficient.

**Missing Elements**:
- When preflight checks occur (before snapshots or after connection?)
- Commands to check free space on both hosts
- Parsing logic for percentage vs absolute thresholds
- Error messages and abort behavior

**Recommendation**: Add preflight disk space validation to the orchestrator initialization flow in `architecture.md`.

#### 3. Snapshot Cleanup Algorithm (Major)
**Location**: Config schema includes retention settings, but no algorithm defined.
**Impact**: FR-014 requires automated cleanup of old snapshots.

**Missing Elements**:
- Logic for applying `keep_recent` and `max_age_days`
- Command sequences for identifying and deleting old snapshots
- Safety checks to avoid deleting active snapshots
- Integration with `pc-switcher cleanup-snapshots` command

**Recommendation**: Add snapshot cleanup workflow to `research.md` with concrete implementation patterns.

#### 4. Installation/Setup Script (Major)
**Location**: FR-035-FR-037 require setup script, but no artifact exists.
**Impact**: Users need automated setup for new machines.

**Missing Elements**:
- Script logic for btrfs detection
- Dependency installation (uv, btrfs-progs, etc.)
- Default config generation with comments
- Error handling and user guidance

**Recommendation**: Create `setup.py` or `setup.sh` artifact, or document the setup workflow in `quickstart.md` (currently developer-focused).

#### 5. Locking Mechanism Integration (Major)
**Location**: `research.md` covers flock mechanics, but `architecture.md` doesn't show lock acquisition in sync flow.
**Impact**: FR-047 requires preventing concurrent syncs.

**Missing Elements**:
- When locks are acquired (source and target)
- Lock file locations and naming
- Lock release on normal/abnormal termination
- Error messages for lock conflicts

**Recommendation**: Add lock management to the orchestrator lifecycle in `architecture.md`.

---

## Correctness Assessment

### Technical Correctness Issues

#### 1. JobContext Logger Inconsistency (Critical)
**Location**: `data-model.md` includes `logger: JobLogger`, but `contracts/job-interface.md` omits it and uses `event_bus` directly.
**Impact**: Implementation ambiguity for job logging.

**Issue**: The contract shows jobs accessing `context.event_bus` directly, but data-model includes a logger. Need to choose one approach:
- If using EventBus directly, remove logger from data-model
- If using JobLogger, add it to contract and update helper methods

**Recommendation**: Standardize on EventBus direct access (simpler, matches current contract). Remove logger from JobContext in data-model.

#### 2. Host Parameter in Logging (Major)
**Location**: `contracts/job-interface.md` helper methods lack `host` parameter, but `LogEvent` requires it.
**Impact**: Jobs cannot specify which host a log message relates to.

**Issue**: Architecture mentions JobLogger bound to host, but contract doesn't show this. Options:
1. Add `host: Host` parameter to `_log()` method
2. Provide separate source/target loggers in JobContext
3. Infer host from context (ambiguous)

**Recommendation**: Add `host: Host` parameter to `_log()` and `_report_progress()` helper methods in the contract.

#### 3. Dummy Job Implementation Mismatch (Minor)
**Location**: `quickstart.md` example doesn't match spec requirements (FR-039).
**Impact**: Reference implementation may mislead developers.

**Issue**: Example shows 1s intervals, different timing for warnings/errors, incomplete progress reporting.

**Recommendation**: Update `quickstart.md` dummy job example to match spec exactly (20s per host, 2s logging, WARNING at 6s, ERROR at 8s, progress 0/25/50/75/100).

#### 4. Event Type Definition (Minor)
**Location**: `data-model.md` uses incorrect TypeVar for Event union.
**Impact**: Type checking issues.

**Issue**: `Event = TypeVar("Event", LogEvent, ProgressEvent, ConnectionEvent)` should be `type Event = LogEvent | ProgressEvent | ConnectionEvent`

**Recommendation**: Fix the type definition in data-model.md.

#### 5. Progress Event Structure (Minor)
**Location**: Inconsistency between architecture and data-model for ProgressEvent.
**Impact**: Implementation confusion.

**Issue**: Architecture shows ProgressEvent with direct fields, data-model wraps ProgressUpdate.

**Recommendation**: Standardize on data-model approach (wrapping ProgressUpdate) for consistency.

---

## Consistency Assessment

### Document Consistency Issues

#### 1. Validation Phase Terminology (Minor)
**Location**: Consistent across documents, but ensure implementation matches the three-phase separation.
**Status**: Documents align, but verify FR-030 doesn't conflate phases.

#### 2. Logging Serialization Strategy (Major)
**Location**: `quickstart.md` shows `event.to_json()`, but `data-model.md` LogEvent has no such method.
**Impact**: Implementation mismatch.

**Issue**: Need to define how events are serialized. FR-022 specifies structlog JSONRenderer.

**Recommendation**: Update quickstart.md to use structlog processors instead of custom to_json().

#### 3. Job Config Schema Access (Minor)
**Location**: FR-001 mentions `get_config_schema()` method, but contract uses `CONFIG_SCHEMA` class attribute.
**Impact**: API confusion.

**Issue**: Contract correctly uses class attribute, but FR-001 references a method.

**Recommendation**: Update FR-001 in spec.md to reference `CONFIG_SCHEMA` class attribute.

#### 4. Version Check Ordering (Critical)
**Location**: `architecture.md` execution flow conflicts with spec (User Story 2, FR-005).
**Impact**: Incorrect implementation would violate spec requirements.

**Issue**: Architecture shows pre-sync snapshots before InstallOnTargetJob, but spec requires version check/install as very first operation.

**Recommendation**: Reorder execution flow in architecture.md to: version check/install → subvolume validation → snapshots → job validation/execution.

---

## Valuable Feedback

### Positive Aspects
1. **Excellent EventBus Design**: The decoupling of producers/consumers is a strong architectural choice that prevents UI blocking and enables extensibility.
2. **Three-Phase Validation**: The fail-fast approach with clear phase separation provides excellent user experience.
3. **Modern Python Usage**: Full asyncio adoption, type hints, and modern syntax demonstrate best practices.
4. **Comprehensive Research**: Technology choices are well-justified with alternatives considered.

### Recommendations for Improvement
1. **Add Implementation Timeline**: `plan.md` could benefit from estimated effort/complexity for each component to aid prioritization.
2. **Error Handling Patterns**: Consider documenting common error handling patterns (exceptions vs return values) across components.
3. **Testing Strategy**: While pytest is mentioned, consider adding more detail on integration testing approaches, especially for SSH operations.
4. **Performance Benchmarks**: Define expected performance baselines for key operations (connection time, command execution, etc.).
5. **Security Considerations**: Document SSH key requirements, sudo usage patterns, and any security implications of the "smart source, dumb target" model.

### Implementation Readiness
The plan is **conditionally ready** for implementation. The identified gaps and inconsistencies must be resolved first, particularly the self-installation details, disk space preflight, and JobContext logging standardization. Once fixed, the plan provides a solid foundation for reliable implementation.

---

## Action Items Summary

**Critical (Block Implementation)**:
- Resolve JobContext logger inconsistency
- Add self-installation workflow details
- Fix version check ordering in architecture
- Add disk space preflight checks

**Major (Required for Completeness)**:
- Implement snapshot cleanup algorithm
- Add locking mechanism integration
- Create installation/setup script

**Minor (Quality of Life)**:
- Fix dummy job example to match spec
- Correct Event type definition
- Standardize ProgressEvent structure
- Update logging serialization approach

**Spec Updates Needed**:
- Clarify CONFIG_SCHEMA access in FR-001</content>
<parameter name="filePath">/home/janfr/dev/pc-switcher/specs/001-foundation/plan-review-4-detail.md