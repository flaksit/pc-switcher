# ADR-021: Origin-replicating package convergence — the review decides packages, `/etc/apt` follows

Status: Accepted

Date: 2026-07-27

Supersedes: ADR-020

## TL;DR
An apt package replicates as (name, origin), not name: the review decides packages and `/etc/apt` config files, while repositories, their keys and their pins are derived from the packages approved from them and are never reviewed in the add direction — and the target is refused an install whose real post-`apt-get update` candidate comes from an origin the source does not use.

## Implementation Rules

**Required:**
- `/var/lib/dpkg`, `/var/lib/snapd` and the flatpak OSTree store MUST NOT be rsynced or otherwise file-mirrored; convergence happens only through `apt`, `snap` and `flatpak` invocations (unchanged from ADR-020).
- An apt package's identity for replication purposes MUST include the origin URIs its installed version comes from on the source. An approved install MUST be refused when the target's candidate origins, read after the run's `apt-get update` and before its first install, do not intersect the source's. Origins declared by the distribution's own source files are exempt from that check.
- A repository file, its signing keyring and the `preferences.d` pins MUST be written as derived mechanism, never as review lines: they land because a package approved in the review comes from them.
- `apt_sync` MUST NOT prompt after its first mutating command. Every apt decision is taken in one review, before the `/etc/apt` group converges.
- `/etc/apt/apt.conf.d` MUST stay a reviewed item in all three directions (add, change, remove) with the full three-way decision and the machine-local registry.
- Removing an apt source file or a `preferences.d` pin file MUST offer exactly two answers — remove, or skip once — and MUST NOT be recordable in a decision file.
- `ubuntu.sources`, `/etc/apt/sources.list` and the `ubuntu-esm-*` source files MUST be written when missing and overwritten when different, and MUST NEVER be offered for removal or removed. When an `ubuntu-esm-*` file would be written and the target reports no Ubuntu Pro attachment, `apt_sync` MUST ask the user before its first mutating command, with exactly two answers: attach now (pc-switcher re-probes the target and continues) or skip `apt_sync` for this run while every other job runs.
- A derived `/etc/apt` write that fails MUST fail every package that depended on it, naming the file — not an item of its own.
- The `/etc/apt` convergence group MUST stay transactional: a group whose metadata refresh fails leaves the target's `/etc/apt` as it found it (unchanged from ADR-020).
- A package manager's own transaction MUST be constrained to what the review approved, determined at plan time and classified there, never prompted mid-apply. Collateral protection keys on the TARGET's `apt-mark showmanual` set. An install the target's package manager cannot yet resolve MUST NOT enter the plan-time rehearsal at all, and MUST be protected by the apply-time guard alone.
- `apt_sync`, `snap_sync`, `flatpak_sync` and `manual_installs_sync` MUST stay four separate `SyncJob`s, each with its own enable flag, validation, progress reporting and `JobResult` (unchanged from ADR-020).
- Machine-local decision files MUST live at `~/.config/pc-switcher/<manager>.decisions.yaml`, one per manager, excluded from `folder_sync` non-overridably and outside `config_sync` (unchanged from ADR-020).
- Whether an unreproducible item counts as reproducible MUST be judged by whether the SOURCE holds a snippet, and the snippet registry MUST be pushed and replayed by `manual_installs_sync` in the same run (unchanged from ADR-020).

**Forbidden:**
- No `--delete` file mirror of `/etc/apt` or any other package-database directory.
- No installing a package from an origin the source does not use, in preference to reporting that the origin cannot be replicated.
- No second review inside one job's `execute()`, and no prompt of any kind after that job's first write.
- No decision-file entry for an apt source or an apt pin, in any direction.
- No re-fetching signing keys from vendors; a repo's key travels with its repo, byte-for-byte.
- No standing block on a package manager's own auto-update left behind by a run.
- No writing an `ubuntu-esm-*` source to a target that reports no Pro attachment without asking, and no attempt to attach the target on the user's behalf.

## Context

ADR-020 modelled every `/etc/apt` file as an inventory item with its own three-way decision, and matched packages by name alone. Both cost more than they bought. Name-only matching lets the target satisfy an approved install from whatever vendor happens to offer that name, so replicating `firefox` from `packages.mozilla.org` produces Ubuntu's transitional snap package on the target — the name replicates and the provenance inverts. And putting repositories in the review asks the user a question they cannot answer independently of the package that motivated it: a repository ticked without its package does nothing, a package ticked without its repository cannot be installed, and the pairing was never expressible.

The rest of ADR-020 held up. This ADR replaces it rather than amending it (ADR-001's immutability rule) so a fresh reader finds one decision list, not two.

## Decision

### Carried forward from ADR-020, unchanged

D-01 (manifest capture, convergence through each manager's own tooling), D-03 (`apt-mark showmanual` is the apt manifest), D-04 and D-05 (versions float; mismatch is reported, never force-downgraded), D-06 (snap converges revision and channel; the sync-window auto-refresh pause), D-08/D-08a/D-09/D-10 (the machine-local decision file and where an entry is written), D-12 (a repository's key travels byte-for-byte and is never re-fetched), D-15/D-16/D-17 (four jobs over one shared core, package jobs before `folder_sync`), D-18 through D-23 (`manual_installs_sync` owns bare-`.deb` and `/usr/local`+`/opt` detection; snippets are opaque blobs in the shared registry, pushed and replayed the same run), D-26 (a non-interactive run does not fail on undecided items alone), D-27 (continue, collect, report; a failing item does not stop the job), D-28 (the target downloads from its own repos), D-29 (path export to `folder_sync`), D-33.

D-02's principle carries forward intact and is what licenses the changes below: *mechanism the user has no basis to judge is not an item and is the job's own business to keep correct.* D-11's rejection of a `--delete` file mirror of `/etc/apt` carries forward too, for its original reason — it would wipe the target's own machine-specific sources.

### Overturned

- **D-02's enumeration.** apt sources and apt pins are no longer items in the add and change directions; they are mechanism, exactly as the signing key already was. They remain items in the removal direction, where the user is the only one who can say whether a target-only file is wanted. apt config remains an item in every direction.
- **D-07's universality.** "Every actionable item gets a three-way decision" now has documented exceptions: an apt source removal, an apt pin removal and a repository conflict-overwrite take two answers — act, or skip once — and record nothing. A permanent machine-local mark on a file whose whole purpose is to feed packages would silently and permanently change where those packages come from; the user's remedy is consolidating the two files, not recording a preference. Packages, holds and apt config keep the three-way decision.
- **D-11/D-13's "apt sources, pins and apt config are inventory items"**, in the add and change directions. Replaced by four buckets: derived-from-approved-packages (repository adds, keys, conflict-free overwrites), always-synced (pins, and the distribution's own source files), reviewed two-way (repository and pin removals, and the conflict case), reviewed three-way (apt config).
- **D-24's "a job MAY review again when this run's own changes invalidate the facts an earlier review was answered on"**, retired for apt. Under origin replication a package's classification depends on the SOURCE's origins, which no run mutates, so no fact a review is answered on can be invalidated by that run. Every apt prompt precedes the job's first mutating command, unconditionally.
- **D-25's diff taxonomy.** `HELD_OR_PINNED` is retired: a pin's only job is deciding which origin wins, which D-35 now checks against the target's real state, and the per-package echo made a pinned target-only package impossible to remove and impossible to silence. `REPO_UNAVAILABLE` is redefined as "the source's origin cannot be provided on the target", no longer "apt printed `Candidate: (none)`". `ORIGIN_MISMATCH` is added for a package installed on both machines from different vendors.
- **D-30's trigger.** See D-40.
- **D-18's phase-context calibration** ("4 apt packages have no repository candidate on P17") is void: it came from a rule that never matched reality, and the packages it counted belong to `manual_installs_sync` alone. D-18 itself — the four-job split and that ownership — is unaffected.

### Origin replication (D-34)

The unit of replication for an apt package is (name, origin), not name. The target must end up with the package installed from an origin the source installed it from. A package whose origin cannot be provided on the target — no source file declares it, or the file that does cannot be written — is reported, never installed from somewhere else. The failure mode this closes is a package name offered by two vendors, where name-only matching silently replicates the name and inverts the provenance.

A repository, its keyring and its pins are derived from the packages approved from it. A repository the source has that feeds no package this run syncs does not travel; the distribution's own files (D-38) are the deliberate exception.

### Origin enforcement, at the target's real state (D-35)

The guarantee is checked, not inferred. After the `/etc/apt` group's single `apt-get update` and before the first install, one batched `apt-cache policy` over the approved install names re-reads the target's candidate origins; an approved install whose candidate origins do not intersect the source's fails as its own item, naming both origins. Plan-time classification decides what to derive; only this check decides what may be installed.

Origins declared by the distribution's own source files are exempt, computed per machine. Two machines on different Ubuntu mirrors are not two vendors, and without the exemption every package on such a pair would fail.

### Pins are mechanism, not inventory (D-36)

A pin travels because it is what makes an origin win, in the same sense and for the same reason a signing key travels because it is what makes a repository trusted. Neither is reviewed in the add or change direction; pin adds and updates always sync, silently. A pin naming an origin the target does not have is inert, so always-syncing them costs nothing and cannot get a per-package derivation wrong.

The evidence that forces this: measured on the development machine, Ubuntu's archive offers `firefox` at version `1:1snap1-0ubuntu5` at priority 500. That version carries **epoch 1**; Mozilla's own `firefox` deb carries no epoch, and under equal priority apt takes the highest version, where any epoch-1 version outranks every epoch-0 version regardless of the upstream number. Adding the vendor's repository alone therefore still installs Ubuntu's package. Only the vendor's `preferences.d` pin, at priority 1000, changes the outcome — so a design in which repositories travel and pins do not would replicate the repository and still install the wrong package.

### The review's scope, and its one non-package exception (D-37)

The apt review decides packages. That is a near-rule, not an absolute: `/etc/apt/apt.conf.d` is reviewed in all three directions, with the three-way decision and the registry.

The distinction is derivability. Every other `/etc/apt` file earns its place by serving a package — a repository is where a package comes from, a keyring makes it trusted, a pin makes it win — so the package decision implies the file decision. An `apt.conf.d` file governs apt's own behaviour, and nothing about an approved package implies whether it should travel; with nothing to derive it from, the only honest source of the answer is the user. It keeps the registry for the same reason: a proxy or a recommends policy is a standing machine-local preference someone can hold permanently, unlike a repository removal, whose remedy is consolidating two files.

Repository removals additionally disclose, on the line being decided, which machine-specific packages on the target the removal would strand. A repository changed on both machines is overwritten silently unless it feeds such a package, in which case both file contents are shown and the answer is overwrite or skip once.

### The distribution's own source files, and ESM (D-38)

`ubuntu.sources`, `/etc/apt/sources.list`, `ubuntu-esm-apps.sources` and `ubuntu-esm-infra.sources` are written when missing and overwritten when different, and are never removed and never offered for removal. They define the distribution origins D-35 exempts, so they are the one repository bucket that does not wait for a package to derive it.

The two ESM files are gated on the target's Ubuntu Pro attachment. When they would be written and the target reports unattached, `apt_sync` asks — before its first mutating command — with exactly two answers: attach now, meaning the user attaches on the target by hand and pc-switcher re-probes and continues; or skip `apt_sync` for this run, while every other job runs. Writing them to an unattached target silently is not an option, because it leaves an apt that fails on a subset of installs for a reason the user will not connect to the sync.

The question precedes the review rather than joining it: one of its answers means there is no review to hold, and it asks about the target's environment, not about an item. It is still raised before the job's first mutating command, so the one-review-then-write rule is intact. A run with nobody to ask keeps the two files back, warns, and lets the rest of `apt_sync` proceed — an unwritten file leaves the target exactly as it already is.

The hazard, **measured** in a stock `ubuntu:24.04` container carrying both real ESM source files copied from a Pro-attached host: `esm.ubuntu.com` serves its repository *index* publicly (HTTP 200 on `.../dists/noble-apps-security/InRelease`), so the suites are fetched, marked `Trusted: yes`, and enter candidate selection at priority 500 — above `noble/universe`. Only the *pool* is 401. The failure therefore lands at install time, not refresh time: `apt-get install 7zip` exits 100 with `401 Unauthorized` on the `.deb`. That container had 0 of 13 upgradable packages with an ESM candidate; that a desktop with a large `universe` set has many more is **inferred from the priority ordering, not measured**.

The hypothesis this replaces, recorded so no future reader re-derives it: D-38 previously withheld the two files on the reasoning that an unattached target's `apt-get update` fails and rolls the transactional `/etc/apt` group back. Every part of it is **false, measured** in the same container. `apt-get update` exits 0 with the ESM sources present and no credentials. A source that genuinely fails does not abort the others: with the ESM keyrings removed the run exits 100 with `E: The repository ... is not signed.` and still fetches and writes all 19 other lists, and against a synthetic index-level 401 it exits 100 and writes all 27 others — a non-zero exit is an aggregate signal, never an abort, and triggers no rollback. The missing-keyring case cannot arise here anyway: `/usr/share/keyrings` is one of the three key directories (`src/pcswitcher/jobs/apt_sync.py:200,203`), so `ubuntu-pro-esm-apps.gpg` travels with the source file.

The question cannot be answered by the tool. `pro attach` needs a subscription token from the user's Pro dashboard or an interactive browser short-code flow; the source machine's own credentials are root-only (`/var/lib/ubuntu-advantage/private/` is unreadable to the ordinary user) and a machine's token is not reusable to attach another machine; and holding a subscription token would put a secret on a command line. pc-switcher therefore asks, waits for the user to attach, and re-checks.

Detection is `pro status --format json` on the target — exit 0 for an unprivileged user, top-level `attached: true|false`, measured this session. Its payload also carries the subscriber's account; only the parsed boolean may be logged or shown.

### Derived-work failure attribution (D-39)

A derived `/etc/apt` write that fails does not fail an item of its own — there is no item, because the user decided about a package, not a file. It fails every approved package whose derived file set contains it, naming the file and the reason. A rollback of the `/etc/apt` group fails all of them. This is also what a repository conflict answered "skip once" does to the packages that depended on that file: a package cannot be quietly installed from the wrong origin because its repository was skipped.

### Collateral protection narrows to the target's manual set (D-40)

D-30's model is unchanged — apt's real transaction is determined at plan time, collateral is classified rather than blanket-refused, and collateral that removes or downgrades a manually-installed package becomes its own review item offering install-anyway / skip / abort, in the review and never mid-apply.

One package cannot be classified at plan time and is not asked about there: a §2.3 class-3 install, whose repository this run derives from the package's own approval and writes during converge. Until that write lands the target's apt has never heard the name, so `apt-get --dry-run install` refuses the whole batch containing it — and D-30's rehearsal is one batch, so including such a name removes the protection from every other package in the run rather than weakening it for one. It is therefore excluded from the rehearsal on the evidence of the target's `apt-cache policy`, never on the simulation's exit code (ADR-022 D-01).

What covers it instead is the apply-time guard, which runs the same rehearsal per item after `/etc/apt` has converged and `apt-get update` has run, where apt CAN resolve the package: unapproved manual collateral fails that one item (D-27). The cost is real and accepted — for those packages the user is told afterwards rather than offered install-anyway / skip / abort beforehand — because the facts that question needs do not exist while `plan()` runs. A package whose origin can never be replicated needs no rule: it is `REPO_UNAVAILABLE`/`REPORT_ONLY` and is never an install candidate.

What changes is the protected set: the TARGET's `apt-mark showmanual`, no longer the union of both machines'. The case this gives up is **knowingly accepted, not overlooked**: a package the user installed by hand on the source, which arrives on the target as an automatic dependency and is later removed as collateral, is no longer protected. If the target's apt installed it automatically, the target's apt owns it, and reclaiming it as a user choice on the strength of the other machine's bookkeeping is a guess. The narrower set is also the set apt itself consults, so "manually installed" means the same thing to pc-switcher and to apt on the machine being changed.

## Consequences

**Positive:**
- Provenance replicates, not just presence: a package installed from a vendor on the source cannot arrive from a different vendor on the target, and the guarantee is enforced against the target's real post-refresh state rather than inferred at plan time.
- The review asks only what the user can answer. The unrepresentable pairing "package ticked, repository unticked" is gone, and so is the whole class of runs where an approved package could not be installed because its repository was declined.
- One review per job, always before the first write. The mid-`execute()` prompt — a `questionary` screen firing with the Rich live display paused, at a point no test ever exercised — no longer exists.
- Deleting the pin echo fixes a real defect: a target-only package named by any pin was previously impossible to remove and impossible to silence.

**Negative (costly to reverse):**
- `/etc/apt` state on the target is now partly outside the user's line-by-line control. Repositories, keys and pins appear because a package was approved, and the only way to decline one is to decline the package.
- A repository on the source that feeds no package this run syncs does not travel at all. The two machines' `/etc/apt` are converged for what packages need, not made identical.
- Pins always-sync, so a `preferences.d` file the user wanted only on one machine comes back on every run, and the only way to keep it machine-local is to delete it on the source.
- The source-intent collateral case (D-40) is given up outright: a package hand-installed on the source but auto-resolved on the target can now be removed as collateral without a prompt.
- A package whose repository this run derives gets no plan-time collateral question (D-40). Its manual collateral is discovered at converge and fails the item there, so the user learns about it after approving rather than while deciding.
- ESM makes `apt_sync` interruptible by a question no other job can raise (D-38): an unattached target turns a sync the user expected to be unattended into a prompt, and the "skip" answer costs the whole apt job for that run rather than only the two files. Accepted because the alternative leaves the target's apt failing installs of ESM-covered packages with a 401 the user has no way to trace back to the sync.
- `REPO_UNAVAILABLE` changed meaning rather than being replaced. Anything written against ADR-020's reading of it — matrix rows, tests, docs — is wrong rather than merely stale, and reads plausibly either way.
- Origin capture adds work to every run: source-side installed origins, source-side and target-side repository URI scans, target-side candidate origins, and one more batched policy call before installing. All batched, none measured.

## Alternatives Considered

- **Keeping repositories as reviewed items and adding an origin check on top** — rejected: it keeps the unrepresentable pairing and asks the user a question whose answer is already implied by the package decision, while the origin check does the actual work.
- **Deriving pins per package rather than always-syncing `preferences.d`** — rejected: a pin naming an absent origin is inert, so the precision buys nothing and the derivation has a wrong-answer mode that always-syncing does not.
- **Writing the ESM sources to an unattached target and only warning** — rejected per D-38: measured, the refresh succeeds and the ESM suites then win candidate selection, so the target's next install of an ESM-covered package fails with a 401 long after the sync that caused it.
- **Withholding the two files silently, as D-38 first ruled** — rejected: the transactional-rollback premise behind it is refuted by measurement, and what remains is a choice between an apt without ESM and an attachment only the user can perform, which is the user's to make.
- **Refusing the whole run when an approved package's origin cannot be replicated** — rejected: it contradicts D-27's continue-and-report model; the package fails, the run continues.
- **Keeping the union manual set for collateral (ADR-020 D-30)** — rejected per D-40; the union protects a package on the strength of the wrong machine's bookkeeping.
- **File-level replication of the package databases**, **a single combined `package_sync` job**, **a `--delete` mirror of `/etc/apt`**, **source-cache reuse** — all rejected for the reasons ADR-020 recorded, unchanged.

## References

- ADR-020: the superseded decision this replaces.
- ADR-002: SSH as communication channel; ADR-005: asyncio concurrency; ADR-010: logging (per-item detail at FULL); ADR-014: unified dry-run contract — each job's batched review doubles as its dry-run output; ADR-015: topology-based sync-safety model; ADR-018: the path-export mechanism D-29 reuses.
- `.planning/phases/02-package-management-sync/02-SPEC-package-review-model.md`: the implementation contract — the four screens, the origin classification, the enforcement point, the staged plan and the residual hypotheses.
- `docs/system/package-sync.md` and `docs/jobs/package-sync.md`: the resulting spec and user-facing description.
- GitHub issue #118: the feature issue.
