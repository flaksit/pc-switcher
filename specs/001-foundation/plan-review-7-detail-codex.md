# Plan Review 7 (Detail)

Findings on completeness, correctness, and consistency for the current plan (all files under `specs/001-foundation/` except `spec.md` and `checklists/`).

1) Self-installation flow diverges from the spec (User Story 2, FR-005/006/007).  
   - Execution order in `architecture.md` runs schema + job-config validation, then SSH + locks, *then* version check; the spec requires the version check/auto-install to be the very first orchestrator action before any validation or other operations.  
   - The install path in `architecture.md` uses `install.sh` with `uv tool install pc-switcher` (no Git URL) and does not show how the exact source version is enforced. The spec requires `uv tool install git+https://github.com/.../pc-switcher@v<version>` pinned to the source version, with a CRITICAL abort if the target is newer. Clarify how the source version is obtained and passed, and ensure the command matches the spec (including timeout target of 30s).

2) Snapshot infrastructure positioning and path details are unclear (User Story 3, FR-008/009/010/011/015).  
   - `architecture.md` models snapshots as `BtrfsSnapshotJob` SystemJobs. The spec calls this “orchestrator-level infrastructure (not a SyncJob) and always active.” Spell out how these jobs are invoked outside `sync_jobs`, cannot be skipped, and are always run pre/post.  
   - Snapshot creation paths are not defined. Commands assume subvolumes at `/{subvol}` and do not specify the top-level btrfs mount or where snapshots are stored. On typical Ubuntu btrfs layouts, subvolumes are mounted elsewhere (e.g., `/` is a subvolume), so `/{subvol}` may not exist. Define how mount points/top-level roots are discovered and where snapshots are written so naming in FR-010 is usable and restoration is possible on both hosts.

3) Disk space checks are too narrow (FR-016/017, Edge Case “insufficient space”).  
   - Preflight and runtime checks in `architecture.md`/`research.md` only run `df` on `/`. If `/home` (or other configured subvolumes) are separate mounts, low space there will be missed. Tie both preflight and `DiskSpaceMonitorJob` checks to the configured snapshot subvolume mount points (or allow per-path checks) using the thresholds from config.  
   - Clarify the runtime abort flow (interval, grace, logging) and ensure the monitor enforces `runtime_minimum` for both hosts, not just the default `/`.

4) Logs omit hostnames; data model conflicts with intended format (User Story 4, FR-021/022).  
   - `data-model.md`’s `LogEvent.to_dict()` emits `host` enum only. The spec acceptance examples require hostnames in the output (`[MODULE] [HOSTNAME] ...`), and `architecture.md` claims logger resolves host→hostname. Add hostname fields to file/CLI output and ensure both consumers include them.

5) `pc-switcher logs --last` behavior is underspecified (User Story 4, acceptance scenario 6).  
   - `architecture.md` lists the command but has no flow for locating the newest log, handling absence, or rendering with Rich highlighting. Add the retrieval/formatting/filters expected by the scenario.

6) Target-side log parsing protocol missing (User Story 4, acceptance scenario 5).  
   - Plan mentions parsing “log level prefix (if present)” but never defines the prefix format, how stdout/stderr from remote helpers are streamed, or fallback handling when no prefix is present. Specify the protocol so target helpers can reliably feed the unified logger.

7) File logger example is incorrect (`quickstart.md`).  
   - The sample uses `structlog.processors.JSONRenderer()` and treats its return as a `(str, str)` tuple; JSONRenderer returns a string, so the snippet would crash. Provide the correct structlog pipeline (processors, context binding) and ensure sentinel handling (`None`) is compatible with the queue typing.

8) Job execution order from config not defined (FR-004).  
   - The plan states “order from config,” but no mechanism is described to preserve YAML order or to reject unknown job names. `sync_jobs` schema currently allows arbitrary keys; the spec edge case says unknown jobs should error. Clarify how the orchestrator preserves order and validates job names before instantiation.

9) Interrupt/termination handling needs concrete guarantees (FR-002/003/024/025/027).  
   - The sequence diagrams rely on TaskGroup cancellation but do not show the enforced 5s grace window or how remote processes started via `RemoteExecutor` are tracked and killed to avoid orphans. `executor.py` in `quickstart.md` tracks processes for `start_process` only; long-lived `run_command` processes aren’t tracked. Document the timeout enforcement, remote kill strategy, and double-SIGINT behavior so orphan processes are impossible on either host.

10) Dummy-fail job behavior is undocumented (User Story 8, FR-041).  
    - Only `dummy-success` is described. Define `dummy-fail` behavior per spec (progress milestones, failure at 60%, termination logging) and how it exercises the orchestrator error path.

11) Lock diagnostics on target are incomplete (Edge Case: concurrent invocations).  
    - The target lock (`flock -n ... -c "cat"`) doesn’t write holder info, so the orchestrator cannot emit the required “Another sync is in progress (PID/host: ... )” style message. Align target-lock acquisition with the diagnostic expectations and add guidance for stale lock resolution.

12) Snapshot cleanup CLI/options need alignment (User Story 3, FR-014).  
    - `architecture.md` shows `cleanup-snapshots --older-than 7d`, but the algorithm expects an integer days value; `7d` would fail parsing. Specify accepted formats (e.g., `--older-than 7` days vs `7d`) and ensure they match the schema/defaults.

13) Connection-loss edge case not covered (Edge Case: target unreachable mid-sync).  
    - The spec expects a reconnect attempt and CRITICAL abort with diagnostics if reconnection fails. No reconnection strategy is described in `architecture.md` or `research.md`. Add detection, single retry, and abort messaging flow.

14) Config validation gaps for unknown jobs and defaults.  
    - `config-schema.yaml` allows arbitrary `sync_jobs` keys, conflicting with the edge-case requirement to error on unknown job names. Add validation/explicit allowlist for current jobs and required jobs, and ensure defaults/omissions are filled consistently with FR-031.

15) Terminal UI resilience is implicit only (User Story 9).  
    - Rich likely handles resize, but the plan doesn’t state redraw/resize handling or throttling strategy. Document resize behavior and any batching to meet “smooth updates, graceful resize” acceptance criteria.
