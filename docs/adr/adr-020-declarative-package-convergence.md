# ADR-020: Declarative package convergence — (name, origin) replicates; repository configuration is derived

Status: Draft

Date: 2026-07-23

## TL;DR

The source captures a manifest; the target diffs its own state against it and converges via `apt`, `snap` and `flatpak` themselves. Package databases are never rsynced. A package replicates as **(name, origin)**, not name alone. Seven independent jobs each run plan → review → apply inside their own `execute()`. Repositories, keys, pins, flatpak remotes and blocks (apt holds, snap refresh holds, flatpak masks) are derived from the packages approved from them and never reviewed directly.

## Identifiers

Each decision below carries an ID of the form `ADR-020-D-<name>` — the stable, greppable citation that code, tests and other docs use to refer to it. The articles it governs live in [`docs/system/package-sync.md`](../system/package-sync.md) and name the decision in their `Lineage:`; grep there for a decision ID to list them.

## Context

Package sync must replicate presence, version and provenance of packages across apt, snap and flatpak, plus the repository/keyring/pin/remote configuration those installs depend on. Application data belongs to `folder_sync`.

Provenance is the hard part: `firefox` exists in Ubuntu's archive and in Mozilla's own repository; `org.mozilla.firefox` on Flathub and Flathub-beta. Matching by name alone replicates the name and inverts the provenance, silently, at exit 0.

## Decision

### ADR-020-D-CONVERGE-MODEL — Convergence model

Source captures a manifest; target diffs and converges via the ecosystem's own tools. `/var/lib/dpkg`, `/var/lib/snapd` and the flatpak OSTree store are never rsynced.

### ADR-020-D-ITEM-MODEL — Item model

An item is something the user can decide about. Item classes: apt package, apt config file, snap, snap channel, flatpak ref, unreproducible install; for removal only, apt source and pin files. Flatpak remotes and blocks (holds, masks) are derived, not items (see `PKG-FR-BLOCKS-DERIVED`).

### ADR-020-D-VERSION-POLICY — Version and manifest policy

Apt manifest carries `apt-mark showmanual`. Versions float; a mismatch is `REPORT_ONLY`. Held apt packages take the source's exact version — a hold blocks install, upgrade and removal alike.

### ADR-020-D-SNAP-REVISION — Snap converges revision and channel

Snap embeds the version in its per-user data path (`~/snap/<app>/<rev>`), so both machines must be on the same revision for `folder_sync` to mirror snap data cleanly. Snapd's automatic refresh is paused across the sync window on both hosts with a timeout, and each host's prior setting is restored on cleanup.

### ADR-020-D-DECISION-SHAPE — Decision shape

Three-way by default: apply / skip once / skip always. Two-way (act / skip once, no record) for apt source removal, apt pin removal and repository-conflict overwrite prompts. `REPORT_ONLY` takes no answer.

### ADR-020-D-DECISION-FILE — Machine-local decision file

One file per manager at `~/.config/pc-switcher/<manager>.decisions.yaml`, never synced. An entry on machine M makes the item inert on M in both roles. An entry is dropped once M no longer has the item; a dead entry would refuse the item's return.

### ADR-020-D-APT-CONFIG-DERIVED — `/etc/apt` is derived, not mirrored

Four buckets: derived from approved packages (repository files, keyrings, conflict-free overwrites); always synced (`preferences.d` pins, distribution source files); reviewed two-way (repository and pin removals, repository-conflict overwrite); reviewed three-way (`apt.conf.d`). Only files apt itself reads. Keys travel byte-for-byte from the source, never re-fetched. Derived writes precede the installs that need them.

### ADR-020-D-SEVEN-JOBS — Seven separate jobs

`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_deb_sync`, `manual_snap_sync`, `manual_flatpak_sync`, `manual_installs_sync` are seven separate `SyncJob`s over one shared core. Each ships disabled. No review spans two managers. All seven run before `folder_sync`.

### ADR-020-D-UNREPRODUCIBLE-ITEMS — Unreproducible items

Every item carries an install-or-update body, replayed on the target; it is mandatory for every kind. A second body, printing the version installed on whichever machine runs it, exists only where the kind has no version source of its own — an install under a path no package manager owns. For a hand-installed package, a sideloaded snap and a bundle-installed flatpak the manager itself answers the version question, so those items have no version body at all. Version comparison drives convergence; there is no folder diff and no payload hash. A version body runs on both machines during `plan()`, ungated by `--confirm-each-command`. Replay loops until versions match or the user skips. No purge-and-retry answer; no uninstall snippets.

### ADR-020-D-BATCHED-REVIEW — Batched review, rounds when correctness needs them

Each job runs plan → review → apply in its own `execute()`. One screen per manager per action where the logic permits. `apt_sync` may ask in up to three rounds — the second round asks questions scoped to APPROVED work; the third asks collateral for an install whose repository this run itself writes.

### ADR-020-D-COLLATERAL — Collateral protects the target's `apt-mark showmanual`

apt collateral affecting the target's own manually-installed set becomes its own reviewable item (act / skip now / stop the sync). Only APPROVED removals waive the protection.

### ADR-020-D-ORIGIN-VERIFY — (name, origin) enforced at the target's real state

The unit of replication for apt is (name, origin). After the `/etc/apt` group's single `apt-get update` and before the first install, one batched `apt-cache policy` re-reads target candidate origins; an install whose candidates do not intersect the source's fails as its own item. Distribution origins are per-machine exempt.

### ADR-020-D-DEB-OWNERSHIP — One verdict per name, from apt's candidate

A package belongs to `manual_deb_sync` rather than `apt_sync` when apt has no repository it can install that name from — read off apt's own `Candidate:` row, whose origins are empty exactly when apt would install nothing from a repository. Version is not part of it. The verdict is reached once per name and withholds that name from both machines' apt manifests: the source's answer decides for a name the source has, the target's own for a name only the target has.

### ADR-020-D-PINS-ALWAYS-SYNC — Pins always-sync

`preferences.d` pin adds and updates sync silently. A pin naming an absent origin is inert.

### ADR-020-D-DISTRO-AND-ESM — Distribution source files and ESM

`ubuntu.sources`, `/etc/apt/sources.list`, `ubuntu-esm-apps.sources`, `ubuntu-esm-infra.sources` are written when missing, overwritten when different, never removed. When the two ESM files would be written and the target reports unattached, `apt_sync` asks (attach and re-probe / skip `apt_sync`); non-interactive runs take the skip. pc-switcher cannot attach on the user's behalf.

### ADR-020-D-FLATPAK-REMOTES — Flatpak remotes derived from refs

A flatpak remote travels because an approved ref names it in that ref's scope. Ref identity is `<application>/<arch>/<branch>`; origin stays out of identity, so a ref on both machines from different remotes is `ORIGIN_MISMATCH` and never converged. Origin is compared by URL, never remote name.

### ADR-020-D-SNAP-NO-DERIVATION — Snap: nothing to derive

One store per device, name→publisher pinned by canonical-signed `snap-declaration`. No repository or key decision.

## Consequences

**Positive**
- Provenance replicates, not just presence, and the guarantee is checked against the target's real state.
- Package managers stay authoritative for their own state.
- The review asks only what the user can answer.

**Negative (costly to reverse)**
- Manifest schema, item identity and decision-file format are shaped by `ADR-020-D-CONVERGE-MODEL`; switching to file-level replication later replaces the whole job core.
- Package sync requires passwordless sudo on both machines.
- A repository or remote feeding no synced item does not travel: the two machines converge for what packages need, not to identical configurations.
- Pins always-sync, so a `preferences.d` file the user wanted on one machine returns every run unless deleted on the source.
- An unreproducible item is only as converged as its version string says.

## Alternatives Considered

- **File-level replication of the package databases** — Rejected: managers must stay authoritative.
- **A single combined `package_sync` job** — Rejected: independent enable flags, config, failure isolation.
- **A `--delete` file mirror of `/etc/apt`** — Rejected: wipes the target's own machine-specific sources.
- **Repositories and remotes as reviewed items on top of an origin check** — Rejected: keeps the unrepresentable "package ticked, repository unticked" pairing.
- **Deriving pins per package rather than always-syncing** — Rejected: a pin naming an absent origin is inert, so precision buys nothing.
- **Always-syncing every flatpak remote** — Rejected: a remote costs a summary fetch on every `flatpak update`.
- **Writing ESM sources to an unattached target and only warning** — Rejected: refresh succeeds, ESM suites win candidate selection, install fails with 401.
- **Withholding the two ESM files silently** — Rejected: pins travel regardless, so pins land over a repository set neither machine has.
- **Refusing the whole run when an approved origin cannot be replicated** — Rejected: contradicts `PKG-FR-OUTCOME-FAILED`'s continue-and-report model.
- **Protecting the union of both machines' manual sets from collateral** — Rejected: the union protects on the wrong machine's bookkeeping.
- **Reading ownership off the INSTALLED version's origins** — Rejected: a phased update leaves an installed version no repository carries any more, which would read every laggard as a hand `.deb`.
- **Unioning the two machines' exclusion sets** — Rejected: it hides the symptom of two verdicts for one name while leaving the package misclassified on the machine that produced the wrong one.
- **A recursive folder diff or payload hash for an install under an unowned path** — Rejected: its version body replaces both.
- **Comparing snippet body before version** — Rejected: a cosmetic edit to a comment or mirror URL would raise a false review item.
- **Letting the higher version decide sync direction** — Rejected: sync goes source-to-target, always.
- **A purge-and-replace answer beside retry** — Rejected: with no folder diff, purging cannot change the version the machine reports.
- **Uninstall snippets** — Rejected: ecosystem's own removal covers three jobs, `rm -rf` the fourth.
- **Defaulting the missing version body of an unowned-path entry** — Rejected: nothing else can answer that item's version question, so every such entry would silently converge on presence again.
- **A version body on every kind, for one uniform registry shape** — Rejected: three kinds never run it, so the user writes a command that never runs and an entry complete for its kind reads as malformed.
- **Gating `version_body` behind `--confirm-each-command`** — Rejected: it runs before the run has proposed anything, so the confirm would arrive before the user had seen a single change.
- **Pre-validating target sudo for the four unreproducible jobs** — Rejected: only approved removals need it.
- **Source-cache reuse for offline installs** — Deferred.

## References

- ADR-002, ADR-005, ADR-010, ADR-014, ADR-015, ADR-018, ADR-021, ADR-022
- `docs/system/package-sync.md` — the specification these decisions govern
- `docs/planning/package-sync-user-requirements.md` — intent
- `docs/adr/considerations/adr-020-flatpak-filter-and-trust-measurements.md`
- `docs/adr/considerations/adr-020-apt-esm-and-snap-measurements.md`
- `docs/adr/considerations/package-sync-rationale.md` — per-article justification, keyed by `PKG-FR-*` id
- GitHub issue #118
