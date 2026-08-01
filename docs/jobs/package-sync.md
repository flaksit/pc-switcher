# Package Sync

The package jobs replicate *what is installed* — apt packages plus the `/etc/apt` repository configuration they depend on, snaps, flatpaks, and the things no package manager can reproduce — rather than user data. Package *data* (`~/.var/app`, `~/snap/<app>/common`, dotfiles) stays `folder_sync`'s territory.

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

- **`apt_sync`** — the manually-installed apt package set (`apt-mark showmanual`, not the full dpkg selection — apt resolves dependencies on the target itself), minus the packages you installed from a hand-downloaded `.deb` (see below), plus the `/etc/apt` configuration that governs where packages come from. Three things under `/etc/apt` are reviewed: `apt.conf.d` files — added, changed or deleted — the deletion of a repository or pin file the source no longer has, and one narrow repository conflict. Repository files under `sources.list.d`, their signing keys, and pins under `preferences.d` are derived from the packages you approve and never get a review row — see [Repositories, pins and keys are derived](#repositories-pins-and-keys-are-derived).
- **`snap_sync`** — installed snaps, converged to the source's exact revision and tracking channel.
- **`flatpak_sync`** — installed flatpak refs, per user/system installation scope, plus the remotes those refs are derived to need.
- **`manual_installs_sync`** — everything no package manager can reproduce: apt packages installed from no configured repository (a `.deb` you installed by hand, whether or not apt marks it as one you chose), plus unowned software under `/opt` and `/usr/local`. It also owns the [install-snippet registry](#install-snippets).

### Hand-installed `.deb` packages belong to one job only

A package whose installed version comes from no repository your machine has configured was put there with `dpkg --install`. It is `manual_installs_sync`'s territory exclusively: `apt_sync` detects the same packages, with the same test, on BOTH machines, and drops them from that machine's manifest before it diffs anything. They produce no apt item, no review entry, no install and no removal — including the one only the machine being synced TO has, which would otherwise look like software the source had deleted.

That detection is over everything installed, not just what you chose explicitly. A `.deb` that arrived as another `.deb`'s dependency is marked automatic by apt, and apt's mark says how it got there rather than whether anything can supply it again.

There is nothing apt could do with them anyway. The target's apt has never heard the name, so offering it as an ordinary install would fail with "Unable to locate package" — while `manual_installs_sync` offers the same package as an [install snippet](#install-snippets) in the same run. Only one of the two answers works, so only one job asks.

The consequence is worth knowing: the two jobs have separate enable flags, and `apt_sync` does not consult `manual_installs_sync`'s. **If you enable `apt_sync` but disable `manual_installs_sync`, your hand-installed `.deb` packages are synced by nobody** — they are silently absent from the review rather than offered as installs that fail. Keep `manual_installs_sync` enabled if you install software from downloaded `.deb` files.

## Job ordering is enforced

All four package jobs **must** be listed before `folder_sync` in `sync_jobs`. This is not a convention: pc-switcher validates the resolved order and aborts the run with a config error if any of them is enabled but sits after `folder_sync`.

The reason is the "defaults, then your data" layering. Installing software usually writes its own default config and data files on first appearance. If `folder_sync` ran first, the fresh install would overwrite your synced versions of those files with stock defaults. Running the package jobs first means the software already exists when `folder_sync` lands your versions on top. `manual_installs_sync` is in the rule for the same reason: your snippet installs software, and that software writes its own defaults too.

## Batched review

Because an enabled package job can install or remove software on the target, each one shows you a review and waits for your approval before it changes anything.

Every screen names the two machines by their **hostnames** — `atlas`, `nomad` — never as "the source" and "the target". Those are the tool's names for the two ends of a run, and the question a review asks is always about one of your computers: which machine loses the package, which machine's version of a file wins, which machine an install snippet runs on.

The review lists every difference the job found between the two machines, grouped by action, and installs are always kept separate from removals: a group that would install software is never mixed with one that would remove it, and a removal group names the removal explicitly (`Remove apt packages`) rather than saying "apply". Removal rows start at **skip now**, so a bulk approval can never silently delete something. So do rows that would replace an `apt.conf.d` file the target already holds — that file is your own work, and overwriting it unread is as irreversible as a deletion. A snap the run moves to another revision or channel still starts applied: converging software you asked for overwrites nothing you wrote. That one is offered with **two** answers rather than three — its line says what your machine's copy is overwritten with ("overwrites revision 15 on nomad with revision 20"), and there is no permanent answer, because nobody keeps a revision as a standing preference about one machine.

Most items that would actually change something — packages, applications and `apt.conf.d` files — offer the same three-way choice:

- **Apply** it — make this change on the target.
- **Skip now** — leave it alone this sync; it comes back next sync.
- **Skip for good** — mark it as belonging to one machine only, so no future sync touches it and you are not asked again (see [Machine-specific packages](#machine-specific-packages)). Said as this screen's own act: `keep for good` on a removal screen, `never <verb>` on every other.

You give those answers on **one screen per group**, not in two passes. Every item is a row; the decision it currently carries is shown in a column to the right of the longest item; the arrow keys move between rows — the first and last rows are walls, not a way round to the other end — and one key sets the focused row:

- `<y>` — apply, shown in the column as the group's own verb (`install`, `remove`, `overwrite`, …)
- `<s>` — `skip now`
- `<x>` — skip for good, as a cross for "exclude". Not `<n>`, which beside `<y>` reads as a plain "no" and would invite "not now" from the one answer that is permanent
- `<space>` steps the focused row through the answers, and the shift of any key sets **every** row at once
- `<enter>` confirms the whole screen; `<ctrl-c>` aborts the whole sync (the screen does not offer this — it is not one of the answers)

Each answer is listed with a sentence of its own, naming the machine it happens to and how long it lasts, because the column word is too short to say either. The two skips share the act's own words and differ in the duration that follows, so the permanent one is chosen on what it costs you — never being asked about the item again — rather than on what it stops pc-switcher doing:

```plain
? Remove apt packages
  <y> remove          remove from nomad
  <s> skip now        keep on nomad for now; will be asked again next sync
  <x> keep for good   keep on nomad for good; it is nomad's own, and will not be asked again
  <space> cycles   <shift+key> sets every row   <enter> confirm

 » ● fortunes-min  remove
   ○ cowsay        skip now
   ⊘ sl            keep for good
```

Four questions take **two** answers instead — act, or leave it for now, with nothing recorded either way: deleting an apt repository file, deleting an apt pin file, overwriting a repository file the two machines disagree about, and repointing a flatpak remote a machine-specific app takes as its origin. Their screens simply do not offer `<x>`. See [Deletions](#deletions) and [Flatpak remotes](#flatpak-remotes).

Some questions come one item per screen rather than as a list: a repository or pin file being deleted, a repository whose two versions differ, a collateral package, and an unreproducible item. Each has something to show you first — a file body, two file bodies, or what a change would do to a package you installed or marked as that machine's own — and the decision follows the thing it is about. The answered list stays on screen afterwards; nothing is echoed back at you.

Items that only **report** a condition are not asked about at all — they are printed, grouped by what the condition IS, and the review moves on. Neither answering nor declining would change anything on either machine or be remembered, so there is nothing to answer. The groups are `Version differences`, `Installed from different repositories` and `Origins <target> cannot reproduce`. Each title ends with what the job syncs — `(apt packages)`, `(snap packages)`, `(flatpak applications)` — and the flatpak origin group says `remotes` where apt says `repositories`, because that is what flatpak calls them. The conditions: a version difference between the two machines (named as `atlas has 1.0-1, nomad has 2.0-1`), an apt package whose repository cannot be reproduced on the target, and a package or application the two machines installed from different origins. These change nothing on the target, and neither machine "holds" the item in the way a machine-specific mark requires — marking a version difference would silently stop the package syncing altogether rather than stop reporting the drift. Resolve them by fixing the underlying condition (align the versions, restore the repository on the source, pick one origin). A version difference is the one that resolves itself — its report says so, naming the upgrade command to run.

### One review per job, and nothing applied you did not approve

`apt_sync` asks everything it can ask before it writes anything, and it never applies something you did not approve. It is one review, in one sitting, and most of it is one pass. A few questions come in a second pass immediately after: a repository whose last remaining package you have just approved removing, a repository file whose two versions only matter because you approved the install that carries it, and a package a *keep for good* answer you gave a moment ago now protects. Each of them is worth asking only because of an answer you have just given, so it cannot be put before you gave it — and none of them would be worth answering if it were asked from the whole list of things the run merely *proposed*. Nothing on the other machine has changed when that second pass opens, so **stop the sync** there still stops everything.

Nothing else needs a second look: a package is judged by where your **source** installed it from and a sync never changes the source, so nothing the run does to the target can make an answer you already gave wrong. The one fact that does depend on what the run wrote, which repository actually ends up supplying a package, is measured afterwards rather than guessed: a package that comes out wrong is refused and reported, not turned into another question.

A pin file gets no line of its own on a package either. Pins are synced — they are the files under `/etc/apt/preferences.d` that make one repository's build outrank the Ubuntu archive's — but a package named by a pin is reviewed like any other: if it is on the target and not on the source, it is offered for removal, and you can mark it machine-specific.

### Confirming every individual command

The batched review approves *items*, not commands. One approved line can expand into several: an apt package is an `apt-get --dry-run` simulation then an `apt-get install`; an apt repository file is a backup, an upload, a `sudo install` promotion and an `apt-get update`.

`pc-switcher sync <hostname> --confirm-each-command` inserts one question before every one of them, headed by the job and the hostname of the machine about to change, then what the change does, then the exact command (or, for a file transfer, the source and destination paths). It waits for **p** to proceed or **a** to abort the whole sync. There is no "skip this one": a single reviewed item can span several commands, so skipping one would leave that item half-applied. An unanswerable prompt (Ctrl-C, EOF) aborts.

It covers every write the four jobs make, plus the machine-local decision files on both machines, the snippet registry and its push, the snapd auto-refresh pause and restore, and the sync-history update on both ends. Read-only commands are never gated. The flag needs a real terminal and is refused without one. It is meant for auditing or debugging a run you do not trust yet, not for everyday syncs.

### apt collateral

An apt change sometimes forces apt to remove, downgrade or upgrade *other* packages to satisfy it — so the package on a review line is not always the whole transaction. While the review is being built, `apt_sync` simulates the whole set of candidate installs and the whole set of candidate removals with `apt-get --dry-run` and inspects that collateral. Dependencies apt pulls in or drops on its own are apt doing its job: they are not shown to you, but each one is named in the log, so a change nobody asked you about is still a change you can see afterwards.

Two kinds of package are protected instead of let go: one you installed by hand on the target — in the target's own `apt-mark showmanual` set — and one you marked as that machine's own. Each becomes its own review item on a decision screen of its own, one package per screen, because each one's cause and effect differ and the answers name them. It states what the approved change would do to it (`Installing sl on nomad would remove fortunes`), and under that which ground protects it — apt's manual mark, your mark on that machine, or both. A marked package is named nowhere else in the review, so this is the only place its mark can be said. Three answers, each naming this package's own change:

- `<y>` — the act, named as what happens to the package: `remove`, `downgrade` or `upgrade`. Its line reads, for example, `install sl on nomad, so fortunes is removed as well`.
- `<s>` `skip now` — `keep fortunes on nomad; sl will not be installed; will be asked again next sync`. Everything else you approved is applied as you decided.
- `<q>` `stop the sync` — not just `apt_sync`. Its line reads `nothing more is changed on nomad; what earlier jobs already did stays done`: no further job runs, and what jobs that already finished did stays done. `apt_sync` itself has changed nothing at this point: the review runs before its first mutating command.

Being offered for removal is not the same as being given up. A package you were offered for removal and kept — by skipping it, or by marking it as this machine's own — keeps its protection, so an unrelated install that would take it still asks you first. A mark you made earlier in the same review counts from the moment you made it.

The same package can be the casualty of two different changes in one run — an install's own transaction and an approved removal's cascade. Those are two questions, not one, and letting one go ahead says nothing about the other: the answer you give is to the consequence, not to the package.

The source's manual set is not consulted, which gives up one case on purpose: a package you installed by hand on the source, which arrived on the target as an automatic dependency, can be removed as collateral without asking you. If the target's apt installed it automatically, the target's apt owns it, and that is also the set apt itself consults when deciding what it may remove.

That holds inside the batch of removals as well, and it takes a second pass to say so. While the review is being built, each candidate is exempt from its own transaction — otherwise every one of them would be raised as collateral of itself — and there is no answer yet to tell an approved removal from a skipped one. So once you have answered, the run rehearses the removals you *approved* once more and asks about every candidate you did not: skipped, marked as this machine's own, or left alone. The question comes in the review's second pass, before anything is changed, and it reads the same as any other — go ahead, keep the package, or stop.

Skipping cancels only the changes that actually cause the collateral. The question names them, and everything else you approved in the same review is applied as you decided. Where no single change causes it on its own — apt keeps a package as long as either of two others is there, and drops it once both go — the question says "the packages listed earlier" and keeping the package cancels all of them, because that combination really is the cause. Skipping never rewrites an answer you gave: a package you marked never-offer-again keeps that mark even when the skip cancels it for this run.

One class of install cannot be classified while the review is being built: a package whose repository this run is about to add on the target's behalf. Until that repository lands the target's apt has never heard the name, and apt refuses the whole simulated batch on one such name — which would strip the protection from every other package in the run rather than weaken it for one.

You are asked about those later in the same run, once the repositories have landed and the package lists have been refreshed, which is the first moment apt can say what installing them would cost. It is the same question with the same three answers, and every one of those questions comes before the first of those installs runs — so keeping a package leaves its install unapplied rather than undoing it, and stopping the sync stops it before any of them.

Every change is rehearsed once more immediately before it runs, and what that rehearsal turns up can differ from what the review saw: the machine has moved since, or this run's own earlier changes moved it. Where the change would now take a protected package nobody was asked about, you get the same three answers again, right before that one command — keeping the package leaves that change unapplied, and it is neither applied nor reported as a failure.

An install you withdraw that way is neither applied nor failed. The run says what happened to it and moves on to the rest of what you approved. Its hold, if the source holds it, is withdrawn with it — that is true of every install you decline, however you decline it.

The repository that install needed landed before the question could be asked, and it stays. If nothing else you approved comes from it, the run names it — by URL as well as by filename, since the filename is whatever whoever wrote the file chose — and says nothing installs from it any more. It is left where it is: removing it is yours to decide, and the answer you gave was about a package.

## Repositories, pins and keys are derived

You are asked about packages. The `/etc/apt` machinery a package needs to be installable — the repository file it comes from, the signing key that makes that repository trusted, the pin that makes that repository's build win — follows from your answer and gets no review line of its own. Approving a repository without its package does nothing; approving a package without its repository cannot be installed; the pairing was never expressible as two review rows.

A package is replicated as name **and** origin. If the source installed `gh` from `cli.github.com`, the target gets it from `cli.github.com` or not at all, and the review line names the repository when it is not the distribution's own archive. Approving that line is what carries that repository's file, its key and the source's pin files across. If no repository file on the source declares the origin, or every file that does names a key the source machine does not have, the package is reported instead of installed — never satisfied from a different repository.

Two machines can also disagree about where a package they both have came from, and that is reported rather than converged: `gh` from `cli.github.com` on one machine and `gh` from Ubuntu's archive on the other is one name and two pieces of software, so the review names both and proposes nothing — reinstalling from the other origin is not something you asked for. It takes precedence over a version difference on the same package, since two origins' builds are numbered independently. Your whole distribution counts as ONE origin, worked out from that machine's own `ubuntu.sources` and friends, so two machines on different Ubuntu mirrors agree about every package.

After the run's single metadata refresh and before its first install, the target's real candidate origins are read back for every approved install whose origin is not the distribution's own archive. If that repository's build is still not what the target would install, that install is refused as its own item naming both origins and the rest of the run continues. That check is the guarantee; everything before it is preparation.

Pins are the reason the check can fail even when the repository landed. Ubuntu's own `firefox` is version `1:1snap1-0ubuntu5`, and that epoch outranks every epoch-free Mozilla version at equal priority — so adding Mozilla's repository alone still installs Ubuntu's package, and only Mozilla's pin file changes the outcome. Every `/etc/apt/preferences.d` file the source has is therefore written to the target when missing and overwritten when different, always and silently. A pin naming an origin the target does not have is inert, so that costs nothing. The price is that a pin file you wanted on one machine only comes back every run; deleting it on the source is the only way to stop that.

The distribution's own files — `ubuntu.sources`, `/etc/apt/sources.list`, `ubuntu-esm-apps.sources` and `ubuntu-esm-infra.sources` — are written when missing and overwritten when different, and are never removed or offered for removal. They are what defines "the distribution's own origin" on each machine, which is what stops two machines on different Ubuntu mirrors from disagreeing about every package. Files apt itself does not read are ignored, in all three of its fragment directories and in every direction. `sources.list.d` is read for `.list` and `.sources` alone; `preferences.d` accepts no extension or `.pref`, `apt.conf.d` no extension or `.conf`; and none of them accepts a name with a character outside letters, digits, `_`, `-` and `.`, or one starting with a dot. So the `.save`, `.orig`, `.bak` and `.dpkg-dist` copies apt tooling and your editor leave behind are never written to the other machine and never offered for deletion — apt reads them on neither machine, so syncing one would change nothing.

A repository present on both machines with different content is overwritten with the source's version silently. You are asked about exactly one case: a file this run would write because a package you approved comes from it, which also feeds a package you marked machine-specific on the target. Approved, not merely offered — which is why the question comes in the second pass: declining that package's install leaves the file unwritten, and there is then nothing to decide. Then you are shown both file contents whole, side by side and never as a diff, and asked to overwrite or leave it for now. Leaving it fails every approved package whose origin depended on that file, by name, rather than installing it from somewhere else.

Both halves matter: a file no approved package needs is never written, so "overwrite" would change nothing, and a file whose packages are all ordinary ones is one whose changes you can already see item by item elsewhere in the review.

### Ubuntu Pro and ESM

The two `ubuntu-esm-*` files are part of the distribution set, so they would be written to a target that lacks them. `esm.ubuntu.com` serves its repository *index* publicly, so an unattached target's `apt-get update` still succeeds and the ESM versions win candidate selection — only the package pool is behind the 401. The failure lands later, at install time, on a package nobody will connect to the sync.

pc-switcher cannot fix that itself: attaching needs a subscription token from your Pro dashboard or an interactive browser flow, the source machine's own credentials are root-only and not reusable for another machine, and holding a token would put a secret on a command line.

So before `apt_sync` writes anything, it probes the target and — if the target reports no attachment — asks, with exactly two answers:

- **I have attached `<target>` — check again and continue.** pc-switcher probes again rather than trusting the answer. You can answer this as many times as you like; re-probing is free.
- **Skip `apt_sync` this run (every other job still runs).** The target's `/etc/apt` is left exactly as it was and every other job runs normally.

The commands the prompt gives you, to run on the target, are `sudo pro attach <token>` followed by `sudo pro enable esm-apps esm-infra`, and it links Ubuntu's own tutorial — [Attach a machine to your subscription](https://documentation.ubuntu.com/pro/attach-tutorial/) — which stays current if the procedure changes.

Skipping costs the whole apt job, not just the two files, and that is deliberate: pin files are always synced, so a pin the source has and the target lacks would reach the target whether or not the sources it names did, leaving a candidate selection matching neither machine. A run with nobody to ask takes the skip too. A dry run never asks — it warns that the target is unattached and that a real run would skip `apt_sync` entirely.

Only the yes/no attachment answer is ever logged or shown. The probe's own output names the subscriber's account and never leaves the check.

### Signing keys

You think in repositories and packages. A signing key is just how a repository is made to work, so pc-switcher never asks you about one: no key gets a review line of its own, and no key can be marked machine-specific. Not asked about is not hidden, though — every derived write is logged as it lands and previewed under `--dry-run`, so you see what reached `/etc/apt`.

When a repository is written to the target, the keyring it names arrives first — copied byte-for-byte from the source machine, never downloaded from the network. The same check runs for every repository that is *already* on the target: if the key on the source machine has different bytes, the target's copy is refreshed. That is what makes a **rotated** key follow you. A repository replacing its signing key changes no `.sources` file, so nothing in the review would ever mention it, and the target's apt would start failing that repository's signature check until you noticed by hand. A key that already matches is left alone entirely — no transfer, no command.

Keys are looked for in three places: `/etc/apt/keyrings`, `/etc/apt/trusted.gpg.d` and `/usr/share/keyrings`. The last one matters more than its name suggests — it is where `add-apt-repository`, Ubuntu's own `ubuntu.sources` and most third-party `.deb`s put the key their `Signed-By:` line points at. A repository whose `Signed-By:` carries the key **inline** instead — the armored block written straight into the `.sources` file, which is what `add-apt-repository` does for a PPA — needs no keyring at all, so nothing is copied and nothing is missing.

There is one thing pc-switcher will not overwrite: a keyring the target already has that belongs to a package the target got from your distribution. Replacing a distro keyring is not a sync's job. A keyring some *vendor's* package owns is refreshed like any other — shipping the keyring in a `.deb` is exactly how a vendor rotates one, so leaving it alone would mean the rotation never reaches the other machine and that repository eventually fails its signature check. Ownership only stops the *overwrite*, though — a keyring the target is **missing** is always copied, even one a package owns. Some repositories ship a `.deb` that carries both the repository entry and the key that trusts it, so refusing to copy an owned key would leave that repository permanently untrustable and the package permanently uninstallable.

When you approve removing a repository, the keyring it was the last user of goes with it. That count is taken *after* the repository is actually gone, against the real state of the target, so it gets the awkward cases right: a repository you left skipped still counts, one you marked machine-specific still counts, and so does `/etc/apt/sources.list`, which pc-switcher never syncs at all. Nothing is deleted unless the source machine has dropped that key too. If you remove no repository in a run, nothing is collected.

Only `/etc/apt/keyrings` is ever cleaned up. Keys in `/etc/apt/trusted.gpg.d` are *ambient* trust — no repository names them, so there is no way to tell which one is still doing a job — and `/usr/share/keyrings` is package territory. pc-switcher copies from both and deletes from neither; those keys are allowed to accumulate rather than be removed on a guess.

If a repository on the source machine names a keyring that machine does not actually have, no package can be replicated through it: every package whose origin that repository declares is reported instead, naming the origin and the missing key. The repository is never written to the target without its key — a repository apt cannot verify is worse than no repository.

A derived write that fails has no review line of its own to fail. The failure is charged to every approved package whose origin depended on it, naming the file and the reason — you decided about a package, not about a file.

Everything under `/etc/apt` that a run writes or deletes is backed up first, applied, and followed by exactly one `apt-get update`. If that refresh fails, every file the group touched is restored, the target's `/etc/apt` is left as it was found, and the rollback fails the same approved packages a single failed write would.

## What the log keeps

The report tells you what a job did. The log is where you reconstruct why, months later, so it keeps more than the report does:

- Every item a review offered you, with the answer you gave — including the ones you skipped, which change nothing and would otherwise leave no trace at all.
- Every change a package manager made on its own behalf. Dependencies apt resolves for itself are never a question, but each one is named.
- At `log.file: DEBUG`, every command pc-switcher ran and everything that command printed, verbatim. That is what makes a post-mortem read the tool's own words rather than a paraphrase. It is also why debug runs produce log files of several hundred megabytes.

One thing never reaches any of it. A private PPA or a commercial repository carries its credential inside its own address, so `https://user:token@host/...` is printed as `https://***@host/...` — in the log, in the `--confirm-each-command` prompt, in every review line, and inside the repository, remote or pin file a question prints whole. Log files are readable by anyone with an account on the machine that wrote them. The rule covers credentials in URLs and nothing else: a secret that reaches a command another way is not withheld.

## Machine-specific packages

Choosing the permanent answer — `<x>`, `keep for good` or `never <verb>` — marks that package as belonging to *this specific machine*, the one running as source or target right now. A machine-specific package is never synced out to peers when this machine is the source, and never installed or removed here by a sync arriving from another machine. Use it for things tied to one box: a hardware driver, a tool for an attached peripheral.

Which of the two machines it is written on is the one whose copy your answer keeps: an install you never want here is recorded on the machine that has it, and both a removal you refuse and an overwrite you refuse are recorded on the machine you are syncing *to* — its copy is the one the answer protects. An item both machines have is the case where that is not the machine the sync was launched from, so the mark still counts when a later sync is launched the other way round.

The mark is recorded in that machine's own decision file at `~/.config/pc-switcher/<manager>.decisions.yaml` (one per manager: `apt.decisions.yaml`, `snap.decisions.yaml`, `flatpak.decisions.yaml`, `manual.decisions.yaml`). That file is **never synced** — it stays local to the machine it describes. An annotated example lives at [`src/pcswitcher/machine-packages.example.yaml`](../../src/pcswitcher/machine-packages.example.yaml).

To un-mark something, delete its entry from the decision file (or delete the whole file to clear every machine-specific decision for that manager). The next sync treats the item as live again and re-offers it in the review.

A machine-specific package never appears in a review again, which is why the run protects it where it would otherwise be lost without a word: an apt repository it still installs from is not offered for deletion, a collateral removal that would take it is a question, and a flatpak remote it takes as its origin cannot be repointed silently.

## Install snippets

Some installed things no package manager can reproduce — a bare `.deb` downloaded and installed by hand, or software dropped under `/usr/local` or `/opt` by an install script. `manual_installs_sync` detects these and surfaces them in its review as items needing a resolution.

The filesystem half of that scan is deliberately shallow. It looks in `/opt`, directly under `/usr/local`, and inside `/usr/local`'s `bin`, `sbin`, `lib`, `games` and `src` — one level each — and reports whatever dpkg does not own. It names what is there so you can decide about it; it never walks a tree, so an application under `/opt` is one finding rather than a thousand. `etc`, `include`, `man` and `share` are not looked into at all: what a hand install puts there comes with an application the scan finds elsewhere.

Three things are never offered. A path a package owns. One of the nine directories Ubuntu itself creates under `/usr/local` — `bin`, `etc`, `games`, `include`, `lib`, `man`, `sbin`, `share`, `src` — so a stock `/usr/local/bin` is not something you are asked to write an install snippet for. And a directory with no file anywhere beneath it, which is a leftover shape rather than software.

You are asked about only what the OTHER machine does not already have. Both machines are scanned, and a finding that is already there is dropped. That is what stops one snippet's several traces — the tree it unpacks under `/opt` and the symlink in `bin` that starts it — from coming back at you on every later sync once the snippet has run. Nothing the other machine alone has is ever named, in either direction.

One directory under `/opt` cannot be judged by looking at it, and you are asked about it while the sync is still planning, before the review. If `/opt/something` holds files of its own it is one application. If it holds a single directory and no file, that directory is the application. If it holds several directories and no file, it is either one application or one publisher's directory holding several, and only you know which — so the question names what is inside and offers the two answers as what each would mean for the other machine.

Each finding then gets a decision screen of its own — one item per screen, because answering `<y>` opens an editor for that item — with the review's usual three answers in the usual order:

- `<y>` `install` — `write a command snippet that installs it; nomad runs it`, now and on every future sync.
- `<s>` `skip now` — `do not install on nomad for now; will be asked again next sync`.
- `<x>` `never install` — `do not install on nomad for good; it is atlas's own, and will not be asked again`.

An install snippet is a shell command that reproduces the item — the tool never parses, interprets, or reasons about it. It is **stored and replayed verbatim**, and it runs **non-interactively**: no stdin is supplied during replay, so a command that prompts (for example a debconf question) fails rather than hanging the sync. A typical shape:

```bash
sudo DEBIAN_FRONTEND=noninteractive dpkg --install /path/to/package.deb || \
sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --fix-broken
```

Snippets run **unprivileged**. On the target the body is replayed as `bash -c '<body>'` as the target user, with no outer `sudo` wrapped around it. Any privilege a snippet needs must be written inside the snippet by its author — that is why the example above calls `sudo` itself.

The snippet registry lives at `~/.config/pc-switcher/package-snippets.yaml`. `folder_sync` never carries it, even when `~/.config` is inside a synced folder — it reaches the target through this job's own push and its question, below, and nowhere else. Unlike the machine-local decision files, it **does** reach the target: how to install something is knowledge about the *package*, not the machine, so a snippet authored on one machine reproduces the same item on any peer. Whether an item counts as reproducible is decided by whether the **source** holds a snippet for it; a snippet present only on the target does not make the item reproducible.

### Registry push and consolidation

`manual_installs_sync` pushes the source's registry to the target as a **whole-file overwrite** — the source's `package-snippets.yaml` replaces the target's wholesale, no per-entry merge. Before the push, pc-switcher compares the two. A purely additive push (the source is a superset of the target) proceeds silently. But if the overwrite would **lose** an entry the target holds (absent from the source) or **change** one, pc-switcher shows you exactly which entries and asks you to confirm. "Change" is the whole entry, not the command alone: the label and the record of when and where the snippet was written are part of what the target holds, and the question shows you only the fields that differ. Declining aborts the run, and a non-interactive run that cannot ask aborts too — so you can consolidate the two registries by hand and re-run rather than silently dropping the target's snippets.

If either machine's `package-snippets.yaml` cannot be read as a registry — hand-edited into invalid YAML, truncated by a full disk — the sync stops there and names the file. An unreadable registry is not an empty one: reading it as empty would make the push look like it loses nothing, and it would overwrite entries nobody could see. Repair or delete that file and start a new sync. A registry that is simply absent, or empty, means what it says: no snippets.

That question displays a changed body in full, so a snippet that fetches from a private address has its credential withheld the same way every other URL does: you see `https://***@dl.example.com/agent.deb`. Only what you are shown is rewritten — the stored snippet and the command replayed on the target keep the bytes you wrote.

## Resolving unreproducible items

Every unreproducible item is resolved before the run continues: it gets a snippet, it is declared the source machine's own and never installed on the target, or you skip it now. There is no fourth "unresolved" outcome on an interactive run.

- **Ctrl-C** at the review means you want to stop, so it aborts the whole sync — never a silent per-item skip.
- Choosing "add an install snippet" and then submitting an **empty** body is not accepted: the review re-prompts the three-way choice rather than falling through. You must enter a real snippet or pick one of the two skips.
- A **non-interactive** run (no TTY) cannot ask, so it marks every undecided item skip-now and reports them; it never records a snippet or a machine-specific mark. Re-run interactively to actually resolve anything.

## When a package manager cannot be read

A package sync compares what the source has with what the target has. If one of those reads fails — snapd is not running, apt's status file cannot be read, a lock is held, the network dropped — the answer is not "that machine has nothing". pc-switcher stops the job and tells you which command failed and what the tool said, instead of proposing to remove everything the other machine has.

An empty *answer* is different and is left alone: a machine with no snaps, no flatpaks, no held packages and no pins is an ordinary machine, and syncing it is ordinary work.

Only that job stops. The run continues with the others and reports the failed one by name, because a read that went dark on one package manager says nothing about the work another one has already had approved.

## Non-interactive runs

A run without a TTY prompts for nothing, so every review item comes back skip-now and the job converges nothing. When the review had anything to **decide**, the job therefore reports **SKIPPED**, not SUCCESS, and the run continues with the remaining jobs. A run whose review had nothing to decide still reports SUCCESS — either because the target already matches the source for that package manager, or because everything the review held was a finding you are told about rather than asked about, such as a version difference. No answer of yours would have changed either outcome.

`apt_sync` has a second reason to report SKIPPED, and it applies to interactive runs too: the target reports no Ubuntu Pro attachment and the source carries ESM sources that would otherwise be written to it. Attach the target and re-run, or answer the question's re-check once you have — see [Ubuntu Pro and ESM](#ubuntu-pro-and-esm).

A skipped package job applies nothing, records no decision, and pushes no install-snippet registry. The session still completes and the exit code is unchanged, so a headless run says plainly that it converged nothing rather than reporting four successful package syncs.

No run without a terminal pushes the registry, not even the one that reports SUCCESS because its review had nothing to decide. That says this run found nothing to ask you about; the registry on disk still holds every snippet you have ever authored, and sending it over the target's copy is a change nobody approved.

One environment variable overrides all of that, and it is not a feature: `PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` carries a JSON map of item id to decision and answers a package review without asking. It exists so the integration tests can exercise a review with no terminal to answer at, and it appears in no help text and no configuration key. Its answers count as yours — a permanent one writes a machine-specific mark or an install snippet — so anything that sets it on a real run makes silent, unreviewed, permanent decisions on your machines.

## Versions

apt and flatpak let versions **float**. pc-switcher installs by name and takes whatever each machine's own repositories currently offer; a version difference between source and target is detected and reported in the review, never silently forced. (Deliberate pinning still replicates: `/etc/apt/preferences.d` files are always synced, without a review line — see [Repositories, pins and keys are derived](#repositories-pins-and-keys-are-derived).)

snap is the exception: it converges the source's exact **revision and channel**. The reason is where snap keeps per-user application data. snap stores it in revision-number-named directories, `~/snap/<app>/<rev>/`, whereas apt uses stable paths and flatpak uses id-named ones (`~/.var/app/<id>`). Only snap's data path embeds the version, so for `folder_sync` to mirror a snap's data cleanly both machines must be on the same revision — hence convergence.

With both machines on the same revision, snap application data now follows you: `folder_sync` mirrors the current-revision data directory (`~/snap/<app>/<current-rev>/`, resolved through snapd's `current` symlink) plus the revision-independent `~/snap/<app>/common`.

A revision directory travels only when the **target is on that revision**. `folder_sync` runs after the package jobs and asks the target which revision each snap is at, so it never plants data for a revision the target's snapd never installed: retained older-revision directories stay home, and so does the current one whenever the two machines have not ended up on the same revision — you skipped that snap's revision change, it failed, its install was declined, or `snap_sync` is disabled and nothing converged anything. `~/snap/<app>/common` and the `current` symlink always travel. The practical consequence of running `folder_sync` with `snap_sync` off is that per-revision snap data stops being mirrored: nothing in that run establishes that the two machines agree on a revision, so nothing may be written under one.

A snap installed from a local `.snap` file (`snap install --dangerous`, `snap try`) is the one thing snap sync leaves alone. Such a snap has a revision no store can serve — `snap list` shows it with an `x` prefix, `x1`, `x2` — and pc-switcher has no way to carry the file itself to the other machine. Sideloaded snaps are left alone, on whichever machine they sit: they are never installed, never removed, and produce no review item, nor does a hold set on one. The run does not mention them either. A snap only the target has is no exception — the run will not offer to delete something it could not put back. Reproducing one on the other machine is manual work.

To keep the revision from changing mid-sync, snapd's **automatic** refresh is briefly paused on both machines for the duration of the run (snapd auto-refreshes several times a day, even for closed apps). The pause blocks only automatic refreshes; snap_sync's own `--revision` convergence still works. Each machine's prior refresh policy is read before the pause and written back when the run ends, so a hold you set yourself — including an indefinite one — survives the sync. If that prior value cannot be read on a machine, that machine's refresh policy is left untouched: no pause is set there and nothing is cleared afterwards, because a pause written over an unknown policy could not be put back — it would expire into "no hold at all" and take your own hold with it. The run says so and continues unpaused on that machine.

## Flatpak refs

A flatpak app comes from the source's remote or it does not arrive. Same *name* is not enough: two remotes can be called `flathub` and point at different repositories, serving different builds of the same app with nothing said about it. So before each install pc-switcher re-reads the target's remote list and requires that app's origin remote to carry the source remote's URL and verification setting, and after the install it reads the target's remotes again and checks what the app's origin actually resolves to on those same two counts. Either check failing is that app's own failure, naming both URLs — never an install issued in hope.

Those two checks guard an install, and an app already installed on both machines issues none — so the case they cannot see is reported instead. The same app, same scope, same branch, coming from different remotes on the two machines is an **origin divergence**: the review names both remotes and both URLs and proposes nothing, and it takes precedence over a version difference on the same app, because two remotes' builds are numbered independently and showing the two numbers would read as ordinary drift. There is nothing to converge: flatpak refuses to install a ref that is already installed from another remote, so the only mechanical resolution would be uninstalling the app you have and reinstalling it from the other remote. Deciding which machine is the odd one out is yours.

The comparison runs on URLs here too, so a remote the two machines merely named differently is not a divergence and two remotes sharing a name and pointing at different repositories is one. If a machine's app names a remote that machine no longer configures there is no URL at all — and no URL matches nothing, not even the same missing URL on the other side, so that app is reported as coming from a different origin and the entry says which side's URL is missing. Calling two unresolvable origins the same origin would state agreement on no evidence.

A flatpak app is identified by its full `<application>/<arch>/<branch>` reference, not by the bare application id, and that reference is what the install and the uninstall name. Two branches of one app can be installed side by side, and a remote can offer several — flatpak refuses to guess between them and exits with `Multiple branches available`, so an app whose remote carries more than one branch never converges when only the id is named. The review line therefore shows the branch, and the same app on `stable` on one machine and `beta` on the other reads as an install plus a removal rather than as a version difference.

## Flatpak remotes

A flatpak remote is **derived** from the apps approved from it, exactly as an apt repository is. You never approve a remote directly, whether it is being added, repointed or deleted: approving an app is what makes its remote arrive, and declining the app is the only way to decline the remote. Otherwise an app could be approved with the only thing that could deliver it declined — or, worse, approved from a same-named remote whose URL change was declined, meaning from a different source.

A remote the source has that feeds no app approved in this run is not provisioned at all. There is no flatpak equivalent of the distribution's own repositories: a fresh flatpak install configures **zero** remotes and a machine with none is a perfectly ordinary machine, so even Flathub is synced only as a consequence of something needing it.

Derivation includes the **runtime** an approved app is built against: the app's install pulls its runtime too, and if the source holds that runtime from a different remote, the app's own remote alone would leave the install unable to resolve it — so that remote is synced as well. Scope is still identity, so `flathub` in the user installation and `flathub` in the system installation are provisioned separately, and a user-scope app derives only the user-scope remote.

A remote is synced with its **trust**, not only its name and URL. pc-switcher captures whether the source verifies the remote's signatures and, when it does, the remote's own signing key, and provisions the remote on the target with that key imported (`flatpak remote-add --gpg-import`). The key is copied byte-for-byte from the source machine and never fetched from the network — the same rule apt signing keys follow. Without it a provisioned remote would be configured but unusable: every install from it fails with `Can't check signature: public key not found`. A verified remote is never turned into an unverified one. A remote the source itself does not verify is provisioned unverified, and the run warns once per such remote, in a dry run too: nothing checks what it serves on either machine, and a successful provisioning would otherwise read as a remote you can trust.

Some remotes carry no key of their own. flatpak also verifies against `/usr/share/ostree/trusted.gpg.d`, which belongs to the machine rather than to any one remote — a keyring dropped there by a vendor package trusts every remote at once. pc-switcher does not replicate that directory: it takes the files the target is missing and imports them into the replicated remote's **own** keyring instead. The target's copy of the remote then verifies exactly what the source's copy verified, and nothing else on the target gains trust it did not have. A key the target already holds is left alone.

A key can also sit outside both places, named by the ostree `gpgkeypath` option in the installation's own repo config — a setting only a hand edit puts there, since flatpak never writes it. pc-switcher reads it and carries the files it names, a directory meaning every file in it, exactly as it carries any other key.

If the source verifies a remote and has no key for it anywhere — not its own keyring, not that directory, not a `gpgkeypath` — there is nothing to sync, and the run says so once. That remote refuses installs on the source too; the target ends up in the same state rather than a better-looking one.

A remote that already exists on both machines with a differing URL, verification setting or signing key is repointed in place, without a review line, keeping the apps that name it as their origin intact. A target that already trusted a different key for that remote ends up trusting both — flatpak merges imported keys rather than replacing them.

There is one exception, and it is apt's repository-conflict rule in a second ecosystem: if a differing URL or verification setting would repoint a remote that an app you marked machine-specific on the target takes as its origin in that scope, you are shown both configurations — the target's first, one differing field per line, never a computed diff — and asked to overwrite or leave it for now. A machine-specific app is invisible in the review by design, so nothing else in the run would tell you its updates were about to come from somewhere else. The entry names those apps. Two answers, nothing recorded either way; leaving it fails every approved app that needed the source's URL, quoting your own decision. A key-only difference never raises it: importing a key can neither move an app's origin nor withdraw trust.

A remote that cannot be provisioned has no review item of its own to fail, so the failure lands on every app that needed it, naming the remote and quoting flatpak's own error.

A remote you restricted with a **filter** keeps it. `flatpak remote-modify --filter=<file>` records the file's path, not its content, so pc-switcher copies the file byte-for-byte to the same absolute path on the target and applies it there. Like a signing key it is derived: no review line, and declining the apps is the only way to decline it. It lands *before* the apps do — the remote is added, its filter set, and only then does anything install from it — so no run ever leaves that remote on the target offering more than either machine meant. A filter only the **target** had comes off in the same step when you no longer filter that remote on the source, which is what makes the two machines converge. If the filter cannot be copied, written or applied, the run warns naming the remote and the path, every approved app from that remote fails with the same reason, and the target's remote is left exactly as it was. Neither direction reaches a remote no approved app needs this run: a remote you install nothing from keeps the filter it has, on both machines.

A filter that denies an app the source itself has installed from that remote ends the run before anything is written, naming the app, the remote and the filter. That machine is saying both "install this from there" and "nothing from there may be installed"; correct the filter, or uninstall the app, and sync again.

A user-scope filter is written as the ordinary SSH user, so one kept outside your home directory on the target fails rather than escalating a user-scope run to root.

A remote the source no longer has is **deleted**, not offered. Nothing is asked, because nothing about it is yours to decide once nothing uses it: the run counts what the target actually holds when the apps are done — every installed ref, runtimes and apps you marked machine-specific included — and deletes the remote only if nothing names it as its origin. While anything still does, the remote stays and the log says which refs kept it. A removal you approved that then failed leaves its app installed, so its remote stays too. Deleting a remote also drops its signing key, since flatpak stores that key with the remote.

## Holds and masks

Beyond *what is installed*, pc-switcher also replicates what you deliberately set to stop a package from moving: apt holds (`apt-mark hold`), per-snap refresh holds (`snap refresh --hold`), and flatpak masks (`flatpak mask`).

None of them is a review item. They travel exactly as apt version *pins* do — because the software they apply to travels, without a line of their own and without a question. A hold or a mask changes nothing about what software exists, only about what may move, so replicating it costs nothing and there is nothing per-package to get wrong. What the source has, added and removed alike, is what the target ends the run with; the log names each one as it lands, and a dry run previews it.

Because they follow the software, an install you decline takes its hold with it. Nothing is held on a machine that did not get the package: `apt-mark hold` accepts a package that is not installed and a hold recorded that way blocks every later attempt to install it. The run says the hold was not applied — not a failure, since your own answer is what withdrew it. If the install was approved and then failed, or the package is only reported because the run cannot reproduce the repository it comes from, the hold fails as its own item.

A flatpak mask is different in one way: it lands whether or not the application it covers is still there. A mask says what may not be installed, so masking software the machine no longer has is exactly what it is for. Masks are patterns, replicated whether or not a matching ref is installed; a pattern edit reads as remove-old plus add-new, and a user/system scope change as add plus remove, reported as found rather than normalised. Masks land after the applications, so one can never suppress a dependency the same run is pulling in.

Replicating a hold never touches the package's version: a held apt package is never installed or upgraded by a sync, and its version is left exactly as it is. A package the target has and holds produces no item at all — apt refuses to move it, so an item proposing that could only fail.

There is one case where the version is not the target's own to choose: an apt package the source holds and the target does not have at all. apt gives a hold no way to say "at whatever version you happen to get", so installing the target's version and then holding it would freeze the two machines apart for good — nothing moves a held package again. That install therefore asks for the source's exact version. If the target cannot supply it, the install fails as its own item naming both versions, and the hold fails with it rather than pinning a package that is not there.

### A hold on a package the machine does not have

`apt-mark hold` accepts a name that is not installed, and the hold it records then refuses every later attempt to install that name — while freezing nothing, because there is nothing there to freeze. It is a bookkeeping mistake rather than a state worth replicating or protecting.

If either machine carries one, the run **stops**, before anything is written. It names the package and the machine and gives you the `sudo apt-mark unhold` that clears it. Clear it and sync again.

Snap and flatpak need no equivalent: snapd records a refresh hold only against an installed snap, and a flatpak mask is a rule about what may be installed rather than a freeze on an installed copy.

## Deletions

Removals propagate for the three package managers. A package removed from the source's `apt-mark showmanual` set, a snap uninstalled on the source, or a flatpak ref removed on the source becomes a removal review item on the target — starting at **skip now**, so you approve deletions deliberately.

Deletion is where an apt repository file and an apt pin file are still review lines — the repository conflict is the only other one — and both take **two** answers rather than three: delete it, or leave it on the target. A flatpak remote gets no line at all: see [Flatpak remotes](#flatpak-remotes).

A repository file the target still gets software from is **not offered at all**. Deleting it would leave those packages installed and never updated again, which is not a trade you can make usefully — and the packages most at risk are the ones you marked machine-specific, which are invisible in the review by design. "Still gets software from it" is counted after the removals you approve in this same review, which is why the offer comes in the second pass: a repository whose last package you decide to keep is never offered, and one whose packages are all going is offered alongside them. The run logs the repositories it kept back and why. One that is offered is named by its filename *and* by the repository URLs it declares — `nomad would stop getting software from https://cli.github.com/packages` — because the filename is whatever whoever created the file happened to call it, while the URL is what the deletion actually takes away. A file declaring no URL says so rather than trailing off.

"Still gets software from it" is counted over every package installed on the target plus the ones you marked machine-specific, minus the packages this run proposes to remove — so removing a repository together with the software it feeds, the ordinary cleanup case, still works. That subtraction is what the offer is based on, and it assumes you approve those removals. If you then approve the repository and skip one of them, the file stays: something on the target still installs from it, and the run says so rather than deleting it and stranding the package. It is offered again next run. Packages apt pulled in automatically count too: a removal here is `apt remove`, never `apt autoremove`, so nothing takes them away when their reason goes, and a package you kept can still need them. The link comes from `apt-cache policy`: pc-switcher matches the origin of each of those packages' installed version against the URIs in the repository files. A package installed from a bare `.deb`, or one whose repository was already gone, has no resolvable origin and holds nothing back.

A pin file is shown **whole**: its content is printed above the screen, one file at a time, the way the repository-conflict screen prints two. `99-pin.pref` says nothing about which origin it favours or by how much, and the filename is all a decision row can show. Reading it costs one `sudo cat` per pin file offered for deletion, and only on a run that offers one. There is no permanent answer, and nothing about the answer is recorded either way: a machine-local mark on a file whose whole purpose is to feed packages would silently and permanently change where those packages come from, and the remedy for two machines whose configurations have drifted is consolidating them. `/etc/apt/apt.conf.d` is the counter-case: it keeps the full three-way decision and the permanent mark, because a proxy or a recommends policy is a standing preference someone genuinely holds per machine.

The distribution's own source files are never offered for removal at all.

`manual_installs_sync` is **install-only**: it reads the target only to tell what is already there, and keeps no record of what it put there, so it never proposes removals. Removing a hand-installed item on the target is manual work today (tracking removal for manual installs is deferred to a future issue).

## Prerequisites: passwordless sudo

Each enabled package job needs passwordless sudo for a handful of binaries:

- **`apt_sync`** — on the source (to read `/etc/apt` configuration) and the target (to install packages, write and remove `/etc/apt` configuration including signing keys, and set or clear apt holds via `apt-mark`).
- **`snap_sync`** — on both the **source** and the **target**; validation fails if either lacks it. The target needs it to install, refresh and remove snaps, and both hosts need it to pause snapd's auto-refresh for the sync window (`sudo snap set system refresh.hold`, plus the matching `sudo snap get` — snapd requires admin rights to read snap configuration as well as to write it). The runtime pause itself tolerates a transient failure without aborting the sync, but the sudo grant is checked up front on both machines.
- **`flatpak_sync`** — on the target only, and only when a system-scope ref, remote or mask is in play on either machine. A user-scope-only sync never asks for root.
- **`manual_installs_sync`** — a snippet author decides its own privilege needs; the replay itself runs unprivileged, so the job requires no sudo beyond what a given snippet writes for itself.
