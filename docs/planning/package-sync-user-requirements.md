# Package sync — user requirements

What package synchronisation does, what it asks the user, and what it will not do.

## Navigation

- [High level requirements](high-level-requirements.md) — project vision and scope
- [Package sync conformance criteria](package-sync-conformance-criteria.md) — these requirements as individually checkable obligations
- [Package sync job behaviour](../jobs/package-sync.md) — user guide
- [ADR-020](../adr/adr-020-declarative-package-convergence.md) — the decision record and the reasoning behind it

This document states the intent. Where it and any other document disagree, this one is right.

## What package sync is for

Work on one machine, sync, resume on the other. That only works if the software is there: a synced home directory on a machine without the applications gives configuration for programs that will not start.

Package sync replicates *what software is installed*. Application data belongs to folder sync, which must run after it — installing software writes its own stock defaults, and those have to land before the user's synced settings go on top of them.

It replicates by **convergence, not by copying**. Both machines' package managers are asked what they have, and the difference is what the sync acts on; no package database, store or installed file is copied between machines. What is synced is a decision — install this, remove that — plus the configuration the target's own package manager needs to carry it out.

Package sync is one job per package manager — apt, snap, flatpak — plus one job per kind of software no package manager can reproduce: hand-installed `.deb` packages, and unowned software under `/usr/local` and `/opt`. Each is enabled separately, reviewed separately, and can fail without stopping the others. Enabling one authorises pc-switcher to install and remove software on the target.

## Vocabulary

**Source** and **target** are per-run roles. The sync is launched on the source; the target is the machine being changed. The next run may swap them.

**The holding machine** is the machine that has the item being decided about — the source for something it has and the target lacks, the target for something only the target has.

An **item** is one thing the user can be asked about: a package, a snap, a flatpak application, a configuration file. Its identity is stable across runs, so a decision about it can be remembered.

A **decision** is the user's answer about an item: apply it, skip it this run, or always skip it in future runs.

**Machine-specific** describes an item marked "always skip". The job that marked it never touches that item again on that machine of its own accord, whichever machine the sync runs from — it is neither sent to the other machine nor changed by it. Where an approved change would touch it anyway, the user is asked first.

**Derived** describes plumbing that is synced because approved software needs it — the repository a package comes from, its signing key, a pin, a flatpak remote, a block. It is never a question of its own.

A **block** is a standing refusal to let software move: an apt hold, a snap refresh hold, a flatpak mask.

An **origin** is where software actually comes from — a repository or remote URL. Not its name: two remotes can share a name and serve different builds.

A **snippet** is a shell recipe, written once, for software no package manager can install.

**Flatpak scope** is the user or system flatpak installation. **Snap revision** and **snap channel** are the exact build and the update track.

## The model

**A sync goes one way, from source to target.** The source's installed state is what will be replicated on the target.

**Identity includes the origin.** `gh` from GitHub's repository and `gh` from Ubuntu's archive share a name and are different software. A package is installed on the target from the same origin it has on the source.

**The user is asked about software; the plumbing is derived.** Approving a package also approves the repository it comes from — a repository without its package does nothing, and a package without its repository cannot be installed. A block is plumbing too: it changes nothing about what software exists, only about what may move, so it follows the software it applies to rather than being asked about.

**Consent precedes every change.** Nothing is written to the target before the user has approved the changes that job proposes.

Where an answer follows from something already approved, it is not asked again. Where it does not, it is asked.

Asking about every package separately would interrupt the user constantly, so a job's questions are **batched**: they come one after another with no work between them, each in whatever shape answers it best. Repeated decisions of one kind are one list settled in a single pass; a question that has to show the user something first — a file, a transaction — takes a screen of its own. Batching is about when the questions come, not about one shape fitting every item.

That is a preference, not a rule: applying an approved change can reveal something the plan could not know, and a further question is then correct.

Approving a removal takes a gesture distinct from approving an install, and is never the default.

**A failure lands on the package it concerns**, or on the configuration item itself where no package is involved. Every approved item is attempted and every failure is named. If a pin, repository or key fails to land, the packages that needed it fail with it, rather than being installed from whatever origin the machine would otherwise use.

```mermaid
flowchart TD
    A["plan — reads only"] --> B["review — the user decides"]
    B --> C["apply the approved changes"]
    C --> D["report, per item"]
    C -.->|"an applied change reveals<br/>something the plan could not know"| E["a further question"]
    E -.-> C
```

## What happens during a sync

In its validation step, pc-switcher checks every enabled job's prerequisites before any of them changes anything — core behaviour, not specific to package sync. Each package job checks there that it has the passwordless sudo it needs on each machine, and fails validation naming the machine that lacks it rather than running with less. apt additionally refuses to start while the target's dpkg lock is held.

Snap auto-refresh is paused on both machines for the run and restored afterwards. The pause is timed, so it expires by itself if the run dies.

Each job then plans, reviews, applies and reports.

**Plan** issues read commands only: what each machine has, filtered by the standing machine-specific marks, and the difference between them. A question that cannot be derived is asked here rather than in the review — [apt's Ubuntu Pro question (see below)](#esm-repositories--ubuntu-pro) is one, because one of its answers ends the job before there is anything to review, and the [`/opt` shape question](#software-no-manager-can-reproduce) is another, because its answer decides what the review lists.

**Review** presents one group at a time: items of one kind, all doing the same thing, each carrying its own decision and all settled in a single pass. The default choice for each item is the action that does no harm: *apply* for an install, *skip* for anything that removes or overwrites. Three answers are offered, or two where a permanent decision is not meaningful.

The two machines are named by hostname wherever the user reads them. *Apply*, *skip this run* and *always skip* name the decisions; they are not what the user reads. Each answer is offered as the act itself — *install*, *skip now*, *never install*, or *keep for good* where the item is already on the machine — and carries a sentence of its own naming the machine the act happens to and how long the answer lasts, so a permanent answer says it will not be asked again. The answers to one question read as a set: one grammar for all of them, and the machine named in every sentence or in none.

The user can abort the whole sync at any question, and aborting is never read as declining one item.

**Apply** converges one item at a time, in the order the job requires, and announces every write. Nothing unapproved is written, and no write escapes pc-switcher's per-command confirmation — including the decision records, the snippet registry and the snap refresh pause.

**Report** gives the job's outcome: success, skipped with the reason, or failed naming each failed item.

The log holds more than the report. It names every item the job presented and the decision each one got; every change a job applied, a line each saying what was done, to what, by which manager and on which machine; and every change the package manager made on its own behalf — the collateral the review never showed. The package manager's own output is kept verbatim in the debug log.

A credential embedded in a URL is withheld wherever the user or the log would otherwise see it — in a command, in a package manager's output, in a configuration file shown whole for a decision. A repository can carry its password in its own address, and a log file is readable by anyone with an account on the machine that wrote it.

A **dry run** plans and reviews exactly as a real run does — the questions are still asked — then changes nothing and records nothing. A **non-interactive run** — one with no terminal to answer at, such as from cron or a script — asks nothing and leaves skipped for this run every item that needed a decision, and reports as skipped any job that held one. A job whose review held nothing to decide reports success instead: either it found nothing at all, or everything it found was reported rather than asked about, and no answer would have changed the outcome. Either way nothing is recorded and the snippet registry does not travel — it holds every snippet written on earlier runs, and sending it over the other machine's copy is a change nobody was there to approve.

## Decisions and their memory: "machine specific"

**Apply** does the thing. **Skip** declines it for this run only. **Always skip** marks the item as **machine-specific**. The mark is recorded on the **holding machine** — not necessarily the machine the sync was launched from.

Example: two machines Atlas and Vega, sync launched from Atlas. Vega has `steam`; Atlas does not, so the sync offers to remove it from Vega. Answering "always skip" writes the mark **on Vega**, because Vega holds `steam`. The reverse case: Atlas has `wireshark`, the sync offers to install it on Vega, and "always skip" writes the mark on **Atlas**.

A machine-specific item is filtered out before the difference is computed, so it never appears in a later review. Because of that, the repository-conflict question below has to disclose it explicitly.

A mark lasts as long as the software it protects. Once the holding machine no longer has that software — removed by hand, or taken by a removal the user approved elsewhere in the review — the mark is dropped and the run says so. Leaving it would not be the cautious choice: a mark works in both directions, so one left behind quietly refuses to install that software on that machine ever again, which is not what "keep my copy" meant. If the software comes back, it is reviewed again like anything else.

Marks never sync between machines. Snippets do, because how to install something is knowledge about the software rather than the machine.

Repositories and pins cannot be marked machine-specific, whether they are being added, changed or deleted; a flatpak remote is never asked about at all, so there is nothing to mark. Where the two machines disagree about where software comes from, that disagreement keeps surfacing every run instead of being silenced once, and the remedy is to align the two machines. apt's own configuration files *can* be marked, because they say how apt behaves rather than where software comes from.

Blocks cannot be marked either. A hold and a mask are never asked about at all, so there is nothing to mark, and a mark on the software they apply to silences them with it.

A snap's revision or channel cannot be marked. Nobody holds a revision as a standing preference about one machine, and a mark would leave the two machines' manifests disagreeing about a snap neither would raise again; skipping it for the run says what the user means, and the difference surfaces again next sync.

Report-only findings cannot be marked either — no machine holds a version difference, and a mark would stop the package syncing rather than stop the report.

## apt

apt sync covers the **manually installed** package set — what the user asked for, not what apt pulled in to satisfy it. With it are synced the repositories and pins that decide where those packages come from, apt's own configuration, and apt holds.

### Installing

```mermaid
flowchart TD
    A["On the source,<br/>absent on the target"] --> B{"Where did the<br/>source get it?"}
    B -->|"the distribution"| C["Ordinary install"]
    B -->|"elsewhere"| D{"Does the target already<br/>offer it from that origin?"}
    D -->|yes| C
    D -->|no| E{"Can the source's origin<br/>be replicated?"}
    E -->|yes| F["Install, naming the origin.<br/>Its repository, key and pin<br/>are synced as a consequence"]
    E -->|no| G["Report it, with origin<br/>and reason. Do not install"]
    F --> H{"After the configuration lands,<br/>what would apt really install?"}
    H -->|"the source's origin"| I["Install"]
    H -->|otherwise| J["Refuse this one install,<br/>naming both origins.<br/>Continue the run"]
```

The review names the origin whenever an install would come from anything other than the distribution.

The last step is a real check against the target's own state after the configuration has landed, not a prediction. An install that would come from the wrong origin fails alone, naming both, and the run continues.

Two machines on different Ubuntu mirrors are not two origins: each machine's own distribution source files define what "the distribution" means for it.

A package from a hand-downloaded `.deb` is not apt-sync's business — see [*Software no manager can reproduce*](#software-no-manager-can-reproduce).

### Removing a package

A package on the target that the source lacks is offered for removal, with "skip" selected as default action. Removal does not purge the package's configuration. What apt leaves behind under `/etc` can be purged by hand at any time, whereas a purge cannot be undone; and the configuration a user thinks of as their own lives under `$HOME`, which no removal touches.

### Reporting without acting

Same package, same origin, same version shows nothing at all.

Three situations are reported and never acted on:

- **different versions** — both named; versions float — package managers handle updates, not pc-switcher
- **different origins** — both named; takes precedence over a version difference, and is never reported for a mirror difference
- **an origin that cannot be replicated** — warning reported with the reason, never installed from somewhere else

### Holds

An apt hold follows the package it applies to. It replicates from the source without review, added and removed alike, exactly as a pin does: a hold changes nothing about what software exists, only about what may move, so replicating it costs nothing and there is nothing per-package to get wrong. Where the package is itself something this run installs, the hold lands after the install; where that install was declined or failed, no hold is registered, since there would be nothing on the target to freeze. A package marked machine-specific takes its hold with it into silence.

A hold naming a package the machine does not have ends the run, on either machine. It names the package and the machine and says the hold must be cleared before the next sync. Such a hold freezes nothing while blocking every later attempt to install that name; it is a bookkeeping failure, it is rare, and it is not worth carrying logic for.

A hold blocks everything: apt will not install, upgrade or remove a held package, not even as an unused dependency. So it serves two intents at once — "never lose this" and "never move this off the version that works" — and apt gives no way to tell them apart.

That second intent decides how a held package is installed. Everywhere else a version floats, because the user expressed no preference; a hold *is* that preference. So when the source holds a package the target lacks, the target gets the **source's exact version**, not whatever repositories currently offer. If that version is no longer available on the target, the install fails as its own item naming both versions — better than silently freezing the target on a version the user never chose, which nothing would ever move again.

### Collateral damage

Approving an install can make apt remove or downgrade something else, through *conflicts*, *replaces* or version constraints.

If apt installed that something automatically, the collateral action proceeds silently — apt is resolving its own dependencies. The log names it.

If it is **manually installed on the target**, the user is asked first about any removal/downgrade/upgrade. The question names the package, says that this machine's apt has it marked manually installed, and says what the approved change would do to it. Three answers, each stating its effect: install anyway, skip and leave the triggering install unapplied, or stop — which ends the whole sync, not just apt.

A package marked **machine-specific** is the case that matters most, and the question mentions explicitly that the package is machine specific.

A package the user chose to keep is protected too. Only a removal the user *approved* is exempt from this question, and one skipped for this run, or marked machine-specific, keeps its protection. A decision made earlier in this same run counts.

Declining cancels only the changes that actually cause the collateral. Where several cause it together, all are cancelled and the question says so. It never overwrites a decision the user already gave.

### Repositories, keys and pins

Adding or changing a repository is never a question: it is written because an approved package comes from it, and one that feeds nothing this run syncs is not synced at all.

Deleting one is a question, but only once nothing on the target still uses it — counted after this run's approved removals, and counting packages marked machine-specific. While anything still uses it, the repository stays and is never raised. The question names the URLs the file declares, not just its filename.

A repository both machines have with different content is overwritten with the source's version silently — unless the overwrite would repoint a package the target marked machine-specific. Then the user is asked, and shown both versions of the file in full. Declining fails every approved package whose origin depended on that file.

Only a repository this run writes because an approved package comes from it can raise that question. A differing file nothing approved this run needs, is not written, so there is nothing to consent to.

The distribution's own source files are written and updated, never removed or offered for removal. Files apt itself does not read are not treated as repository configuration.

**Signing keys are never a review item.** A key the target lacks is copied byte-for-byte from the source before the repository naming it is written; one that differs is refreshed, unless the target's own distribution packaging owns it. Keys are synced from the source machine, not fetched from the internet. An approved repository deletion may take an unreferenced key with it.

**Every** pin the source has is written to the target, always, without review: a pin decides which origin wins, and one naming an absent origin does nothing. Deleting a pin only the target has *is* reviewed, and the file is shown whole.

A pin is never read as a statement about the packages it names.

apt's own configuration — proxy settings, recommends policy — is reviewed whether it is being added, changed or removed, with the full decision, because no approved package implies whether such a setting should be synced.

### ESM repositories — Ubuntu Pro

If the source carries ESM repositories and the target reports no Ubuntu Pro attachment, the user is asked before anything is written and before any other apt question. Two answers: attach the target now, or skip apt for this run while everything else proceeds. The user is told what to run on the target to attach it.

Writing them to an unattached target fails invisibly: `apt-get update` succeeds and the failure surfaces later, as a 401 during some unrelated install. pc-switcher cannot attach the target itself.

"I have attached it" re-probes rather than taking the answer on trust, and may be answered any number of times. Skipping skips the whole apt job, not just the ESM files, leaving apt's configuration untouched. A non-interactive run takes the skip; a dry run warns instead.

Only whether the target is attached is ever logged or shown.

### Applying apt's changes

All repository-configuration work is applied as one unit: backed up, written in apt's required order, then one refresh of the package lists. If the refresh fails, everything is restored and every approved package that depended on the unit fails by name. If a restore itself fails, the backup is kept and its path named.

Every derived change is logged as it lands and appears in a dry run's preview.

## snap

snap follows the shape every job has: what the source has is offered for install, what only the target has for removal, what cannot be converged is reported. What is particular to snap:

### Revision and channel

snap converges the source's **exact revision and channel**, where apt and flatpak let versions float: snap keeps per-user data in `~/snap/<app>/<revision>/`, which folder sync can only mirror correctly when both machines are on the same revision.

So a difference of revision or channel is a change to apply rather than something reported. It is worded as the effect it has — what the target's copy is overwritten with — and it takes two answers, apply and skip for this run, because a revision is not a standing preference anyone holds per machine. An install lands the source's revision and channel on the target; the same revision and channel on both machines produces nothing.

### Removing a snap

Removing a snap leaves snapd's own pre-removal snapshot in place — the only recovery path if the removal was a mistake.

### Refresh holds

A refresh hold follows the snap it applies to, exactly as an apt hold follows its package: replicated from the source without review, added and removed alike, and never a question of its own. A hold recorded for a snap the source no longer has produces nothing.

### Sideloaded snaps

Snaps installed from a local `.snap` file are out of scope (#221). They are ignored on both machines: never installed, never removed, never an item. A run does nothing else about them and does not report them.

## flatpak

flatpak follows the shape every job has: what the source has is offered for install, what only the target has for removal, what cannot be converged is reported. What is particular to flatpak:

### Identity

A flatpak application is identified by its **scope** and its **full reference including branch**. The same application in both scopes, or on two branches, is two independent items — one install and one removal — reported as found.

Origin is not part of that identity: flatpak refuses to install a reference already present from a different remote, so a difference of origin is reported rather than converged, and takes precedence over a version difference.

### Installing an application

An application is installed from the source's remote or not at all, and that remote is identified by **URL and verification setting, never by name** — checked before the install and read back after it. Either failure fails that one application.

### Remotes

A remote is never a review item, whether it is being added, changed or deleted. It is plumbing, derived from the applications the user approved.

There is no distribution remote to start from: a remote is synced only because an approved application needs it — including the remote supplying that application's runtime — and is provisioned before the first install.

**A remote replicates with its trust**, not just its name and URL: its signing key is synced byte-for-byte from the source, a verified remote is never replicated as unverified, and an unverified one is replicated as such with a warning.

A remote both machines have whose URL or verification differs is repointed silently — unless that would move the origin of a machine-specific application, in which case the user is asked, shown both configurations, and declining fails every approved application that needed the source's URL.

One the source does not have is deleted once nothing on the target still uses it — after this run's approved removals, counted against what the machine actually has, including applications marked machine-specific. While anything still uses it, it stays. Deleting a remote takes its signing key with it.

A remote the source restricts with a filter is replicated **with its filter**. The filter is a separate file at a path of the user's choosing — flatpak records the path, not the content — so it is copied byte-for-byte to the same path on the target and applied there. Like a signing key, it is derived and never a review item. It is in force before the applications install: the remote is added, its filter set, and only then does anything install from it, so no run can leave that remote offering more than either machine meant. A filter only the target had comes off in the same step where the source no longer restricts that remote. If the filter cannot land, the run says so and every approved application from that remote fails. Filters follow the remotes: only a remote some approved application (or its dependency) needs this run is touched at all, so a remote nothing in the run installs from keeps whatever filter each machine had.

A filter that denies an application the source itself has installed from that remote ends the run, naming the application, the remote and the filter. That machine is contradicting itself, and the tool carries no logic for a state that should not exist.

### Masks and scopes

Mask patterns replicate per scope, added and removed alike, whether or not anything currently matches them, and land after the applications.

A mask is derived, like the two holds: replicated from the source without review and never a question of its own. Unlike a hold, a mask whose application this run removes still lands — a mask says what may not be installed, so it means something precisely when the application is gone.

A flatpak installation that is neither the user nor the system one is skipped. Remotes belong to an installation, not to the machine, so nothing in such an installation depends on a remote a sync touches — a remote of the same name in the user or system installation is a different remote.

### Applications no remote can supply

An application whose origin names no remote configured in its own scope is not this job's. It came from a local bundle or from a remote since deleted, so there is nowhere to fetch it from: it is never installed, never removed, and no remote is derived from its origin. Its own job, `manual_flatpak_sync`, offers it instead.

The one thing still said about such an application is a divergence both machines already have: if both hold it and only the target's origin no longer resolves, that is reported as an origin difference like any other. Nothing is installed or removed either way.

## Software no manager can reproduce

Software that arrived on the source by a route nothing can replay automatically. Several kinds, one mechanism: the **install snippet**, a shell recipe written once that is synced with the software. Each kind in scope is a job of its own, enabled and reviewed on its own, and the kinds share the one snippet registry — how to install something is knowledge about the software, so one recipe file answers for all of them.

- **A hand-downloaded `.deb`** — apt knows the name, but no configured repository offers that version. Its own job is the only one that offers it: apt drops these packages on both machines whatever else is enabled, so with apt synced and this job off they are replicated by nobody.
- **A flatpak application no remote can supply** — installed from a local bundle, or from a remote since deleted, so its origin names nothing that can be fetched from. Its own job is the only one that offers it: `flatpak_sync` drops these applications on both machines whatever else is enabled, so with flatpak synced and this job off they are replicated by nobody.
- **Unowned software under `/usr/local` or `/opt`** — dropped there by an install script or a tarball. The scan looks in `/opt`, directly under `/usr/local`, and inside `/usr/local`'s `bin`, `sbin`, `lib`, `games` and `src`. It never looks in `etc`, `include`, `man` or `share`: whatever is installed there arrives with an application the scan finds elsewhere. A finding — a file, a directory or a symlink — is named where it is found and never opened, or one application under `/opt` would arrive as thousands of findings. The directories the distribution itself creates under `/usr/local` are not findings, and neither is a directory with no file anywhere beneath it.
- **A sideloaded snap** — out of scope for now (#221).

Both machines are scanned, and only what the target lacks is presented. That is what stops a second path to one application — the symlink in `bin` that starts what the snippet unpacked under `/opt` — from being asked about again every run after the snippet has already installed it. What only the target has produces nothing: **nothing here is ever removed**, and no record is kept of what a snippet put there.

One shape cannot be judged by looking at it. An unowned `/opt/<name>` holding files of its own is one application. Holding a single directory and no file, it is a publisher's own directory and the application is that directory. Holding several directories and no file, it is either, and the user is asked which — the one question here that shapes the list of items rather than answering about one of them, so it is asked while the run is still planning.

Every detected item ends the run with a snippet, marked machine-specific, or skipped for this run.

A snippet is **opaque** — stored and replayed exactly as written, never parsed. It runs as the target user with no privilege added around it and no standing input, so a command expecting an answer fails rather than hanging the sync.

Reproducibility is judged by what the **source** holds. The snippet registry is synced; if that would lose or change an entry only the target has, the user is asked and declining aborts the run. A registry file on either machine that cannot be read as a registry aborts it too, naming the file: an unreadable one is not an empty one, and treating it as empty would push over snippets nobody could see. Repair it and sync again. A snippet written during a review is replayed in that same run.

## When something goes wrong

Every approved item is attempted, and all failures are reported together, each naming its item. One failed item does not block the rest of its job; one failed job does not stop the others.

A repository, key, pin or remote has no item of its own to fail on. When one of them fails to land, that failure is charged to every approved package that needed it, and all of those packages fail — including ones that would otherwise have installed, because the origin they were approved for is not there.

It does not work the other way round. A package that fails to install leaves the repository it came from in place, and leaves the other packages that share it untouched.

A read that does not answer is different. If a package manager cannot be queried at all, its silence is never read as "this machine has nothing installed", which would propose removing everything on the other machine. It fails once, naming the command, and it fails only its own job — the other jobs still run. An *empty* answer is ordinary data.

## What this deliberately does not do

- **The target's apt configuration is not under line-by-line control.** Repositories, keys and pins appear because a package was approved; declining the package is the only way to decline them.
- **A pin cannot be kept on one machine only.** It is asked about every run until source and target have the same pin.
- **A hold or a mask cannot be kept on one machine only.** Like a pin, it replicates from the source until the source drops it.
- **A snap's revision cannot be kept on one machine only.** The difference is offered every run until the two machines agree.
- **Version drift and origin divergence are reported, never resolved**, for apt and flatpak.
- **Hand-installed software is never removed from the target.**
- **A target with no Ubuntu Pro attachment costs the whole apt job for that run.**
- **A non-interactive run can answer no review.** There is no file of standing answers and no assume-yes option.
- **Machine-specific marks are per job, per machine, and never synced.**
- **An environment variable can answer a review, and its answers count as the user's own.** `PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` exists so the integration tests, which have no terminal, can answer one; it appears in no help text and no configuration key. Anything able to set it on a real run gets silent, unreviewed, permanent decisions.
