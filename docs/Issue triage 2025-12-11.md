# GitHub Issues Analysis - Project Manager Perspective

**Date**: 2025-12-11

## Context
- **Features 1-3 implemented**: Basic CLI, Safety Infrastructure, Installation & Setup
- **Feature 4**: Rollback - explicitly deferred for later
- **Current branch**: 003-foundation-tests (tests for features 1-3)
- **Next phase**: Features 5-10 (User Data, Packages, System Config, Docker, VMs, k3s)

---

## Issues for 003-foundation-tests (before merge to main)

These directly relate to the testing infrastructure being built in the current branch:

| Order | Issue | Title | Rationale |
|-------|-------|-------|-----------|
| 1 | **#52** | Set lock on each VM used by testing framework | Test infrastructure integrity - prevents race conditions |
| 2 | **#53** | Version functions should return Version instead of str | Code quality improvement for foundation module |
| 3 | **#55** | Make Version.release_version() aware of GitHub releases | Correctness fix for version semantics |
| 4 | **#59** | Make BashLogin an option of Remote Executor | Refactor test infrastructure for broader use |

---

## Issues to implement NOW (before features 5-10)

Critical for safe development of the sync features:

| Order | Issue | Title | Rationale |
|-------|-------|-------|-----------|
| 1 | **#37** | Add --dry-run to sync | Essential safety feature - test sync operations without risk. Required before implementing actual data sync in features 5-10. |
| 2 | **#48** | Command line options/flags for all interactive questions | Enables non-interactive mode for CI/automation. Important for testing features 5-10. |
| 3 | **#40** | Automatic deletion of test-VMs when not in use for x time | Cost optimization for test infrastructure. |

---

## Issues to DEFER to specific features

| Issue | Title | When | Rationale |
|-------|-------|------|-----------|
| **#31** | Rollback cli command | Feature 4 | Explicitly planned for later per feature breakdown |
| **#46** | Sync VSCode | Feature 5 (User Data Sync) | This is a sub-task of user data sync |
| **#47** | Warning on consecutive syncs from same source | Feature 5 | UX safety feature needed when actual syncing starts |

---

## Issues to DEFER (architectural enhancements for "Ideas for later")

These are interconnected architectural improvements for advanced execution patterns:

| Issue | Title | Dependency | Priority |
|-------|-------|------------|----------|
| **#28** | DAG for Jobs | None | Low - mentioned in "Ideas for later: Parallel run of sync jobs" |
| **#29** | "Forever running" Jobs | #28 | Low - complex feature, not needed for basic sync |
| **#30** | Remove 1 job_module = 1 Job class constraint | None | Low - enhancement, not blocking |
| **#24** | Make DiskSpaceMonitor optional and config-controlled | #28, #29 | Low - requires both DAG and forever-running jobs |

---

## Issues to DEFER (test infrastructure improvements)

Nice-to-have operational improvements:

| Issue | Title | When | Priority |
|-------|-------|------|----------|
| **#45** | SSH key management: VMs should accept multiple authorized keys | When multiple developers actively contribute | Medium |
| **#36** | Restore test-VMs from Hetzner snapshot | When cost optimization needed | Low |

---

## Issues for FUTURE requirement changes

Keep open - to be addressed when relaxing requirements:

| Issue | Title | Notes |
|-------|-------|-------|
| **#26** | Remove constraint to be on Ubuntu 24.04 | Current requirements specify Ubuntu 24.04, but could be relaxed later |
| **#23** | Don't require BTRFS | Current requirements specify btrfs, but could support other filesystems later (without rollback) |

---

## Recommended Implementation Order Summary

### Phase A: Complete 003-foundation-tests branch
1. #52 - Set lock on each VM used by testing framework
2. #53 - Version functions should return Version instead of str
3. #55 - Make Version.release_version() aware of GitHub releases
4. #59 - Make BashLogin an option of Remote Executor
5. → Merge 003 to main

### Phase B: Pre-requisites for features 5-10
1. #37 - Add --dry-run to sync
2. #48 - Command line options for interactive questions
3. #40 - Automatic deletion of test-VMs when not in use

### Phase C: Start features 5-10
- Feature 5 (User Data Sync) incorporates #46 and #47
- Feature 4 (Rollback) implemented when planned, includes #31

### Phase D: Future architectural enhancements (later)
- #28, #29, #30, #24 - Job execution improvements
- #45, #36 - Test infrastructure improvements

### Keep open for future requirement relaxation
- #26 - Remove Ubuntu 24.04 constraint
- #23 - Don't require BTRFS
