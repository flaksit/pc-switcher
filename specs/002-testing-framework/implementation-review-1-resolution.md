# Implementation Review 1 Resolution

## ARGUMENTS

(No arguments passed - resolving all findings from implementation-review-1.md)

## Conversation Log

### Initial Analysis

I analyzed the implementation review findings against the actual codebase:

**Finding 1 (Integration tests absent)**: Confirmed. `tests/integration/` contains only `__init__.py` and `conftest.py` (fixtures), no actual test files. User Story 2 acceptance scenarios are unmet.

**Finding 2 (CI logs not preserved)**: Confirmed. The CI workflow uploads `pytest-output.log` but pytest isn't redirected to create this file.

**Finding 3 (Forked PRs skip silently)**: Confirmed. The workflow conditionally skips integration on forks but there's no visible notice in the workflow output.

**Finding 4 (Lock docs wrong)**: Confirmed. `docs/testing-framework.md` incorrectly describes lock as file-based at `/tmp/pc-switcher-integration-test.lock`, but actual implementation uses Hetzner Server Labels via `lock.sh`.

**Finding 5 (Baseline docs mismatch)**: Confirmed.
- Docs reference `/.snapshots/pc-switcher/baseline` but scripts use `/.snapshots/baseline`
- Docs reference `restore-baseline-snapshots.sh` which doesn't exist (reset-vm.sh does the work)

No clarifying questions needed - all findings were straightforward to address.

---

## Resolutions

### Finding 1: Integration Test Suite Absent

**Resolution Approach**: Create minimal integration tests that verify:
- VM connectivity and command execution
- Real btrfs snapshot operations
- Both success and failure paths as required by User Story 2 and FR-003a

**Files Created**:

1. `tests/integration/test_vm_connectivity.py` (12 tests):
   - `test_pc1_basic_command` - Basic command execution on pc1
   - `test_pc2_basic_command` - Basic command execution on pc2
   - `test_command_captures_stdout` - stdout/stderr separation
   - `test_command_failure_nonzero_exit` - Non-zero exit code handling
   - `test_command_failure_invalid_command` - Invalid command handling
   - `test_command_timeout` - Timeout enforcement
   - `test_pc1_hostname_matches` - Hostname verification
   - `test_pc2_hostname_matches` - Hostname verification
   - `test_inter_vm_ssh_connectivity` - PC1 to PC2 SSH (critical User Story 2 scenario)
   - `test_command_working_directory` - Working directory isolation
   - `test_environment_variables` - Environment variable availability
   - `test_multiline_output` - Multi-line output handling

2. `tests/integration/test_btrfs_operations.py` (11 tests):
   - `test_btrfs_filesystem_present` - Btrfs filesystem verification
   - `test_btrfs_test_volume_exists` - Test volume existence
   - `test_create_readonly_snapshot` - Read-only snapshot creation
   - `test_create_writable_snapshot` - Writable snapshot creation
   - `test_list_snapshots` - Snapshot listing
   - `test_delete_snapshot` - Snapshot deletion
   - `test_snapshot_creation_failure_invalid_source` - Failure path (FR-003a)
   - `test_snapshot_creation_failure_invalid_destination` - Failure path (FR-003a)
   - `test_delete_snapshot_failure_nonexistent` - Failure path (FR-003a)
   - `test_snapshot_preserves_content` - Data integrity verification
   - `test_multiple_snapshots_independent` - Snapshot isolation

**Verification**: 23 integration tests collected, all pass type checking and linting.

### Finding 2: CI Logs/Artifacts Not Preserved

**Changes to `.github/workflows/test.yml`**:

1. **Provision step** (line 137): Added `2>&1 | tee provision-output.log`
2. **Reset step** (lines 147-150): Wrapped in subshell with `2>&1 | tee reset-output.log`
3. **Pytest step** (line 166): Added `2>&1 | tee pytest-output.log`
4. **Upload step** (lines 174-184):
   - Renamed from "Upload pytest output" to "Upload test logs"
   - Artifact name changed to "test-logs"
   - Path includes all three log files
   - Changed `if-no-files-found: ignore` to `if-no-files-found: warn`

### Finding 3: Forked PR Skip Notice

**Changes to `.github/workflows/test.yml`**:

1. Removed job-level `if` condition that prevented fork PRs from running the job
2. Added new step "Check fork status" (lines 69-78) that:
   - Detects fork PRs by comparing repo names
   - Emits `::notice::Integration tests skipped: Fork PRs do not have access to secrets`
   - Sets output `is_fork=true/false`
3. Added "Exit if fork" step (lines 80-82) for graceful exit
4. Updated all subsequent steps to check `steps.check_fork.outputs.is_fork == 'false'`

### Finding 4: Locking Documentation Divergence

**Changes to `docs/testing-framework.md`**:

Updated the "Lock-Based Isolation" section (lines 233-239) from:
```
The lock file is stored on pc1 VM at `/tmp/pc-switcher-integration-test.lock`.
- Lock holder info includes: CI job ID or username
- Maximum wait time: 5 minutes
```

To:
```
The lock is stored as **Hetzner Server Labels** on the `pc-switcher-pc1` server (not as a file on the VM). This approach survives VM reboots and snapshot rollbacks:
- **Lock labels**: `lock_holder` (identifier) and `lock_acquired` (ISO8601 timestamp)
- **Lock holder format**: `ci-<run_id>` for CI jobs, `local-<username>` for local runs
- **Maximum wait time**: 5 minutes with 10-second retry intervals
- **Atomic acquisition**: Uses Hetzner API with 1-second verification to detect race conditions
```

### Finding 5: Baseline/Reset Docs Out of Sync

**Changes to `docs/testing-framework.md`**:

1. Updated snapshot path from `/.snapshots/pc-switcher/baseline/@` to `/.snapshots/baseline/@` (lines 212-214)
2. Added `(via reset-vm.sh)` reference to reset procedure
3. Updated "Additional Guides" section to link to existing docs (not "to be created")

**Changes to `docs/testing-developer-guide.md`**:

1. Updated "Resetting VMs to Baseline" section (lines 580-592):
   - Replaced `./scripts/restore-baseline-snapshots.sh` with `./scripts/reset-vm.sh`
   - Added proper usage with host variable

2. Updated "Lock acquisition timeouts" troubleshooting (lines 656-670):
   - Changed from SSH to VM file check to `lock.sh "" status`
   - Changed cleanup from removing file to removing Hetzner labels

3. Updated "Baseline snapshot issues" troubleshooting (lines 678-692):
   - Fixed path from `/.snapshots/pc-switcher/baseline` to `/.snapshots/baseline`
   - Changed from `create-baseline-snapshots.sh` to `provision-test-infra.sh`

4. Updated "Btrfs Device busy" troubleshooting (lines 743-747):
   - Changed from `restore-baseline-snapshots.sh` to `reset-vm.sh`

---

## Validation

All changes validated:

- **Type check**: `uv run basedpyright` → 0 errors, 0 warnings, 0 notes
- **Lint**: `uv run ruff check` → 0 errors (after auto-fix)
- **Format**: `uv run ruff format` → all files formatted
- **Unit/Contract tests**: `uv run pytest tests/unit tests/contract -v` → 53 passed
- **Integration tests collected**: `uv run pytest tests/integration -v -m integration --collect-only` → 23 tests
- **YAML validation**: Valid YAML syntax
- **Codespell**: No spelling errors in modified files

---

## Files Modified

### New Files
- `tests/integration/test_vm_connectivity.py`
- `tests/integration/test_btrfs_operations.py`

### Modified Files
- `.github/workflows/test.yml`
- `docs/testing-framework.md`
- `docs/testing-developer-guide.md`
- `tests/integration/conftest.py` (auto-formatted, one lint fix)
