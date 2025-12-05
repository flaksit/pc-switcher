# Plan Review 7 Resolution Report

**Date**: 2025-11-30
**Review File**: plan-review-7-detail-codex.md

## Overriding Instructions from User

The following instructions were provided and take precedence over the review feedback:

- For 1.: The flow in @specs/001-foundation/architecture.md is good. Maybe @specs/001-foundation/spec.md needs to be updated. But don't put too much detail in spec.md
- For 2.: Snapshots are to be placed in a subfolder of /.snapshots/: a new subfolder for each sync session
- For 3.: It is good as it is now: we suppose a **single** btrfs filesystem. It would be good to put this in the assumptions in the documentation. We won't add a check for this now. Update the docs:
    - Chapter "Environment" in @"docs/High level requirements.md"
    - Chapter "Requirements" in @README.md
    - Other relevant places?
- For 6.: Is probably a flaw in spec.md, specifying too much detail for a user spec. The Job does the logging based on the output of the command running on the target, so the command executed on the target is not required to do anything special for their output. At least, we shouldn't specify it here: it's the Jobs responsibility to handle this.
- For 8. and 14.: Verify as well that config validation does NOT accept arbitrary sync_jobs names.
- For 9.: If this remark is valid, it probably applies to LocalExecutor as well.
- For 13.: Is reconnection and resume possible without adding much more logic? Is suppose that if we loose connection and a command is running over a RemoteExecutor, the command is killed automatically? Or would it possibly still be running on the target and could we re-attach to it? This all seems very complicated. We don't do reconnection for now. Remove this from any document, including spec.md

## Clarifying Questions and Answers

**Q1 (Feedback point 5 - `pc-switcher logs --last`):**
> Should I add more detail about log file location, Rich highlighting, handling missing logs?

**A:** Ignore. This is implementation level detail.

**Q2 (Feedback point 7 - FileLogger structlog usage):**
> The quickstart.md example has an error. How to fix?

**A:** Use structlog to translate dict to log line string. Keep the responsibility with structlog.

**Q3 (Feedback point 10 - dummy-fail job):**
> Should I add full implementation pattern for dummy-fail?

**A:** Ignore. The quickstart.md is only there to give examples to the developer. In the plan phase, we don't do the full implementation.

**Q4 (Feedback point 11 - Target lock diagnostics):**
> Current flock implementation can't show who holds the lock. What approach?

**A:** Accept as is. Just say "Another sync is in progress on target" without source info.

**Q5 (Feedback point 12 - cleanup-snapshots CLI):**
> What format for `--older-than`?

**A:** Human-readable format. Use a library for parsing (don't write parsing ourselves).

**Q15 (Feedback point 15 - Terminal UI resize):**
> Need more detail about resize handling?

**A:** No more detail needed. Rich handles it automatically.

---

## Resolution by Feedback Point

### Point 1: Self-installation flow diverges from spec
**Status:** RESOLVED - No change needed

The flow in architecture.md is correct. The architecture shows:
1. Schema validation + Job config validation (config file parsing)
2. SSH connection + locks
3. Version check/install
4. Subvolume validation
5. Disk preflight
6. Snapshots

This is the intended order. The spec says version check should be "before any validation or snapshots" - config validation is not system validation. Config is loaded locally before connecting to target.

**Action:** No changes to architecture.md. The flow is correct as designed.

### Point 2: Snapshot infrastructure positioning and path details
**Status:** RESOLVED - Documentation updated

**Decision:** Snapshots are stored in `/.snapshots/<session-id>/` on both source and target. Each sync session gets its own subfolder.

**Changes made:**
- Updated architecture.md to specify snapshot location: `/.snapshots/<session-id>/<snapshot-name>`
- Updated research.md to document snapshot path strategy
- Added note that subvolumes must be accessible from btrfs top-level mount

### Point 3: Disk space checks too narrow
**Status:** RESOLVED - Documented as assumption

**Decision:** We assume a single btrfs filesystem. Checking `/` is sufficient because all configured subvolumes are on the same filesystem. We won't add per-path checks.

**Changes made:**
- Updated "Environment" section in `docs/High level requirements.md` to document single btrfs assumption
- Updated "Requirements" section in `README.md` to include single btrfs filesystem assumption
- Updated architecture.md assumptions section

### Point 4: Logs omit hostnames
**Status:** RESOLVED - Already handled

The architecture.md already states: "Logger resolves `host` → `hostname` internally for output (UI and log files)". The data-model.md `LogEvent.to_dict()` outputs `host` enum value, but the Logger is responsible for including the resolved hostname in the actual output.

**Action:** Updated data-model.md to clarify that Logger adds `hostname` field when writing to file/console, using the hostnames from JobContext.

### Point 5: `pc-switcher logs --last` behavior underspecified
**Status:** IGNORED per user instruction

This is implementation-level detail, not plan-level.

### Point 6: Target-side log parsing protocol missing
**Status:** RESOLVED - Removed from spec

**Decision:** This is too much detail for spec.md. Jobs are responsible for handling command output and deciding what to log. Target commands don't need any special output format.

**Changes made:**
- Removed acceptance scenario 5 from spec.md (User Story 4)

### Point 7: File logger example incorrect
**Status:** RESOLVED - Fixed example

**Changes made:**
- Updated quickstart.md FileLogger example to use correct structlog pattern

### Point 8 & 14: Config validation gaps for unknown jobs
**Status:** RESOLVED - Schema updated

**Decision:** The config schema should NOT accept arbitrary sync_jobs names. Only known job names are valid.

**Changes made:**
- Updated config-schema.yaml to use explicit property names instead of additionalProperties for sync_jobs
- Added validation note in architecture.md about rejecting unknown job names

### Point 9: Interrupt/termination handling needs concrete guarantees
**Status:** RESOLVED - LocalExecutor also tracks processes

**Decision:** Both LocalExecutor and RemoteExecutor should track processes started via `start_process()` to enable cleanup.

**Changes made:**
- Updated quickstart.md LocalExecutor pattern to include process tracking
- Added note about `run_command` using wait_for timeout

### Point 10: Dummy-fail job behavior undocumented
**Status:** IGNORED per user instruction

The quickstart.md provides examples, not full implementation. This is plan phase, not implementation.

### Point 11: Lock diagnostics on target incomplete
**Status:** RESOLVED - Accepted limitation

**Decision:** Accept that target lock cannot show holder info. Error message will be "Another sync is in progress on target" without source identification.

**Action:** Updated architecture.md error message example to reflect this.

### Point 12: Snapshot cleanup CLI/options alignment
**Status:** RESOLVED - Use library for duration parsing

**Decision:** Use human-readable format (`7d`, `2w`) and parse with a library (e.g., `pytimeparse` or similar).

**Changes made:**
- Updated architecture.md to specify human-readable duration format
- Added note to use a duration parsing library

### Point 13: Connection-loss edge case not covered
**Status:** RESOLVED - Removed reconnection

**Decision:** Reconnection is too complex. If connection is lost, remote commands are killed automatically by SSH termination. We abort the sync.

**Changes made:**
- Removed reconnection mention from spec.md edge cases
- Removed "reconnecting" status from ConnectionEvent in data-model.md
- Updated architecture.md to clarify: connection loss = abort (no reconnection)

### Point 15: Terminal UI resilience implicit only
**Status:** RESOLVED - No additional detail needed

Rich handles terminal resize automatically. No explicit documentation needed.

---

## Summary of Document Changes

| Document | Changes |
|----------|---------|
| spec.md | Removed acceptance scenario 5 (target-side log parsing); Removed reconnection from edge cases; Removed `required` job concept (FR-034); Removed btrfs check from install; Updated snapshot naming format; Renamed `disk.` to `disk_space_monitor.` |
| architecture.md | Added snapshot location (`/.snapshots/pc-switcher/<timestamp>-<session-id>/`); Updated error messages; Added single btrfs assumption; Removed reconnection; Clarified unknown job rejection; Removed `required` from Job classes; Simplified install script (no btrfs check); Added `packaging.version` usage; Renamed `disk:` to `disk_space_monitor:`; Added config key naming convention note |
| data-model.md | Removed "reconnecting" from ConnectionEvent; Clarified hostname resolution; Updated snapshot naming format; Renamed `disk.` to `disk_space_monitor.` |
| research.md | Added snapshot path strategy; Added duration parsing library note; Added version comparison library (packaging.version) |
| quickstart.md | Fixed FileLogger structlog usage; Added LocalExecutor process tracking; Added packaging and pytimeparse2 dependencies; Renamed `disk:` to `disk_space_monitor:` |
| config-schema.yaml | Changed sync_jobs to explicit property list instead of additionalProperties; Renamed `disk:` to `disk_space_monitor:`; Added developer note on naming convention |
| job-interface.md | Removed `required` attribute from Job class hierarchy |
| High level requirements.md | Added single btrfs filesystem assumption |
| README.md | Added single btrfs filesystem requirement |

---

## Conversation Log

### Claude's Clarifying Questions

> Based on my analysis of the review feedback and your overriding instructions, I have a few clarifying questions:
>
> **Question 1 (Feedback point 5 - `pc-switcher logs --last`):**
> The review notes this behavior is underspecified. Should I add more detail to the plan about:
> - How to locate the newest log file (glob for `sync-*.log` sorted by timestamp)?
> - Rich syntax highlighting for JSON log viewing?
> - Handling when no logs exist?
>
> Or should this be left as implementation detail for the tasks phase?
>
> **Question 2 (Feedback point 7 - FileLogger structlog usage):**
> The review correctly identifies that the quickstart.md example has an error - `JSONRenderer` returns a `str`, not a `(str, str)` tuple. Should I:
> - Fix this to use the correct structlog pattern (calling the processor chain correctly)?
> - Or simplify it by just using `json.dumps()` directly since we already have the dict from `to_dict()`?
>
> **Question 3 (Feedback point 10 - dummy-fail job):**
> The review notes that only `dummy-success` is documented in quickstart.md. Should I add a full implementation pattern for `dummy-fail` similar to `dummy-success`, or just add a brief description of its expected behavior?
>
> **Question 4 (Feedback point 11 - Target lock diagnostics):**
> The current target lock implementation with `flock` doesn't write holder info before acquiring the lock (the info is written after `flock` succeeds). This means if another sync is in progress, we can't easily tell who holds it. Options:
> - Accept this limitation and just say "Another sync is in progress on target" without source info
> - Change the approach to write holder info first, then use `flock` (more complex)
> - Use a different locking approach on target
>
> Which approach do you prefer?
>
> **Question 5 (Feedback point 12 - cleanup-snapshots CLI):**
> The `--older-than 7d` format vs integer days. Should the CLI accept:
> - Only integer days (`--older-than 7`)
> - Human-readable format (`--older-than 7d`, `--older-than 2w`)
> - Both
>
> **Question 15 (Terminal UI resize):**
> The review asks about documenting resize behavior. Rich's Live does handle resize automatically. Should I just add a note that "Rich handles terminal resize automatically" or do we need more detail?

### User's Answers

> Q1: Ignore 5. This is implementation level.
> Q2: Best to leave the responsibility to translate a dict to a log line string with structlog
> Q3: Ignore 10. The quickstart.md is only there to give some examples to the developer. In the plan phase, we don't do the full implementation.
> Q4: Accept as it is
> Q5: Human-readable format. Ensure we don't write the parsing ourselves but use a library that does this for us.
> Q15: No more detail needed

---

## Additional Changes (Post-Review)

After initial resolution, additional refinements were made to the snapshot location strategy:

### Snapshot Location Improvements

**User Request:**
> - Put them in a subfolder `/.snapshots/pc-switcher` to easily recognize them from other snapshots
> - The folder name should not only contain the session id. Use the datetime stamp + session id (e.g. `20251129T143022-abc12345`) as folder name so that they are sorted in order
> - If the `/.snapshots/` folder already exists, it should be verified that it is a separate btrfs volume
> - If the `/.snapshots/` folder does not exist, create it as a subvolume and tell the user

**Changes Made:**

1. **architecture.md** - Updated snapshot location section:
   - Path changed to `/.snapshots/pc-switcher/<timestamp>-<session-id>/`
   - Added "Snapshot Directory Validation" section with detailed validation logic
   - Documented why `/.snapshots/` must be a subvolume (prevents recursive snapshots)

2. **spec.md** - Added new requirements and scenarios:
   - Added **FR-015b**: System MUST verify `/.snapshots/` is a btrfs subvolume; create if missing; abort if exists but not a subvolume
   - Updated acceptance scenario 2 to include new path format
   - Added acceptance scenarios 4 and 4b for `/.snapshots/` validation

3. **research.md** - Updated snapshot location section:
   - New path structure with `pc-switcher/` subfolder
   - Validation commands for checking/creating subvolume

**Final Snapshot Path Format:**
```text
/.snapshots/pc-switcher/20251129T143022-abc12345/pre-@home-20251129T143022
```

### Install Script Simplification

**User Request:**
> Remove btrfs filesystem check from install script - it's already documented in README.md and checked at runtime by pc-switcher.

**Rationale:**
- btrfs filesystem is a documented prerequisite (README.md Requirements section)
- Runtime check by pc-switcher catches non-btrfs systems before any sync operations
- Duplicate check in install script is unnecessary
- Removing it allows installation on any system for development/testing purposes

**Changes Made:**

1. **spec.md**:
   - Removed btrfs check from acceptance scenario 1 (User Story 7)
   - Removed acceptance scenario 2 (non-btrfs detection at install time)
   - Removed FR-036 (btrfs detection requirement)
   - Renumbered FR-037 → FR-036 (default config generation)
   - Updated FR-035 to note btrfs is checked at runtime, not installation

2. **architecture.md**:
   - Removed "Checks filesystem is btrfs" from install script steps
   - Removed btrfs check from mermaid diagrams (shared logic, installation flow)
   - Removed "Filesystem Check" section with bash code
   - Updated FR references (FR-037 → FR-036)
   - Added note explaining why btrfs check is done at runtime

### Config Key Naming Convention

**User Request:**
> Make config schema more straightforward: all config for Jobs should be under a top-level key that has exactly the name of the module. `disk` should become `disk_space_monitor`.

**Rationale:**
- Predictable config location for any job
- Automatic config routing by orchestrator
- Clear correspondence between code modules and config keys

**Changes Made:**

1. **config-schema.yaml**:
   - Renamed `disk:` to `disk_space_monitor:`
   - Added developer note explaining the naming convention

2. **spec.md**:
   - Updated acceptance scenarios 8-9 to use `disk_space_monitor.` prefix
   - Updated FR-016, FR-017 to use `disk_space_monitor.` prefix

3. **architecture.md**:
   - Updated all config examples to use `disk_space_monitor:`
   - Added "Config Key Naming Convention (Developer Note)" section with table of examples

4. **quickstart.md**:
   - Updated config example to use `disk_space_monitor:`

5. **data-model.md**:
   - Updated validation rules table to use `disk_space_monitor.`

---

## Resolution Complete

All feedback points from plan-review-7-detail-codex.md have been addressed according to the overriding instructions and the clarified decisions above. The plan documents are now consistent with the specifications and ready for task generation.
