Notes from JF in square brackets [] in between the feedback from Codex. JF is the boss.


1. **Module dependency handling contradicts the spec.**  
   *Spec reference:* User Story 1, Scenario 7 requires modules to declare dependencies and for the orchestrator to topologically sort them.  
   *Plan reference:* Both plan.md (Summary item 1) and the contracts (module-interface.py, orchestrator-module-protocol.md) repeatedly state “sequential execution in config-defined order, no dependency resolution.”  
   → Reintroduce dependency declarations plus the sorting/validation logic in the plan, data model, and contracts so they match the spec.

   [Update the spec instead to remove the dependency requirement.]

2. **Rollback offer after CRITICAL failures is missing.**  
   *Spec reference:* User Story 3, Scenario 6 mandates that when a module fails critically, the orchestrator must prompt the user to restore the pre-sync snapshots.  
   *Plan reference:* The contracts only mention “manual rollback” (e.g., module-interface.py abort docs, orchestrator-module-protocol.md cleanup section) with no UX flow for offering or executing the rollback.  
   → Add the rollback-offer workflow (prompt text, confirmation handling, how snapshots are applied) to the orchestrator protocol, module responsibilities, and task plan.

3. **Quickstart still suggests installing uv via an unpinned “latest” script.**  
   In quickstart.md → Prerequisites, developers are told to run `curl -LsSf https://astral.sh/uv/install.sh | sh`, which fetches whatever the latest version is — explicitly called out as “not allowed” in JF’s review.  
   → Replace that command with a version-pinned installer URL (e.g., `https://astral.sh/uv/0.9.9/install.sh`) and mention .tool-versions as the single source of truth.

4. **Installer coverage on the source side is still undefined.**  
   The project structure in plan.md only lists `remote/installer.py` (target install/upgrade) and `scripts/target/remote_helpers.py`. JF explicitly asked for “the installer itself (used on the source system as well),” but no source-side installer entry or plan exists.  
   → Add the missing installer artifact to the structure (path, purpose) plus a short execution plan so there’s a concrete place to implement the self-install/setup workflow.

5. **Configuration schema vs. data model inconsistencies.**  
   - config-schema.yaml nests disk thresholds under a `disk` object (`disk.min_free`, `disk.reserve_minimum`, etc.), while data-model.md describes separate top-level fields (`disk_min_free`, `disk_reserve_minimum`, `disk_check_interval`).  
   - Config schema treats `btrfs_snapshots.subvolumes` as *flat names* from `btrfs subvolume list /`, but the data model defines `Snapshot.subvolume` as paths like `/` or home.  
   → Reconcile both documents so the shapes and semantics match exactly (either flatten the schema or update the data model and snapshot naming examples).

   [Subvolumes should be specified in the flat form ("@" for the root subvolume, "@home" for /home, etc.). Update where needed.]

7. **Asynchronous streaming expectations still unresolved.**  
   The review requested a discussion on whether module lifecycle methods need to become async to “constantly stream” progress/logs. data-model.md / `remote` sections only mark streaming as a “future enhancement” without weighing trade-offs or choosing a direction.  
   → Capture the design decision (e.g., stick to sync methods with injected callbacks vs. make them async) and describe how continuous progress/log streaming will work in v1.

   [I think the decision was to keep sync methods with callbacks and threading, because good enough for now and simpler than async methods. Document this decision.]

8. **“SSH as only network protocol” wording is still ambiguous.**  
   plan.md → Deliberate Simplicity section still says “SSH as only network protocol (ADR-002)” even though JF asked to clarify it’s “for orchestration” so readers don’t assume sync payloads must also use SSH.  
   → Update that sentence (and any similar mentions) to “SSH as the only orchestration/control channel” to avoid misinterpretation.

9. **Self-install logging requirements aren’t covered.**  
   User Story 2, Scenario 4 demands CRITICAL logging plus remediation hints when installation fails on the target. Neither plan.md nor the contracts explain what gets logged or how remediation guidance is surfaced.  
   → Spell out those log messages and the user feedback path so the implementation team knows what “suggest remediation steps” means.

   [Remove the "remediation hints" from the spec. Not needed. Just log the critical error and abort. Then we are ok.]

## suggested next steps

- Update the plan, data model, and contracts to restore dependency metadata, rollback prompting, and consistent configuration modeling. [Not the dependency metadata part.]  
- Fix the quickstart and research docs where JF’s earlier feedback hasn’t been actioned (uv install command, installer artifacts, version-management analysis, async streaming decision, SSH wording).  
- Extend the orchestrator protocol with the missing UX/error-handling flows (rollback prompts, installer failure remediation, disk-threshold enforcement results) so implementation tasks have clear acceptance criteria.
