# Phase 2: Package Management Sync - Context

**Gathered:** 2026-07-22

**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 2 replicates *what is installed* from source to target across apt, snap and flatpak, plus the repository/keyring/remote state those installs depend on, with every difference surfaced for an explicit decision before any change is applied. Package *data* (`~/.var/app`, `~/snap/<app>/common`, dotfiles) is already covered by Phase 1's folder_sync — this phase is about presence, version and provenance of the packages themselves.

**In scope:**

- Three new SyncJobs — `apt_sync`, `snap_sync`, `flatpak_sync` — each with its own config section, enable flag, machine-local decision file and progress reporting, sharing an extracted package-sync core.
- Declarative manifest capture on the source; item-level convergence on the target via each ecosystem's own tooling (`apt`, `snap`, `flatpak`).
- Item classes beyond packages: apt sources (`sources.list.d`), signing keys (`keyrings/`, `trusted.gpg.d/`), apt pins (`preferences.d`), apt config (`apt.conf.d`), flatpak remotes, snap channels.
- `/etc/apt/**` is pulled into Phase 2 (same precedent as `/root` moving into Phase 1). Phase 3 keeps the rest of `/etc`.
- A per-machine, never-synced decision file recording "machine-specific" items that this machine's jobs must not touch.
- A shared, synced install-snippet registry for items no package manager can reproduce (bare `.deb`s and manual installs).
- Detection of unowned installs under `/usr/local` and `/opt`, each requiring a permanent decision or a snippet.
- A batched, checkable TUI review presented before any change is applied.

**Out of scope (deferred):**

- Reproducing script installs as files (`/usr/local`, `/opt` trees) — detection and decision only, no file replication.
- Snippets for snap/flatpak items (YAGNI — every current snap/flatpak comes from a reachable remote).
- Source-cache reuse for offline installs (target downloads from its own repos).
- The rest of `/etc`, systemd, users/groups, GNOME dconf — Phase 3.
- Docker, VMs, k3s, rollback — Phases 4-7.

**Roadmap impact:** `/etc/apt/**` moves from Phase 3 into Phase 2. ROADMAP.md and REQUIREMENTS.md must be updated so REQ-sync-scope-app-and-system-config no longer implies apt repository state.
</domain>

<decisions>
## Implementation Decisions

### Replication model

- **D-01:** Convergence is **declarative manifest, replayed**. The source captures a manifest of items; the target diffs its own state against it and converges using each ecosystem's own tooling. Package databases and stores (`/var/lib/dpkg`, `/var/lib/snapd`, the flatpak OSTree store) are **never** rsynced — the package managers stay authoritative for their own state. — **Reversibility:** costly — the manifest schema, the item model and the decision file format are all shaped by this; switching to file-level replication would replace the whole job core.
- **D-02:** Everything the jobs handle is an **item** with a stable identity, not just packages. Item classes: apt package, apt source, apt key, apt pin, apt config file, snap, snap channel, flatpak ref, flatpak remote, unreproducible/manual install. All classes flow through one diff → decide → apply pipeline.
- **D-03:** apt's manifest carries the **manually-installed set** (`apt-mark showmanual`, 153 packages on P17), not the full dpkg selection set (2306). apt resolves dependencies on the target. The set is curated by the machine-specific decision file (below).

### Version fidelity

- **D-04:** Versions **float to whatever the repos currently offer**; the tool installs by name and does not pin the source's exact version. Version mismatches are detected and reported as a diff class, never force-downgraded.
- **D-05:** Version *constraints* do sync: `/etc/apt/preferences.d` pins are items like any other, so deliberate pinning replicates while incidental version skew does not.
- **D-06:** Snap revisions and flatpak scope **do** converge: the manifest records snap name + channel + revision and flatpak ref + origin + user/system scope, and the target converges with `snap refresh`/`snap install --revision` and `flatpak install --user/--system`. **Constraint: convergence must not leave a snap held or otherwise blocked from auto-refresh** — the goal is that both machines land on the same revision, not that either machine stops updating. The exact snapd mechanism that satisfies both is a research item.

### Machine-specific items and removal

- **D-07:** Every difference gets a **three-way decision**: apply / skip once / skip always, for every item class in every job — not just apt packages. **"Apply" means "converge this item", and the direction depends on the diff class:** present on source and absent on target → install/add/enable; present on target and absent on source → **remove/delete/disable**; present on both but different (version, channel, scope, file content) → change the target to match the source. So "apply" is the destructive branch as often as it is the additive one, and the review UI (D-24) must name the concrete action per item — "remove brscan3", not "apply" — so a user ticking items off a list always sees what will actually happen.
- **D-08:** "Skip always" records the item in a **per-machine decision file** that is never synced. Semantics: an item recorded on machine M is **inert on M in both roles** — not pushed when M is the source, not installed or removed when M is the target. One rule covers both "this is my hardware driver, don't share it" and "don't put that here".
- **D-08a:** **Which machine's file gets the entry follows which machine holds the item.** Source-held item declined → recorded on the source, so it is never pushed to *any* peer (`brscan3` on P17 stops propagating everywhere, not just to one laptop). Target-held item whose removal is declined → recorded on the target, so it is never removed there. Both readings collapse to the user's own framing: each machine keeps a list of what is machine-specific *to it*. Since the file is machine-local and unsynced, the job must write it on the correct end of the connection — on the target this means writing through the remote executor, not locally.
- **D-09:** The decision files live in `~/.config/pc-switcher/`, next to `config.yaml`, **one file per manager**. Because that directory is inside folder_sync's mirrored tree, these files need their own non-overridable exclusion alongside the ADR-017 runtime-state exclusion — and they must not be picked up by config_sync. — **Reversibility:** costly — moving the files later means migrating user state on every machine.
- **D-10:** Defaults ship as an **example file only**, never hardcoded in Python and never merged into `default-config.yaml` (consistent with the Phase 1 "default excludes live in YAML" convention). The file's real content accumulates from recorded skip-always decisions.

### Repositories, keys and remotes

- **D-11:** apt sources, keys, pins, apt config, flatpak remotes and snap channels are **inventory items with the same three-way decision**, not a file mirror. A `--delete` mirror of `/etc/apt` would wipe the target's own machine-specific sources, which contradicts D-07/D-08. "Merge on first sync" is therefore not a special mode — item-level convergence is merge-like on every run, in both directions.
- **D-12:** A repo's **signing key travels bundled with its repo item, verbatim** — the item is "this repo plus the keyring file it points at", copied byte-for-byte. Keys are never re-fetched from vendors. Legacy `/etc/apt/trusted.gpg.d` keys, which no repo references explicitly, form a separate global-trust item set.
- **D-13:** Both `/etc/apt/preferences.d` and `/etc/apt/apt.conf.d` sync as items.
- **D-14:** flatpak remotes (name, URL, key, per-scope) and snap channel tracking are provisioned on the target before installs that need them.

### Job structure

- **D-15:** **Four separate jobs** — `apt_sync`, `snap_sync`, `flatpak_sync`, `manual_installs_sync` — rather than one `package_sync`. Cleaner scope for both development and the user (independent enable flags, independent config, independent failure isolation, independent progress). `manual_installs_sync` owns everything no package manager can reproduce (D-18) and the snippet registry (D-23); it must NOT ride on `apt_sync`'s enable flag, because disabling apt would then silently disable manual-install detection with nothing telling the user.
- **D-16:** The shared core (item model, diff, three-way decision flow, batched TUI review, machine-local file I/O, snippet registry) is **extracted while designing and building**, not deferred to a post-hoc refactor and not written three times.
- **D-17:** **Package jobs run before folder_sync.** Apps are provisioned first, then their data lands on top — decisive for flatpak, where `~/.local/share/flatpak` must be created by `flatpak install` while `~/.var/app` comes from folder_sync, and it keeps package postinst defaults from overwriting real synced config.

### Unreproducible and manual installs

- **D-18:** Unreproducible items are owned by `manual_installs_sync`, not by `apt_sync`. Half of them are not apt's business at all (unowned files under `/usr/local` and `/opt`), and the other half — packages dpkg knows but no repo offers — are still resolved by a manual install, not by apt. The job does its own `dpkg-query`/`apt-cache policy` queries rather than sharing `apt_sync`'s, so ownership stays clean. Items no package manager can reproduce are detected: apt packages with no repo candidate (4 on P17: `brscan3`, `brother-udev-rule-type1`, `cnpg`, `falco-app`) plus unowned installs under `/usr/local` and `/opt` (`flux`, `talosctl`, `yq`, `batteryhealthchargingctl-janfr`, `/opt/containerd`). Note `/opt` is otherwise dpkg-owned (az→azure-cli, brother→brother-udev-rule-type1, Falco→falco-app, google→google-chrome-stable) and `/usr/local/bin/kubectl-cnpg` belongs to `cnpg`.
- **D-19:** Scanning for unowned installs is acceptable **because decisions are recorded** — a scanned finding produces noise exactly once, then never again. Every finding must end in a recorded decision or a snippet.
- **D-20:** An **install-snippet registry** ships in Phase 2 as part of the shared core. A snippet is an **opaque text blob**: the tool records it and replays it non-interactively on the target through the existing executor, and the exit code decides success. The tool never parses, versions, diffs or reasons about snippet content.
- **D-21:** An unreproducible item ends a run in one of **three** valid resolutions: it has a snippet, it is recorded as machine-specific (skip-always), or the user skipped it **once**. Skip-once is a real decision, not an unresolved state — the user may be declining something temporary, and a run where they made that choice is clean. Only an item nobody decided on — a non-interactive run, where nothing is recorded (D-26) — leaves the run visibly unclean. pc-switcher must offer to **add a snippet on the fly**, during the review, so resolving an item never requires leaving the sync.
- **D-22:** Snippets cover **bare `.deb`s and manual installs only**. Snap and flatpak items do not carry snippets (YAGNI — every current one comes from a reachable remote).
- **D-23:** Snippets live in the **shared, synced config**, not the machine-local decision file: how to install something is knowledge about the package, not about the machine. `manual_installs_sync` pushes `package-snippets.yaml` to the target **itself**, with `send_file()`, immediately after its own review — so snippets added on the fly during that review (D-21) are included. It must NOT travel via `config_sync`: that runs at SyncStep 9, before any review, so it cannot carry a snippet the user has not authored yet. It must also NOT rely on `folder_sync`: user sync jobs are in the user's hands and can be disabled or filtered, so no job's correctness may depend on another one running.

### Review UX, conflicts and failures

- **D-24:** Each job presents **its own batched review before it applies anything**, grouped by action. The batching is per manager, NOT across managers: the jobs are independent (D-15), and a cross-manager coordinator that reviews every enabled manager at once contradicts that independence. There is no shared review phase and no coordinator; each job runs plan → review → apply within its own `execute()`. The user wants a real TUI element — a checkable list to tick items off — rather than a sequence of prompts. Grouping by action matters precisely because "apply" is direction-dependent (D-07): installs and removals must be visibly separate groups, with removals labelled as removals, so a bulk tick can never silently delete. This must compose with the single persistent Live panel and its pause/resume-around-prompts behavior established in Phase 1 (plan 01-17/01-18).
- **D-25:** "Conflicts and version mismatches" are a **diff class inside that same review**, not a second reporting mechanism: missing-on-target, extra-on-target, version-mismatch (both versions shown), held/pinned, repo-unavailable, unreproducible. Success criterion 3's "reported before any destructive change" is satisfied because the whole review precedes every change.
- **D-26:** **No TTY / non-interactive run: skip all, once.** Nothing is applied that needed a decision, nothing is recorded permanently, everything unresolved is reported. Any special non-interactive behavior needed for integration testing must be **hidden** — undocumented, absent from `--help`, and active only when a specific testing environment variable is set.
- **D-27:** A failing item does not stop the job: **continue, collect, report at the end.** Each failure is logged with its stderr and listed in the job summary, and the job result is a failure so the sync is visibly not clean. Snapshots remain the backstop.
- **D-28:** The target **downloads from its own repos**. No source-cache reuse, no offline mode.

### folder_sync overlap

- **D-29:** The package jobs **export their excluded paths to folder_sync**, following the ADR-018 precedent set by `vscode_state_sync`: `flatpak_sync` owns `~/.local/share/flatpak`, `snap_sync` owns the `~/snap/<app>/<rev>` revision dirs, and folder_sync translates the supplied absolute paths into non-overridable filters without knowing anything about either ecosystem. Enabling a package job therefore automatically stops folder_sync from fighting it, and the corresponding hand-written lines can leave the user filter files. — **Reversibility:** reversible — it is the same export mechanism folder_sync already implements for VS Code state.

### apt transaction fidelity

- **D-30:** `apt-get -s` simulation runs at **plan time**, and collateral effects are classified before anything is refused. A package the simulation would remove or downgrade that is **auto-installed** (a dependency apt pulled in, `apt-mark showauto`) is apt doing its job — proceed without asking. A package that is **manually installed** (`apt-mark showmanual`) is something the user chose to have, so it becomes its own reviewable item offering install-anyway / skip / abort. Blanket refusal is wrong: it blocks legitimate installs whose only collateral is a dependency nobody chose. The question belongs in the review, never mid-apply — a prompt during apply reintroduces the prompt-flooding the batched review exists to prevent, and violates review-before-any-change.

### Code and documentation layout

- **D-31:** `jobs/` contains job modules only, plus `base.py` and `context.py` as the job infrastructure both sides share. The package-sync helpers live in a `jobs/packages/` python package with the `package_` prefix stripped: `items.py`, `review.py`, `state.py`, `sync_core.py`. Job modules stay directly in `jobs/` because job discovery resolves a `sync_jobs` key to `jobs/<name>.py`.
- **D-32:** No empty configuration sections. A job gets a config section when it has a real key, never as a placeholder for a possible future one — an empty `apt_sync: {}` with a banner comment explaining that it has no keys is worse than its absence. `default-config.yaml` and `config-schema.yaml` follow this together.
- **D-33:** `configuration.md` and `default-config.yaml` explain **configuration**. What a job does belongs in its own document, not in the configuration reference — including for the jobs that predate this phase, whose explanations move out too. The package jobs share enough of the item → diff → review → converge model to be documented together. In `default-config.yaml`, a job's enable flag gets one brief line, with rationale living in the job document.

### Claude's Discretion

- Manifest serialization format and on-disk layout, and the exact item-identity scheme per class (planner/researcher decide, consistent with D-02).
- The snapd mechanism that converges revisions without blocking auto-refresh (D-06) — research item.
- Concrete TUI widget choice for the checkable review list (D-24), within the constraints of the existing Rich Live model.
- Whether apt pins/config files (D-13) are diffed as whole files or as parsed entries.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project scope & requirements

- `.planning/PROJECT.md` — project vision, locked decisions, reliability priority order.
- `.planning/REQUIREMENTS.md` — REQ-sync-scope-packages, REQ-conflict-detection-no-resolution. NOTE: needs update for the `/etc/apt` scope move out of REQ-sync-scope-app-and-system-config.
- `.planning/ROADMAP.md` §"Phase 2" — goal and the 3 success criteria. NOTE: needs the same `/etc/apt` boundary update.
- `docs/planning/High level requirements.md` — item 2 (installed packages), the "Never Synced: Machine-Specific items" list, and the acceptable-constraints clause that permits a documented convention for bare installs.
- `.planning/phases/01-home-sync-mvp-user-data-sync/01-CONTEXT.md` — Phase 1 decisions this phase builds on and must not contradict.

### Architecture decisions (locked)

- `docs/adr/_index.md` — ADR index / supersession tracking.
- `docs/adr/adr-002-ssh-communication-channel.md` — multiplexed ControlMaster, source orchestrates, target runs stateless scripts.
- `docs/adr/adr-005-asyncio-concurrency.md` — all Job methods `async def`; package-manager invocations must be async subprocesses.
- `docs/adr/adr-010-logging-infrastructure.md` — six-level logging; per-item detail belongs at FULL, per-job summaries at INFO.
- `docs/adr/adr-015-topology-based-sync-safety-model.md` — warn-and-confirm safety model, never hard-abort; the precedent D-25/D-26 follow.
- ADR-014 (unified dry-run contract) — every SyncJob must produce a real read-only preview; the batched review doubles as the dry-run output for these jobs.
- ADR-017 — pc-switcher runtime state is hard-excluded from folder_sync; the model D-09's decision files must follow.
- ADR-018 — `vscode_state_sync` owns absolute paths that folder_sync translates into non-overridable filters; the exact mechanism D-29 reuses.

### Existing code to build on

- `src/pcswitcher/jobs/base.py` — `Job`/`SyncJob` base: `validate()`, `execute()`, `CONFIG_SCHEMA`, `describe_first_sync_scope()`.
- `src/pcswitcher/jobs/folder_sync.py` — the reference SyncJob implementation; see `_RUNTIME_EXCLUDE_RELPATHS` (~line 101) and `_vscode_state_exclude_filters` for the exclusion-export mechanism D-29 reuses.
- `src/pcswitcher/jobs/vscode_state_sync.py` — `vscode_state_exclude_paths()`, the ADR-018 export contract.
- `src/pcswitcher/jobs/context.py` — `JobContext` (executors, config, event bus).
- `src/pcswitcher/executor.py` — Local/Remote executor protocol; note the no-stdin constraint, which shapes how snippets (D-20) are executed.
- `src/pcswitcher/confirmer.py` + `src/pcswitcher/ui.py` + `src/pcswitcher/terminal.py` — existing confirmation and Live-panel machinery the batched review (D-24) must compose with.
- `src/pcswitcher/config.py` + `src/pcswitcher/default-config.yaml` — config schema and the `sync_jobs:` registry the three new jobs plug into.
- `src/pcswitcher/config_sync.py` — how `config.yaml` reaches the target; the machine-local files (D-09) must stay outside it.
- `.planning/codebase/ARCHITECTURE.md` — orchestrator 10-phase flow; jobs run sequentially in Phase 9, which is where D-17's ordering applies.

### External research

- GitHub issue #118 (`gh issue view 118 --comments`) — the feature issue, and the snap-revision comment that motivates D-06: both machines should converge on the same snap revisions via `snap refresh`/`snap install --revision` instead of rsyncing per-revision data dirs.
- `~/.config/pc-switcher/home-janfr.filter` — the live filter file whose flatpak (`/.local/share/flatpak`) and snap (`/snap/firefox`, `/snap/**/.cache`) rules D-29 supersedes.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets

- `SyncJob` base: all three jobs slot in with `validate()`/`execute()`/`CONFIG_SCHEMA`; no orchestrator plumbing needed beyond registering them in `sync_jobs:`.
- `vscode_state_sync`'s path-export contract (ADR-018) is exactly the mechanism D-29 needs — folder_sync already translates supplied absolute paths into non-overridable filters emitted before any user filter surface.
- `_RUNTIME_EXCLUDE_RELPATHS` in `folder_sync.py` is the precedent for D-09's machine-local decision files, though those live under `~/.config/pc-switcher/` rather than `~/.local/share/pc-switcher/` and therefore need a new home-relative exclusion.
- Executor protocol drives all package-manager invocations asynchronously; the same path executes install snippets (D-20).
- `confirmer.py` plus the single persistent Live with pause/resume (Phase 1 plans 01-17/01-18) is the substrate for the batched review (D-24).
- Six-level logger: per-item detail at FULL, per-job summaries at INFO — mirrors folder_sync's D-16 logging split.

### Established Patterns

- Jobs are discovered by name from `sync_jobs:` config; module name matches job name (`jobs/apt_sync.py` → `AptSyncJob`). One module = one Job class is still enforced (issue #30 tracks relaxing it).
- Three-phase validation: config schema → job config schema → system-state checks. Each job validates its manager's availability, sudo access, and remote reachability in `validate()`.
- Defaults live in YAML, never hardcoded in Python (D-10 extends this to the example decision files).
- Every SyncJob describes its own destructive first-sync scope via `describe_first_sync_scope()` — each of the three new jobs must.
- No blocking I/O in the event loop; no stdin available to executed commands.

### Integration Points

- New modules `src/pcswitcher/jobs/apt_sync.py`, `snap_sync.py`, `flatpak_sync.py` plus a shared package-sync core module.
- New config sections and `sync_jobs:` entries in `default-config.yaml`, plus example machine-local decision files and the shared snippet registry.
- folder_sync gains three more exclusion-path providers (D-29) alongside `vscode_state_exclude_paths()`.
- Job execution order in orchestrator Phase 9 must place the package jobs ahead of folder_sync (D-17).
- `/etc/apt/**` becomes Phase 2 territory — a boundary change ROADMAP.md and REQUIREMENTS.md must record.

</code_context>

<specifics>
## Specific Ideas

- Live inventory of P17 taken during this discussion, which several decisions are calibrated against: apt 2306 installed / 153 manual; 21 entries in `sources.list.d` (PPAs: git-core, libreoffice, jeffreyratcliffe; vendor repos: azure-cli, github-cli, google-chrome, hashicorp, nodesource, pgdg, spotify, tailscale, vscode, claude-desktop, antigravity); keys split 3 in `/etc/apt/keyrings` and 5 in `/etc/apt/trusted.gpg.d`; 17 snaps; ~30 flatpak refs split across user and system scope with several runtimes installed in both.
- Only 4 apt packages have no repo candidate — which is why bare-`.deb` replay stayed small and snippet-based rather than becoming its own subsystem.
- The intended workflow for "skip once": the user installs the thing themselves on the target after pc-switcher finishes, so the next sync shows no difference at all.
- The snippet registry's real value is inventory visibility — a list of things you know how to reinstall — and making upgrades a deliberate decision rather than a per-machine accident.
- "Machine-specific package" is the mental model to name in the UI and docs, in preference to "exclusion": *don't touch that package on that machine*.
- The user wants the review to feel like ticking items off a list, not answering a queue of questions.

</specifics>

<deferred>
## Deferred Ideas

- **Source-cache reuse / offline installs** — copying `/var/cache/apt/archives` and snap/flatpak blobs over SSH for LAN-speed, internet-free installs. Rejected for Phase 2 (D-28); revisit if target-side downloads prove slow or unreliable.
- **Snippets for snap/flatpak items** — YAGNI today; the mechanism is generic enough to extend if a private store or unreachable remote ever appears.
- **Replicating `/usr/local` and `/opt` trees as files** — out of scope; those items are detected and decided on, not reproduced. Relevant to Phase 3's system-config scope.
- **Normalizing the flatpak system/user scope split** — several runtimes are installed in both scopes on P17. Cleaning that up is a change to the machines, not a sync feature.
- **Migrating legacy `/etc/apt/trusted.gpg.d` keys to per-repo keyrings** — deprecated global trust is copied verbatim (D-12), not migrated; a migration is its own piece of work.
- **Relaxing one-module-one-Job (issue #30) and job DAG ordering (issue #28)** — D-17's ordering is explicit and documented for now; a real dependency graph is separate work.

</deferred>


<shipped>
## Already Delivered — Do Not Undo

Phase 2 executed 13 plans before these corrections. The corrections above change the review scope, the job split, the snippet transport and two policies; everything below is independent of them, was verified, and must survive replanning.

- Privileged `/etc/apt` writes stage under the target user's `~/.cache` and are promoted with `sudo install -o root -g root -m 0644`. `send_file()` is plain SFTP as the SSH user and can never target `/etc`.
- `sudo install` is preceded by `sudo mkdir -p -m 0755` on the destination directory: `/etc/apt/keyrings` does not exist on a fresh Ubuntu 24.04 install, and `install` without `-D` fails outright when it is absent.
- Repository/key/pin/config convergence is one transaction: back up every destination, write, run a single `apt-get update`, and restore on failure — including deleting files that did not previously exist.
- A backup failure records a per-item failure for the whole group rather than leaving the outcome map unpopulated, which previously produced an uncaught `KeyError` that aborted the run.
- The session status and CLI exit code derive from `job_results`, not from whether an exception propagated. A run whose items all failed must not exit 0.
- `validate()` checks passwordless sudo on the **source** as well as the target: capturing `/etc/apt` state runs `sudo find` there, and without it the capture degrades to empty digest maps and the sync reports success having replicated nothing.
- Every environmental assumption is checked in `validate()` with copy-paste remediation via `sudoers.passwordless_sudo_hint`, never discovered mid-execute. This rule is recorded in the `SyncJob.validate()` contract in `jobs/base.py`.
- `simulate_apt_transaction` fails closed when the simulation command itself fails, rather than reading a failed `apt-get -s` as a clean preview. D-30 changes what is done with a *successful* simulation's result, not this.
- Debian version ordering is decided by `dpkg --compare-versions`, never string comparison.
- `snap list --all` is parsed by header column name, never fixed offsets. No command in `snap_sync` sets a hold.
- Machine-local `*.decisions.yaml` files are excluded from `folder_sync` non-overridably, emitted before any user filter surface.
- The full VM integration suite passes in CI (60 passed, 5 skipped). Tests assert against the target's own package manager, never against pc-switcher's log text.

</shipped>

---

*Phase: 2-Package Management Sync*

*Context gathered: 2026-07-22*
