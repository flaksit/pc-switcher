# ADR-020: Declarative package convergence — (name, origin) replicates; repository configuration is derived

Status: Draft

Date: 2026-07-23

## TL;DR

The source captures a manifest; the target diffs its own state against it and converges via `apt`, `snap` and `flatpak` themselves. Package databases are never rsynced. A package replicates as **(name, origin)**, not name alone. Seven independent jobs each run plan → review → apply inside their own `execute()`. Repositories, keys, pins, flatpak remotes and blocks (apt holds, snap refresh holds, flatpak masks) are derived from the packages approved from them and never reviewed directly.

## Context

Phase 2 must replicate presence, version and provenance of packages across apt, snap and flatpak, plus the repository/keyring/pin/remote configuration those installs depend on. Application data belongs to Phase 1 `folder_sync`.

Provenance is the hard part: `firefox` exists in Ubuntu's archive and in Mozilla's own repository; `org.mozilla.firefox` on Flathub and Flathub-beta. Matching by name alone replicates the name and inverts the provenance, silently, at exit 0.

## Decision

### D-01 — Convergence model

Source captures a manifest; target diffs and converges via the ecosystem's own tools. `/var/lib/dpkg`, `/var/lib/snapd` and the flatpak OSTree store are never rsynced.

### D-02 — Item model

An item is something the user can decide about. Item classes: apt package, apt config file, snap, snap channel, flatpak ref, unreproducible install; for removal only, apt source and pin files. Flatpak remotes and blocks (holds, masks) are derived, not items.

### D-03/D-04/D-05 — Version and manifest policy

Apt manifest carries `apt-mark showmanual`. Versions float; a mismatch is `REPORT_ONLY`. Held apt packages take the source's exact version — a hold blocks install, upgrade and removal alike.

### D-06 — Snap converges revision and channel

Snap embeds the version in its per-user data path (`~/snap/<app>/<rev>`), so both machines must be on the same revision for `folder_sync` to mirror snap data cleanly. Snapd's automatic refresh is paused across the sync window on both hosts with a timeout, and each host's prior setting is restored on cleanup.

### D-07 — Decision shape

Three-way by default: apply / skip once / skip always. Two-way (act / skip once, no record) for apt source removal, apt pin removal and repository-conflict overwrite prompts. `REPORT_ONLY` takes no answer.

### D-08 — Machine-local decision file

One file per manager at `~/.config/pc-switcher/<manager>.decisions.yaml`, never synced. An entry on machine M makes the item inert on M in both roles. An entry is dropped once M no longer has the item; a dead entry would refuse the item's return.

### D-11 through D-14 — `/etc/apt` is derived, not mirrored

Four buckets: derived from approved packages (repository files, keyrings, conflict-free overwrites); always synced (`preferences.d` pins, distribution source files); reviewed two-way (repository and pin removals, repository-conflict overwrite); reviewed three-way (`apt.conf.d`). Only files apt itself reads. Keys travel byte-for-byte from the source, never re-fetched. Derived writes precede the installs that need them.

### D-15 — Seven separate jobs

`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_deb_sync`, `manual_snap_sync`, `manual_flatpak_sync`, `manual_installs_sync` are seven separate `SyncJob`s over one shared core. Each ships disabled. No review spans two managers. All seven run before `folder_sync`.

### D-22 — Unreproducible items

Each item has two mandatory bodies: `install_body` (replayed on the target) and `version_body` (prints the installed version on whichever machine runs it). Version comparison drives convergence; there is no folder diff and no payload hash. `version_body` runs on both machines during `plan()`, ungated by `--confirm-each-command`. Replay loops until versions match or the user skips. No purge-and-retry answer; no uninstall snippets.

### D-24 — Batched review, rounds when correctness needs them

Each job runs plan → review → apply in its own `execute()`. One screen per manager per action where the logic permits. `apt_sync` may ask in up to three rounds — the second round asks questions scoped to APPROVED work; the third asks collateral for an install whose repository this run itself writes.

### D-30 / D-40 — Collateral protects the target's `apt-mark showmanual`

apt collateral affecting the target's own manually-installed set becomes its own reviewable item (act / skip now / stop the sync). Only APPROVED removals waive the protection.

### D-34 / D-35 — (name, origin) enforced at the target's real state

The unit of replication for apt is (name, origin). After the `/etc/apt` group's single `apt-get update` and before the first install, one batched `apt-cache policy` re-reads target candidate origins; an install whose candidates do not intersect the source's fails as its own item. Distribution origins are per-machine exempt.

### D-36 — Pins always-sync

`preferences.d` pin adds and updates sync silently. A pin naming an absent origin is inert.

### D-38 — Distribution source files and ESM

`ubuntu.sources`, `/etc/apt/sources.list`, `ubuntu-esm-apps.sources`, `ubuntu-esm-infra.sources` are written when missing, overwritten when different, never removed. When the two ESM files would be written and the target reports unattached, `apt_sync` asks (attach and re-probe / skip `apt_sync`); non-interactive runs take the skip. pc-switcher cannot attach on the user's behalf.

### D-41 — Flatpak remotes derived from refs

A flatpak remote travels because an approved ref names it in that ref's scope. Ref identity is `<application>/<arch>/<branch>`; origin stays out of identity, so a ref on both machines from different remotes is `ORIGIN_MISMATCH` and never converged. Origin is compared by URL, never remote name.

### D-42 — Snap: nothing to derive

One store per device, name→publisher pinned by canonical-signed `snap-declaration`. No repository or key decision.

## Consequences

**Positive**
- Provenance replicates, not just presence, and the guarantee is checked against the target's real state.
- Package managers stay authoritative for their own state.
- The review asks only what the user can answer.

**Negative (costly to reverse)**
- Manifest schema, item identity and decision-file format are shaped by D-01; switching to file-level replication later replaces the whole job core.
- Package sync requires passwordless sudo on both machines.
- A repository or remote feeding no synced item does not travel: the two machines converge for what packages need, not to identical configurations.
- Pins always-sync, so a `preferences.d` file the user wanted on one machine returns every run unless deleted on the source.
- An unreproducible item is only as converged as its version string says.

## Alternatives Considered

- **File-level replication of the package databases** — rejected: managers must stay authoritative.
- **A single combined `package_sync` job** — rejected: independent enable flags, config, failure isolation.
- **A `--delete` file mirror of `/etc/apt`** — rejected: wipes the target's own machine-specific sources.
- **Repositories and remotes as reviewed items on top of an origin check** — rejected: keeps the unrepresentable "package ticked, repository unticked" pairing.
- **Deriving pins per package rather than always-syncing** — rejected: a pin naming an absent origin is inert, so precision buys nothing.
- **Always-syncing every flatpak remote** — rejected: a remote costs a summary fetch on every `flatpak update`.
- **Writing ESM sources to an unattached target and only warning** — rejected: refresh succeeds, ESM suites win candidate selection, install fails with 401.
- **Withholding the two ESM files silently** — rejected: pins travel regardless, so pins land over a repository set neither machine has.
- **Refusing the whole run when an approved origin cannot be replicated** — rejected: contradicts D-27's continue-and-report.
- **Protecting the union of both machines' manual sets from collateral** — rejected: the union protects on the wrong machine's bookkeeping.
- **A recursive folder diff or payload hash for a manual install** — rejected: version body replaces both.
- **Comparing snippet body before version** — rejected: a cosmetic edit to a comment or mirror URL would raise a false review item.
- **Letting the higher version decide sync direction** — rejected: sync goes source-to-target, always.
- **A purge-and-replace answer beside retry** — rejected: with no folder diff, purging cannot change what the version body reports.
- **Uninstall snippets** — rejected: ecosystem's own removal covers three jobs, `rm -rf` the fourth.
- **A backwards-compatible registry defaulting the missing version body** — rejected: every such entry would silently converge on presence again.
- **Gating `version_body` behind `--confirm-each-command`** — rejected: it runs before the run has proposed anything, so the confirm would arrive before the user had seen a single change.
- **Pre-validating target sudo for the four unreproducible jobs** — rejected: only approved removals need it.
- **Source-cache reuse for offline installs** — deferred.

## References

- ADR-002, ADR-005, ADR-010, ADR-014, ADR-015, ADR-018, ADR-021, ADR-022
- `docs/system/package-sync.md` — the specification these decisions govern
- `docs/planning/package-sync-user-requirements.md` — intent
- `docs/adr/considerations/adr-020-flatpak-filter-and-trust-measurements.md`
- `docs/adr/considerations/adr-020-apt-esm-and-snap-measurements.md`
- `docs/adr/considerations/package-sync-rationale.md` — per-article justification, keyed by `PKG-FR-*` id
- GitHub issue #118
