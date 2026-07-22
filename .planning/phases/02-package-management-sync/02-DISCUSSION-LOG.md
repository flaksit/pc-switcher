# Phase 2: Package Management Sync - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-22

**Phase:** 2-Package Management Sync

**Areas discussed:** Replication model & version fidelity, Removal semantics & machine-specific exclusions, Repo sources & PPAs vs the Phase 3 boundary, Bare installs (.debs and scripts), Job granularity, Conflict / version-mismatch response, Partial-failure policy, Download source & offline capability, folder_sync overlap, In/out-of-scope call for manual installs

## Area selection

The user selected all eight proposed gray areas and added two: the overlap with folder_sync (pointing at the snap-revision comment on issue #118), and an explicit decision on whether individual `.deb` and manual/script installs are in scope — to be made *after* an inventory existed. A live inventory of P17 was taken before continuing, and it shaped several answers.

## Replication model

| Option | Description | Selected |
|--------|-------------|----------|
| Declarative manifest, replayed | Source captures a manifest; target converges via its own package managers; no rsyncing of package DBs | ✓ |
| File-level copy of package state | rsync /var/lib/dpkg, /var/lib/snapd, flatpak OSTree store | |
| Hybrid: manifest + cache reuse | Manifest convergence with blobs pulled from the source's caches | |

**User's choice:** Declarative manifest.

## Apt manifest scope

| Option | Description | Selected |
|--------|-------------|----------|
| Manually-installed set only | 153 packages from `apt-mark showmanual`; apt resolves dependencies | ✓ (amended) |
| Full dpkg selection set | All 2306 packages including auto-installed | |
| Manual set + auto-set audit | Sync manual set, report auto-set drift | |

**User's choice:** Manual set, but curated — e.g. to exclude hardware-specific packages.

**Notes:** The user later generalized this: curation is not an apt concept, it applies to every package manager and source, and it belongs in a per-machine list rather than in the manifest.

## Version fidelity

| Option | Description | Selected |
|--------|-------------|----------|
| Same set, repo-current versions | Install by name; report mismatches, don't pin | ✓ (amended) |
| Pin exact source versions | `apt install pkg=version`, `snap install --revision` for everything | |
| Pin where it matters, float otherwise | Config list of must-match packages | |

**User's choice:** Repo-current versions by default, but sync the config files that pin versions or impose other constraints.

**Notes:** This is what pulled `/etc/apt/preferences.d` into the item model.

## Snap revisions and flatpak scope

| Option | Description | Selected |
|--------|-------------|----------|
| Converge revisions + preserve scope | Manifest records snap channel+revision and flatpak ref+origin+scope | ✓ (amended) |
| Names and channels only | Let each machine refresh independently | |
| Converge revisions; normalize flatpak scope | Also collapse the system/user split | |

**User's choice:** Converge revisions and preserve scope, ensuring this does not block (auto)updates.

**Notes:** The auto-update constraint is a genuine tension with revision pinning — flagged in CONTEXT.md as a research item (D-06).

## Removal semantics

| Option | Description | Selected |
|--------|-------------|----------|
| Remove, after warn + confirm | Mirror semantics with a confirmation and an override flag | |
| Additive only — never remove | Target accumulates | |
| Remove silently, like a file mirror | Snapshots and dry-run as the only safety net | |

**User's choice:** Neither as posed — a three-way decision per item: skip now once / skip forever (recorded) / remove. Applies to every package manager and source, not just apt.

**Notes:** This became D-07 and generalized to every item class in the phase, including installs, not just removals.

## Machine-specific list: location and expression

| Option | Description | Selected |
|--------|-------------|----------|
| Per-manager lists in the central YAML config | Lists in `config.yaml` | |
| A .filter-style file per manager | Reuse folder_sync's filter authoring surface | |
| One flat list across all managers | Single list matched against any manager | |

**User's choice:** Per-manager lists, but *not* in the central synced `config.yaml` — the list is specific to each machine and stays on that machine.

Follow-up on where the file lives:

| Option | Description | Selected |
|--------|-------------|----------|
| `~/.local/share/pc-switcher/` | Already hard-excluded from folder_sync by ADR-017 | |
| `~/.config/pc-switcher/` next to config.yaml | Discoverable, but inside folder_sync's mirror — needs a new exclusion | ✓ |
| `/etc` or another root-owned location | Unambiguously machine-scoped, but Phase 3 territory and needs sudo | |

**User's choice:** `~/.config/pc-switcher/`.

**Notes:** Discoverability won over the free exclusion. Recorded in CONTEXT.md as needing its own non-overridable folder_sync exclusion.

## Machine-specific list: semantics and seeding

| Option | Description | Selected |
|--------|-------------|----------|
| Inert on M in both roles | Not pushed from M, not installed/removed on M | ✓ |
| Two lists: don't-push and don't-install | More precise, two lists to maintain | |
| Inert on M as target only | Peer answers the same prompt in the other direction | |

**User's choice:** Inert in both roles.

**Notes:** The user reframed the concept: "machine-specific package" is clearer than "exclusion" — *don't touch that package on that machine*. On seeding: ship defaults as an example file only, never hardcoded; the real content accumulates from recorded skip-always decisions.

## Review UX and headless behavior

| Option | Description | Selected |
|--------|-------------|----------|
| One batched review before any change | Grouped by manager and action, per-item apply/skip-once/skip-always | ✓ |
| Prompt per package as encountered | Simple, but reproduces the Phase 1 prompt-flooding problem | |
| Report, then require a second run | Fully non-interactive, breaks the single-command workflow | |

**User's choice:** Batched — and explicitly "would be cool with a nice TUI element where I can check off items on a list."

Headless behavior:

| Option | Description | Selected |
|--------|-------------|----------|
| Apply installs, skip removals, report | Non-destructive but still makes progress | |
| Skip everything unresolved, report only | Most conservative | ✓ (amended) |
| Abort the job | Refuse to proceed without a human | |

**User's choice:** In normal usage, skip all once. Testing-only behavior may exist but must be hidden — undocumented, absent from `--help`, active only behind a specific testing environment variable.

## Repo sources and the Phase 3 boundary

| Option | Description | Selected |
|--------|-------------|----------|
| Pull the apt subset into Phase 2 | Phase 2 owns `/etc/apt/**`; Phase 3 keeps the rest of /etc | ✓ |
| Declarative repos in the manifest | Reconstruct repo files from parsed data | |
| Wait for Phase 3 — Phase 2 only reports | Success criterion 2 would not be met | |

**User's choice:** Pull `/etc/apt` into Phase 2.

Mechanism, as first posed:

| Option | Description | Selected |
|--------|-------------|----------|
| Reuse folder_sync's rsync machinery on scoped paths | Same transport, proven in Phase 1 | (superseded) |
| Purpose-built copy in the package job | Second file-transfer implementation | |
| Configure a folder_sync entry instead | Decouples repo state from the job that needs it | |

**User's choice:** Initially "reuse folder_sync", immediately qualified — machines can differ here too (sources for machine-specific packages), and sources and keys need *merging* rather than overwriting, possibly the same sync/skip-once/skip-always treatment as packages. Claude agreed the qualification defeats the mirror approach: a `--delete` mirror of `/etc/apt` would wipe the target's own machine-specific sources.

Re-posed as an item model:

| Option | Description | Selected |
|--------|-------------|----------|
| Inventory items, same 3-way decision as packages | Merge is just what item-level convergence does, every run | ✓ |
| File mirror with an exception list | Exception unit is a filename, not a repo | |
| Union-only: add missing, never remove | Stale repos live forever | |

**User's choice:** Inventory items.

**Notes:** Keys travel bundled with their repo item, verbatim. Both `preferences.d` and `apt.conf.d` sync as items (the user chose "both", over Claude's recommendation to keep `apt.conf.d` machine-local). Flatpak remotes and snap channels are equivalent to apt sources and keys, and in scope.

## Job granularity

| Option | Description | Selected |
|--------|-------------|----------|
| One file per manager for decisions | Cleaner if the job is later split per manager | ✓ |
| One file, sectioned by item type | One file to exclude and back up | |
| You decide | Leave layout to the planner | |

**User's choice:** One file per manager — "I propose to immediately create distinct jobs per manager. Much cleaner scope both for dev and user."

Follow-up on the job set:

| Option | Description | Selected |
|--------|-------------|----------|
| 3 jobs + a shared package-sync core | Shared item model, decision flow, TUI, file I/O | |
| 3 fully independent jobs | Logic written three times | |
| 3 jobs, extract the core later | Avoid abstracting before two real users exist | ✓ (amended) |

**User's choice:** Three jobs, extracting the core *while* designing and building — not as a later refactor.

## Job ordering

| Option | Description | Selected |
|--------|-------------|----------|
| Packages first, then folder_sync | Apps provisioned before their data lands | ✓ |
| folder_sync first, then packages | postinst could overwrite synced config | |
| You decide, but document it | Leave to planner with a documentation requirement | |

**User's choice:** Packages first.

## Bare .debs and manual installs

Inventory finding presented: exactly 4 apt packages have no repo candidate (`brscan3`, `brother-udev-rule-type1`, `cnpg`, `falco-app`); `/usr/local/bin` holds `flux`, `talosctl`, `yq`, `kubectl-cnpg`, `batteryhealthchargingctl-janfr`; `/opt` holds az, brother, containerd, Falco, google.

| Option | Description | Selected |
|--------|-------------|----------|
| Out of scope; detect and report | Machine-local treatment, no replay machinery | |
| In scope via a tracked directory convention | Install .debs from a folder_sync'd dir, replay on target | |
| In scope by copying the installed .deb | Pull from apt cache or dpkg-repack | |

**User's choice:** Neither as posed. Pragmatically: delete / skip once / skip always, where "skip once" means the user installs it themselves on the target after the sync so the next run shows no difference. But the user proposed a stricter alternative — require a bash snippet that performs the install, recorded by the tool — and asked for Claude's opinion, citing better inventory visibility and easier upgrades.

**Notes:** The user corrected a factual error in the option text: Falco is a standalone app with its own internal update mechanism, not a k8s plugin. Claude's response: worth doing, but only if the snippet stays opaque and optional — recorded and replayed via the existing executor with the exit code deciding success, never parsed or reasoned about — because "record how to install things" otherwise grows into a bespoke package manager (idempotency, version detection, upgrade vs reinstall, ordering, rollback). Claude also recommended the registry live in the shared synced config rather than the machine-local file, since how to install something is knowledge about the package, not the machine.

Snippet registry, as decided:

| Question | Options | User's choice |
|----------|---------|---------------|
| Adopt the registry? | opaque and optional / mandatory / no, report only | Yes, **mandatory** — and pc-switcher must allow adding snippets on the fly |
| Coverage | any unreproducible item / bare .debs only / bare .debs + free-form manual list | Bare .debs and manual installs; snap and flatpak are YAGNI |
| Timing | Phase 2 / own phase / Phase 2 report-only | Phase 2, as part of the shared core |
| Detection of unowned installs | package-manager signals only / scan /usr/local and /opt / one-off audit command | Scan — "we record decisions and snippets, so there is only noise when a new thing got installed" |

## Script installs

| Option | Description | Selected |
|--------|-------------|----------|
| Out of scope; document the convention | ~/.local/bin and uv tools already sync via folder_sync | ✓ |
| Add /usr/local and /opt as synced paths | Would fight apt over vendor-owned trees | |
| Manifest of re-runnable install commands | A package manager of our own making | |

**User's choice:** Out of scope — "most (or all?) of what you listed comes actually from apt packages and manually installed .debs."

**Notes:** Verified and partly corrected: `/opt` is almost entirely dpkg-owned (az→azure-cli, brother→brother-udev-rule-type1, Falco→falco-app, google→google-chrome-stable, only `/opt/containerd` unowned), but in `/usr/local/bin` only `kubectl-cnpg` belongs to a package — `flux`, `talosctl`, `yq` and `batteryhealthchargingctl-janfr` are genuinely unowned. These fall to the detection-plus-decision path rather than to file replication.

## Conflicts, failures, downloads, overlap

| Question | Options | User's choice |
|----------|---------|---------------|
| What is a conflict? | diff class in the same checklist / separate pre-flight report / hard block | Diff class in the same checklist |
| Item fails mid-apply | continue and report / abort job / abort sync | Continue, collect, report at the end |
| Download source | target's own repos / reuse source caches over SSH / opt-in flag | Target's own repos |
| Who owns the overlapping filter rules | package jobs export paths to folder_sync / keep in user filter files / export as overridable defaults | Package jobs export paths (the ADR-018 precedent) |

## Claude's Discretion

- Manifest serialization format and per-class item-identity scheme.
- The snapd mechanism that converges revisions without blocking auto-refresh.
- Concrete TUI widget for the checkable review list, within the existing Rich Live model.
- Whether apt pins and config files are diffed as whole files or parsed entries.

## Deferred Ideas

- Source-cache reuse for LAN-speed, internet-free installs.
- Snippets for snap and flatpak items.
- Replicating `/usr/local` and `/opt` trees as files.
- Normalizing the flatpak system/user scope split (several runtimes are installed in both scopes on P17).
- Migrating legacy `/etc/apt/trusted.gpg.d` keys to per-repo keyrings.
- Relaxing one-module-one-Job (issue #30) and a real job dependency graph (issue #28).
