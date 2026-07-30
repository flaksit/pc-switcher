# ADR-020: Declarative package convergence — a package replicates as (name, origin), and repository configuration follows the packages approved from it

Status: Draft

Date: 2026-07-23

## TL;DR

The source captures a manifest of package-related items; the target diffs its own state against it and converges using `apt`, `snap` and `flatpak` themselves — package databases are never rsynced. A package replicates as (name, origin), not name: the review decides packages, holds, masks and apt config, while repositories, their keys, their pins and flatpak remotes are derived from the packages approved from them and are never reviewed when added or changed — a flatpak remote is never reviewed at all. Four independent jobs (`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_installs_sync`) each run plan then review then apply inside their own `execute()`, with every change approved before it is made and the questions batched into one screen per manager per action.

## Implementation Rules

**Required:**
- `/var/lib/dpkg`, `/var/lib/snapd` and the flatpak OSTree store MUST NOT be rsynced or otherwise file-mirrored; convergence happens only through `apt`, `snap` and `flatpak` invocations.
- An apt package's identity for replication purposes MUST include the origin URIs its installed version comes from on the source. A flatpak ref's identity MUST be its full `<application>/<arch>/<branch>` ref within its scope, and its origin MUST be compared by the remote's URL, never by the remote's name.
- An approved install MUST be refused when the target's real origin — read off the target after this run's own writes and before or after the install, whichever the ecosystem allows — does not match the source's. Origins declared by the distribution's own apt source files are exempt from that check.
- A repository file, its signing keyring, the `preferences.d` pins and a flatpak remote MUST be written as derived mechanism, never as review lines: they land because a package or ref approved in the review comes from them.
- Each package job MUST obtain the user's approval for a change before it makes that change. Reviews SHOULD be batched — one screen per manager per action is the norm and the shape to design for — and a job MUST NOT degrade a batch into a per-item question queue for the ordinary item classes. The exceptions are enumerated and are about what the screen must SHOW: repository and pin deletions, repository/remote conflicts, manual collateral and unreproducible items are asked one screen at a time (D-24). A job MAY ask again, including after it has begun writing, when the answer it needs rests on facts this run's own changes invalidated or that could not be established before the first write: correctness outranks the batching preference.
- `/etc/apt/apt.conf.d`, apt holds, snap holds and flatpak masks MUST stay reviewed items in all three directions (add, change, remove) with the full three-way decision and the machine-local registry.
- Removing an apt source file or a `preferences.d` pin file MUST offer exactly two answers — remove, or skip once — and MUST NOT be recordable in a decision file. The same two answers, and the same non-recordability, apply to the repository- and remote-conflict prompts. An apt source file MUST NOT be offered for removal while anything on the target still uses it, and a flatpak remote MUST NOT be a review item in add, change or remove alike (D-41).
- `ubuntu.sources`, `/etc/apt/sources.list` and the `ubuntu-esm-*` source files MUST be written when missing and overwritten when different, and MUST NEVER be offered for removal or removed. When an `ubuntu-esm-*` file would be written and the target reports no Ubuntu Pro attachment, `apt_sync` MUST ask the user before its first mutating command, with exactly two answers: attach now (pc-switcher re-probes the target and continues) or skip `apt_sync` for this run while every other job runs. A run with nobody to ask MUST take the skip.
- Only files under `/etc/apt/sources.list.d` carrying an extension apt itself reads (`.sources`, `.list`) may be captured, compared or written; anything else in that directory MUST be left alone.
- A derived write that fails MUST fail every package or ref that depended on it, naming the file or the remote — not an item of its own.
- What a package job's log must name, and what it must withhold, is ADR-021's — including the collateral no review shows and the credentials a repository URL can carry.
- The `/etc/apt` convergence group MUST be transactional: a group whose metadata refresh fails MUST leave the target's `/etc/apt` as it found it.
- A package manager's own transaction MUST be constrained to what the review approved: what it would change beyond the approved item MUST be determined at plan time and classified there, because apt's own rehearsal states that transaction in advance and there is nothing left to discover once the apply runs. Collateral protection keys on the TARGET's `apt-mark showmanual` set. An install the target's package manager cannot yet resolve MUST NOT enter the plan-time rehearsal at all, and MUST be protected by the apply-time guard alone.
- `apt_sync`, `snap_sync`, `flatpak_sync` and `manual_installs_sync` MUST be four separate `SyncJob`s, each with its own enable flag, validation, progress reporting and `JobResult` — never merged into one `package_sync` job. `manual_installs_sync` MUST carry its own `sync_jobs` enable flag so disabling apt cannot silently disable manual-install detection.
- Machine-local decision files MUST live at `~/.config/pc-switcher/<manager>.decisions.yaml`, one per manager, excluded from `folder_sync` non-overridably and outside `config_sync`.
- Whether an unreproducible item counts as reproducible MUST be judged by whether the SOURCE holds a snippet, never by whether the target already does. The install-snippet registry (`~/.config/pc-switcher/package-snippets.yaml`) MUST be pushed to the target by `manual_installs_sync` itself and replayed there the same run — including a snippet authored on the fly during that run's review. A push that would lose or change an entry only the target holds MUST require explicit confirmation and abort the run when it cannot get it. `config_sync` MUST NOT carry the registry.

**Forbidden:**
- No `--delete` file mirror of `/etc/apt` or any other package-database directory.
- No installing a package or a ref from an origin the source does not use, in preference to reporting that the origin cannot be replicated.
- No change applied that the user did not approve, and no batched review degraded into a question per item.
- No decision-file entry for an apt source, an apt pin or a flatpak remote, in any direction.
- No re-fetching signing keys or remote keyrings from a vendor; a repository's or a remote's key travels with it, byte-for-byte from the source machine.
- No standing block on a package manager's own auto-update left behind by a run. A transient guard across the sync window is allowed if it is restored on cleanup and self-expires (D-06).
- No writing an `ubuntu-esm-*` source to a target that reports no Pro attachment without asking, and no attempt to attach the target on the user's behalf.
- No component outside a package job may own that job's review; the review call stays inside the job's own `execute()`.

## Context

Phase 2 must replicate presence, version and provenance of packages across apt, snap and flatpak, plus the repository/keyring/pin/remote configuration those installs depend on. Package data under `~/.var/app`, `~/snap/<app>/common` and dotfiles is already Phase 1 `folder_sync` territory; this ADR concerns the packages themselves and the configuration that governs where they come from.

Provenance is the hard part, because a package name is offered by more than one vendor: `firefox` exists in Ubuntu's archive and in Mozilla's own repository, and `org.mozilla.firefox` exists on Flathub and on Flathub-beta. Matching by name alone replicates the name and inverts the provenance, silently and at exit 0. Replication is therefore of (name, origin), and the repository configuration that makes an origin reachable is mechanism serving that guarantee rather than something the user is asked about — a repository ticked without its package does nothing, a package ticked without its repository cannot be installed, and the pairing was never expressible in a checkbox list.

The review boundary is the manager: each package job captures its own diff and surfaces it for an explicit decision before it applies anything, and no review spans more than a single manager's items. This keeps four deliberately independent jobs independent — each owns its own capture, review, apply, failure isolation and progress — rather than binding them to a shared review that would give them a common ordering and failure surface.

## Decision

### Convergence model (D-01)

The source captures a manifest; the target diffs its own state against it and converges using `apt`, `snap` and `flatpak` themselves. `/var/lib/dpkg`, `/var/lib/snapd` and the flatpak OSTree store are never rsynced — the package managers stay authoritative for their own state.

### Item model (D-02)

An item is something the user can meaningfully decide about, and it carries a stable identity through one diff → decide → apply pipeline. The item classes are the apt package, the apt config file, the apt hold, the snap, the snap channel, the snap hold, the flatpak ref, the flatpak mask, the unreproducible/manual install, and — for removal only — the apt source file and the apt pin file. A flatpak remote is not an item at all (D-41).

Standing user intent gets its own identity even when it is attached to something else: a deliberate block on a package (apt hold, snap hold, flatpak mask) is a decision in its own right and replicates as its own item, separate from the package it applies to.

Mechanism the user has no basis to judge is not an item and is the job's own business to keep correct. That is what puts repository files, signing keys, pins and flatpak remotes outside the review in the add and change directions (D-34, D-36, D-37, D-41): they exist to make an origin reachable and trusted, so the package decision already implies them. A transient guard a run sets and clears is not an item either.

### Manifest content for apt (D-03)

The manifest carries the manually-installed set from `apt-mark showmanual`, not the full dpkg selection set; apt resolves dependencies on the target.

### Version policy (D-04, D-05)

Versions float to whatever the target's repos currently offer; version mismatch is a reported diff class, never a forced downgrade. This float-only policy is apt and flatpak, with one exception inside apt: an apt hold blocks install, upgrade and removal alike, so it carries the intent "do not move this off the version that works" as well as "do not lose this". Where the source holds a package the target lacks, the target therefore takes the source's EXACT version, and the install fails naming both versions when that version can no longer be had — installing another version and then freezing it there is worse than ordinary drift, because nothing will move it again. Snap is the deliberate exception: it converges revision and channel (D-06), because snap is the only ecosystem that embeds the version in its per-user data path (`~/snap/<app>/<rev>`), so keeping both machines on the same revision is what lets `folder_sync` mirror that data cleanly.

Version *constraints* travel even though versions float: `/etc/apt/preferences.d` pins always sync (D-36), so deliberate pinning replicates while incidental version skew does not.

### Revision/scope convergence (D-06)

The snap manifest carries name + channel + revision; the flatpak manifest carries ref + origin + user/system scope. Snap convergence pins a revision explicitly and must never leave a snap blocked from auto-refresh once a run ends.

snapd auto-refreshes in the background (~4×/day, even for closed apps), which could move a snap off the converged revision mid-run and desync the data dir `folder_sync` is mirroring. The orchestrator therefore pauses snapd's automatic refresh across the whole sync window on BOTH hosts, restoring each host's prior setting on cleanup and relying on a timeout so a crashed run does not leave the pause behind. This gates only the auto-refresh manager, not the explicit revision convergence.

### Decision shape and direction (D-07)

The default is a three-way decision: apply / skip once / skip always — the names of the three decisions, not the words a screen shows for them (D-24 rules the wording). A `REPORT_ONLY` item takes NO answer at all, superseding the earlier rule that gave it apply/skip-once: it converges in neither direction and records nothing, so both answers left both machines and the next run identical. Such items are printed, grouped by the `DiffClass` that caused them, each group titled by that cause and carrying the remedy where there is one — a version difference resolves itself, so its group names the upgrade command. "Apply" is direction-dependent — missing on target means install/add/enable, extra on target means remove/delete/disable, different on both means change the target to match the source. The review names the concrete action per item (e.g. "remove brscan3", not "apply").

Two cases take **two** answers — act, or skip once — and record nothing: an apt source removal and an apt pin removal (D-37), and the repository- and remote-conflict overwrite prompts (D-37, D-41). A flatpak remote is never asked about in any form (D-41). A permanent machine-local mark on a file or a remote whose whole purpose is to feed packages would silently and permanently change where those packages come from; the user's remedy is consolidating the two configurations, not recording a preference. Packages, holds, masks and apt config keep the three-way decision.

Every question is asked on the same kind of screen, whether it carries one item or twenty: rows, glyphs, one key per answer, and a sentence beside each key. A question that cannot be batched is asked one screen at a time, never in a different widget: where the answers must name one item's own change, where answering opens an editor, or where the item's own content has to be READ before it can be answered — a pin file's body, a repository's two versions — because a batch printed those in a row and then asked about the ones that had scrolled away. Every screen states its answers as effects on a NAMED machine, and never as the tool's own vocabulary for the two ends of a run. The two machines are identified by hostname in every title, detail, prompt and answer the user reads; "source" and "target" stay in code, docstrings and logs.

The answers on one screen MUST also read as a set: one grammar across all of them, and the machine named on every answer or on none. Concretely, and taking precedence over the earlier form of this rule ("keep it on `nomad`" as one answer's whole text): a screen's answers are its own act verb, `skip now`, and — where permanence is offered — that act refused for good, said as the act ("never install"; "keep for good" where the item is already on the machine). Each carries a legend sentence of its own naming the machine it happens to and how long it lasts, which is where the machine is named and where a recorded answer says it will not be asked again. Naming one machine inside a single answer's text, beside two answers that named none, read as though those two were about something else. Elsewhere unchanged: "`atlas` has 1.0-1, `nomad` has 2.0-1" rather than "source has …, target has …", and the two panels of a conflict prompt titled with the machines that hold them.

Report-only diffs (version mismatch, origin mismatch, an origin that cannot be provided) offer apply or skip only. They carry no converge verb, so there is no holder machine for D-08a to record a permanent decision on, and recording one would suppress the item entirely rather than stop reporting the drift the user meant. They are resolved by fixing the underlying condition.

### Machine-local decision file (D-08, D-08a, D-09, D-10)

One file per manager lives at `~/.config/pc-switcher/<manager>.decisions.yaml`, never synced, excluded from `folder_sync` non-overridably and outside `config_sync`. An entry on machine M makes the item inert on M in both roles — not pushed when M is the source, not installed or removed when M is the target. The entry is written on the end of the connection that holds the item: source-held item declined is recorded on the source, target-held item whose removal is declined is recorded on the target.

### Repository configuration is derived mechanism (D-11, D-12, D-13, D-14)

`/etc/apt` is not file-mirrored: a `--delete` mirror would wipe the target's own machine-specific sources, and it is not an inventory of reviewed items either. Four buckets cover every file:

- **Derived from approved packages** — repository files under `/etc/apt/sources.list.d`, their signing keyrings, and conflict-free overwrites of a repository present on both machines (D-34, D-37).
- **Always synced** — every `/etc/apt/preferences.d` pin (D-36), and the distribution's own source files (D-38).
- **Reviewed with two answers** — repository and pin removals, and the repository-conflict overwrite (D-07, D-37). A repository removal is raised only once nothing on the target still uses that repository, packages marked machine-specific included; while anything uses it the file stays and is never mentioned. A deletion is presented with the thing itself, not with its filename: a repository names the URLs it declares, and a pin file is shown whole. A filename is whatever created the file chose to call it, a pin's filename says nothing about which origin it favours, and D-36's rule against reading a pin as an inventory rules out summarising one instead.
- **Reviewed with the full three-way decision** — `/etc/apt/apt.conf.d` (D-37).

Only files carrying an extension apt itself reads (`.sources`, `.list`) are captured, compared or written; `sources.list.d` is a directory users and packagers also drop `.save`, `.distUpgrade` and editor backups into, and treating those as repositories would propose changes apt would never act on.

A repository's signing key travels with the repository, byte-for-byte from the source machine, and is never re-fetched from a vendor: the target must end up trusting exactly what the source trusts, and a fresh fetch would silently substitute whatever the vendor serves today. The same rule governs a flatpak remote's ostree keyring (D-41). A repository whose key cannot be made to work on the target is reported rather than written.

Ordering is a guarantee, not an accident: every repository, key, pin and flatpak remote an approved item needs is provisioned on the target before the install that needs it (D-14). For apt this is the `/etc/apt` group converging ahead of the first `apt-get install`; for flatpak it is the derived remote writes running ahead of the first `flatpak install`.

### Job split into four jobs (D-15, D-16, D-17, D-18)

Four jobs — `apt_sync`, `snap_sync`, `flatpak_sync` and `manual_installs_sync` — over one shared core extracted while building, not deferred to a post-hoc refactor. The core is what all four use: the item taxonomy, the plan/review/apply order, the decision flow, the batched TUI review, the machine-local file I/O and the read-failure guard (ADR-022). Each manager's own item shapes and its own diff stay in that manager's module — one manager's diff on the shared base would make the other three inherit inputs they never supply, which is the coupling D-15 exists to prevent. All four package jobs run before `folder_sync`, `manual_installs_sync` included, so software is provisioned before its data lands (decisive for flatpak, where `~/.local/share/flatpak` must exist before `~/.var/app` arrives). A snippet-installed program writes its own stock defaults exactly as a package does, so the ordering rule covers it too.

`manual_installs_sync` owns everything no package manager can reproduce: the apt packages installed from no configured repository and the scan for unowned installs under `/usr/local` and `/opt`, plus the snippet registry.

### Batched review inside each job (D-24)

Each package job runs plan then review then apply inside its own `execute()` and converges only what the review approved: approval precedes the change it authorises, always. Reviews are batched — one screen per manager per action is the norm and the shape every job is designed for — and a job never turns that batch into a question per item.

Batching is a strong preference, not a ban on asking twice. A job may ask again, including after it has begun writing, when the answer it needs rests on facts this run's own changes invalidated or that no read could establish before the first write. Correctness outranks the batching preference, and some things are knowable only once an action has landed.

As the four jobs stand, none needs a second review, which is a property of their diffs rather than a rule they obey: a package's classification depends on the SOURCE's origins, which no run mutates, so no fact a review is answered on can be invalidated by what that run writes; the one fact that does depend on this run's writes — whether the target really ends up with the source's origin — is checked by D-35 and reported as a per-item failure rather than re-asked. `apt_sync` therefore has no second review pass and needs none — nothing this section permits is an argument for adding one back. Where the facts genuinely do change, a second question is the correct answer.

Grouping by action matters because "apply" is direction-dependent (D-07): installs and removals show as separate groups, removals labelled as removals, so a bulk tick can never silently delete.

Three properties are absolute. Nothing is applied that the user did not approve, and approval precedes the change it authorises. No review spans two managers — the jobs are independent by D-15, so a single owner reviewing every enabled manager at once would contradict that independence, which is why there is no shared review phase and no coordinator. And a job never degrades a batch into a per-item question queue.

### Diff taxonomy (D-25)

Conflicts, mismatches and unavailability are diff classes inside the job's own review, not a second reporting mechanism: missing-on-target, extra-on-target, version-mismatch (both versions shown), origin-mismatch, origin-unavailable, unreproducible. `ORIGIN_MISMATCH` covers a package or ref installed on both machines from different vendors. `REPO_UNAVAILABLE` means "the source's origin cannot be provided on the target" — a statement about provenance, not about whether apt happened to print a candidate.

There is no per-package echo of a pin or a hold. A pin's only job is deciding which origin wins, which D-35 checks against the target's real state; echoing it onto every package it names would make a pinned target-only package impossible to remove and impossible to silence.

### Transactional repository convergence (D-27 boundary)

The `/etc/apt` group is the one place D-27's continue-and-report model does not apply: it is transactional, and a group whose metadata refresh fails leaves `/etc/apt` as it found it. Continuing past a bad write would leave the target's package manager unusable, and automatic snapshot rollback does not arrive until Phase 7.

### Unreproducible items and where a run terminates (D-21 with D-26 and D-27)

An unreproducible item ends a run resolved in one of three ways: it has a snippet, it is recorded machine-specific (skip-always), or the user chose to skip it once. Skip-once is a real decision, not an unresolved state — the user may be declining something temporary, and a run where they made that choice is clean. pc-switcher offers to add a snippet on the fly during the review, so resolving an item never requires leaving the sync. In an interactive review the "unresolved" outcome is unrepresentable, and abandoning the review aborts the whole sync rather than manufacturing a skip-once. Only a non-interactive run leaves items undecided; it does not fail on undecided items alone, because the user was never given the chance to resolve them (D-26).

### Unreproducible items and snippets (D-18 through D-23)

Detection covers apt packages whose installed version comes from no configured repository — a bare `.deb`, whose only origin is dpkg's own status file — and unowned installs under `/usr/local` and `/opt`, and is owned by `manual_installs_sync`. An install snippet is an opaque text blob replayed non-interactively through the existing executor with the exit code deciding success — the tool never parses, versions, diffs or reasons about snippet content. Snippets live in the shared, synced config (`~/.config/pc-switcher/package-snippets.yaml`) and cover bare `.deb`s and manual installs only; snap and flatpak items do not carry snippets (YAGNI — every current one comes from a reachable remote).

Whether an item counts as reproducible is decided by whether the SOURCE — the machine being replicated — holds a snippet, never by whether the target already does. `manual_installs_sync` pushes the registry to the target itself and replays it the same run, so a snippet authored on the fly during that run's review takes effect immediately rather than next run. The push is a wholesale overwrite gated on being non-destructive: one that would lose or change an entry only the target holds needs explicit confirmation, so a snippet the user only has on the target is never silently discarded. The registry does not travel via `config_sync`, which runs before any review and so cannot carry a snippet the user has not authored yet; and it does not rely on `folder_sync`, a user-controlled job that can be disabled or filtered — no job's correctness may depend on another job running.

### Failure and dry-run (D-26 through D-28)

Non-interactive runs skip all once and record nothing. A failing item does not stop the job — continue, collect, report, and the job result is a failure. The target always downloads from its own repos; no source-cache reuse.

### folder_sync overlap (D-29)

Package jobs export their owned paths to `folder_sync` via the ADR-018 mechanism, which turns them into non-overridable filters without knowing anything about either ecosystem: `flatpak_sync` owns `~/.local/share/flatpak`, and `snap_sync` owns the retained OLDER `~/snap/<app>/<rev>` revision dirs only. The CURRENT revision's dir is deliberately NOT excluded — `folder_sync` mirrors it, so the active revision's per-user app data travels, which is the whole point of converging the revision (D-06). Older revision dirs are excluded to avoid planting data for revisions the target's snapd never installed, and when the active revision cannot be determined all of that app's revision dirs are excluded as the safe default.

### Origin replication (D-34)

The unit of replication for an apt package is (name, origin), not name. The target must end up with the package installed from an origin the source installed it from. A package whose origin cannot be provided on the target — no source file declares it, or the file that does cannot be written — is reported, never installed from somewhere else. The failure mode this closes is a package name offered by two vendors, where name-only matching silently replicates the name and inverts the provenance.

A repository, its keyring and its pins are derived from the packages approved from it. A repository the source has that feeds no package this run syncs does not travel; the distribution's own files (D-38) are the deliberate exception.

A package the source has and the target does not falls into exactly one of four classes, and the class decides both what is derived and what may be rehearsed:

1. The target already has a candidate from an origin the source uses — an ordinary install, nothing derived.
2. The target has a candidate, but from none of the source's origins — an install plus the derived repository, key and pins that make the source's origin win.
3. The target has no candidate at all, and the source's origin can be provided — an install plus the same derived work. The target's apt cannot resolve the name until that repository lands, which is why this class is excluded from D-40's plan-time collateral rehearsal.
4. The source's origin cannot be provided on the target — `REPO_UNAVAILABLE`/`REPORT_ONLY`, never an install candidate.

### Origin enforcement, at the target's real state (D-35)

The guarantee is checked, not inferred. After the `/etc/apt` group's single `apt-get update` and before the first install, one batched `apt-cache policy` over the approved install names re-reads the target's candidate origins; an approved install whose candidate origins do not intersect the source's fails as its own item, naming both origins. Plan-time classification decides what to derive; only this check decides what may be installed.

Origins declared by the distribution's own source files are exempt, computed per machine. Two machines on different Ubuntu mirrors are not two vendors, and without the exemption every package on such a pair would fail.

### Pins are mechanism, not inventory (D-36)

A pin travels because it is what makes an origin win, in the same sense and for the same reason a signing key travels because it is what makes a repository trusted. Neither is reviewed in the add or change direction; pin adds and updates always sync, silently. A pin naming an origin the target does not have is inert, so always-syncing them costs nothing and cannot get a per-package derivation wrong.

The evidence that forces this: measured on the development machine, Ubuntu's archive offers `firefox` at version `1:1snap1-0ubuntu5` at priority 500. That version carries **epoch 1**; Mozilla's own `firefox` deb carries no epoch, and under equal priority apt takes the highest version, where any epoch-1 version outranks every epoch-0 version regardless of the upstream number. Adding the vendor's repository alone therefore still installs Ubuntu's package. Only the vendor's `preferences.d` pin, at priority 1000, changes the outcome — so a design in which repositories travel and pins do not would replicate the repository and still install the wrong package.

### The review's scope, and its one non-package exception (D-37)

The apt review decides packages. That is a near-rule, not an absolute: `/etc/apt/apt.conf.d` is reviewed in all three directions, with the three-way decision and the registry.

The distinction is derivability. Every other `/etc/apt` file earns its place by serving a package — a repository is where a package comes from, a keyring makes it trusted, a pin makes it win — so the package decision implies the file decision. An `apt.conf.d` file governs apt's own behaviour, and nothing about an approved package implies whether it should travel; with nothing to derive it from, the only honest source of the answer is the user. It keeps the registry for the same reason: a proxy or a recommends policy is a standing machine-local preference someone can hold permanently, unlike a repository removal, whose remedy is consolidating two files.

A repository removal is not raised while anything on the target still uses the repository — counted after this run's approved removals, and counting packages marked machine-specific — so the line has nothing left to strand and nothing to disclose. A repository present on both machines with different content is overwritten silently unless the overwrite would repoint such a package, in which case both file contents are shown side by side and the answer is overwrite or skip once. Only a repository this run writes for an approved package can raise that question, which is the gate D-41 already applies to remotes.

### The distribution's own source files, and ESM (D-38)

`ubuntu.sources`, `/etc/apt/sources.list`, `ubuntu-esm-apps.sources` and `ubuntu-esm-infra.sources` are written when missing and overwritten when different, and are never removed and never offered for removal. They define the distribution origins D-35 exempts, so they are the one repository bucket that does not wait for a package to derive it.

The two ESM files are gated on the target's Ubuntu Pro attachment. When they would be written and the target reports unattached, `apt_sync` asks — before its first mutating command — with exactly two answers: attach now, meaning the user attaches on the target by hand and pc-switcher re-probes and continues; or skip `apt_sync` for this run, while every other job runs. Writing them to an unattached target silently is not an option, because it leaves an apt that fails on a subset of installs for a reason the user will not connect to the sync.

The question precedes the review rather than joining it: one of its answers means there is no review to hold, and it asks about the target's environment, not about an item. It is still raised before the job's first mutating command, so nothing is written before it is answered. "Attach now" re-probes the target rather than trusting the answer, and may be given any number of times — re-probing is free and the exit is choosing to skip.

The hazard, **measured** in a stock `ubuntu:24.04` container carrying both real ESM source files copied from a Pro-attached host: `esm.ubuntu.com` serves its repository *index* publicly (HTTP 200 on `.../dists/noble-apps-security/InRelease`), so the suites are fetched, marked `Trusted: yes`, and enter candidate selection at priority 500 — above `noble/universe`. Only the *pool* is 401. The failure therefore lands at install time, not refresh time: `apt-get install 7zip` exits 100 with `401 Unauthorized` on the `.deb`. That container had 0 of 13 upgradable packages with an ESM candidate, which its tiny package set explains. **Measured** since on a Pro-attached 24.04 desktop: 60 of 2297 installed packages resolve their candidate to `esm.ubuntu.com` — `ffmpeg`, `gimp`, `imagemagick`, `7zip` and the `libav*` set among them — so roughly one installed package in forty is exposed.

An unattached target's `apt-get update` does **not** fail and does not roll the transactional `/etc/apt` group back — **measured** in the same container: it exits 0 with the ESM sources present and no credentials. A source that genuinely fails does not abort the others either: with the ESM keyrings removed the run exits 100 with `E: The repository ... is not signed.` and still fetches and writes all 19 other lists, and against a synthetic index-level 401 it exits 100 and writes all 27 others — a non-zero exit is an aggregate signal, never an abort, and triggers no rollback. The missing-keyring case cannot arise here anyway: `/usr/share/keyrings` is one of the three key directories `apt_sync` captures, so `ubuntu-pro-esm-apps.gpg` travels with the source file.

A run with nobody to ask takes the skip too. Withholding only the two files is not a coherent partial outcome: `preferences.d` always-syncs with no derivation predicate (D-36), so any pin the source holds that the target does not reaches it whether or not the sources that pin names do, leaving a candidate selection that matches neither machine. (The two pins `ubuntu-pro-client` ships are conffiles present on every Ubuntu regardless of attachment, so those two are identical on both machines and are not what travels — measured.) Skipping leaves the target's `/etc/apt` exactly as it was, which is a state the user can reason about. A `--dry-run` never asks: it warns that the target is unattached and that a real run would skip `apt_sync` entirely, because a rehearsal must not send the user to attach a machine and ADR-014 makes the preview the whole report.

The question cannot be answered by the tool. `pro attach` needs a subscription token from the user's Pro dashboard or an interactive browser short-code flow; the source machine's own credentials are root-only (`/var/lib/ubuntu-advantage/private/` is unreadable to the ordinary user) and a machine's token is not reusable to attach another machine; and holding a subscription token would put a secret on a command line. pc-switcher therefore asks, waits for the user to attach, and re-checks.

Detection is `pro status --format json` on the target — exit 0 for an unprivileged user, top-level `attached: true|false`, measured. Its payload also carries the subscriber's account; only the parsed boolean may be logged or shown.

### Derived-work failure attribution (D-39)

A derived write that fails does not fail an item of its own — there is no item, because the user decided about a package or a ref, not a file or a remote. It fails every approved item whose derived set contains it, naming the file or the remote and the reason. A rollback of the `/etc/apt` group fails all of them. This is also what a repository or remote conflict answered "skip once" does to the items that depended on it: a package cannot be quietly installed from the wrong origin because its repository was skipped.

### Collateral protection keys on the target's manual set (D-30, D-40)

apt may remove or downgrade packages other than the one named in order to satisfy dependencies, so the item the user ticked is not necessarily the transaction apt will run. The transaction is therefore determined at plan time and its collateral classified rather than blanket-refused, which would block a legitimate install whose only collateral is a dependency nobody chose: collateral apt pulled in on its own proceeds, while collateral that is manually installed becomes its own reviewable item offering the act / skip now / stop the sync. One item per screen — the cause and the effect differ per package, so the answers name them and no shared legend could — but on a decision screen like every other, not a picker of sentences (D-24). What is protected here is the TARGET's own `apt-mark showmanual` set, which is a fact reported by that machine's package manager and not a machine-specific mark anybody recorded; the prompt says so, because describing it as a recorded preference would attribute a decision the user never made. The stopping answer states its reach: it raises the ordinary user-decline exception, which the orchestrator re-raises untouched, so it ends the WHOLE sync and no later job runs — not merely this job, and not merely this question. The question belongs in the review because the answer it needs is already available there: apt's own rehearsal states the transaction in advance, so the collateral is knowable while the user is deciding about the install that causes it, and discovering it later would only mean having asked too late.

Three refinements the requirements review added. The transaction classes that raise the question are removal, downgrade and upgrade alike, not the destructive two only. Being offered for removal is not consent to lose a package: only a removal the user APPROVED exempts it from this protection, and one skipped for this run keeps it. And where the collateral package is marked machine-specific the question says so explicitly, because a marked package is structurally invisible to the review and this is the only line the user ever gets about it.

Declining collateral cancels the packages whose OWN transaction causes it, and nothing else. The batched rehearsal names a transaction but not its causes, so when — and only when — the batch turns up manual collateral, each candidate is rehearsed alone and blamed for the collateral its own transaction reproduces. The cost is one extra `apt-get --dry-run` per candidate on such a run, and none on a run with nothing to attribute; the alternative, blaming the batch, makes one collateral question cancel every package in the review. Collateral no single candidate reproduces is jointly caused — apt drops what depends on `a | b` only once both go — and is attributed to the whole batch, which is conservative and true. The prompt's wording has to hold in both cases, because it is the statement of what declining cancels: it names the one causing package where one causes it, and refers to the whole batch where the batch does.

A cancellation may never overwrite a decision the user made. Only an approval is downgraded: a trigger the user had already declined needs no cancelling, and one the user marked never-offer-again keeps that mark, which is read off the same decision map that the permanent record is written from.

The protected set is the TARGET's `apt-mark showmanual`, and only the target's. A package the user installed by hand on the source, which arrives on the target as an automatic dependency and is later removed as collateral, is not protected: if the target's apt installed it automatically, the target's apt owns it, and reclaiming it as a user choice on the strength of the other machine's bookkeeping is a guess. The narrower set is also the set apt itself consults, so "manually installed" means the same thing to pc-switcher and to apt on the machine being changed.

One package cannot be classified at plan time and is not asked about there: an install whose repository this run derives from the package's own approval and writes during converge. Until that write lands the target's apt has never heard the name, so `apt-get --dry-run install` refuses the whole batch containing it — and the rehearsal is one batch, so including such a name removes the protection from every other package in the run rather than weakening it for one. It is therefore excluded from the rehearsal on the evidence of the target's `apt-cache policy`, never on the simulation's exit code (ADR-022 D-01).

What covers it instead is the apply-time guard, which runs the same rehearsal per item after `/etc/apt` has converged and `apt-get update` has run, where apt CAN resolve the package: unapproved manual collateral fails that one item (D-27). The cost is real and accepted — for those packages the user is told afterwards rather than offered go-ahead / keep-the-package / stop-the-sync beforehand — because the facts that question needs do not exist while `plan()` runs. A package whose origin can never be replicated needs no rule: it is `REPO_UNAVAILABLE`/`REPORT_ONLY` and is never an install candidate.

### flatpak: remotes are derived from the refs approved from them (D-41)

A flatpak remote travels because a ref approved this run comes from it, in that ref's scope, and for no other reason. Remote adds and URL/trust changes are not review lines at all; the derivation also completes an approved app's runtime, whose own remote the app needs and which the app's own origin does not name. A remote the source has that feeds no approved ref does not travel.

A ref's identity is its full `<application>/<arch>/<branch>` ref within its scope. The bare application id is not enough: two branches of one id coexist in one installation, and both `flatpak install` and `flatpak uninstall` refuse an ambiguous id (measured). Origin deliberately stays OUT of identity — `flatpak install <other remote> <ref>` on an already-installed ref refuses (measured), so the install half of an origin "move" could never run and the removal half would propose deleting the app the user has. A ref present on both machines from different remotes is therefore `ORIGIN_MISMATCH`, reported and never converged.

Origin is compared by the remote's **URL**, never by its name. A target remote carrying the source remote's name and a different URL installs another vendor's build at exit 0 with no warning, and `flatpak list --columns=origin` reports the same name in both cases (measured). So before a ref is installed the target's remote list is re-read and the ref's origin remote must carry the source remote's URL and verification setting, and after the install the landed origin is read back and resolved to a URL again. This also catches the derived write that silently did nothing: `flatpak remote-add --if-not-exists <name> <different url>` exits 0 and leaves the old URL in place (measured), so the write's own exit code proves nothing.

Repointing a remote is silent derived mechanism, with one exception, which is D-37's repository-conflict rule applied to a second ecosystem: a remote whose URL or verification setting differs is overwritten without a word UNLESS a ref the TARGET recorded skip-always takes it as its origin in that scope, in which case both configurations are shown and the answer is overwrite or skip once. Machine-specific is the trigger, not target-only — a skip-always ref is structurally invisible, so nothing else in the review would ever mention it, while an ordinary target-only ref already has a removal line of its own.

A remote is never a review line, in add, change or remove alike. One the source does not have is deleted once nothing on the target still uses it — counted after this run's approved removals, and counting refs marked machine-specific as well as refs reported as an origin mismatch — and while anything still uses it, it stays. The refs are the software and the remote is plumbing, so removing the refs is the decision and the remote follows; asking separately let the user delete a remote whose refs are structurally invisible to the review and strand them. Masks keep the three-way decision and the registry, for D-37's `apt.conf.d` reason: a mask is a standing user preference about updating, not mechanism serving a ref, so nothing about an approved ref implies whether it should travel.

There is **no flatpak counterpart to D-38's distribution sources**. Flathub is not shipped or blessed by Ubuntu; flatpak installs with zero remotes configured and a machine with none is a perfectly ordinary flatpak machine (measured). So there is no always-synced remote bucket, no never-removed set and no attachment gate — even Flathub travels only as a consequence of a ref needing it.

A remote's ostree keyring travels with it byte-for-byte and is never re-fetched (D-12): without it a replicated remote is configured but unusable, because flatpak refuses every install from it with `Can't check signature: public key not found`.

### snap: nothing to derive (D-42)

Snap has no repository or key decision, so there is nothing for D-34's derivation to reach and no screen to invent. One store serves the device, and name→publisher is pinned store-side by a canonical-signed `snap-declaration` assertion snapd validates itself: one name resolves to one snap-id resolves to one publisher, so there is no second `firefox` for the target to install by accident (measured). Keys are snapd's own, not the user's, so there is no per-remote key material to copy. A device could in principle be provisioned against a brand store or pointed at a store proxy, but neither is ordinary snap use and neither is a per-snap fact pc-switcher could converge, so snap is treated as having one store.

Sideloaded snaps — installed from a local `.snap` file — are out of scope (#221) and are ignored on both machines: never installed, never removed, never an item, never the subject of a hold item. A run names the ones it found so the user knows they are unmanaged, and does nothing else. No store can serve such a revision and nothing carries the file between machines, so a snap the tool cannot reinstall must not be one it offers to delete; handling half the case from one job and the other half from another is worse than leaving it whole until it is designed.

The provenance variable that remains is which revision of that one snap is installed, and which channel it tracks. D-06 converges both: the manifest carries name, channel and revision, a difference in either produces one `CHANGE` diff, and a channel-only difference reads as a retrack rather than an install.

## Consequences

**Positive:**
- Provenance replicates, not just presence: a package or ref installed from a vendor on the source cannot arrive from a different vendor on the target, and the guarantee is enforced against the target's real state rather than inferred at plan time.
- Package managers stay authoritative for their own dependency resolution and state, avoiding the correctness problems of file-level package database replication.
- The review asks only what the user can answer. The unrepresentable pairing "package ticked, repository unticked" does not exist, and neither does the class of runs where an approved package could not be installed because its repository was declined.
- One batched screen per manager per action, so an ordinary run answers a job's questions in one sitting instead of item by item, and every approval precedes the change it authorises.
- Keeping each job's review inside its own `execute()` keeps four independent jobs independent — separate enable flags, config, validation, failure isolation and progress — with no shared ordering surface that could couple one job's failure to another's.
- Determining the real transaction at plan time catches collateral dependency changes before anything is applied, so a legitimate install is not blocked by a dependency nobody chose.

**Negative (costly to reverse):**
- The manifest schema, the item identity scheme and the decision-file format are all shaped by D-01; switching to file-level replication later would replace the whole job core.
- The decision files' location under `~/.config/pc-switcher/` means moving them later requires migrating user state on every machine.
- Package sync requires passwordless sudo on BOTH machines: the source's `/etc/apt` state and snap configuration are root-only reads.
- Repository configuration on the target is partly outside the user's line-by-line control. Repositories, keys, pins and flatpak remotes appear because a package or ref was approved, and the only way to decline one is to decline the item.
- A repository or remote on the source that feeds no item this run syncs does not travel at all. The two machines' repository configurations are converged for what packages need, not made identical.
- Pins always-sync, so a `preferences.d` file the user wanted only on one machine comes back on every run, and the only way to keep it machine-local is to delete it on the source.
- A package hand-installed on the source but auto-resolved on the target can be removed as collateral without a prompt (D-40).
- A package whose repository this run derives gets no plan-time collateral question (D-40). Its manual collateral is discovered at converge and fails the item there, so the user learns about it after approving rather than while deciding.
- A run whose batched rehearsal finds manual collateral pays one further `apt-get --dry-run` per candidate in that direction to attribute it (D-40), which is what a first sync with a large install set and one protected casualty spends before the review appears.
- Jointly-caused collateral cannot be attributed to fewer than the whole batch, so declining it there cancels every candidate — correct, but coarser than the single-cause case (D-40).
- ESM makes `apt_sync` interruptible by a question no other job can raise (D-38): an unattached target turns a sync the user expected to be unattended into a prompt, and the "skip" answer costs the whole apt job for that run rather than only the two files. An unattended run pays that cost with no prompt at all: it skips `apt_sync` and reports it. Accepted because the alternative leaves the target's apt failing installs of ESM-covered packages with a 401 the user has no way to trace back to the sync.
- Origin capture adds work to every run: source-side installed origins, source-side and target-side repository URI scans, target-side candidate origins, and one more batched policy call before installing. All batched, none measured.
- A filtered flatpak remote replicates WITH its filter. flatpak records the filter's path rather than its content (measured), so the content is an ordinary file the run carries byte-for-byte exactly as it carries a keyring: written to the same absolute path on the target and re-applied there after that remote's refs land. The cost is that the job writes one path per filtered remote that it does not otherwise own.
- Sideloaded snaps sit outside the model entirely (#221), so a machine can hold software package sync will neither replicate nor remove — it only names it.

## Alternatives Considered

- **File-level replication of the package databases** (`/var/lib/dpkg`, `/var/lib/snapd`, the flatpak OSTree store) — rejected: the package managers must stay authoritative for their own state, and file-level replication would fight their own consistency mechanisms.
- **A single combined `package_sync` job** — rejected per D-15: four separate jobs give independent enable flags, independent config, independent failure isolation and independent progress reporting, at the cost of one shared core module holding only what all four use.
- **A `--delete` file mirror of `/etc/apt`** — rejected per D-11: it would wipe the target's own machine-specific sources, which contradicts the machine-local decision model (D-07/D-08).
- **Repositories and remotes as reviewed items, with an origin check on top** — rejected: it keeps the unrepresentable pairing and asks the user a question whose answer is already implied by the package decision, while the origin check does the actual work.
- **Deriving pins per package rather than always-syncing `preferences.d`** — rejected: a pin naming an absent origin is inert, so the precision buys nothing and the derivation has a wrong-answer mode that always-syncing does not.
- **Always-syncing every flatpak remote, as pins are always-synced** — rejected: unlike a pin, a remote costs a summary fetch on every `flatpak update`, and "the two machines' remote lists are converged for what refs need, not made identical" is the property D-34 already chose for apt. One rule, not two.
- **Writing the ESM sources to an unattached target and only warning** — rejected per D-38: measured, the refresh succeeds and the ESM suites then win candidate selection, so the target's next install of an ESM-covered package fails with a 401 long after the sync that caused it.
- **Withholding the two ESM files silently** — rejected: pins travel regardless (D-36), so the target can end up with pins over a repository set neither machine has. Rejected as the no-TTY fallback for the same reason.
- **Refusing the whole run when an approved package's origin cannot be replicated** — rejected: it contradicts D-27's continue-and-report model; the package fails, the run continues.
- **Protecting the union of both machines' manual sets from collateral** — rejected per D-40: the union protects a package on the strength of the wrong machine's bookkeeping.
- **A snap store or key decision mirroring apt's** — rejected per D-42: there is nothing to derive, and a screen with nothing behind it is worse than none.
- **Source-cache reuse for offline installs** — deferred per D-28; revisit if target-side downloads prove slow or unreliable.

## References

- ADR-002: SSH as communication channel — package-manager invocations run through the same executor protocol.
- ADR-005: Asyncio concurrency — all package-manager invocations are async subprocesses.
- ADR-010: Logging infrastructure — per-item detail at FULL, per-job summaries at INFO.
- ADR-021: What the log records and withholds — the content rules these jobs' reviews and collateral forced, and the credential redaction they need.
- ADR-014: Unified dry-run contract — each job's batched review doubles as its dry-run output.
- ADR-015: Topology-based sync-safety model — the warn-and-confirm precedent D-25/D-26 follow; this ADR's review is never a hard abort.
- ADR-018: Selective VS Code state sync — the path-export mechanism D-29 reuses for `flatpak_sync` and `snap_sync`.
- ADR-022: a read that did not answer fails the job; an answer of "nothing" is data — the guard every capture in these jobs passes through.
- `docs/system/package-sync.md` and `docs/jobs/package-sync.md`: the resulting spec and user-facing description.
- `docs/planning/package-sync-user-requirements.md`: the user-viewpoint statement of what these jobs must deliver, and the authority on intent.
- `docs/planning/package-sync-conformance-criteria.md`: that intent as individually checkable articles.
- `.planning/phases/02-package-management-sync/02-SPEC-package-review-model.md`: the apt implementation contract — the four review screens, the origin classification, the enforcement point.
- `.planning/phases/02-package-management-sync/02-SPEC-snap-flatpak-derivation.md`: the snap and flatpak implementation contract, and the measurements behind D-41 and D-42.
- GitHub issue #118: the feature issue, including the snap-revision discussion motivating D-06.
