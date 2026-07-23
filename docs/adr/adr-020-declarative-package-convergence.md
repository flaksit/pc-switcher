# ADR-020: Declarative package convergence: manifest capture, item diff, replay via each ecosystem's own tooling

Status: Accepted

Date: 2026-07-23

## TL;DR

The source captures a manifest of package-related items (apt/snap/flatpak packages, repos, keys, pins, config, remotes); the target diffs its own state against it and converges using `apt`, `snap` and `flatpak` themselves — package databases are never rsynced, and a `PackagePhaseCoordinator` runs one batched cross-manager review between each job's `plan()` and `apply()` so no job mutates the target before every job has diffed.

## Implementation Rules

**Required:**
- `/var/lib/dpkg`, `/var/lib/snapd` and the flatpak OSTree store MUST NOT be rsynced or otherwise file-mirrored; convergence happens only through `apt`, `snap` and `flatpak` invocations.
- Every handled thing (package, source, key, pin, config file, snap, snap channel, flatpak ref, flatpak remote, unreproducible install) MUST be modeled as an `Item` with a stable identity flowing through one diff → decide → apply pipeline.
- `apt_sync`, `snap_sync` and `flatpak_sync` MUST be three separate `SyncJob`s, each with its own config section, enable flag, validation, progress reporting and `JobResult` — never merged into one `package_sync` job.
- Each package job MUST split into `plan()` (capture manifest, query target, diff, build review groups) and `apply()` (converge approved diffs); `PackageSyncJob.execute()` MUST refuse to run without a coordinator-supplied accepted plan.
- A `PackagePhaseCoordinator` MUST run every enabled package job's `plan()` first, concatenate their review groups into one review grouped by manager and action, present it once, and hand each job back only its own slice of the outcome before any job's `apply()` runs.
- Files bound for `/etc/apt` MUST be staged under the target user's own `~/.cache/pc-switcher/` and promoted with `sudo install` setting root ownership and mode explicitly; `send_file` MUST NOT be given a destination outside the target user's home.
- The repository/key/pin/config convergence group MUST back up every target file it is about to change, write the new files, run `apt-get update`, and restore the backups if that update fails, before reporting the group as failed.
- Every approved apt install or removal MUST be simulated with `apt-get -s` immediately before the real command; an item whose simulation would remove or downgrade a package that was not itself an approved item in this run MUST be refused and reported as a per-item failure rather than executed.
- Machine-local decision files MUST live at `~/.config/pc-switcher/<manager>.decisions.yaml`, one per manager, excluded from `folder_sync` and outside `config_sync`.
- The install-snippet registry MUST live in the shared, synced config (`~/.config/pc-switcher/package-snippets.yaml`), never in a machine-local decision file.

**Forbidden:**
- No `--delete` file mirror of `/etc/apt` or any other package-database directory.
- No job may call its own review/converge sequence directly inside `execute()`; the coordinator's `plan()`/`apply()` split is the only path to mutating the target.
- No `snap refresh --hold` (or equivalent) as the mechanism for revision convergence — it blocks auto-refresh.
- No re-fetching signing keys from vendors; a repo's key travels bundled with its repo item, byte-for-byte.

## Context

Phase 2 must replicate presence, version and provenance of packages across apt, snap and flatpak, plus the repository/keyring/pin/remote state those installs depend on. Package data under `~/.var/app`, `~/snap/<app>/common` and dotfiles is already Phase 1 `folder_sync` territory; this ADR concerns the packages themselves and the `/etc/apt` state that governs where they come from.

A cross-AI review of the Phase 2 plan found nine verified defects, the most structural of which was that three independently-executing jobs (`apt_sync`, `snap_sync`, `flatpak_sync`) each running their own capture → diff → review → converge sequence inside `execute()` would let the orchestrator's sequential job loop complete `apt_sync` — mutating the target — before `snap_sync` had even diffed, defeating the "one batched review before any change" guarantee (D-24). This ADR records both the convergence model and the coordinator that fixes that defect.

## Decision

### Convergence model (D-01)

The source captures a manifest; the target diffs its own state against it and converges using `apt`, `snap` and `flatpak` themselves. `/var/lib/dpkg`, `/var/lib/snapd` and the flatpak OSTree store are never rsynced — the package managers stay authoritative for their own state.

### Item model (D-02)

Every handled thing is an item with a stable identity, not just packages. Item classes: apt package, apt source, apt key, apt pin, apt config file, snap, snap channel, flatpak ref, flatpak remote, unreproducible/manual install. All classes flow through one diff → decide → apply pipeline.

### Manifest content for apt (D-03)

The manifest carries the manually-installed set from `apt-mark showmanual`, not the full dpkg selection set; apt resolves dependencies on the target.

### Version policy (D-04, D-05)

Versions float to whatever the target's repos currently offer; version mismatch is a reported diff class, never a forced downgrade. Deliberate pinning replicates because `/etc/apt/preferences.d` entries are items.

### Revision/scope convergence (D-06)

The snap manifest carries name + channel + revision; the flatpak manifest carries ref + origin + user/system scope. Convergence must never leave a snap held or otherwise blocked from auto-refresh: the mechanism is `snap install --revision=N` / `snap refresh --revision=N`. `snap refresh --hold` is rejected as it blocks auto-refresh.

### Three-way decision and direction (D-07)

Every item class in every job gets a three-way decision: apply / skip once / skip always. "Apply" is direction-dependent — missing on target means install/add/enable, extra on target means remove/delete/disable, different on both means change the target to match the source. The review names the concrete action per item (e.g. "remove brscan3", not "apply").

### Machine-local decision file (D-08, D-08a, D-09, D-10)

One file per manager lives at `~/.config/pc-switcher/<manager>.decisions.yaml`, never synced, excluded from `folder_sync` and outside `config_sync`. An entry on machine M makes the item inert on M in both roles — not pushed when M is the source, not installed or removed when M is the target. The entry is written on the end of the connection that holds the item: source-held item declined is recorded on the source, target-held item whose removal is declined is recorded on the target. Defaults ship as an example file only (`src/pcswitcher/machine-packages.example.yaml`), consistent with the Phase 1 "defaults live in YAML" convention.

### Repository state as items (D-11, D-12, D-13)

apt sources, keys, pins, apt config, flatpak remotes and snap channels are inventory items, not a file mirror; a `--delete` mirror of `/etc/apt` would wipe the target's own machine-specific sources. A repo's signing key travels bundled with its repo item, byte-for-byte; keys are never re-fetched from vendors. Legacy `/etc/apt/trusted.gpg.d` keys, which no repo references explicitly, form a separate global-trust item set. Both `/etc/apt/preferences.d` and `/etc/apt/apt.conf.d` sync as items.

### Job split (D-15, D-16, D-17)

Three jobs — `apt_sync`, `snap_sync`, `flatpak_sync` — over one shared core (item model, diff, three-way decision flow, batched TUI review, machine-local file I/O, snippet registry) extracted while building, not deferred to a post-hoc refactor. Package jobs run before `folder_sync` so apps are provisioned before their data lands (decisive for flatpak, where `~/.local/share/flatpak` must exist before `~/.var/app` arrives).

### Two-phase convergence and the package-phase coordinator (D-15 + D-24, ADR-014)

This is the mechanism that makes "one batched review before any change" true across three independent jobs. Each package job splits into `plan()` — capture the source manifest, query the target, diff, build its own review groups — and `apply()` — converge the approved diffs. A `PackagePhaseCoordinator` runs every enabled package job's `plan()` first, concatenates their review groups into one review grouped by manager and action, presents it once, and hands each job back only its own slice of the outcome; the jobs then apply in order. The three jobs stay three separate `SyncJob`s with their own config sections, enable flags, validation, progress reporting and `JobResult`s (D-15) — the only thing that moves out of a job is the review call itself. `PackageSyncJob.execute()` refuses to run without a coordinator-supplied accepted plan, which makes the ordering a structural guarantee rather than a convention.

Rejected alternative: keeping capture → diff → review → converge inside each job's own `execute()`. The orchestrator's sequential job loop would then let `apt_sync` complete — mutating the target — before `snap_sync` had even diffed, which is exactly the defect the cross-AI review found.

### Privileged writes to `/etc/apt` (implementation constraint on D-11/D-12)

The executor's `send_file` is plain SFTP as the ordinary SSH user and cannot write under `/etc`. Files bound for `/etc/apt` are staged under the target user's own `~/.cache/pc-switcher/` and promoted into place with `sudo install` setting root ownership and mode explicitly. `send_file` is never given a destination outside the target user's home.

### Transactional repository convergence (D-27 boundary)

The key/source/pin/config group backs up every target file it is about to change, writes, runs `apt-get update`, and restores the backups if that update fails — before reporting the group as failed. D-27's continue-and-report model would otherwise leave broken files in `/etc/apt`, and automatic snapshot rollback does not arrive until Phase 7, so a failed metadata refresh must not be allowed to leave the target's package manager unusable.

### apt transaction fidelity (D-24, D-25)

apt may remove or downgrade packages other than the one named in order to satisfy dependencies, so the item the user ticked is not necessarily the transaction apt will run. Every approved apt install or removal is simulated with `apt-get -s` immediately before the real command, and an item whose simulation would remove or downgrade a package that was not itself an approved item in this run is refused and reported as a per-item failure rather than executed. The review additionally shows the aggregate collateral effects computed at plan time.

### Mandatory registration and where it terminates (D-21 with D-26 and D-27)

Unresolved unreproducible items are allowed to remain for a given run, but the package job's result is a failure while any remains after an interactive review — the sync is visibly not clean until every unreproducible item is snippet-backed or recorded machine-specific. A non-interactive run instead follows D-26 (nothing applied, nothing recorded, everything reported) and does not fail on unresolved items alone, because the user was never given the chance to resolve them. D-21 and D-26 read alone point in different directions; this reconciliation is the intended reading.

### Unreproducible items and snippets (D-18 through D-23)

Detection covers no-repo-candidate apt packages and unowned installs under `/usr/local` and `/opt`. An install snippet is an opaque text blob replayed non-interactively through the existing executor with the exit code deciding success — the tool never parses, versions, diffs or reasons about snippet content. Snippets live in the shared, synced config (`~/.config/pc-switcher/package-snippets.yaml`) and cover bare `.deb`s and manual installs only; snap and flatpak items do not carry snippets (YAGNI — every current one comes from a reachable remote).

### Review, failure and dry-run (D-24 through D-28)

One batched review, grouped by manager and action, precedes any change. Conflicts and version mismatches are diff classes inside that review, not a second reporting mechanism. Non-interactive runs skip all once and record nothing. A failing item does not stop the job — continue, collect, report, and the job result is a failure. The target always downloads from its own repos; no source-cache reuse.

### folder_sync overlap (D-29)

Package jobs export their owned paths to `folder_sync` via the ADR-018 mechanism: `flatpak_sync` owns `~/.local/share/flatpak`, `snap_sync` owns the `~/snap/<app>/<rev>` revision dirs. `folder_sync` translates the supplied absolute paths into non-overridable filters without knowing anything about either ecosystem.

## Consequences

**Positive:**
- Package managers stay authoritative for their own dependency resolution and state, avoiding the correctness problems of file-level package database replication.
- The `PackagePhaseCoordinator` makes "one batched review before any target mutation" a structural guarantee across three independently-executing jobs, not a convention that can silently regress.
- The apt simulate-before-execute step catches collateral dependency changes before they happen, not after.

**Negative (costly to reverse):**
- The manifest schema, the item identity scheme and the decision-file format are all shaped by D-01; switching to file-level replication later would replace the whole job core.
- The decision files' location under `~/.config/pc-switcher/` means moving them later requires migrating user state on every machine.
- The `plan()`/`apply()` split is now load-bearing for every package job; a fourth package-family job added later must adopt the same split to keep the coordinator's guarantee intact.

## Alternatives Considered

- **File-level replication of the package databases** (`/var/lib/dpkg`, `/var/lib/snapd`, the flatpak OSTree store) — rejected: the package managers must stay authoritative for their own state, and file-level replication would fight their own consistency mechanisms.
- **A single combined `package_sync` job** — rejected per D-15: three separate jobs give independent enable flags, independent config, independent failure isolation and independent progress reporting, at the cost of one shared core module.
- **Per-job self-contained review** (capture → diff → review → converge inside each job's own `execute()`) — rejected: the orchestrator's sequential job loop would let `apt_sync` complete — mutating the target — before `snap_sync` had even diffed, breaking the single-batched-review guarantee (D-24). The `PackagePhaseCoordinator`'s `plan()`/`apply()` split closes this.
- **A `--delete` file mirror of `/etc/apt`** — rejected per D-11: it would wipe the target's own machine-specific sources, which contradicts the machine-local decision model (D-07/D-08).
- **Source-cache reuse for offline installs** — deferred per D-28; revisit if target-side downloads prove slow or unreliable.

## References

- ADR-002: SSH as communication channel — package-manager invocations run through the same executor protocol.
- ADR-005: Asyncio concurrency — all package-manager invocations are async subprocesses.
- ADR-010: Logging infrastructure — per-item detail at FULL, per-job summaries at INFO.
- ADR-014: Unified dry-run contract — the batched review doubles as the dry-run output for the three package jobs.
- ADR-015: Topology-based sync-safety model — the warn-and-confirm precedent D-25/D-26 follow; this ADR's review is never a hard abort.
- ADR-018: Selective VS Code state sync — the path-export mechanism D-29 reuses for `flatpak_sync` and `snap_sync`.
- `.planning/phases/02-package-management-sync/02-CONTEXT.md`: D-01 through D-29, the source of every position recorded here.
- GitHub issue #118: the feature issue, including the snap-revision discussion motivating D-06.
