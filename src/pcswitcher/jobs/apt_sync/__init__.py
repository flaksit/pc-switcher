"""`apt_sync`: apt package convergence — install, remove, and the full diff taxonomy
(`PKG-FR-MANAGER-CONVERGES`, `PKG-FR-APT-SCOPE`, `PKG-FR-VERSION-FLOAT`, `PKG-FR-SKIP-ONCE`, `PKG-FR-BATCHED`,
`PKG-FR-APT-VERSION-DIFF`, ADR-020).

Captures the source's `apt-mark showmanual` set with `dpkg-query`-sourced versions
(never `apt list --installed` — its own manpage says the output has no stable scripting
contract), diffs it against the same query on the target into every diff class this
manager produces (`diffing.diff_apt_packages`, this job's own — the base class holds no diff
for anyone to inherit, `PKG-FR-JOB-INDEPENDENCE`), and converges the approved `INSTALL`/`REMOVE` items via
`apt-get install`/`apt-get remove`.

A package is matched by (name, ORIGIN), never by name alone (`PKG-FR-APT-IDENTITY`). The target
having a candidate for a name is not evidence it can supply the source's software: one name
is often offered by two vendors, and Ubuntu's `firefox` carries epoch 1, which outranks
every unpinned vendor version, so an install matched by name replicates the name and
inverts the origin. `plan()` therefore reads where the source installed each package
from, maps that back to the repository file on the SOURCE that declares it, and classifies
the package against the target's real candidate origins: same origin -> ordinary install;
different or absent origin with a writable declaring file -> install, with that repository
derived from the package's own approval; no writable declaring file -> `REPO_UNAVAILABLE`,
reported rather than installed from somewhere else. A package installed on both machines
from two different vendors is `ORIGIN_MISMATCH`, report-only.

That classification is not the guarantee — it is only what decides which repository work to
derive. The guarantee is `origins.OriginClassifier.refusal` (`PKG-FR-APT-ORIGIN-VERIFY`): after this run's single
`apt-get update` and before its first install, ONE batched `apt-cache policy` re-reads the
target's candidate origins, and an approved install apt would now satisfy from none of the
source's origins fails as its own item (`PKG-FR-OUTCOME-FAILED`), naming both. It sees the state the derivation
actually produced, so a repository whose write failed or a pin that never landed is caught
there rather than shipping the wrong vendor's package. Packages the source has only from its
own distribution files are exempt: two machines on different Ubuntu mirrors are one vendor.

That refusal is for a request that is wrong, which is per-item. A probe that did not run at
all is a different thing and gets a different answer: `commands.require_apt_answer` fails the
run once, naming the command, rather than blaming N packages' origins for a transient
network, an apt lock or an interrupted dpkg.

Bare-`.deb` packages are NOT in scope and are dropped at capture
(`probe.AptProbe.capture_source_items`). A package whose installed version comes from no
configured repository was put there with `dpkg --install`; apt cannot install it on the
target, and `manual_deb_sync` offers it as an install snippet in the same run (`PKG-FR-MANUAL-SCOPE`).
Both jobs compute the predicate from the shared `packages/apt_policy.py` parser — the same
test, never a result passed between them, since `PKG-FR-JOB-INDEPENDENCE` keep the package jobs independent.

The ownership split has a consequence this job may not paper over: `manual_deb_sync`
has its own enable flag, and reading another job's flag is exactly the coupling `PKG-FR-JOB-INDEPENDENCE`
forbids. So enabling `apt_sync` while disabling `manual_deb_sync` leaves these packages
replicated by nobody — silently absent rather than offered as installs that fail. Documented
for the user in `docs/jobs/package-sync.md`.

Every approved item's transaction is simulated with `apt-get --dry-run` before the real
command runs, guarding against apt silently doing more than the review showed. Collateral
effects are classified by origin (`PKG-FR-COLLATERAL-MANUAL`): a package the simulation would remove or
downgrade that is auto-installed (not in the target's `apt-mark showmanual` set) is apt
resolving its own dependencies and proceeds silently, while a manually-installed one is
something the user chose to have and is refused unless the user approved losing it. `plan()`
runs two BATCHED simulations (the whole install candidate set, the whole removal candidate
set — not one per-package, which would cost more than the sync itself for 150 packages) and
classifies their collateral against the target manual set, emitting a three-way
apply / keep-the-package / stop-the-sync review item for each manual-collateral package so
the decision is made in the batched review, never as a prompt during apply. A batch that
turns up manual collateral then simulates each candidate alone, so `skip` cancels the
packages that actually cause the collateral rather than everything the user was reviewing
(`collateral.Collateral.for_direction`).

One install is admitted to no plan-time simulation at all
(`origins.OriginClassifier.target_resolvable`, `PKG-FR-COLLATERAL-MANUAL`): a `PKG-FR-APT-IDENTITY` class-3 package,
whose
repository this run derives from its own approval and writes during converge. Until that
write lands the target's apt has never heard the name and `apt-get --dry-run` refuses the
WHOLE batch containing it, which would strip the protection from every other package in the
run rather than weaken it for one. The exclusion keys on the target's own `apt-cache policy`,
never on the simulation's exit code, which cannot separate "unable to locate" from a held
dpkg lock (`PKG-FR-READ-FAILS-JOB`). What covers those packages instead is the per-item simulation
`packages.PackageConverger.install` runs after the repository unit has converged, where apt
CAN resolve them: unapproved manual collateral fails that one item, so the user is told
afterwards rather than asked beforehand.

The same plan-time-classification rule covers the `/etc/apt` removal direction (C26): a
source file offered for deletion because the source machine no longer has it carries, in
its review `detail`, the machine-specific packages the target still installs from that
repository. Those packages are recorded skip-always, so `filter_inert` keeps them out of
the target manifest and they produce no diff of their own in any run; without this the
review shows a bare file deletion and nothing else. Disclosure, not refusal: removing a
repository whose packages are going too is legitimate, so the removal stays offered (and,
like every removal group, unticked).

Neither is `/etc/apt` itself, in most directions. Only what the user has a basis to judge
is an item (`PKG-FR-REPO-DELETE`/`PKG-FR-PIN-DELETE`/`PKG-FR-APTCONF`): a repository REMOVAL, a pin REMOVAL, and apt
config in all
three directions. Everything else under `/etc/apt` is derived and written without a
question, in three buckets `derived.DerivedWrites` assembles from the accepted decisions:

- the repository files serving the packages the user approved (ruling 4 — a repository
  that feeds no package this run syncs does not travel at all);
- every `/etc/apt/preferences.d` file the source has, always. A pin decides which origin
  wins, which is precisely what origin replication turns on, and one naming an origin the
  target does not have is inert — so always-sync costs nothing and cannot derive wrongly;
- the distribution's own source files — `ubuntu.sources`, the two `ubuntu-esm-*` files and
  `/etc/apt/sources.list` — written when missing and overwritten when different, never
  removed and never offered for removal (`PKG-FR-DISTRO-FILES`).

The two `ubuntu-esm-*` files carry the one question this job asks that is not about an item
(`esm_gate.EsmGate`, `PKG-FR-DISTRO-FILES`). Writing them to a target with no Ubuntu Pro attachment is not
harmless: `esm.ubuntu.com` serves its INDEX publicly, so the refresh succeeds and the ESM
suites win candidate selection, and the failure surfaces much later as a 401 on the `.deb`
of the target's next install of an ESM-covered package — a failure nobody traces back to a
sync. pc-switcher cannot attach the target itself (`pro attach` needs a subscription token
or a browser flow, and a machine's own credentials are root-only and not reusable), so it
asks, with exactly two answers: attach now, which RE-PROBES rather than trusting the answer
and may be given any number of times, or skip `apt_sync` for this run while every other job
runs. A run with nobody to ask takes the skip; a dry run warns instead. Skipping the whole
job rather than withholding the two files is the only coherent partial outcome, because
`preferences.d` always-syncs with no derivation predicate and the source's ESM pins would
land regardless. The gate sits in `plan()`, right after the origin state supplies the
digests its trigger reads and before any review group is built: one answer ends the job, so
it must precede the planning and the review the user would otherwise answer for nothing.
Only the parsed `attached` boolean ever leaves `esm_gate` — the probe's payload names the
subscriber's account.

A derived write has no item, so it cannot fail as one. It is recorded against its
destination and charged to every approved package whose origin depended on it (`PKG-FR-DERIVED-FAILURE`):
the refusal lands on the thing the user actually decided about, naming the file. A rollback
marks every derived write failed, exactly as it already does every reviewed group item.

A signing key is NOT an item either. It has no `ItemClass`, no `item_id`, no diff, no
review entry and no decision-file identity: the user thinks in repositories and packages,
and a key is only how a repository is made to work. Keys are two plain file operations
bracketing the repository unit (`keyrings.py`):

- provisioning runs BEFORE any source file is written. It copies every source
  `/etc/apt/trusted.gpg.d` key the target lacks or differs on, and the keyrings named by
  the source files this run actually writes (a repository this run overwrites may point at
  a keyring the target has never seen). `keyrings.Keyrings.gap` still refuses to write a
  source whose keyring did not arrive, so a repository is never written ahead of its key —
  and that refusal, like every other derived-write failure, is charged to the package.
  Provisioning is ownership-aware in ONE direction: a keyring the target LACKS is copied
  whatever owns it on the source, but a keyring the target already has with different bytes
  is left alone when the target's own DISTRIBUTION packaging owns that path — clobbering a
  distro keyring is not this job's business. Ownership by any other package is not an
  exemption (`PKG-FR-KEY-REFRESH`): a vendor ships its keyring in a `.deb` of its own and
  rotates it there, so leaving that one alone is how the rotation never arrives. Ownership
  must not gate the COPY either, because a vendor `.deb` (`code`,
  `tailscale-archive-keyring`) ships both its `sources.list.d` entry and its keyring, so the
  repository the package comes from cannot be trusted until the key that package owns is
  already there; skipping package-owned keys would make that bootstrap
  unsatisfiable. Every derived write is
  logged at FULL as it lands, and previewed under `--dry-run` (ADR-014), which is how a
  file with no review entry stays visible without becoming a question.

Three key directories are captured, not two: `/etc/apt/keyrings`, `/etc/apt/trusted.gpg.d`
and `/usr/share/keyrings`. The last is where `add-apt-repository`, several vendor `.deb`s
and Ubuntu itself put keyrings, and most real `Signed-By:` values point there; leaving it
out made every such reference resolve to nothing and downgraded the repository to
`REPORT_ONLY`. Like `/etc/apt/keyrings` it is provisioned only for keys a source actually
references, and unlike it, it is never collected: it is package territory.

- collection runs AFTER every source write and deletion, and only when this
  run actually removed a source file. It re-scans the target's REAL source files and drops
  each `/etc/apt/keyrings` file no surviving source references. Counting against the
  post-write state is what makes the hard cases come out right: a repository this run
  deleted stops counting as a reference, while one the user left unticked, one recorded
  machine-specific, and one pc-switcher never syncs at all (`/etc/apt/sources.list`) all
  keep counting.

Legacy `/etc/apt/trusted.gpg.d` keys are replicated but never collected: they are ambient
trust with no discoverable referent, so "unused" is not computable for them and they are
allowed to accumulate rather than be deleted on a guess.

Keys travel byte-for-byte and are never re-fetched from a vendor (`PKG-FR-KEY-COPY`).

This job reviews once per run before its first mutating command (`PKG-FR-BATCHED`), and asks
again only where applying an approved change reveals a fact the plan could not know — the
requirements' own model, and a preference rather than a rule. Nothing this run writes can
invalidate a decision it already took: a package is classified from the SOURCE's origins,
which no run mutates, and the one fact that genuinely depends on the target's post-write
state — which origin actually wins — is not guessed at plan time at all, it is read back by
`origins.OriginClassifier.refusal` and turned into a per-item refusal rather than a question.

That is also why a pin says nothing about the packages it names. A per-package
"pinned" report would fire for every package a target-side `preferences.d` stanza names,
turning a no-op into review noise and — worse — making a package present only on the target
and named by any pin impossible to REMOVE (a `REPORT_ONLY` echo outranks its own
`EXTRA_ON_TARGET` diff) and impossible to silence (a `REPORT_ONLY` item cannot be recorded
skip-always). Pins themselves DO replicate, as FILES under `/etc/apt/preferences.d`, and
that is the whole mechanism: a report about them was never part of it.

## Where things live

Reads produce frozen facts (`probe.py`); the deciding half — `origins`, `diffing`,
`derived`, `keyrings`, `collateral` — is pure over them; only `etc_apt`, `packages`, `files`
and `esm_gate` touch a machine. `items.py` says what an apt item is and where its file
lives, `messages.py` holds every sentence the user reads, `commands.py` every apt command
this job builds.
"""

from __future__ import annotations

from pcswitcher.jobs.apt_sync.commands import AptTransactionPreview, simulate_apt_transaction
from pcswitcher.jobs.apt_sync.job import AptSyncJob

__all__ = ["AptSyncJob", "AptTransactionPreview", "simulate_apt_transaction"]
