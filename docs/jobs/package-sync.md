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

- **`apt_sync`** — the manually-installed apt package set (`apt-mark showmanual`, not the full dpkg selection — apt resolves dependencies on the target itself), minus the packages you installed from a hand-downloaded `.deb` (see below), plus the repository configuration that governs where packages come from: sources under `/etc/apt/sources.list.d`, pins (`/etc/apt/preferences.d`) and apt config (`/etc/apt/apt.conf.d`). Signing keys travel too, but you are never asked about them — see [Signing keys](#signing-keys).
- **`snap_sync`** — installed snaps, converged to the source's exact revision and tracking channel.
- **`flatpak_sync`** — installed flatpak refs and their remotes, per user/system installation scope.
- **`manual_installs_sync`** — everything no package manager can reproduce: apt packages installed from no configured repository (a `.deb` you installed by hand), plus unowned files under `/usr/local` and `/opt`. It also owns the [install-snippet registry](#install-snippets).

### Hand-installed `.deb` packages belong to one job only

A package whose installed version comes from no repository your machine has configured was put there with `dpkg --install`. It is `manual_installs_sync`'s territory exclusively: `apt_sync` detects the same packages, with the same test, and drops them from its manifest before it diffs anything. They produce no apt item, no review entry and no install.

There is nothing apt could do with them anyway. The target's apt has never heard the name, so offering it as an ordinary install would fail with "Unable to locate package" — while `manual_installs_sync` offers the same package as an [install snippet](#install-snippets) in the same run. Only one of the two answers works, so only one job asks.

The consequence is worth knowing: the two jobs have separate enable flags, and `apt_sync` does not consult `manual_installs_sync`'s. **If you enable `apt_sync` but disable `manual_installs_sync`, your hand-installed `.deb` packages are synced by nobody** — they are silently absent from the review rather than offered as installs that fail. Keep `manual_installs_sync` enabled if you install software from downloaded `.deb` files.

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

You give those answers with two lists per group, not with a question per item. The first list is the apply list: ticked means apply. Whatever you leave unticked is then offered once more — "never offer again on this machine?" — and ticking it there is skip-always. Ticking nothing on that second list (just pressing Enter) is skip-this-run, so the items come back next sync. If you ticked everything for apply, the second list is not shown at all. Ctrl-C at either list aborts the whole sync.

Items that only **report** a condition are not offered skip-always: a version difference between source and target, an apt package whose repository cannot be reproduced on the target, an apt package the two machines installed from different vendors, and the pin echo on a held or pinned package. These change nothing on the target, and neither machine "holds" the item in the way a machine-specific mark requires — marking a version difference would silently stop the package syncing altogether rather than stop reporting the drift. Resolve them by fixing the underlying condition (align the versions, restore the repository on the source, pick one vendor, remove the pin).

### A second apt review, when this run changed `/etc/apt`

`apt_sync` reads the target's pins and asks its apt what it can install while it builds the review — and then, in the same run, rewrites `/etc/apt`. Both of those facts can be false by the time packages are converged: a pin you just deleted was still suppressing its packages when the list was drawn, and a repository you just installed can supply a package apt had no candidate for.

So `apt_sync` converges the repository configuration first, re-reads the target it has just produced, and — if that genuinely changed anything — shows you one more screen with what it revealed. Its groups are marked "(revealed by this run's /etc/apt changes)". Everything you already answered stands; only the newly-actionable items are asked about. Approvals the new state contradicts go the other way and are simply dropped: if a pin file you installed now governs a package you had approved for removal, that removal is abandoned without another question.

A run that changes nothing under `/etc/apt` never shows a second screen, and neither does a dry run — nothing is written, so nothing is invalidated. A dry-run preview therefore shows the pre-repository classification of packages, which is the one place this staleness is still visible.

### Where an apt package comes from

A package is replicated as *name and origin*, never name alone. One name is often offered by two vendors — `firefox` is Mozilla's build on your source and Ubuntu's snap-transition package in the archive — and Ubuntu's copy carries epoch 1, which outranks every unpinned vendor version, so matching on the name would replicate the name and invert the provenance.

So just before installing, once the run has refreshed the target's package lists, `apt_sync` asks the target's apt where each approved install would actually come from. If none of those places is a place the source has the package from, the install is refused and reported with both origins named — never installed from the other vendor. Only that package fails; the rest of the run continues. Packages your source gets from the Ubuntu archive itself are exempt, so two machines on different Ubuntu mirrors do not read as two vendors.

### Confirming every individual command

The batched review approves *items*, not commands. One ticked line can expand into several: an apt package is an `apt-get --dry-run` simulation then an `apt-get install`; an apt repository file is a backup, an upload, a `sudo install` promotion and an `apt-get update`.

`pc-switcher sync <target> --confirm-each-command` inserts one prompt before every one of them, showing the exact command (or, for a file transfer, the source and destination paths) and waiting for **p** to proceed or **a** to abort the whole sync. There is no "skip this one": a single reviewed item can span several commands, so skipping one would leave that item half-applied. An unanswerable prompt (Ctrl-C, EOF) aborts.

It covers every write the four jobs make, plus the machine-local decision files on both machines, the snippet registry and its push, the snapd auto-refresh pause and restore, and the sync-history update on both ends. Read-only commands are never gated. The flag needs a real terminal and is refused without one. It is meant for auditing or debugging a run you do not trust yet, not for everyday syncs.

### apt collateral

When you approve an apt change, apt sometimes has to remove or downgrade *other* packages to satisfy it — so the package you approved is not always the whole transaction. `apt_sync` simulates every approved change with `apt-get --dry-run` before applying anything and inspects that collateral. Dependencies apt pulls in or drops on its own are apt doing its job and are not shown to you. But if the collateral would remove or downgrade a package you installed by hand on the target — one in the target's own `apt-mark showmanual` set — that becomes its own review item with an install-anyway / skip / abort choice. The source's manual set is not consulted, which gives up one case on purpose: a package you installed by hand on the source, which arrived on the target as an automatic dependency, can be removed as collateral without asking you. If the target's apt installed it automatically, the target's apt owns it, and that is also the set apt itself consults when deciding what it may remove. The classification happens during the review, never mid-apply, so you are never prompted while changes are landing.

## Signing keys

You think in repositories and packages. A signing key is just how a repository is made to work, so pc-switcher never asks you about one: no key gets a review line of its own, and no key can be marked machine-specific. It keeps them correct on its own.

Not asked about is not the same as hidden. A repository offered for install or change names, in its own review line, the keys approving it would copy — so you see the files that would land in `/etc/apt` before you tick it, and `--dry-run` reports them too. A repository whose key the target already has, byte-identical, names nothing: there is no write to report.

When a repository is installed or changed on the target, the keyring it names arrives first — copied byte-for-byte from the source machine, never downloaded from a vendor. The same check runs for every repository that is *already* on the target: if the key on the source machine has different bytes, the target's copy is refreshed. That is what makes a **rotated** key follow you. A vendor replacing its signing key changes no `.sources` file, so nothing in the review would ever mention it, and the target's apt would start failing that repository's signature check until you noticed by hand. A key that already matches is left alone entirely — no transfer, no command.

Keys are looked for in three places: `/etc/apt/keyrings`, `/etc/apt/trusted.gpg.d` and `/usr/share/keyrings`. The last one matters more than its name suggests — it is where `add-apt-repository`, Ubuntu's own `ubuntu.sources` and most vendor `.deb`s put the key their `Signed-By:` line points at.

There is one thing pc-switcher will not overwrite: a keyring the target already has that the target's own package manager owns. That file belongs to a package installed on that machine, and replacing a distro keyring is not a sync's job. Ownership only stops the *overwrite*, though — a keyring the target is **missing** is always copied, even one a package owns. Some vendors ship a `.deb` that carries both the repository entry and the key that trusts it, so refusing to copy an owned key would leave that repository permanently untrustable and the package permanently uninstallable.

A repository whose `Signed-By:` carries the key **inline** — the armored block written straight into the `.sources` file, which is what `add-apt-repository` does for a PPA — needs no keyring at all. The key travels inside the file, so nothing is copied and nothing is missing.

When you approve removing a repository, the keyring it was the last user of goes with it. That count is taken *after* the repository is actually gone, against the real state of the target, so it gets the awkward cases right: a repository you left unticked still counts, one you marked machine-specific still counts, and so does `/etc/apt/sources.list`, which pc-switcher never syncs at all. Nothing is deleted unless the source machine has dropped that key too. If you remove no repository in a run, nothing is collected.

Only `/etc/apt/keyrings` is ever cleaned up. Keys in `/etc/apt/trusted.gpg.d` are *ambient* trust — no repository names them, so there is no way to tell which one is still doing a job — and `/usr/share/keyrings` is package territory. pc-switcher copies from both and deletes from neither; those keys are allowed to accumulate rather than be removed on a guess.

If a repository on the source machine names a keyring that machine does not actually have, that is a fact about a **repository** and you do see it: the repository is reported rather than offered for install, naming the missing key. It is never written to the target without its key — a repository apt cannot verify is worse than no repository.

Each key write and deletion is still a real command, so `--confirm-each-command` shows every one of them.

## Machine-specific packages

Choosing **skip always** on a review item marks that package as belonging to *this specific machine* — the one running as source or target right now. A machine-specific package is never synced out to peers when this machine is the source, and never installed or removed here by a sync arriving from another machine. Use it for things tied to one box: a hardware driver, a vendor tool for an attached peripheral.

The mark is recorded in this machine's own decision file at `~/.config/pc-switcher/<manager>.decisions.yaml` (one per manager: `apt.decisions.yaml`, `snap.decisions.yaml`, `flatpak.decisions.yaml`). That file is **never synced** — it stays local to the machine it describes. An annotated example lives at [`src/pcswitcher/machine-packages.example.yaml`](../../src/pcswitcher/machine-packages.example.yaml).

To un-mark something, delete its entry from the decision file (or delete the whole file to clear every machine-specific decision for that manager). The next sync treats the item as live again and re-offers it in the review.

A machine-specific apt package never appears in a review again, so an apt repository offered for **removal** names it explicitly — see [Deletions](#deletions).

## Install snippets

Some installed things no package manager can reproduce — a bare `.deb` downloaded and installed by hand, or software dropped under `/usr/local` or `/opt` by an install script. `manual_installs_sync` detects these and surfaces them in its review as items needing a resolution. For each one the review offers three choices: add an install snippet, mark it machine-specific (skip always), or skip for now.

An install snippet is a shell command that reproduces the item — the tool never parses, interprets, or reasons about it. It is **stored and replayed verbatim**, and it runs **non-interactively**: no stdin is supplied during replay, so a command that prompts (for example a debconf question) fails rather than hanging the sync. A typical shape:

```bash
sudo DEBIAN_FRONTEND=noninteractive dpkg --install /path/to/package.deb || \
sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --fix-broken
```

Snippets run **unprivileged**. On the target the body is replayed as `bash -c '<body>'` as the target user, with no outer `sudo` wrapped around it. Any privilege a snippet needs must be written inside the snippet by its author — that is why the example above calls `sudo` itself.

The snippet registry lives at `~/.config/pc-switcher/package-snippets.yaml`. Unlike the machine-local decision files, it **does** reach the target: how to install something is knowledge about the *package*, not the machine, so a snippet authored on one machine reproduces the same item on any peer. Whether an item counts as reproducible is decided by whether the **source** holds a snippet for it; a snippet present only on the target does not make the item reproducible.

### Registry push and consolidation

`manual_installs_sync` pushes the source's registry to the target as a **whole-file overwrite** — the source's `package-snippets.yaml` replaces the target's wholesale, no per-entry merge. Before the push, pc-switcher compares the two. A purely additive push (the source is a superset of the target) proceeds silently. But if the overwrite would **lose** an entry the target holds (absent from the source) or **change** one (a differing body), pc-switcher shows you exactly which entries and asks you to confirm. Declining aborts the run, and a non-interactive run that cannot ask aborts too — so you can consolidate the two registries by hand and re-run rather than silently dropping the target's snippets.

## Resolving unreproducible items

Every unreproducible item is resolved before the run continues: it gets a snippet, it is marked machine-specific (skip always), or you skip it once. There is no fourth "unresolved" outcome on an interactive run.

- **Ctrl-C** at the review means you want to stop, so it aborts the whole sync — never a silent per-item skip.
- Choosing "add an install snippet" and then submitting an **empty** body is not accepted: the review re-prompts the three-way choice rather than falling through. You must enter a real snippet or pick skip-once / skip-always.
- A **non-interactive** run (no TTY) cannot ask, so it marks every undecided item skip-once and reports them; it never records a snippet or a machine-specific mark. Re-run interactively to actually resolve anything.

## Versions

apt and flatpak let versions **float**. pc-switcher installs by name and takes whatever each machine's own repositories currently offer; a version difference between source and target is detected and reported in the review, never silently forced. (Deliberate pinning still replicates, because `/etc/apt/preferences.d` pin files sync as items like any other apt configuration.)

snap is the exception: it converges the source's exact **revision and channel**. The reason is where snap keeps per-user application data. snap stores it in revision-number-named directories, `~/snap/<app>/<rev>/`, whereas apt uses stable paths and flatpak uses id-named ones (`~/.var/app/<id>`). Only snap's data path embeds the version, so for `folder_sync` to mirror a snap's data cleanly both machines must be on the same revision — hence convergence.

With both machines on the same revision, snap application data now follows you: `folder_sync` mirrors the current-revision data directory (`~/snap/<app>/<current-rev>/`, resolved through snapd's `current` symlink) plus the revision-independent `~/snap/<app>/common`. Retained older-revision directories — revisions the target's snapd never installed — stay excluded to avoid leaving orphan data behind.

A snap installed from a local `.snap` file (`snap install --dangerous`, `snap try`) is the one thing snap sync leaves alone. Such a snap has a revision no store can serve — `snap list` shows it with an `x` prefix, `x1`, `x2` — and pc-switcher has no way to carry the file itself to the other machine. Sideloaded snaps on the source are therefore named in a warning and skipped: they produce no review item, and neither does a hold set on one. Reproducing one on the other machine is manual work. A sideloaded snap that exists only on the *target* is unaffected — it is still offered for removal like any other snap the source does not have.

To keep the revision from changing mid-sync, snapd's **automatic** refresh is briefly paused on both machines for the duration of the run (snapd auto-refreshes several times a day, even for closed apps). The pause blocks only automatic refreshes; snap_sync's own `--revision` convergence still works. Each machine's prior refresh policy is read before the pause and written back when the run ends, so a hold you set yourself — including an indefinite one — survives the sync. If that prior value cannot be read on a machine, its refresh policy is left untouched rather than cleared; the pause pc-switcher set expires on its own a few hours later.

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

An apt repository file offered for removal does the same for **machine-specific** packages: its detail names the packages you marked skip-always on this machine that are installed from that repository. The removal is still offered and still unticked — you decide. This matters because a machine-specific package is invisible in the review by design: it is filtered out before any diff is computed, so nothing else in the run would tell you the repository feeding it is about to go.

The link comes from `apt-cache policy`: pc-switcher matches the origin of each machine-specific package's installed version against the URIs in the repository files. A package installed from a bare `.deb`, or one whose repository was already gone, has no resolvable origin and is not named. Ordinary (non-machine-specific) packages are out of scope — they can still surface as removal items of their own, and naming every installed package from, say, the Ubuntu archive would list hundreds.

`manual_installs_sync` is **install-only**: it has no target-side manifest of what it installed, so it never proposes removals. Removing a hand-installed item on the target is manual work today (tracking removal for manual installs is deferred to a future issue).

## Prerequisites: passwordless sudo

Each enabled package job needs passwordless sudo for a handful of binaries:

- **`apt_sync`** — on the source (to read `/etc/apt` configuration) and the target (to install packages, write and remove `/etc/apt` configuration including signing keys, and set or clear apt holds via `apt-mark`).
- **`snap_sync`** — on both the **source** and the **target**; validation fails if either lacks it. The target needs it to install, refresh and remove snaps, and both hosts need it to pause snapd's auto-refresh for the sync window (`sudo snap set system refresh.hold`, plus the matching `sudo snap get` — snapd requires admin rights to read snap configuration as well as to write it). The runtime pause itself tolerates a transient failure without aborting the sync, but the sudo grant is checked up front on both machines.
- **`flatpak_sync`** — on the target only, and only when the diff involves a system-scope item (a system-scope ref, remote, or mask). User-scope masks need no sudo.
- **`manual_installs_sync`** — a snippet author decides its own privilege needs; the replay itself runs unprivileged, so the job requires no sudo beyond what a given snippet writes for itself.
