# ADR-020: Declarative package convergence: manifest capture, item diff, replay via each ecosystem's own tooling

Status: Superseded by ADR-021

Date: 2026-07-23

Superseded by: [ADR-021](adr-021-origin-replicating-package-convergence.md) — an apt package replicates as (name, origin), not name, so repositories, keys and pins became mechanism derived from the packages approved from them instead of reviewed inventory items.

## TL;DR

The source captures a manifest of package-related items (apt/snap/flatpak packages, repos, pins, config, remotes, blocks); the target diffs its own state against it and converges using `apt`, `snap` and `flatpak` themselves — package databases are never rsynced. Four independent jobs (`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_installs_sync`) each run plan then review then apply inside their own `execute()`, and each reviews its own items — batched per manager and grouped by action — before that job issues its first mutating command.

## Implementation Rules

**Required:**
- `/var/lib/dpkg`, `/var/lib/snapd` and the flatpak OSTree store MUST NOT be rsynced or otherwise file-mirrored; convergence happens only through `apt`, `snap` and `flatpak` invocations.
- Every handled thing (package, source, pin, config file, block, snap, snap channel, flatpak ref, flatpak remote, unreproducible install) MUST be modeled as an `Item` with a stable identity flowing through one diff → decide → apply pipeline.
- `apt_sync`, `snap_sync`, `flatpak_sync` and `manual_installs_sync` MUST be four separate `SyncJob`s, each with its own enable flag, validation, progress reporting and `JobResult` — never merged into one `package_sync` job. `manual_installs_sync` MUST carry its own `sync_jobs` enable flag so disabling apt cannot silently disable manual-install detection.
- Each package job MUST review an item before issuing any command that changes it; the plan → review → apply sequence lives inside that job's own `execute()`. Reviews SHOULD be batched — one screen per manager per action — and a job MUST NOT prompt per item where a batch would do. A job MAY review again when this run's own changes invalidate the facts an earlier review was answered on; correctness outranks the batching preference.
- The `/etc/apt` convergence group MUST be transactional: a group whose metadata refresh fails MUST leave the target's `/etc/apt` as it found it.
- A package manager's own transaction MUST be constrained to what the review approved: what it would change beyond the approved item is determined at plan time and classified there, never prompted mid-apply.
- Machine-local decision files MUST live at `~/.config/pc-switcher/<manager>.decisions.yaml`, one per manager, excluded from `folder_sync` non-overridably and outside `config_sync`.
- Whether an unreproducible item counts as reproducible MUST be judged by whether the SOURCE holds a snippet, never by whether the target already does. The install-snippet registry (`~/.config/pc-switcher/package-snippets.yaml`) MUST be pushed to the target by `manual_installs_sync` itself and replayed there the same run — including a snippet authored on the fly during that run's review. A push that would lose or change an entry only the target holds MUST require explicit confirmation and abort the run when it cannot get it. `config_sync` MUST NOT carry the registry.

**Forbidden:**
- No `--delete` file mirror of `/etc/apt` or any other package-database directory.
- No component outside a package job may own that job's review; the review call stays inside the job's own `execute()`.
- No standing block on a package manager's own auto-update left behind by a run. A transient guard across the sync window is allowed if it is restored on cleanup and self-expires (D-06).
- No re-fetching signing keys from vendors; a repo's key travels with its repo, byte-for-byte.

## Context

Phase 2 must replicate presence, version and provenance of packages across apt, snap and flatpak, plus the repository/keyring/pin/remote configuration those installs depend on. Package data under `~/.var/app`, `~/snap/<app>/common` and dotfiles is already Phase 1 `folder_sync` territory; this ADR concerns the packages themselves and the `/etc/apt` config that governs where they come from.

The review boundary is the manager: each package job captures its own diff and surfaces it for an explicit decision before it applies anything, and no review spans more than a single manager's items. This keeps four deliberately independent jobs independent — each owns its own capture, review, apply, failure isolation and progress — rather than binding them to a shared review that would give them a common ordering and failure surface.

## Decision

### Convergence model (D-01)

The source captures a manifest; the target diffs its own state against it and converges using `apt`, `snap` and `flatpak` themselves. `/var/lib/dpkg`, `/var/lib/snapd` and the flatpak OSTree store are never rsynced — the package managers stay authoritative for their own state.

### Item model (D-02)

Every handled thing is an item with a stable identity, not just packages: apt package, apt source, apt pin, apt config file, apt hold, snap, snap channel, snap hold, flatpak ref, flatpak remote, flatpak mask, unreproducible/manual install. All classes flow through one diff → decide → apply pipeline.

Item granularity follows what the user can meaningfully decide about. Standing user intent gets its own identity even when it is attached to something else: a deliberate block on a package (apt hold, snap hold, flatpak mask) is a decision in its own right and replicates as its own item, separate from the package it applies to. Mechanism the user has no basis to judge is not an item and is the job's own business to keep correct. A transient guard a run sets and clears is not an item either.

### Manifest content for apt (D-03)

The manifest carries the manually-installed set from `apt-mark showmanual`, not the full dpkg selection set; apt resolves dependencies on the target.

### Version policy (D-04, D-05)

Versions float to whatever the target's repos currently offer; version mismatch is a reported diff class, never a forced downgrade. This float-only policy is apt and flatpak. Snap is the deliberate exception: it converges revision and channel (D-06), because snap is the only ecosystem that embeds the version in its per-user data path (`~/snap/<app>/<rev>`), so keeping both machines on the same revision is what lets `folder_sync` mirror that data cleanly. Deliberate pinning replicates because `/etc/apt/preferences.d` entries are items.

### Revision/scope convergence (D-06)

The snap manifest carries name + channel + revision; the flatpak manifest carries ref + origin + user/system scope. Snap convergence pins a revision explicitly and must never leave a snap blocked from auto-refresh once a run ends.

snapd auto-refreshes in the background (~4×/day, even for closed apps), which could move a snap off the converged revision mid-run and desync the data dir `folder_sync` is mirroring. The orchestrator therefore pauses snapd's automatic refresh across the whole sync window on BOTH hosts, restoring each host's prior setting on cleanup and relying on a timeout so a crashed run does not leave the pause behind. This gates only the auto-refresh manager, not the explicit revision convergence.

### Three-way decision and direction (D-07)

Every actionable item gets a three-way decision: apply / skip once / skip always. "Apply" is direction-dependent — missing on target means install/add/enable, extra on target means remove/delete/disable, different on both means change the target to match the source. The review names the concrete action per item (e.g. "remove brscan3", not "apply").

Report-only diffs (version mismatch, no repository candidate, held/pinned echo) offer apply or skip only. They carry no converge verb, so there is no holder machine for D-08a to record a permanent decision on, and recording one would suppress the item entirely rather than stop reporting the drift the user meant. They are resolved by fixing the underlying condition.

### Machine-local decision file (D-08, D-08a, D-09, D-10)

One file per manager lives at `~/.config/pc-switcher/<manager>.decisions.yaml`, never synced, excluded from `folder_sync` non-overridably and outside `config_sync`. An entry on machine M makes the item inert on M in both roles — not pushed when M is the source, not installed or removed when M is the target. The entry is written on the end of the connection that holds the item: source-held item declined is recorded on the source, target-held item whose removal is declined is recorded on the target.

### Repository configuration as items (D-11, D-12, D-13)

apt sources, pins, apt config, flatpak remotes and snap channels are inventory items, not a file mirror; a `--delete` mirror of `/etc/apt` would wipe the target's own machine-specific sources. Both `/etc/apt/preferences.d` and `/etc/apt/apt.conf.d` sync as items. A flatpak remote's identity includes its URL as well as its name and scope, so a remote that moved converges rather than diverging silently.

A repository's signing key travels with the repository, byte-for-byte from the source machine, and is never re-fetched from a vendor: the target must end up trusting exactly what the source trusts, and a fresh fetch would silently substitute whatever the vendor serves today. A repository whose key cannot be made to work on the target is reported rather than written.

### Job split into four jobs (D-15, D-16, D-17, D-18)

Four jobs — `apt_sync`, `snap_sync`, `flatpak_sync` and `manual_installs_sync` — over one shared core extracted while building, not deferred to a post-hoc refactor. The core is what all four use: the item taxonomy, the plan/review/apply order, the three-way decision flow, the batched TUI review and the machine-local file I/O. Each manager's own item shapes and its own diff stay in that manager's module — one manager's diff on the shared base would make the other three inherit inputs they never supply, which is the coupling D-15 exists to prevent. Package jobs run before `folder_sync` so apps are provisioned before their data lands (decisive for flatpak, where `~/.local/share/flatpak` must exist before `~/.var/app` arrives).

`manual_installs_sync` owns everything no package manager can reproduce: the apt packages installed from no configured repository and the scan for unowned installs under `/usr/local` and `/opt`, plus the snippet registry.

### Per-manager review, batched by preference (D-15 + D-24)

Each package job runs plan then review then apply inside its own `execute()`, and only converges diffs that review approved. Grouping by action matters because "apply" is direction-dependent (D-07): installs and removals show as separate groups, removals labelled as removals, so a bulk tick can never silently delete.

Three properties are absolute. Nothing changes before it has been reviewed. No review spans two managers — the jobs are independent by D-15, so a single owner reviewing every enabled manager at once would contradict that independence, which is why there is no shared review phase and no coordinator. And a job never degrades a batch into a per-item question queue.

Batching itself is a strong preference, not a fixed count: one screen per manager per action is the norm and the shape to design for, but a job MAY review again when correctness requires it — `apt_sync` converges `/etc/apt` before packages, so a pin the user deleted or a repository they installed changes what the package diff should have said. Asking twice is worse than asking once; asking once and being wrong is worse than both. Approvals the new state contradicts are withdrawn without asking, since dropping work needs no decision.

### Transactional repository convergence (D-27 boundary)

The `/etc/apt` group is the one place D-27's continue-and-report model does not apply: it is transactional, and a group whose metadata refresh fails leaves `/etc/apt` as it found it. Continuing past a bad write would leave the target's package manager unusable, and automatic snapshot rollback does not arrive until Phase 7.

### apt transaction fidelity (D-30)

apt may remove or downgrade packages other than the one named in order to satisfy dependencies, so the item the user ticked is not necessarily the transaction apt will run. The transaction is therefore determined at plan time and its collateral classified rather than blanket-refused, which would block a legitimate install whose only collateral is a dependency nobody chose: collateral apt pulled in on its own proceeds, while collateral that is manually installed on EITHER machine is something the user chose to have and becomes its own reviewable item offering install-anyway / skip / abort. Checking the source's manual set as well as the target's covers the package the user chose on the source but that arrives as collateral on the target. The question belongs in the review, never mid-apply — a prompt during apply reintroduces the prompt-flooding the batched review exists to prevent, and violates review-before-any-change.

### Unreproducible items and where a run terminates (D-21 with D-26 and D-27)

An unreproducible item ends a run resolved in one of three ways: it has a snippet, it is recorded machine-specific (skip-always), or the user chose to skip it once. Skip-once is a real decision, not an unresolved state — the user may be declining something temporary, and a run where they made that choice is clean. pc-switcher offers to add a snippet on the fly during the review, so resolving an item never requires leaving the sync. In an interactive review the "unresolved" outcome is unrepresentable, and abandoning the review aborts the whole sync rather than manufacturing a skip-once. Only a non-interactive run leaves items undecided; it does not fail on undecided items alone, because the user was never given the chance to resolve them (D-26).

### Unreproducible items and snippets (D-18 through D-23)

Detection covers apt packages whose installed version comes from no configured repository — a bare `.deb`, whose only origin is dpkg's own status file — and unowned installs under `/usr/local` and `/opt`, and is owned by `manual_installs_sync`. An install snippet is an opaque text blob replayed non-interactively through the existing executor with the exit code deciding success — the tool never parses, versions, diffs or reasons about snippet content. Snippets live in the shared, synced config (`~/.config/pc-switcher/package-snippets.yaml`) and cover bare `.deb`s and manual installs only; snap and flatpak items do not carry snippets (YAGNI — every current one comes from a reachable remote).

Whether an item counts as reproducible is decided by whether the SOURCE — the machine being replicated — holds a snippet, never by whether the target already does. `manual_installs_sync` pushes the registry to the target itself and replays it the same run, so a snippet authored on the fly during that run's review takes effect immediately rather than next run. The push is a wholesale overwrite gated on being non-destructive: one that would lose or change an entry only the target holds needs explicit confirmation, so a snippet the user only has on the target is never silently discarded. The registry does not travel via `config_sync`, which runs before any review and so cannot carry a snippet the user has not authored yet; and it does not rely on `folder_sync`, a user-controlled job that can be disabled or filtered — no job's correctness may depend on another job running.

### Review, failure and dry-run (D-24 through D-28)

Each job's batched review, grouped by action, precedes any change that job makes. Conflicts and version mismatches are diff classes inside that review, not a second reporting mechanism. Non-interactive runs skip all once and record nothing. A failing item does not stop the job — continue, collect, report, and the job result is a failure. The target always downloads from its own repos; no source-cache reuse.

### folder_sync overlap (D-29)

Package jobs export their owned paths to `folder_sync` via the ADR-018 mechanism, which turns them into non-overridable filters without knowing anything about either ecosystem: `flatpak_sync` owns `~/.local/share/flatpak`, and `snap_sync` owns the retained OLDER `~/snap/<app>/<rev>` revision dirs only. The CURRENT revision's dir is deliberately NOT excluded — `folder_sync` mirrors it, so the active revision's per-user app data travels, which is the whole point of converging the revision (D-06). Older revision dirs are excluded to avoid planting data for revisions the target's snapd never installed, and when the active revision cannot be determined all of that app's revision dirs are excluded as the safe default.

## Consequences

**Positive:**
- Package managers stay authoritative for their own dependency resolution and state, avoiding the correctness problems of file-level package database replication.
- Keeping each job's review inside its own `execute()` keeps four independent jobs independent — separate enable flags, config, validation, failure isolation and progress — with no shared ordering surface that could couple one job's failure to another's.
- Determining the real transaction at plan time catches collateral dependency changes before anything is applied, so a legitimate install is not blocked by a dependency nobody chose.

**Negative (costly to reverse):**
- The manifest schema, the item identity scheme and the decision-file format are all shaped by D-01; switching to file-level replication later would replace the whole job core.
- The decision files' location under `~/.config/pc-switcher/` means moving them later requires migrating user state on every machine.
- Package sync requires passwordless sudo on BOTH machines: the source's `/etc/apt` state and snap configuration are root-only reads.

## Alternatives Considered

- **File-level replication of the package databases** (`/var/lib/dpkg`, `/var/lib/snapd`, the flatpak OSTree store) — rejected: the package managers must stay authoritative for their own state, and file-level replication would fight their own consistency mechanisms.
- **A single combined `package_sync` job** — rejected per D-15: four separate jobs give independent enable flags, independent config, independent failure isolation and independent progress reporting, at the cost of one shared core module holding only what all four use.
- **A `--delete` file mirror of `/etc/apt`** — rejected per D-11: it would wipe the target's own machine-specific sources, which contradicts the machine-local decision model (D-07/D-08).
- **Source-cache reuse for offline installs** — deferred per D-28; revisit if target-side downloads prove slow or unreliable.

## References

- ADR-002: SSH as communication channel — package-manager invocations run through the same executor protocol.
- ADR-005: Asyncio concurrency — all package-manager invocations are async subprocesses.
- ADR-010: Logging infrastructure — per-item detail at FULL, per-job summaries at INFO.
- ADR-014: Unified dry-run contract — each job's batched review doubles as its dry-run output.
- ADR-015: Topology-based sync-safety model — the warn-and-confirm precedent D-25/D-26 follow; this ADR's review is never a hard abort.
- ADR-018: Selective VS Code state sync — the path-export mechanism D-29 reuses for `flatpak_sync` and `snap_sync`.
- `docs/system/package-sync.md`: the resulting spec — shared job contract, per-job scope, item classes and preconditions.
- `.planning/phases/02-package-management-sync/02-CONTEXT.md`: D-01 through D-33, the source of every position recorded here.
- GitHub issue #118: the feature issue, including the snap-revision discussion motivating D-06.
