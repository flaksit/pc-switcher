# ADR-020: Declarative package convergence: manifest capture, item diff, replay via each ecosystem's own tooling

Status: Accepted

Date: 2026-07-23

## TL;DR

The source captures a manifest of package-related items (apt/snap/flatpak packages, repos, keys, pins, config, remotes); the target diffs its own state against it and converges using `apt`, `snap` and `flatpak` themselves — package databases are never rsynced. Four independent jobs (`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_installs_sync`) each run plan then review then apply inside their own `execute()`, and each presents its own batched review — per manager, grouped by action — before that job issues its first mutating command.

## Implementation Rules

**Required:**
- `/var/lib/dpkg`, `/var/lib/snapd` and the flatpak OSTree store MUST NOT be rsynced or otherwise file-mirrored; convergence happens only through `apt`, `snap` and `flatpak` invocations.
- Every handled thing (package, source, key, pin, config file, snap, snap channel, flatpak ref, flatpak remote, unreproducible install) MUST be modeled as an `Item` with a stable identity flowing through one diff → decide → apply pipeline.
- `apt_sync`, `snap_sync`, `flatpak_sync` and `manual_installs_sync` MUST be four separate `SyncJob`s, each with its own enable flag, validation, progress reporting and `JobResult` — never merged into one `package_sync` job. `manual_installs_sync` MUST carry its own `sync_jobs` enable flag so disabling apt cannot silently disable manual-install detection.
- Each package job MUST complete its own batched review before issuing its own first mutating command; the plan → review → apply sequence lives inside that job's own `execute()` and is never handed to an outside owner.
- Files bound for `/etc/apt` MUST be staged under the target user's own `~/.cache/pc-switcher/` and promoted with `sudo install` setting root ownership and mode explicitly, after a `sudo mkdir -p` creates the destination directory; `send_file` MUST NOT be given a destination outside the target user's home.
- The repository/key/pin/config convergence group MUST back up every target file it is about to change, write the new files, run `apt-get update`, and restore the backups if that update fails, before reporting the group as failed; a backup failure MUST record a per-item failure for the whole group rather than leaving the outcome map unpopulated.
- Session status and the CLI exit code MUST derive from `job_results`, never from whether an exception propagated: a run whose items all failed MUST NOT exit 0.
- `validate()` MUST check passwordless sudo on the source as well as the target, because capturing `/etc/apt` state runs `sudo find` on the source; without it the capture degrades to empty digest maps and reports success having replicated nothing. Every environmental assumption is checked in `validate()` with copy-paste remediation, never discovered mid-execute.
- Every approved apt change MUST be simulated with `apt-get -s` at plan time, and the simulation MUST fail closed — a failed `apt-get -s` is never read as a clean preview. Collateral that is auto-installed (absent from the target's `apt-mark showmanual` set) proceeds; collateral that is manually installed becomes its own reviewable item offering install-anyway / skip / abort. The classification is decided in the review, never mid-apply.
- Machine-local decision files MUST live at `~/.config/pc-switcher/<manager>.decisions.yaml`, one per manager, excluded from `folder_sync` non-overridably and outside `config_sync`.
- The install-snippet registry (`~/.config/pc-switcher/package-snippets.yaml`) MUST be pushed to the target by `manual_installs_sync` itself with `send_file()` immediately after its own review, so a snippet authored on the fly during that review reaches the target in the same run; `config_sync` carries `config.yaml` only and MUST NOT carry the registry.
- Package-sync helpers MUST live in a `jobs/packages/` package as `items.py`, `review.py`, `state.py` and `sync_core.py`; job modules plus `base.py` and `context.py` stay directly in `jobs/`, because job discovery resolves a `sync_jobs` key to `jobs/<name>.py`.
- A job MUST get a configuration section only when it has a real key; `default-config.yaml` and `config-schema.yaml` MUST NOT carry an empty placeholder section.
- What a job does MUST be documented in that job's own document; the configuration reference is restricted to configuration.

**Forbidden:**
- No `--delete` file mirror of `/etc/apt` or any other package-database directory.
- No component outside a package job may own that job's review; the review call stays inside the job's own `execute()`.
- No `snap refresh --hold` (or equivalent) as the mechanism for revision convergence — it blocks auto-refresh.
- No re-fetching signing keys from vendors; a repo's key travels bundled with its repo item, byte-for-byte.

## Context

Phase 2 must replicate presence, version and provenance of packages across apt, snap and flatpak, plus the repository/keyring/pin/remote state those installs depend on. Package data under `~/.var/app`, `~/snap/<app>/common` and dotfiles is already Phase 1 `folder_sync` territory; this ADR concerns the packages themselves and the `/etc/apt` state that governs where they come from.

The review boundary is one batched review per manager: each package job captures its own diff and surfaces it for an explicit decision before it applies anything, and no review spans more than a single manager's items. This keeps four deliberately independent jobs independent — each owns its own capture, review, apply, failure isolation and progress — rather than binding them to a shared review that would give them a common ordering and failure surface.

## Decision

### Convergence model (D-01)

The source captures a manifest; the target diffs its own state against it and converges using `apt`, `snap` and `flatpak` themselves. `/var/lib/dpkg`, `/var/lib/snapd` and the flatpak OSTree store are never rsynced — the package managers stay authoritative for their own state.

### Item model (D-02)

Every handled thing is an item with a stable identity, not just packages. Item classes: apt package, apt source, apt key, apt pin, apt config file, snap, snap channel, flatpak ref, flatpak remote, unreproducible/manual install. All classes flow through one diff → decide → apply pipeline.

### Manifest content for apt (D-03)

The manifest carries the manually-installed set from `apt-mark showmanual`, not the full dpkg selection set; apt resolves dependencies on the target.

### Version policy (D-04, D-05)

Versions float to whatever the target's repos currently offer; version mismatch is a reported diff class, never a forced downgrade. Debian version ordering is decided by `dpkg --compare-versions`, never string comparison. Deliberate pinning replicates because `/etc/apt/preferences.d` entries are items.

### Revision/scope convergence (D-06)

The snap manifest carries name + channel + revision; the flatpak manifest carries ref + origin + user/system scope. Convergence must never leave a snap held or otherwise blocked from auto-refresh: the mechanism is `snap install --revision=N` / `snap refresh --revision=N`, and `snap list --all` is parsed by header column name rather than fixed offsets. `snap refresh --hold` is rejected as it blocks auto-refresh.

### Three-way decision and direction (D-07)

Every item class in every job gets a three-way decision: apply / skip once / skip always. "Apply" is direction-dependent — missing on target means install/add/enable, extra on target means remove/delete/disable, different on both means change the target to match the source. The review names the concrete action per item (e.g. "remove brscan3", not "apply").

### Machine-local decision file (D-08, D-08a, D-09, D-10)

One file per manager lives at `~/.config/pc-switcher/<manager>.decisions.yaml`, never synced, excluded from `folder_sync` non-overridably and outside `config_sync`. An entry on machine M makes the item inert on M in both roles — not pushed when M is the source, not installed or removed when M is the target. The entry is written on the end of the connection that holds the item: source-held item declined is recorded on the source, target-held item whose removal is declined is recorded on the target. Defaults ship as an example file only, consistent with the Phase 1 "defaults live in YAML" convention.

### Repository state as items (D-11, D-12, D-13)

apt sources, keys, pins, apt config, flatpak remotes and snap channels are inventory items, not a file mirror; a `--delete` mirror of `/etc/apt` would wipe the target's own machine-specific sources. A repo's signing key travels bundled with its repo item, byte-for-byte; keys are never re-fetched from vendors. Legacy `/etc/apt/trusted.gpg.d` keys, which no repo references explicitly, form a separate global-trust item set. Both `/etc/apt/preferences.d` and `/etc/apt/apt.conf.d` sync as items.

### Job split into four jobs (D-15, D-16, D-17, D-18)

Four jobs — `apt_sync`, `snap_sync`, `flatpak_sync` and `manual_installs_sync` — over one shared core (item model, diff, three-way decision flow, batched TUI review, machine-local file I/O, snippet registry) extracted while building, not deferred to a post-hoc refactor. Package jobs run before `folder_sync` so apps are provisioned before their data lands (decisive for flatpak, where `~/.local/share/flatpak` must exist before `~/.var/app` arrives).

`manual_installs_sync` owns everything no package manager can reproduce: the apt-no-candidate set and the scan for unowned installs under `/usr/local` and `/opt`, plus the snippet registry. It runs its own `dpkg-query` and `apt-cache policy` queries rather than sharing `apt_sync`'s, so ownership stays clean, and it carries its own `sync_jobs` enable flag — disabling apt must not silently disable manual-install detection.

### Per-manager batched review (D-15 + D-24)

Each package job runs plan then review then apply inside its own `execute()`: it captures the source manifest, queries the target, diffs, builds its review groups, presents its own batched review — grouped by action, one batch per manager — and only then converges the approved diffs. The batching is per manager and never across managers. There is no shared review phase and no coordinator: the jobs are independent by D-15, so a single owner reviewing every enabled manager at once would contradict that independence, and the user's own framing was one batched review per manager rather than one prompt for the whole fleet of managers. Grouping by action matters because "apply" is direction-dependent (D-07): installs and removals show as separate groups, removals labelled as removals, so a bulk tick can never silently delete. This composes with the single persistent Live panel and its pause/resume-around-prompts behaviour established in Phase 1 (plans 01-17/01-18).

### Privileged writes to `/etc/apt` (implementation constraint on D-11/D-12)

The executor's `send_file` is plain SFTP as the ordinary SSH user and cannot write under `/etc`. Files bound for `/etc/apt` are staged under the target user's own `~/.cache/pc-switcher/` and promoted into place with `sudo install` setting root ownership and mode explicitly, after a `sudo mkdir -p` creates the destination directory (`/etc/apt/keyrings` does not exist on a fresh Ubuntu 24.04 install, and `install` fails when it is absent). `send_file` is never given a destination outside the target user's home.

### Transactional repository convergence (D-27 boundary)

The key/source/pin/config group backs up every target file it is about to change, writes, runs `apt-get update`, and restores the backups if that update fails — including deleting files that did not previously exist — before reporting the group as failed. A backup failure records a per-item failure for the whole group rather than leaving the outcome map unpopulated. D-27's continue-and-report model would otherwise leave broken files in `/etc/apt`, and automatic snapshot rollback does not arrive until Phase 7, so a failed metadata refresh must not be allowed to leave the target's package manager unusable.

### apt transaction fidelity (D-30)

apt may remove or downgrade packages other than the one named in order to satisfy dependencies, so the item the user ticked is not necessarily the transaction apt will run. `apt-get -s` simulation runs at plan time and fails closed — a failed simulation is never read as a clean preview. Its collateral effects are classified before anything is refused. Collateral that is auto-installed — a dependency apt pulled in, absent from the target's `apt-mark showmanual` set — is apt doing its job and proceeds without asking. Collateral that is manually installed is something the user chose to have, so it becomes its own reviewable item offering install-anyway / skip / abort. Blanket refusal is wrong: it blocks a legitimate install whose only collateral is a dependency nobody chose. The question belongs in the review, never mid-apply — a prompt during apply reintroduces the prompt-flooding the batched review exists to prevent, and violates review-before-any-change.

### Unreproducible items and where a run terminates (D-21 with D-26 and D-27)

An unreproducible item ends a run resolved in one of three ways: it has a snippet, it is recorded machine-specific (skip-always), or the user chose to skip it once. Skip-once is a real decision, not an unresolved state — the user may be declining something temporary, and a run where they made that choice is clean. Only an item nobody decided on — a non-interactive run, where nothing is recorded (D-26) — leaves the run visibly unclean. pc-switcher offers to add a snippet on the fly during the review, so resolving an item never requires leaving the sync. A non-interactive run follows D-26 (nothing applied, nothing recorded, everything reported) and does not fail on undecided items alone, because the user was never given the chance to resolve them.

### Unreproducible items and snippets (D-18 through D-23)

Detection covers no-repo-candidate apt packages and unowned installs under `/usr/local` and `/opt`, and is owned by `manual_installs_sync`. An install snippet is an opaque text blob replayed non-interactively through the existing executor with the exit code deciding success — the tool never parses, versions, diffs or reasons about snippet content. Snippets live in the shared, synced config (`~/.config/pc-switcher/package-snippets.yaml`) and cover bare `.deb`s and manual installs only; snap and flatpak items do not carry snippets (YAGNI — every current one comes from a reachable remote). `manual_installs_sync` pushes the registry to the target itself with `send_file()` immediately after its own review, so a snippet authored on the fly during that review is included. It does not travel via `config_sync`, which runs before any review and so cannot carry a snippet the user has not authored yet; and it does not rely on `folder_sync`, a user-controlled job that can be disabled or filtered — no job's correctness may depend on another job running.

### Review, failure and dry-run (D-24 through D-28)

Each job's batched review, grouped by action, precedes any change that job makes. Conflicts and version mismatches are diff classes inside that review, not a second reporting mechanism. Non-interactive runs skip all once and record nothing. A failing item does not stop the job — continue, collect, report, and the job result is a failure. The target always downloads from its own repos; no source-cache reuse.

### folder_sync overlap (D-29)

Package jobs export their owned paths to `folder_sync` via the ADR-018 mechanism: `flatpak_sync` owns `~/.local/share/flatpak`, `snap_sync` owns the `~/snap/<app>/<rev>` revision dirs. `folder_sync` translates the supplied absolute paths into non-overridable filters without knowing anything about either ecosystem.

## Consequences

**Positive:**
- Package managers stay authoritative for their own dependency resolution and state, avoiding the correctness problems of file-level package database replication.
- Keeping each job's review inside its own `execute()` keeps four independent jobs independent — separate enable flags, config, validation, failure isolation and progress — with no shared ordering surface that could couple one job's failure to another's.
- The apt simulate-before-execute step catches collateral dependency changes at plan time and classifies auto-installed from manually-installed collateral, so a legitimate install is not blocked by a dependency nobody chose.

**Negative (costly to reverse):**
- The manifest schema, the item identity scheme and the decision-file format are all shaped by D-01; switching to file-level replication later would replace the whole job core.
- The decision files' location under `~/.config/pc-switcher/` means moving them later requires migrating user state on every machine.
- A fifth package-family job added later must adopt the same self-contained plan → review → apply shape and the shared `jobs/packages/` core to stay consistent.

## Alternatives Considered

- **File-level replication of the package databases** (`/var/lib/dpkg`, `/var/lib/snapd`, the flatpak OSTree store) — rejected: the package managers must stay authoritative for their own state, and file-level replication would fight their own consistency mechanisms.
- **A single combined `package_sync` job** — rejected per D-15: four separate jobs give independent enable flags, independent config, independent failure isolation and independent progress reporting, at the cost of one shared core module.
- **A cross-manager review coordinator** that runs every enabled package job's plan first and presents one review spanning all managers — rejected: it makes four deliberately independent jobs share a single ordering and failure surface for no user-visible gain, and contradicts the per-manager batched review the user asked for (D-24). Each job owning its own review between its plan and its apply achieves review-before-any-change without that coupling.
- **A `--delete` file mirror of `/etc/apt`** — rejected per D-11: it would wipe the target's own machine-specific sources, which contradicts the machine-local decision model (D-07/D-08).
- **Source-cache reuse for offline installs** — deferred per D-28; revisit if target-side downloads prove slow or unreliable.

## References

- ADR-002: SSH as communication channel — package-manager invocations run through the same executor protocol.
- ADR-005: Asyncio concurrency — all package-manager invocations are async subprocesses.
- ADR-010: Logging infrastructure — per-item detail at FULL, per-job summaries at INFO.
- ADR-014: Unified dry-run contract — each job's batched review doubles as its dry-run output.
- ADR-015: Topology-based sync-safety model — the warn-and-confirm precedent D-25/D-26 follow; this ADR's review is never a hard abort.
- ADR-018: Selective VS Code state sync — the path-export mechanism D-29 reuses for `flatpak_sync` and `snap_sync`.
- `.planning/phases/02-package-management-sync/02-CONTEXT.md`: D-01 through D-33, the source of every position recorded here.
- GitHub issue #118: the feature issue, including the snap-revision discussion motivating D-06.
