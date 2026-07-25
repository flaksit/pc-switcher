# Package Sync

The package jobs replicate *what is installed* — apt packages plus the `/etc/apt` repository configuration they depend on, snaps, flatpaks, and the things no package manager can reproduce — rather than user data. Package *data* (`~/.var/app`, `~/snap/<app>/common`, dotfiles) stays `folder_sync`'s territory. The package sync jobs are about the presence, version and provenance of the packages themselves.

Configuration for these jobs is limited to their `sync_jobs` enable flags; see the [configuration reference](../configuration.md#sync_jobs). There are no per-job config keys.

## The four jobs

Four independent jobs share one item -> diff -> review -> converge model. Each has its own enable flag, its own validation, its own review and its own failure isolation, so enabling one never drags in another.

```yaml
sync_jobs:
  apt_sync: false             # apt packages plus the /etc/apt repository configuration they depend on
  snap_sync: false            # installed snaps, converged to the source's revision and channel
  flatpak_sync: false         # installed flatpak refs and their remotes, per scope
  manual_installs_sync: false # things no package manager can reproduce, plus the install-snippet registry
```

All four ship **disabled**: enabling any of them lets pc-switcher change installed software on the target, so it is opt-in.

### What each job covers

- **`apt_sync`** — the manually-installed apt package set (`apt-mark showmanual`, not the full dpkg selection — apt resolves dependencies on the target itself), plus the repository configuration that governs where packages come from: sources under `/etc/apt/sources.list.d`, signing keys (`/etc/apt/keyrings`, legacy `/etc/apt/trusted.gpg.d`), pins (`/etc/apt/preferences.d`) and apt config (`/etc/apt/apt.conf.d`).
- **`snap_sync`** — installed snaps, converged to the source's exact revision and tracking channel.
- **`flatpak_sync`** — installed flatpak refs and their remotes, per user/system installation scope.
- **`manual_installs_sync`** — everything no package manager can reproduce: apt packages with no repository candidate, plus unowned files under `/usr/local` and `/opt`. It also owns the [install-snippet registry](#install-snippets).

## Job ordering is enforced

The three package-manager jobs **must** be listed before `folder_sync` in `sync_jobs`. This is not a convention: pc-switcher validates the order and aborts the run with a config error if any of `apt_sync`, `snap_sync` or `flatpak_sync` is enabled but sits after `folder_sync` (`orchestrator._check_package_jobs_precede_folder_sync`).

The reason is the "defaults, then your data" layering. Installing software usually writes its own default config and data files on first appearance. If `folder_sync` ran first, the target would have your synced versions of those files, and then the fresh install would overwrite them with stock defaults. Running the package jobs first means the software (and its defaults) already exists when `folder_sync` lands your versions on top — so your tweaks win, not the installer's defaults.

## Batched review

Because an enabled package job can install or remove software on the target, each one shows you a review and waits for your approval before it changes anything.

The review lists every difference the job found between source and target, grouped by action, and installs are always kept separate from removals: a group that would install software is never mixed with one that would remove it, and a removal group names the removal explicitly (for example "Remove packages") rather than saying "apply". Removal groups start **unticked**, so a bulk approval can never silently delete something.

Every item that would actually change something — an install, a removal, or a change to match the source, holds and masks included — offers the same three-way choice:

- **Apply** it — make this change on the target.
- **Skip this run** — leave it alone for now; it comes back next sync.
- **Skip always** — mark it as belonging to this machine only, so no future sync touches it (see [Machine-specific packages](#machine-specific-packages)).

You give those answers with two lists per group, not with a question per item. The first list is the apply list: ticked means apply. Whatever you leave unticked is then offered once more — "never offer again on this machine?" — and ticking it there is skip-always. Ticking nothing on that second list (just pressing Enter) is skip-this-run, so the items come back next sync. If you ticked everything for apply, the second list is not shown at all. Ctrl-C or EOF at either list aborts the whole sync.

Items that only **report** a condition are not offered skip-always: a version difference between source and target, an apt package with no repository candidate, and the pin echo on a held or pinned package. These change nothing on the target, and neither machine "holds" the item in the way a machine-specific mark requires — marking a version difference would silently stop the package syncing altogether rather than stop reporting the drift. Resolve them by fixing the underlying condition (align the versions, add the repository, remove the pin).

### Confirming every individual command

The batched review approves *items*, not commands. One ticked line can expand into several: an apt package is an `apt-get -s` simulation then an `apt-get install`; an apt repository file is a backup, an upload, a `sudo install` promotion and an `apt-get update`.

`pc-switcher sync <target> --confirm-each-command` inserts one prompt before every one of them, showing the exact command (or, for a file transfer, the source and destination paths) and waiting for **p** to proceed or **a** to abort the whole sync. There is no "skip this one": a single reviewed item can span several commands, so skipping one would leave that item half-applied. An unanswerable prompt (Ctrl-C, EOF) aborts.

It covers every write the four jobs make, plus the machine-local decision files on both machines, the snippet registry and its push, the snapd auto-refresh pause and restore, and the sync-history update on both ends. Read-only commands are never gated. The flag needs a real terminal and is refused without one. It is meant for auditing or debugging a run you do not trust yet, not for everyday syncs.

### apt collateral

When you approve an apt change, apt sometimes has to remove or downgrade *other* packages to satisfy it — so the package you approved is not always the whole transaction. `apt_sync` simulates every approved change with `apt-get -s` before applying anything and inspects that collateral. Dependencies apt pulls in or drops on its own are apt doing its job and are not shown to you. But if the collateral would remove or downgrade a package you installed by hand — one in either machine's `apt-mark showmanual` set, the source's or the target's — that becomes its own review item with an install-anyway / skip / abort choice. Protecting the union of both manual sets closes the case where a package is hand-installed on one machine but auto-resolved on the other. The classification happens during the review, never mid-apply, so you are never prompted while changes are landing.

## Machine-specific packages

Choosing **skip always** on a review item marks that package as belonging to *this specific machine* — the one running as source or target right now. A machine-specific package is never synced out to peers when this machine is the source, and never installed or removed here by a sync arriving from another machine. Use it for things tied to one box: a hardware driver, a vendor tool for an attached peripheral.

The mark is recorded in this machine's own decision file at `~/.config/pc-switcher/<manager>.decisions.yaml` (one per manager: `apt.decisions.yaml`, `snap.decisions.yaml`, `flatpak.decisions.yaml`). That file is **never synced** — it stays local to the machine it describes. An annotated example lives at [`src/pcswitcher/machine-packages.example.yaml`](../../src/pcswitcher/machine-packages.example.yaml).

To un-mark something, delete its entry from the decision file (or delete the whole file to clear every machine-specific decision for that manager). The next sync treats the item as live again and re-offers it in the review.

## Install snippets

Some installed things no package manager can reproduce — a bare `.deb` downloaded and installed by hand, or software dropped under `/usr/local` or `/opt` by an install script. `manual_installs_sync` detects these and surfaces them in its review as items needing a resolution. For each one the review offers three choices: add an install snippet, mark it machine-specific (skip always), or skip for now.

An install snippet is a shell command that reproduces the item — the tool never parses, interprets, or reasons about it. It is **stored and replayed verbatim**, and it runs **non-interactively**: no stdin is supplied during replay, so a command that prompts (for example a debconf question) fails rather than hanging the sync. A typical shape:

```bash
sudo DEBIAN_FRONTEND=noninteractive dpkg -i /path/to/package.deb || \
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -f
```

Snippets run **unprivileged**. On the target the body is replayed as `bash -c '<body>'` as the target user, with no outer `sudo` wrapped around it. Any privilege a snippet needs must be written inside the snippet by its author — that is why the example above calls `sudo` itself.

The snippet registry lives at `~/.config/pc-switcher/package-snippets.yaml`. Unlike the machine-local decision files, it **does** reach the target: how to install something is knowledge about the *package*, not the machine, so a snippet authored on one machine reproduces the same item on any peer. Whether an item counts as reproducible is decided by whether the **source** holds a snippet for it; a snippet present only on the target does not make the item reproducible.

### Registry push and consolidation

`manual_installs_sync` pushes the source's registry to the target as a **whole-file overwrite** — the source's `package-snippets.yaml` replaces the target's wholesale, no per-entry merge. Before the push, pc-switcher compares the two. A purely additive push (the source is a superset of the target) proceeds silently. But if the overwrite would **lose** an entry the target holds (absent from the source) or **change** one (a differing body), pc-switcher shows you exactly which entries and asks you to confirm. Declining aborts the run, and a non-interactive run that cannot ask aborts too — so you can consolidate the two registries by hand and re-run rather than silently dropping the target's snippets.

## Resolving unreproducible items

Every unreproducible item is resolved before the run continues: it gets a snippet, it is marked machine-specific (skip always), or you skip it once. There is no fourth "unresolved" outcome on an interactive run.

- **Ctrl-C / EOF** at the review means you want to stop, so it aborts the whole sync — never a silent per-item skip.
- Choosing "add an install snippet" and then submitting an **empty** body is not accepted: the review re-prompts the three-way choice rather than falling through. You must enter a real snippet or pick skip-once / skip-always.
- A **non-interactive** run (no TTY) cannot ask, so it marks every undecided item skip-once and reports them; it never records a snippet or a machine-specific mark. Re-run interactively to actually resolve anything.

## Versions

apt and flatpak let versions **float**. pc-switcher installs by name and takes whatever each machine's own repositories currently offer; a version difference between source and target is detected and reported in the review, never silently forced. (Deliberate pinning still replicates, because `/etc/apt/preferences.d` pin files sync as items like any other apt configuration.)

snap is the exception: it converges the source's exact **revision and channel**. The reason is where snap keeps per-user application data. snap stores it in revision-number-named directories, `~/snap/<app>/<rev>/`, whereas apt uses stable paths and flatpak uses id-named ones (`~/.var/app/<id>`). Only snap's data path embeds the version, so for `folder_sync` to mirror a snap's data cleanly both machines must be on the same revision — hence convergence.

With both machines on the same revision, snap application data now follows you: `folder_sync` mirrors the current-revision data directory (`~/snap/<app>/<current-rev>/`, resolved through snapd's `current` symlink) plus the revision-independent `~/snap/<app>/common`. Retained older-revision directories — revisions the target's snapd never installed — stay excluded to avoid leaving orphan data behind.

A snap installed from a local `.snap` file (`snap install --dangerous`, `snap try`) is the one thing snap sync leaves alone. Such a snap has a revision no store can serve — `snap list` shows it with an `x` prefix, `x1`, `x2` — and pc-switcher has no way to carry the file itself to the other machine. Sideloaded snaps on the source are therefore named in a warning and skipped: they produce no review item, and neither does a hold set on one. Reproducing one on the other machine is manual work. A sideloaded snap that exists only on the *target* is unaffected — it is still offered for removal like any other snap the source does not have.

To keep the revision from changing mid-sync, snapd's **automatic** refresh is briefly paused on both machines for the duration of the run (snapd auto-refreshes several times a day, even for closed apps). The pause blocks only automatic refreshes; snap_sync's own `--revision` convergence still works. Each machine's prior refresh policy is captured and restored when the run ends.

## Flatpak remotes

A flatpak remote is replicated as its own review item, per installation scope: `flathub` in the user installation and `flathub` in the system installation are two separate items, because flatpak configures them separately. Remotes always converge before the refs that come from them — `flatpak install` refuses outright when its remote is not configured in that scope.

A remote travels with its **trust**, not only its name and URL. pc-switcher captures whether the source verifies the remote's signatures and, when it does, the remote's own signing key, and re-adds the remote on the target with that key imported (`flatpak remote-add --gpg-import`). The key is copied byte-for-byte from the source machine and never fetched from a vendor — the same rule apt signing keys follow. Without it a replicated remote would be configured but unusable: every install from it fails with `Can't check signature: public key not found`. A remote the source itself does not verify is replicated unverified, stated as such in the review; a verified remote is never turned into an unverified one.

A remote that already exists on both machines but whose URL, verification setting or signing key differs is a **change** item that converges the target in place, keeping the refs that name it as their origin intact. A target that already trusted a different key for that remote ends up trusting both — flatpak merges imported keys rather than replacing them — so the difference is reported again on the next sync rather than the target's own trust being deleted.

## Holds and masks

Beyond *what is installed*, pc-switcher also replicates the deliberate **blocks** you set to stop a package from updating: apt holds (`apt-mark hold`), per-snap refresh holds (`snap refresh --hold`), and flatpak masks (`flatpak mask`). (apt version *pins* already travel — they are `/etc/apt/preferences.d` files that sync as apt configuration items.)

Each block is its own review item, distinct from the package it applies to. A held package and its hold are two separate lines in the review, each with the usual three-way choice — **apply** (make the target match the source), **skip this run**, or **skip always**. Adding a block (one present on the source but not the target) is checked by default. **Removing** a block — undoing one you set, present on the target but not the source — lands in its own removal group, **unticked**, so a bulk approval can never silently drop a block you meant to keep.

Replicating the block never touches the package's version. A held apt package is still never installed or upgraded by a sync — its version is left exactly as it is — and the hold itself now travels as its own item rather than only being reported alongside the package.

The review verbs match the mechanism: apt and snap holds read *hold* / *unhold*, flatpak masks read *mask* / *unmask*. flatpak masks are patterns, replicated whether or not a matching ref is installed; a pattern edit reads as remove-old plus add-new, and a user/system scope change as add plus remove, reported as found rather than normalised.

## Deletions

Removals propagate for the three package managers. A package removed from the source's `apt-mark showmanual` set, a snap uninstalled on the source, or a flatpak ref or remote removed on the source becomes a removal review item on the target — unticked by default, so you approve deletions deliberately.

A flatpak remote offered for removal names, in the review item's detail, the refs installed on the target that still have it as their origin in that same scope. The removal is still offered — deleting a remote whose refs are going in the same run is normal cleanup — but you see what it would orphan before approving it. Deleting a remote also drops its signing key, since flatpak stores that key with the remote.

`manual_installs_sync` is **install-only**: it has no target-side manifest of what it installed, so it never proposes removals. Removing a hand-installed item on the target is manual work today (tracking removal for manual installs is deferred to a future issue).

## Prerequisites: passwordless sudo

Each enabled package job needs passwordless sudo for a handful of binaries:

- **`apt_sync`** — on the source (to read `/etc/apt` configuration) and the target (to install packages, write `/etc/apt` configuration, and set or clear apt holds via `apt-mark`).
- **`snap_sync`** — on both the **source** and the **target**; validation fails if either lacks it. The target needs it to install, refresh and remove snaps, and both hosts need it to pause snapd's auto-refresh for the sync window (`sudo snap set system refresh.hold`). The runtime pause itself tolerates a transient failure without aborting the sync, but the sudo grant is checked up front on both machines.
- **`flatpak_sync`** — on the target only, and only when the diff involves a system-scope item (a system-scope ref, remote, or mask). User-scope masks need no sudo.
- **`manual_installs_sync`** — a snippet author decides its own privilege needs; the replay itself runs unprivileged, so the job requires no sudo beyond what a given snippet writes for itself.
