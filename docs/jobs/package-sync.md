# Package Sync

The package jobs replicate *what is installed* — apt packages plus the `/etc/apt` repository configuration they depend on, snaps, flatpaks, and the things no package manager can reproduce — rather than user data. Package *data* (`~/.var/app`, `~/snap/<app>/common`, dotfiles) stays `folder_sync`'s territory. The package sync jobs are about the presence, version and provenance of the packages themselves.

Configuration for these jobs is limited to their `sync_jobs` enable flags; see the [configuration reference](../configuration.md#sync_jobs). There are no per-job config keys.

For what these jobs are for and why they behave as they do, see [Package sync — user requirements](../planning/package-sync-user-requirements.md); for the same requirements as checkable articles — the `PKG-FR-*` obligations per ecosystem and the `PKG-NG-*` non-goals — see [Package sync conformance criteria](../planning/package-sync-conformance-criteria.md). Where this page disagrees with either, this page is wrong.

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

- **`apt_sync`** — the manually-installed apt package set (`apt-mark showmanual`, not the full dpkg selection — apt resolves dependencies on the target itself), minus the packages you installed from a hand-downloaded `.deb` (see below), plus the `/etc/apt` configuration that governs where packages come from. Only two things under `/etc/apt` are reviewed: `apt.conf.d` files, in all three directions, and the deletion of a repository or pin file the source no longer has. Repository files under `sources.list.d`, their signing keys, and pins under `preferences.d` are derived from the packages you approve and never get a review row — see [Repositories, pins and keys are derived](#repositories-pins-and-keys-are-derived).
- **`snap_sync`** — installed snaps, converged to the source's exact revision and tracking channel.
- **`flatpak_sync`** — installed flatpak refs, per user/system installation scope, plus the remotes those refs are derived to need.
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

Every screen names the two machines by their **hostnames** — `atlas`, `nomad` — never as "the source" and "the target". Those are the tool's names for the two ends of a run, and the question a review asks is always about one of your computers: which machine loses the package, which machine's version of a file wins, which machine an install snippet runs on.

The review lists every difference the job found between the two machines, grouped by action, and installs are always kept separate from removals: a group that would install software is never mixed with one that would remove it, and a removal group names the removal explicitly (for example "Remove packages") rather than saying "apply". Removal rows start at **skip now**, so a bulk approval can never silently delete something.

Most items that would actually change something — packages, holds, masks and `apt.conf.d` files — offer the same three-way choice:

- **Apply** it — make this change on the target.
- **Skip now** — leave it alone this sync; it comes back next sync. The repository-conflict screen is the one exception to the wording, because there the answer picks between two versions of one file: it reads `keep <target>'s version`.
- **Skip for good** — mark it as belonging to one machine only, so no future sync touches it and you are not asked again (see [Machine-specific packages](#machine-specific-packages)). Said as this screen's own act: `never install` where the item is not on the target, `keep for good` where it already is.

You give those answers on **one screen per group**, not with a question per item and not in two passes. Every item is a row; the decision it currently carries is shown in a column to the right of the longest item; the arrow keys move between rows — the first and last rows are walls, not a way round to the other end — and one key sets the focused row:

- `<y>` — apply, shown in the column as the group's own verb (`install`, `remove`, `overwrite`, …)
- `<s>` — `skip now`
- `<x>` — skip for good, as a cross for "exclude". Not `<n>`, which beside `<y>` reads as a plain "no" and would invite "not now" from the one answer that is permanent
- `<space>` steps the focused row through the answers, and the shift of any key sets **every** row at once
- `<enter>` confirms the whole screen; `<ctrl-c>` aborts the whole sync (the screen does not offer this — it is not one of the answers)

Each answer is listed with a sentence of its own, naming the machine it happens to and how long it lasts, because the column word is too short to say either:

```plain
? Remove apt packages
  <y> remove          go ahead — nomad changes this sync
  <s> skip now        leave nomad alone; you are asked again next sync
  <x> keep for good   nomad's own — keep it, and never be asked again
  <space> cycles   <shift+key> sets every row   <enter> confirm

 » ● fortunes-min  remove
   ○ cowsay        skip now
   ⊘ sl            keep for good
```

A screen that takes only two answers simply does not offer `<x>`. Some questions come one item per screen rather than as a list: a repository or pin file being deleted, a repository whose two versions differ, a collateral package, and an unreproducible item. Each of those has something to show you first — a file body, two file bodies, or what a change would do to a package you installed yourself — and the decision follows the thing it is about. The answered list stays on screen afterwards, which is the record of what you decided — nothing is echoed back at you.

Four things take **two** answers instead — act, or leave it for now, with nothing recorded either way: deleting an apt repository file, deleting an apt pin file, overwriting a repository file the two machines disagree about, and deleting or repointing a flatpak remote. See [Deletions](#deletions) and [Flatpak remotes](#flatpak-remotes).

Items that only **report** a condition are not asked about at all — they are printed, grouped by what the condition IS, and the review moves on. Neither answering nor declining would change anything on either machine or be remembered, so there is nothing to answer. The groups are `Version differences`, `Installed from different repositories` and `Origins <target> cannot reproduce`. The conditions: a version difference between the two machines (named as `atlas has 1.0-1, nomad has 2.0-1`), an apt package whose repository cannot be reproduced on the target, and an apt package the two machines installed from different repositories. These change nothing on the target, and neither machine "holds" the item in the way a machine-specific mark requires — marking a version difference would silently stop the package syncing altogether rather than stop reporting the drift. Resolve them by fixing the underlying condition (align the versions, restore the repository on the source, pick one repository). A version difference is the one that resolves itself — its report says so, naming the upgrade command to run.

### One review per job, and nothing applied you did not approve

`apt_sync` batches its questions rather than interrupting you package by package: it asks everything it can ask before it writes anything, and it never applies something you did not approve. In practice one review is all it needs.

That is because of what the review decides. A package is judged by where your **source** installed it from, and a sync never changes the source — so nothing the run does to the target can make an answer you already gave wrong. The one fact that does depend on what the run wrote, namely which repository actually ends up supplying a package, is not guessed during the review at all: it is measured afterwards, and a package that comes out wrong is refused and reported rather than turned into another question.

A pin file says nothing about the packages it names, either. Pins travel — they are files under `/etc/apt/preferences.d`, and they are what make one repository's build outrank the Ubuntu archive's — but they get no line of their own on a package. A package named by a pin is reviewed like any other: if it is on the target and not on the source, it is offered for removal, and you can mark it machine-specific.

### Confirming every individual command

The batched review approves *items*, not commands. One approved line can expand into several: an apt package is an `apt-get --dry-run` simulation then an `apt-get install`; an apt repository file is a backup, an upload, a `sudo install` promotion and an `apt-get update`.

`pc-switcher sync <target> --confirm-each-command` inserts one prompt before every one of them, showing the exact command (or, for a file transfer, the source and destination paths) and waiting for **p** to proceed or **a** to abort the whole sync. There is no "skip this one": a single reviewed item can span several commands, so skipping one would leave that item half-applied. An unanswerable prompt (Ctrl-C, EOF) aborts.

It covers every write the four jobs make, plus the machine-local decision files on both machines, the snippet registry and its push, the snapd auto-refresh pause and restore, and the sync-history update on both ends. Read-only commands are never gated. The flag needs a real terminal and is refused without one. It is meant for auditing or debugging a run you do not trust yet, not for everyday syncs.

### apt collateral

When you approve an apt change, apt sometimes has to remove or downgrade *other* packages to satisfy it — so the package you approved is not always the whole transaction. `apt_sync` simulates every approved change with `apt-get --dry-run` before applying anything and inspects that collateral. Dependencies apt pulls in or drops on its own are apt doing its job and are not shown to you. But if the collateral would remove or downgrade a package you installed by hand on the target — one in the target's own `apt-mark showmanual` set — that becomes its own review item, on a decision screen of its own — one package per screen, because each one's cause and effect differ and the answers name them. The row states what the approved change would do to it (`Installing sl on nomad would remove fortunes`) and then why this package is protected at all (apt on that machine has it marked manually installed). Three answers, each naming this package's own change:

- `<y>` — the act, named as what happens to the package: `remove` or `downgrade`. Its line reads, for example, `install sl on nomad, so fortunes is removed as well`.
- `<s>` `skip now` — `keep fortunes on nomad; sl will not be installed; will be asked again next sync`. Everything else you approved is applied as you decided.
- `<q>` `stop the sync` — not just `apt_sync`. No further job runs, nothing more is changed on the target, and what jobs that already finished did stays done. `apt_sync` itself has changed nothing at this point: the review runs before its first mutating command.

This is **not** the machine-specific mark. Nobody recorded a preference about this package; the target's own apt simply says a person asked for it, which is a different fact. The source's manual set is not consulted, which gives up one case on purpose: a package you installed by hand on the source, which arrived on the target as an automatic dependency, can be removed as collateral without asking you. If the target's apt installed it automatically, the target's apt owns it, and that is also the set apt itself consults when deciding what it may remove. The classification happens during the review because apt's simulation already says what the real transaction will do, so you decide about the collateral while you are deciding about the change that causes it.

Skipping cancels only the changes that actually cause the collateral. The question names them, and everything else you approved in the same review is applied as you decided. Where no single change causes it on its own — apt keeps a package as long as either of two others is there, and drops it once both go — the question says "the packages listed earlier" and keeping the package cancels all of them, because that combination really is the cause. Skipping never rewrites an answer you gave: a package you marked never-offer-again keeps that mark even when the skip cancels it for this run.

One class of install cannot be classified that way and is deliberately left out of the plan-time simulation: a package whose repository this run is about to add on the target's behalf. Until that repository lands the target's apt has never heard the name, and apt refuses the whole simulated batch on one such name — which would strip the protection from every other package in the run rather than weaken it for one. Those packages are covered instead by the same simulation re-run per item after `/etc/apt` has converged, where apt can resolve them: unapproved manual collateral fails that one item. The cost is real — for those packages you are told afterwards rather than asked beforehand — and is accepted because the facts the question needs do not exist while the review is being built.

## Repositories, pins and keys are derived

You are asked about packages. The `/etc/apt` machinery a package needs to be installable — the repository file it comes from, the signing key that makes that repository trusted, the pin that makes that repository's build win — follows from your answer and gets no review line of its own. Approving a repository without its package does nothing; approving a package without its repository cannot be installed; the pairing was never expressible as two review rows.

A package is replicated as name **and** origin. If the source installed `gh` from `cli.github.com`, the target gets it from `cli.github.com` or not at all, and the review line names the repository when it is not the distribution's own archive. Approving that line is what carries that repository's file, its key and the source's pin files across. If no repository file on the source declares the origin, or every file that does names a key the source machine does not have, the package is reported instead of installed — never satisfied from a different repository.

After the run's single metadata refresh and before its first install, the target's real candidate origins are read back for every approved install whose origin is not the distribution's own archive. If that repository's build is still not what the target would install, that install is refused as its own item naming both origins and the rest of the run continues. That check is the guarantee; everything before it is preparation.

Pins are the reason the check can fail even when the repository landed. Ubuntu's own `firefox` is version `1:1snap1-0ubuntu5`, and that epoch outranks every epoch-free Mozilla version at equal priority — so adding Mozilla's repository alone still installs Ubuntu's package, and only Mozilla's pin file changes the outcome. Every `/etc/apt/preferences.d` file the source has is therefore written to the target when missing and overwritten when different, always and silently. A pin naming an origin the target does not have is inert, so that costs nothing. The price is that a pin file you wanted on one machine only comes back every run; deleting it on the source is the only way to stop that.

The distribution's own files — `ubuntu.sources`, `/etc/apt/sources.list`, `ubuntu-esm-apps.sources` and `ubuntu-esm-infra.sources` — are written when missing and overwritten when different, and are never removed or offered for removal. They are what defines "the distribution's own origin" on each machine, which is what stops two machines on different Ubuntu mirrors from disagreeing about every package. Files apt itself does not read are ignored: only `.list` and `.sources` under `sources.list.d` are ever captured, compared or written, so the `.save` and `.orig` copies apt tooling leaves behind are left alone.

A repository present on both machines with different content is overwritten with the source's version silently — unless it feeds a package you marked machine-specific on the target, in which case you are shown both file contents whole, side by side and never as a diff, and asked to overwrite or leave it for now. Leaving it fails every approved package whose origin depended on that file, by name, rather than installing it from somewhere else.

### Ubuntu Pro and ESM

The two `ubuntu-esm-*` files are part of the distribution set, so they would be written to a target that lacks them. `esm.ubuntu.com` serves its repository *index* publicly, so an unattached target's `apt-get update` still succeeds and the ESM versions win candidate selection — only the package pool is behind the 401. The failure lands later, at install time, on a package nobody will connect to the sync.

pc-switcher cannot fix that itself: attaching needs a subscription token from your Pro dashboard or an interactive browser flow, the source machine's own credentials are root-only and not reusable for another machine, and holding a token would put a secret on a command line.

So before `apt_sync` writes anything, it probes the target and — if the target reports no attachment — asks, with exactly two answers:

- **I have attached `<target>` — check again and continue.** pc-switcher probes again rather than trusting the answer. You can answer this as many times as you like; re-probing is free.
- **Skip `apt_sync` this run (every other job still runs).** The target's `/etc/apt` is left exactly as it was and every other job runs normally.

The commands the prompt gives you, to run on the target, are `sudo pro attach <token>` followed by `sudo pro enable esm-apps esm-infra`, and it links Ubuntu's own tutorial — [Attach a machine to your subscription](https://documentation.ubuntu.com/pro/attach-tutorial/) — which stays current if the procedure changes.

Skipping costs the whole apt job, not just the two files, and that is deliberate: pin files always travel, so a pin the source has and the target lacks would reach the target whether or not the sources it names did, leaving a candidate selection matching neither machine. A run with nobody to ask takes the skip too. A dry run never asks — it warns that the target is unattached and that a real run would skip `apt_sync` entirely.

Only the yes/no attachment answer is ever logged or shown. The probe's own output names the subscriber's account and never leaves the check.

### Signing keys

You think in repositories and packages. A signing key is just how a repository is made to work, so pc-switcher never asks you about one: no key gets a review line of its own, and no key can be marked machine-specific. It keeps them correct on its own.

Not asked about is not the same as hidden. Every derived write is logged as it lands and previewed under `--dry-run`, so you see what reached `/etc/apt` — it is simply not a question.

When a repository is written to the target, the keyring it names arrives first — copied byte-for-byte from the source machine, never downloaded from the network. The same check runs for every repository that is *already* on the target: if the key on the source machine has different bytes, the target's copy is refreshed. That is what makes a **rotated** key follow you. A repository replacing its signing key changes no `.sources` file, so nothing in the review would ever mention it, and the target's apt would start failing that repository's signature check until you noticed by hand. A key that already matches is left alone entirely — no transfer, no command.

Keys are looked for in three places: `/etc/apt/keyrings`, `/etc/apt/trusted.gpg.d` and `/usr/share/keyrings`. The last one matters more than its name suggests — it is where `add-apt-repository`, Ubuntu's own `ubuntu.sources` and most third-party `.deb`s put the key their `Signed-By:` line points at.

There is one thing pc-switcher will not overwrite: a keyring the target already has that the target's own package manager owns. That file belongs to a package installed on that machine, and replacing a distro keyring is not a sync's job. Ownership only stops the *overwrite*, though — a keyring the target is **missing** is always copied, even one a package owns. Some repositories ship a `.deb` that carries both the repository entry and the key that trusts it, so refusing to copy an owned key would leave that repository permanently untrustable and the package permanently uninstallable.

A repository whose `Signed-By:` carries the key **inline** — the armored block written straight into the `.sources` file, which is what `add-apt-repository` does for a PPA — needs no keyring at all. The key travels inside the file, so nothing is copied and nothing is missing.

When you approve removing a repository, the keyring it was the last user of goes with it. That count is taken *after* the repository is actually gone, against the real state of the target, so it gets the awkward cases right: a repository you left skipped still counts, one you marked machine-specific still counts, and so does `/etc/apt/sources.list`, which pc-switcher never syncs at all. Nothing is deleted unless the source machine has dropped that key too. If you remove no repository in a run, nothing is collected.

Only `/etc/apt/keyrings` is ever cleaned up. Keys in `/etc/apt/trusted.gpg.d` are *ambient* trust — no repository names them, so there is no way to tell which one is still doing a job — and `/usr/share/keyrings` is package territory. pc-switcher copies from both and deletes from neither; those keys are allowed to accumulate rather than be removed on a guess.

If a repository on the source machine names a keyring that machine does not actually have, no package can be replicated through it: every package whose origin that repository declares is reported instead, naming the origin and the missing key. The repository is never written to the target without its key — a repository apt cannot verify is worse than no repository.

A derived write that fails has no review line of its own to fail. The failure is charged to every approved package whose origin depended on it, naming the file and the reason — you decided about a package, not about a file. A rollback of the whole `/etc/apt` group does the same to all of them.

Everything under `/etc/apt` that a run writes or deletes is backed up first, applied, and followed by exactly one `apt-get update`. If that refresh fails, every file the group touched is restored and the target's `/etc/apt` is left as it was found.

Each key write and deletion is still a real command, so `--confirm-each-command` shows every one of them.

## Machine-specific packages

Choosing **skip always** on a review item marks that package as belonging to *this specific machine* — the one running as source or target right now. A machine-specific package is never synced out to peers when this machine is the source, and never installed or removed here by a sync arriving from another machine. Use it for things tied to one box: a hardware driver, a tool for an attached peripheral.

The mark is recorded in this machine's own decision file at `~/.config/pc-switcher/<manager>.decisions.yaml` (one per manager: `apt.decisions.yaml`, `snap.decisions.yaml`, `flatpak.decisions.yaml`). That file is **never synced** — it stays local to the machine it describes. An annotated example lives at [`src/pcswitcher/machine-packages.example.yaml`](../../src/pcswitcher/machine-packages.example.yaml).

To un-mark something, delete its entry from the decision file (or delete the whole file to clear every machine-specific decision for that manager). The next sync treats the item as live again and re-offers it in the review.

A machine-specific apt package never appears in a review again, so an apt repository offered for **removal** names it explicitly — see [Deletions](#deletions).

## Install snippets

Some installed things no package manager can reproduce — a bare `.deb` downloaded and installed by hand, or software dropped under `/usr/local` or `/opt` by an install script. `manual_installs_sync` detects these and surfaces them in its review as items needing a resolution. Each gets a decision screen of its own — one item per screen, because answering `<y>` opens an editor for that item — with the review's usual three answers in the usual order:

- `<y>` `install` — write a command snippet that installs it; the target runs it, now and on every future sync.
- `<s>` `skip now` — the target does not get it this sync, and you are asked again next sync.
- `<x>` `never install` — this one is the source machine's own; the target never gets it and you are not asked again.

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

Every unreproducible item is resolved before the run continues: it gets a snippet, it is declared the source machine's own and never installed on the target, or you skip it now. There is no fourth "unresolved" outcome on an interactive run.

- **Ctrl-C** at the review means you want to stop, so it aborts the whole sync — never a silent per-item skip.
- Choosing "add an install snippet" and then submitting an **empty** body is not accepted: the review re-prompts the three-way choice rather than falling through. You must enter a real snippet or pick one of the two skips.
- A **non-interactive** run (no TTY) cannot ask, so it marks every undecided item skip-now and reports them; it never records a snippet or a machine-specific mark. Re-run interactively to actually resolve anything.

## When a package manager cannot be read

A package sync compares what the source has with what the target has. If one of those reads fails — snapd is not running, apt's status file cannot be read, a lock is held, the network dropped — the answer is not "that machine has nothing". pc-switcher stops the job and tells you which command failed and what the tool said, instead of proposing to remove everything the other machine has.

An empty *answer* is different and is left alone: a machine with no snaps, no flatpaks, no held packages and no pins is an ordinary machine, and syncing it is ordinary work.

Today a job stopping this way ends the whole sync, including jobs that had nothing to do with the failure. That is a known limitation and is being addressed separately.

## Non-interactive runs

A run without a TTY prompts for nothing, so every review item comes back skip-now and the job converges nothing. When the review had anything to offer, the job therefore reports **SKIPPED**, not SUCCESS, and the run continues with the remaining jobs. A run whose review was empty — the target already matches the source for that package manager — still reports SUCCESS: there was nothing to decide because there was nothing to do.

`apt_sync` has a second reason to report SKIPPED, and it applies to interactive runs too: the target reports no Ubuntu Pro attachment and the source carries ESM sources that would otherwise be written to it. Attach the target and re-run — `sudo pro attach <token from https://ubuntu.com/pro/dashboard>`, then `sudo pro enable esm-apps esm-infra`, both on the target — or answer the prompt's re-check once you have. See [Ubuntu Pro and ESM](#ubuntu-pro-and-esm).

A skipped package job applies nothing, records no decision, and pushes no install-snippet registry. The session still completes and the exit code is unchanged, so a headless run says plainly that it converged nothing rather than reporting four successful package syncs.

## Versions

apt and flatpak let versions **float**. pc-switcher installs by name and takes whatever each machine's own repositories currently offer; a version difference between source and target is detected and reported in the review, never silently forced. (Deliberate pinning still replicates: `/etc/apt/preferences.d` files always travel, without a review line — see [Repositories, pins and keys are derived](#repositories-pins-and-keys-are-derived).)

snap is the exception: it converges the source's exact **revision and channel**. The reason is where snap keeps per-user application data. snap stores it in revision-number-named directories, `~/snap/<app>/<rev>/`, whereas apt uses stable paths and flatpak uses id-named ones (`~/.var/app/<id>`). Only snap's data path embeds the version, so for `folder_sync` to mirror a snap's data cleanly both machines must be on the same revision — hence convergence.

With both machines on the same revision, snap application data now follows you: `folder_sync` mirrors the current-revision data directory (`~/snap/<app>/<current-rev>/`, resolved through snapd's `current` symlink) plus the revision-independent `~/snap/<app>/common`. Retained older-revision directories — revisions the target's snapd never installed — stay excluded to avoid leaving orphan data behind.

A snap installed from a local `.snap` file (`snap install --dangerous`, `snap try`) is the one thing snap sync leaves alone. Such a snap has a revision no store can serve — `snap list` shows it with an `x` prefix, `x1`, `x2` — and pc-switcher has no way to carry the file itself to the other machine. Sideloaded snaps on the source are therefore named in a warning and skipped: they produce no review item, and neither does a hold set on one. Reproducing one on the other machine is manual work. A sideloaded snap that exists only on the *target* is unaffected — it is still offered for removal like any other snap the source does not have.

To keep the revision from changing mid-sync, snapd's **automatic** refresh is briefly paused on both machines for the duration of the run (snapd auto-refreshes several times a day, even for closed apps). The pause blocks only automatic refreshes; snap_sync's own `--revision` convergence still works. Each machine's prior refresh policy is read before the pause and written back when the run ends, so a hold you set yourself — including an indefinite one — survives the sync. If that prior value cannot be read on a machine, its refresh policy is left untouched rather than cleared; the pause pc-switcher set expires on its own a few hours later.

## Flatpak refs

A flatpak app comes from the source's remote or it does not arrive. Same *name* is not enough: two remotes can be called `flathub` and point at different repositories, serving different builds of the same app with nothing said about it. So before each install pc-switcher re-reads the target's remote list and requires that app's origin remote to carry the source remote's URL and verification setting, and after the install it reads back what the app's origin actually resolves to. Either check failing is that app's own failure, naming both URLs — never an install issued in hope.

Those two checks guard an install, and an app already installed on both machines issues none — so the case they cannot see is reported instead. The same app, same scope, same branch, coming from different remotes on the two machines is a **provenance divergence**: the review names both remotes and both URLs and proposes nothing, and it takes precedence over a version difference on the same app, because two remotes' builds are numbered independently and showing the two numbers would read as ordinary drift. There is nothing to converge: flatpak refuses to install a ref that is already installed from another remote, so the only mechanical resolution would be uninstalling the app you have and reinstalling it from the other remote. Deciding which machine is the odd one out is yours.

Remotes are compared by URL here too, so a remote the two machines merely named differently is not a divergence, and two remotes sharing a name and pointing at different repositories is one. If a machine's app names a remote that machine no longer configures, there is no URL to compare and the names decide instead: two different names are still reported, two identical ones are left alone.

A flatpak app is identified by its full `<application>/<arch>/<branch>` reference, not by the bare application id, and that reference is what the install and the uninstall name. Two branches of one app can be installed side by side, and a remote can offer several — flatpak refuses to guess between them and exits with `Multiple branches available`, so an app whose remote carries more than one branch never converges when only the id is named. The review line therefore shows the branch, and the same app on `stable` on one machine and `beta` on the other reads as an install plus a removal rather than as a version difference.

## Flatpak remotes

A flatpak remote is **derived** from the apps approved from it, exactly as an apt repository is. You never approve a remote directly: approving an app is what makes its remote travel, and declining the app is the only way to decline the remote. That closes the pairing the old model made expressible — an app approved with the only thing that could deliver it declined, and worse, an app approved from a same-named remote whose URL change was declined, meaning from a different source.

A remote the source has that feeds no app approved in this run does not travel at all. There is no flatpak equivalent of the distribution's own repositories: a fresh flatpak install configures **zero** remotes and a machine with none is a perfectly ordinary machine, so even Flathub travels only as a consequence of something needing it.

Derivation includes the **runtime** an approved app is built against. The app's install pulls its runtime too, and if the source holds that runtime from a different remote, the app's own remote alone would leave the install unable to resolve it — so that remote travels as well.

Scope is still identity: `flathub` in the user installation and `flathub` in the system installation are provisioned separately, because flatpak configures them separately. A user-scope app derives only the user-scope remote.

A remote travels with its **trust**, not only its name and URL. pc-switcher captures whether the source verifies the remote's signatures and, when it does, the remote's own signing key, and provisions the remote on the target with that key imported (`flatpak remote-add --gpg-import`). The key is copied byte-for-byte from the source machine and never fetched from the network — the same rule apt signing keys follow. Without it a provisioned remote would be configured but unusable: every install from it fails with `Can't check signature: public key not found`. A remote the source itself does not verify is provisioned unverified; a verified remote is never turned into an unverified one.

A remote that already exists on both machines with a differing URL, verification setting or signing key is repointed in place, without a review line, keeping the apps that name it as their origin intact. A target that already trusted a different key for that remote ends up trusting both — flatpak merges imported keys rather than replacing them.

There is one exception, and it is apt's repository-conflict rule in a second ecosystem: if a differing URL or verification setting would repoint a remote that an app you marked machine-specific on the target takes as its origin in that scope, you are shown both configurations — the target's first, one differing field per line, never a computed diff — and asked to overwrite or leave it for now. A machine-specific app is invisible in the review by design, so nothing else in the run would tell you its updates were about to come from somewhere else. The entry names those apps. Two answers, nothing recorded either way; leaving it fails every approved app that needed the source's URL, quoting your own decision. A key-only difference never raises it: importing a key can neither move an app's origin nor withdraw trust.

A remote that cannot be provisioned has no review item of its own to fail, so the failure lands on every app that needed it, naming the remote and quoting flatpak's own error.

A remote's **filter** does not travel. `flatpak remote-modify --filter=<file>` records the file's path, not its content, and the file is ordinary local content at whatever path you gave it — so a remote you restricted on the source is provisioned **unfiltered** on the target and will offer apps the source hides. The run warns once per such remote, in a dry run too, and names the command that re-applies the filter there.

## Holds and masks

Beyond *what is installed*, pc-switcher also replicates the deliberate **blocks** you set to stop a package from updating: apt holds (`apt-mark hold`), per-snap refresh holds (`snap refresh --hold`), and flatpak masks (`flatpak mask`). (apt version *pins* already travel, as derived files rather than as items.)

Each block is its own review item, distinct from the package it applies to. A held package and its hold are two separate lines in the review, each with the usual three-way choice — **apply** (make the target match the source), **skip now**, or **skip for good**. Adding a block (one present on the source but not the target) is checked by default. **Removing** a block — undoing one you set, present on the target but not the source — lands in its own removal group starting at **skip now**, so a bulk approval can never silently drop a block you meant to keep.

Replicating the block never touches the package's version. A held apt package is still never installed or upgraded by a sync — its version is left exactly as it is — and the hold itself now travels as its own item rather than only being reported alongside the package.

The review verbs match the mechanism: apt and snap holds read *hold* / *unhold*, flatpak masks read *mask* / *unmask*. flatpak masks are patterns, replicated whether or not a matching ref is installed; a pattern edit reads as remove-old plus add-new, and a user/system scope change as add plus remove, reported as found rather than normalised.

## Deletions

Removals propagate for the three package managers. A package removed from the source's `apt-mark showmanual` set, a snap uninstalled on the source, or a flatpak ref or remote removed on the source becomes a removal review item on the target — starting at skip-this-run, so you approve deletions deliberately.

Removal is the one direction in which an apt repository file, an apt pin file and a flatpak remote are still review lines, and all three take **two** answers rather than three: delete it, or leave it on the target.

A repository file is named by its filename *and* by the repository URLs it declares — `nomad would stop getting software from https://cli.github.com/packages` — because the filename is whatever whoever created the file happened to call it, while the URL is what the deletion actually takes away. A file declaring no URL says so rather than trailing off.

A pin file is shown **whole**: its content is printed in a block above the screen, one block per file, the way the repository-conflict screen prints two. `99-vendor.pref` says nothing about which vendor it favours or by how much, and the filename is all a decision row can show. Reading it costs one `sudo cat` per pin file offered for deletion, and only on a run that offers one. There is no permanent answer — a machine-local mark on a file or a remote whose whole purpose is to feed packages would silently and permanently change where those packages come from, and the remedy for two machines whose configurations have drifted is consolidating them. Nothing about the answer is recorded either way. `/etc/apt/apt.conf.d` is the counter-case: it keeps the full three-way decision and the permanent mark, because a proxy or a recommends policy is a standing preference someone genuinely holds per machine.

The distribution's own source files are never offered for removal at all.

A flatpak remote offered for removal names, in the review item's detail, the refs installed on the target that still have it as their origin in that same scope. The removal is still offered — deleting a remote whose refs are going in the same run is normal cleanup — but you see what it would orphan before approving it. Deleting a remote also drops its signing key, since flatpak stores that key with the remote.

An apt repository file offered for removal does the same for **machine-specific** packages: after the URLs, its detail names the packages you marked as this machine's own that are installed from that repository, and says they would stay installed but never get another update. The removal is still offered and still starts at skip-now — you decide. This matters because a machine-specific package is invisible in the review by design: it is filtered out before any diff is computed, so nothing else in the run would tell you the repository feeding it is about to go.

The link comes from `apt-cache policy`: pc-switcher matches the origin of each machine-specific package's installed version against the URIs in the repository files. A package installed from a bare `.deb`, or one whose repository was already gone, has no resolvable origin and is not named. Ordinary (non-machine-specific) packages are out of scope — they can still surface as removal items of their own, and naming every installed package from, say, the Ubuntu archive would list hundreds.

`manual_installs_sync` is **install-only**: it has no target-side manifest of what it installed, so it never proposes removals. Removing a hand-installed item on the target is manual work today (tracking removal for manual installs is deferred to a future issue).

## Prerequisites: passwordless sudo

Each enabled package job needs passwordless sudo for a handful of binaries:

- **`apt_sync`** — on the source (to read `/etc/apt` configuration) and the target (to install packages, write and remove `/etc/apt` configuration including signing keys, and set or clear apt holds via `apt-mark`).
- **`snap_sync`** — on both the **source** and the **target**; validation fails if either lacks it. The target needs it to install, refresh and remove snaps, and both hosts need it to pause snapd's auto-refresh for the sync window (`sudo snap set system refresh.hold`, plus the matching `sudo snap get` — snapd requires admin rights to read snap configuration as well as to write it). The runtime pause itself tolerates a transient failure without aborting the sync, but the sudo grant is checked up front on both machines.
- **`flatpak_sync`** — on the target only, and only when the diff involves a system-scope item (a system-scope ref, remote, or mask). User-scope masks need no sudo.
- **`manual_installs_sync`** — a snippet author decides its own privilege needs; the replay itself runs unprivileged, so the job requires no sudo beyond what a given snippet writes for itself.
