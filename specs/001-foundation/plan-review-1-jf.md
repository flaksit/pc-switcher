I'm reviewing the generated deliverables. Incorporate the following feedback:

- Python might or might not be installed on the systems (source and/or target). Anyway, I don't want to use the system Python in any way! The installer should install uv if not present yet and use/install python with uv. We want uv v0.9.9 (the needed uv version should be defined somewhere in a single place in this project and this should be documented for the developer). Using "latest" is not allowed (e.g. in github actions or in installer or in developer instructions). The quick start should not have "Python 3.13 installed" as prerequisite. Python should be installed by uv and a virtualenv (managed by uv) should be used for pc-switcher project development.
- uv project initialization: we are already inside the pc-switcher folder, so I think we need to do "uv init --lib ."
- You're not following current standards for structuring pyproject.toml. E.g. "[tool.uv] dev-dependencies". Do your research.
- This project lives in a github organization named flaksit. So the github package registry name should probably contain that organization name instead of "yourusername". Or if it is better to keep that undefined, it should at least read yourusername_or_organization
- Btrfs snapshots should be stored in `/.snapshots` (make this configurable)
- In spec.md, I updated the btrfs_snaphots part in the example yaml config. Update everything accordingly.
- The btrfs check at the start of the sync should be thorough:
  - / should be a btrfs filesystem
  - configured subvolumes should exist in the top-level: visible as output of "btrfs subvolume list /"
- The default yaml config file (generated upon install), should contain clear documentation of each option.
- "Single persistent SSH connection prevents repeated handshake overhead (ADR-002)" has nothing to do with "Minimize SSD Wear". "Minimize SSD Wear" is about avoiding wear on SSD disk drives.
- "SSH as only network protocol (ADR-002)": should be "SSH as only network protocol for orchestration (ADR-002)". To avoid confusion about the other sync modules, which might use entirely other protocols (e.g. Syncthing)
- In the Project Structure - Source Code, I see only an entry installer.py for "Target installation/upgrade logic". I miss the installer itself (should be used on the source system as well).
- Actually, in uv language, we should not be talking about the pc-switcher "package", but the pc-switcher "tool". Run like "uv tool run pc-switcher sync my_target_machine" or when the tool is installed just "pc-switcher sync my_target_machine". Is what I say clear and correct? If so, update all relevant references to "package". If not, tell me. In that light, validate if "--lib" is the correct flag for "uv init".
- Dependencies between sync modules: for the moment, modules run sequentially, in the order as specified in the yaml config. Document this. When checking that the btrfs snapshot module is not disabled, check as well that it is the first in the list. No need for `SyncModules.dependencies` for now.
- in config-schema.yaml, remove the "Future module configs" part. Too early, not in scope.
- ProgressUpdate: percentage should be optional (None if unknown). Should be a float between 0.0 and 1.0 (not int). Document that this should be the percentage of ALL the work of the module, not only the current subtask or item.
- Separation / roles between SyncSession and Orchestrator is not clear. Document this.
- Module discovery is not necessary. The modules to run are defined in the config file, in the desired order.
- Abort Handling: cleanup() of completed modules should not be called. The symantics of cleanup() is to stop running processes, free resources, etc. Not to undo what was done. (We have the manual rollback for that.) So only call cleanup() on modules that are still running at the time of abort. Maybe we need a better name for cleanup(), e.g. "abort()"? Document this clearly.

Versions:
- Do version management with "uv version". Or do we rather want to use the github releases for version management? Discuss pros and cons and suggest the best approach.
- As long as we're working in a monorepo, no need for different versions for the SyncModules. So remove SyncModule.version.

SyncModule contract:
- Not clear how a SyncModule can talk to the target, execute something there, get results back. Document this in the SyncModule contract. Probably we need to add a "target_connection" argument to the constructor or to the lifecycle methods.
- I think cleanup() should get a timeout argument so that it can limit the time spent in cleanup (e.g. giving this timeout to subprocess calls). Document this.
- We want to "constantly stream" the progress and logs. I'm not sure: for a fluent UX, do we need asynchronous methods in the SyncModule contract? Discuss pros and cons and suggest the best approach.
- Logging: better have a single way to do things. So only a single way to log messages. I like the pattern used for emit_progress(): this method will be injected by the orchestrator. The same way, the log() method can be injected by the orchestrator. Then we don't need the Logger argument in the SyncModule constructor anymore, nor the self.logger member.

SyncSession State transitions:
- Aborted: final state. When user requested abort (e.g. Ctrl-C).
- Failed: final state. When some module did output an ERROR log message along the way. Or when a CRITICAL log message was output. Or when a module raised an exception that was not caught. Or when unrecoverable error happened.
- Completed: final state. When all modules completed successfully.
- Cleanup: intermediate state: modules are cleaning up after either
user requested abort (e.g. Ctrl-C), or a CRITICAL log message was output, or a module raised an exception that was not caught or another unrecoverable error happened.
- From Cleanup state, we go to Aborted state when user requested abort (e.g. Ctrl-C).
- From Cleanup state, we go to Failed state when some module did output an ERROR log message along the way. Or when a CRITICAL log message was output. Or when a module raised an exception that was not caught. Or when unrecoverable error happened.
- We always pass through Cleanup before going to Aborted or Failed, except that we could go from Executing directly to Failed if all modules completed but at least one module output an ERROR log message along the way.

- Snapshot.location and Location are not clear names for what it is (source or target). Suggestion: host? Or should we be more specific and set the hostname (as known to the orchestrator) here instead of "source" or "target"? In that case, Snapshot.hostname would be better.
- LogEntry.hostname: same remark. If the choice is only "source" or "target", then LogEntry.host would be better. Or should we be more specific and set the hostname (as known to the orchestrator) here instead of "source" or "target"? In that case, Snapshot.hostname would be better.

TargetConneciton:
- run() returns a Result. Document what Result is.
- send_file() is possibly too simplistic. No need to set permissions, ownership? Ok for now. Document this as a possible later feature.

Orchestrator-Module Protocol:
- Make clear how we work with the CRITICAL log level. My suggestion:
  - The orchestrator does not need to watch the log stream for CRITICAL messages. Instead, a module should raise an exception when a critical situation is reached. The orchestrator will catch that exception, log the exception with CRITICAL level and start the cleanup phase immediately.
  - Update all documents (even the spec.md) in that sense.
  - Modules should not log CRITICAL messages anymore. Only raise exceptions. All info that the module would like to log about the critical situation should be included in the exception message.
- Locking file should be in $XDG_RUNTIME_DIR/pc-switcher/pc-switcher.lock by default. Or /var/lock/pc-switcher.lock if XDG_RUNTIME_DIR is not defined.
- If locking file is stale, warn the user and ask them to confirm they want to proceed (which will delete the stale lock file and create a new one). Because stale lock files can happen when the program crashed or the machine lost power during a sync and maybe the user is not aware of that.
- In SSH Connection Management, you say that the modules receive the connection via the orchestrator. However, then you say that it is the Orchestrator that provides the methods run_on_target() and send_file(). That is contradictory. In module_interface.py, I see nowhere that a SyncModule has an orchestrator or a connection object. So how can a module call run_on_target() or send_file()? Suggestion: add them as methods to the SyncModule contract, and the orchestrator will inject them? Or are there better patterns?
- Rename Orchestrator.send_file() to Orchestrator.send_file_to_target() for clarity.
- Just launching a process on the target is probably not enough. We also want to get its stdout and stderr output, its return code, possibly set a timeout, etc. So suggest to change Orchestrator.run_on_target() to have a signature similar to subprocess.run(). Ideally, we would even like to be able to pass a callable that will be called with each line of stdout/stderr as soon as it is available (for streaming output). Or we should be able to run a remote python SyncModule-like object (RemoteSyncTask?), so that progress and logging can be streamed back to the source machine. Discuss pros and cons and suggest the best approach.