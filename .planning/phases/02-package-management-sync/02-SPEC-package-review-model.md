# Phase 02 — SPEC: the apt review model after the origin-replication rulings

Status: design contract for implementation. Every question this document once carried is answered; nothing in it is open. It supersedes the "derived repos" section of `.planning/phases/02-package-management-sync/02-HANDOVER-package-review.md` (§2), and is the implementation contract behind ADR-020's apt decisions (§8).

This document is what implementation is briefed from. It reconciles the user's rulings with the code as it stands on `gsd/phase-02-package-management-sync` at `9c25d101`. Every claim about the code carries a `path:line`. Anything not verified is labelled as a hypothesis (§11 collects them).

## 0. Settled premises

**Packages and holds keep their three-way decision (apply / skip once / skip always) and their machine-local decision-file registry**, exactly as ADR-020 D-07/D-08 specify and as `sync_core.py:414` and `state.py:166` already implement. Confirmed by the user, not inferred: the no-registry rulings are about `/etc/apt` repository configuration, and ruling 3 depends on the package registry continuing to exist — it is what "machine-specific packages on the target" means.

Apt holds (`apt:hold:<name>`, `apt_sync.py:294`) are untouched by every ruling and keep their current shape, including skip-always.

There is **no backwards compatibility** anywhere in this change: no migration code, no legacy-entry reading, no shim. Package sync has never run outside the test environment, so no `apt:source:` or `apt:pin:` decision record exists to migrate. Every command string this design adds spells its options in long form.

### 0.1 The rulings this document is built from

Referenced throughout as "ruling N".

1. A repository's pin travels with the repository, like its signing key.
2. The unit of replication for a package is (name, origin), not name.
3. The one situation a `/etc/apt` change is put to the user is when a package the target has recorded skip-always comes from the file being changed.
4. Adding a repository is derived from the packages approved from it; it is never a tickable line.
5. Removing a repository is reviewed, with two answers — remove or skip once. No registry entry, no skip-always.
6. Changing a repository present on both machines overwrites the target silently, except in ruling 3's case, where both file contents are shown and the answer is overwrite or skip once.
7. `ubuntu.sources`, `/etc/apt/sources.list` and `ubuntu-esm-*` are never removed, but are written and updated.
8. No backwards compatibility: no migration, no legacy-entry reading, no shims.
9. The review names a package's origin whenever that origin is not the distribution archive, in full-URI-path form.
10. Bare-`.deb` packages are `manual_installs_sync`'s exclusive territory.
11. `/etc/apt/apt.conf.d` is reviewed in all three directions — add, change and remove.
12. Pin adds and pin updates always sync silently; pin removal is reviewed with ruling 5's two answers.
13. D-30's collateral protection triggers on the TARGET's manual set only.
14. When ESM sources would be written to a target with no Ubuntu Pro attachment, the user is asked, with two answers: attach the target now (pc-switcher re-checks and continues) or skip `apt_sync` for this run while the other jobs continue.
15. The second mid-`execute()` review and the `HELD_OR_PINNED` pin echo are both deleted.

## 1. The model in one page

The apt review has exactly four kinds of screen. Screens 1 and 4 are ordinary diff groups and take their order from `_ACTION_ORDER` (`sync_core.py:129`) — install, change, remove, report — with the item class deciding the group inside each action; screens 2 and 3 are sentinel groups `apt_sync` emits itself.

Screen 1 — packages. One checkbox group per direction (install, remove), plus the existing hold/unhold groups and the report-only group. Each line is a package. When the package's origin on the source is not the distribution archive, the line names that origin by its full URI path (ruling 9): `install gh (from cli.github.com/packages)`, `install firefox (from packages.mozilla.org/apt)`. A line for a package served by the Ubuntu archive names no origin. Ticking a line means "make the target have this package, from this origin". Whatever the apply list leaves unticked is offered once more as "never offer again on this machine" (`review.py:340`) — unchanged.

Screen 2 — repository-configuration removals: a `sources.list.d` file or a `preferences.d` pin file that exists on the target and not on the source. Two groups, one per item class, both unticked by default. **Two answers only: remove, or leave it for now.** No "never offer again", no decision-file entry (ruling 5, and ruling 12 for pins). A repository removal's `detail` still names the machine-specific packages on the target that the removal would strand (`apt_sync.py:588`, C26) — disclosure, not refusal. Pin removal is reviewed for the same reason repository removal is: deleting a pin can flip which vendor supplies a package at the target's next upgrade, and that is a consequence no approved package implies.

Screen 3 — repository conflicts. Only ever shown when a source file present on **both** machines with **different content** feeds a package the target has recorded skip-always (ruling 6). Per entry: the target's current file content and the source's, side by side, then overwrite or skip once. Never a unified diff — the user asked for the two versions. No "never offer again". A run with no such conflict never shows this screen.

Screen 4 — apt config. Every `/etc/apt/apt.conf.d` file, in **all three directions**: add, change and remove, each an ordinary review line with the ordinary three-way decision and the ordinary machine-local registry (ruling 11). This is the plain `_diff_apt_configs` path that already exists (`apt_sync.py:857`) and it is deliberately left alone.

Nothing else is ever asked about `/etc/apt`.

Screen 4 makes "only packages are reviewed" a **near-rule, not an absolute**, and the exception is deliberate. Every other `/etc/apt` file earns its place on the target by serving a package: a repository is where a package comes from, a keyring is what makes that repository trusted, a pin is what makes that origin win. An `apt.conf.d` file governs apt's behaviour — recommends, proxies, retention, periodic updates — and nothing about an approved package implies whether it should travel. With no package to derive it from, the only honest source of that answer is the user, in all three directions. It keeps the registry for the same reason: a proxy or a `no-install-recommends` policy is a standing machine-local preference the user can hold permanently, unlike a repository removal, whose remedy is consolidating the two files.

What is derived, never ticked:

- Adding a repository. A source file lands on the target because a package approved on screen 1 comes from it (ruling 4). "Package ticked, its source unticked" is unrepresentable because the source has no tick.
- The signing key that repository names. Already true (`d27337da`, `apt_sync.py:2735`); unchanged.
- The pin that makes that origin win. New, and load-bearing — see §2. Pin adds and pin updates always sync, silently; only pin removal is reviewed (ruling 12).
- Overwriting a repository file that differs on the two machines, when no machine-specific package is involved (ruling 6, normal case).
- `ubuntu.sources`, `/etc/apt/sources.list`, `ubuntu-esm-apps.sources`, `ubuntu-esm-infra.sources`: written when missing, overwritten when different, never removed and never offered for removal (ruling 7, §5 below — with the ESM attachment gate at §5.3).
- Every `/etc/apt/preferences.d` file the source has: written when missing, overwritten when different.
- Deleting a keyring nothing references any more. Already true (`apt_sync.py:2754`); unchanged.

What disappears from the review entirely: repository INSTALL lines, repository CHANGE lines (except screen 3), pin INSTALL and CHANGE lines, the `HELD_OR_PINNED` pin echo on packages, and the second mid-`execute()` review added in `089ea985`. Apt-config lines stay, in every direction.

Consequence to state plainly so it does not read as a contradiction with ruling 7: **a repository on the source that feeds no package this run syncs does not travel.** That is ruling 4 working as intended. Ruling 7's "the user wants package sources to be the same on both" is scoped to the distribution's own files, which is why those four get their own always-sync bucket.

## 2. Origin replication

### 2.1 Where the two sides' origins come from

Source side. `capture_source_items` already runs one batched `apt-cache policy` over the whole `apt-mark showmanual` set (`apt_sync.py:1117`) to find bare-`.deb` packages. **Reuse that same stdout** — do not issue a second policy call. Feed it to `installed_origins_by_package` (`apt_policy.py:72`), which returns `{package: frozenset[origin URI]}` for the *installed* version only, skipping `/var/lib/dpkg/status`. Store as `self._source_origins: Mapping[str, frozenset[str]]`.

Source file → URIs. `_parse_source_file` (`apt_sync.py:682`) already extracts every `URIs:` / `deb`-line URI, normalised through `normalise_repo_uri` (`apt_policy.py:32`, strips the trailing slash apt also strips). `_scan_target_source_references` (`apt_sync.py:778`) does this for every source file on the TARGET in one batched `find … -exec awk`. A **source-machine twin is needed**: same command shape, run against the source, producing `{filename: (keyring_refs, uris)}` for `/etc/apt/sources.list.d` and `/etc/apt/sources.list`. Extract the existing body into a module-level helper taking a `run` callable — it already takes one — and call it for both machines in `_plan_repo_diffs` (`apt_sync.py:1388` is the existing target call site).

Target side. `collect_unavailable_item_ids` (`apt_sync.py:1205`) asks the target `apt-cache policy` over the missing names and keeps only `Candidate: (none)`. That call stays; what is parsed out of it changes. A **new parser** is needed in `apt_policy.py`:

```python
def candidate_origins_by_package(policy_output: str) -> dict[str, frozenset[str]]:
```

Same block-header and eight-space-indent walk as `installed_origins_by_package` (`apt_policy.py:91-114`), but it locates the version row whose version string equals the block's `Candidate:` value rather than the row marked `***`, and collects that row's origin URIs. Same key/value separability rule as `df48cd07` established: a name apt printed no block for gets **no key**; a name whose candidate has no repository origin gets an **empty set**. `Candidate: (none)` yields an empty set. Callers must never read absence as evidence.

### 2.2 The mapping

```python
def _source_files_serving(origins: frozenset[str]) -> frozenset[str]:
```

`{source filename}` for every file in the source's scan whose URI tuple intersects `origins`. The union, not a pick: a package's installed version can genuinely list several origins, and every one of them served it on the source.

Per package, the plan therefore holds: `source_origins`, `source_files` (possibly empty), `target_candidate_origins` (possibly absent).

### 2.3 Classification of a package missing on the target

This replaces `collect_unavailable_item_ids`'s single question ("does the target have any candidate?") with four outcomes. `unavailable_item_ids` as a parameter of `_diff_apt_packages` (`apt_sync.py:326`) is replaced by an `origin_plan: Mapping[str, _OriginPlan]` argument.

1. **Same origin.** `target_candidate_origins ∩ source_origins ≠ ∅`. Ordinary `MISSING_ON_TARGET`/`INSTALL`. No repository work is derived for it. Detail names the origin unless it is a distribution origin (§2.5).
2. **Different origin — the Firefox case.** The target has a candidate, but from none of the source's origins. This is the live bug: today `apt_sync.py:1205` matches on name only, sees a candidate, emits an ordinary `INSTALL`, and installs a different vendor's package. New behaviour: still `MISSING_ON_TARGET`/`INSTALL`, but the item **carries derived repository work** — every file in `source_files`, its keyrings, and the always-sync pin bucket — and its review detail names the origin the install will come from. Approving it adds the Mozilla repository, writes the pin, refreshes metadata, then §2.4's verification decides whether the install is allowed to run. The user is never asked about the repository; they were asked about the package.
3. **No target candidate, origin replicable.** `source_files` non-empty. Also `MISSING_ON_TARGET`/`INSTALL` with derived work. This subsumes what `089ea985`'s second review existed to catch (matrix row N13): the repository lands because the package was approved, so the availability question never has to be re-asked. Because the target's apt cannot locate the name until that repository lands, this outcome is also the one excluded from D-30's plan-time collateral rehearsal and protected by the apply-time guard alone (ADR-020 D-40, ADR-022 D-01).
4. **No target candidate, origin not replicable.** `source_files` empty (the origin is declared by no file on the source — a deleted repository whose packages remain, a `cdrom:` origin), or every serving file is unwritable because its `Signed-By:` resolves to no key on the source (`_dangling_keyring_ref`, `apt_sync.py:737`). `REPO_UNAVAILABLE`/`REPORT_ONLY`, detail naming the origin and the cause. This is `REPO_UNAVAILABLE`'s **only** remaining meaning; it is no longer "apt printed `Candidate: (none)`".

Bare-`.deb` packages never reach any of this: they are dropped at capture (`apt_sync.py:1092`, `30a9eb6f`) and are `manual_installs_sync`'s exclusive territory (ruling 10).

### 2.4 Enforcement — the pin must travel, and the origin is verified before the install

Verified this session on the development machine: Ubuntu's archive offers `firefox` at `1:1snap1-0ubuntu5`, priority 500, from `http://ftp.belnet.be/ubuntu noble/main`. That version carries **epoch 1**. Mozilla's own `firefox` deb carries no epoch. Under equal priority apt picks the highest version, and any epoch-1 version outranks every epoch-0 version regardless of the upstream number. **Adding the Mozilla repository alone does not make Mozilla's firefox win.** Mozilla's documented setup therefore ships a `/etc/apt/preferences.d` file pinning `origin packages.mozilla.org` to priority 1000. If that pin does not travel, the target installs Ubuntu's transitional package and the sync has replicated the name while inverting the provenance.

Two consequences, both mandatory:

- Pins are in the **always-sync bucket** (§1, §5): every `/etc/apt/preferences.d` file the source has is written to the target when missing and overwritten when different, with no review line and no derivation predicate. A pin naming an origin the target does not have is inert, so the always-sync rule costs nothing and cannot get the derivation wrong. This is simpler than deriving pins per package and it is what makes ruling 1's "write the pin" true in the only case that matters.
- After the `/etc/apt` group's single `apt-get update`, and before the first package install, **one batched `apt-cache policy` over the approved install names** re-reads the target's candidate origins. For each approved install, `candidate_origins ∩ source_origins` must be non-empty. A package that fails this check is refused with `ConvergeItemFailed` naming both the origin the source uses and the origin the target would install from. It is a per-item failure (D-27), never a prompt and never a silent proceed. Packages whose source origin is a distribution origin (§2.5) are exempt from the check — a different Ubuntu mirror is not a different vendor.

This verification is the hard guarantee behind ruling 2. Everything else is best effort; this is the thing that makes "silently installs a different vendor's package" unreachable.

### 2.5 Distribution origins, and what the review shows

A **distribution origin** is any URI declared by one of the never-removed files (§5) **on the machine whose origin is being classified**: `ubuntu.sources`, `/etc/apt/sources.list`, `ubuntu-esm-apps.sources`, `ubuntu-esm-infra.sources`. On the development machine that resolves to `http://ftp.belnet.be/ubuntu`, `http://security.ubuntu.com/ubuntu`, `https://esm.ubuntu.com/apps/ubuntu` and `https://esm.ubuntu.com/infra/ubuntu` (read this session). Computing it per machine is what stops two machines on different Ubuntu mirrors from disagreeing about every package.

Review display (ruling 1 + ruling 9): a package line names its origin only when at least one source origin is non-distribution. The display form is the URI with the scheme stripped and the trailing slash removed — `ppa.launchpadcontent.net/git-core/ppa/ubuntu`, not `ppa.launchpadcontent.net` and not the bare host. Several non-distribution origins are named comma-separated, sorted. The comparison form stays exactly what `normalise_repo_uri` produces (scheme included); only the display strips.

```python
def build_origin_detail(origins: Sequence[str]) -> str | None:
```

Returns `None` when every origin is a distribution origin. Lives in `apt_sync.py` beside `build_orphaned_packages_detail` (`apt_sync.py:588`), not in `packages/items.py` — it is apt's own wording (D-15, and the precedent `296bc082` set).

### 2.6 Package present on both machines with divergent provenance

Ruling 2 makes origin part of what is replicated, so a package installed on both machines from **different vendors** is a real divergence even when the diff engine currently sees nothing. Compare each side's **non-distribution** installed origins. If both sides have non-distribution origins and they do not intersect, emit a new diff class:

`DiffClass.ORIGIN_MISMATCH` → `DiffAction.REPORT_ONLY`, detail naming both origins.

Report only, and **no derived repository work**: converging it would mean a cross-vendor reinstall, which is neither a float (D-04) nor something the user asked for. Deriving a repository for a report-only item would also break ruling 4's "derived from the packages approved from it". The distribution-origin suppression is what keeps a mirror difference from firing this on every package.

### 2.7 Cases that break the mapping, enumerated

| Case | Behaviour |
| --- | --- |
| Package served by several origins on the source | `source_files` is the union of every file serving any of them; every non-distribution origin is named in the detail |
| Origin matches no source file on the source | Class 4 above: `REPO_UNAVAILABLE`/`REPORT_ONLY`, detail naming the origin and "no repository file on the source declares it" |
| Target candidate from the same origin | Class 1: ordinary install, zero repository work |
| Target candidate from a different origin | Class 2: repository + key + pins derived, then §2.4 verification decides |
| No target candidate at all | Class 3 if replicable, class 4 if not |
| Source origin is a distribution origin only | No origin shown, no repository work, §2.4 verification skipped |
| `apt-cache policy` produced no block for the name | No key in the map. Never read as evidence (`df48cd07`, `apt_policy.py:117-137`). Treated as class 3 if `source_files` is non-empty, class 4 otherwise |

## 3. Ordering and the transaction

### 3.1 Where derivation runs

Three stages, matching the seams that already exist.

`plan()` (`apt_sync.py:1269`) computes the **mapping**: source origins, source-file URIs on both machines, target candidate origins, and per package the set of source files that would have to land. It needs source reads and it feeds both the review's origin labels and the dry-run preview (ADR-014), so it cannot wait. It writes nothing.

`accept_review()` (`apt_sync.py:1782`) turns decisions into the concrete **write set**: for every `APPLY`-decided install, the union of its source files; plus every always-sync file that is missing or differing; plus every approved repository, pin or apt-config removal; plus every approved apt-config add or change; plus the keyrings those files reference (`_surviving_keyring_refs`, `apt_sync.py:2684`, whose three-population rule is unchanged but now reads "files this run writes" from the derived set rather than from approved `apt:source:` decisions). This is exactly where the synthetic metadata-refresh marker is already inserted (`apt_sync.py:1817`), so the marker's trigger condition changes from "a repository-group item was approved" to "the derived write set or the approved removal set is non-empty, or a keyring needs writing".

`apply()` (`apt_sync.py:1841`) executes it, then the packages.

### 3.2 The `/etc/apt` group stays transactional

ADR-020's boundary ("a group whose metadata refresh fails leaves `/etc/apt` as it found it") is carried forward unchanged in mechanism. `_ensure_repo_group_converged` (`apt_sync.py:2224`) keeps its backup-everything / write / one `apt-get update` / roll-back-everything shape, including `_backup_destination` (`apt_sync.py:2447`), `_rollback_repo_group` (`apt_sync.py:2351`) and the backup-failure short circuit (`apt_sync.py:2280`).

What changes is **membership and failure attribution**. The group is now a mix of reviewed items (repository and pin removals, repository conflict-overwrites, apt-config items in all three directions) and derived files (repository adds, always-sync pin and distribution writes, keyrings). `self._repo_group_outcome` (`apt_sync.py:1029`) keys on `item_id`, so it can only carry the reviewed half. Derived writes therefore report differently:

- A derived write that fails is logged at `ERROR` and recorded in a new `self._failed_derived_writes: dict[str, str]` keyed by absolute destination path.
- Every approved package install whose derived file set intersects `_failed_derived_writes` fails with `ConvergeItemFailed` naming the file and the reason. This is the replacement for `_require_keyrings_ready`'s source-level refusal (`apt_sync.py:2813`): the refusal now lands on the thing the user actually decided about, which is the package.
- A rollback marks **every** derived write as failed, so every package that depended on one fails too — matching what the rollback already does to reviewed items (`apt_sync.py:2345`).

`apply()` calls `_ensure_repo_group_converged()` eagerly and unconditionally when the write set is non-empty, then runs §2.4's origin verification, then `super().apply()`. The lazy first-repository-diff trigger in `converge()` (`apt_sync.py:2048`) survives only for the reviewed removal and conflict items.

### 3.3 The command order

Unchanged in shape, restated for the derived world:

1. keyrings referenced by any file the group will write (`_provision_keyrings`, `apt_sync.py:2735`)
2. `/etc/apt/preferences.d` pin writes (always-sync) and approved `/etc/apt/apt.conf.d` writes — before sources, so a pin is in place the moment its origin becomes fetchable and an apt-config setting governs the update that follows
3. `/etc/apt/sources.list` and the never-removed `sources.list.d` files (always-sync bucket)
4. derived repository adds and conflict-overwrites
5. approved removals: repository files, then pin files, then apt-config files
6. unused-keyring collection (`_remove_unused_keyrings`, `apt_sync.py:2754`) — only when step 5 removed a repository file
7. one `sudo apt-get update` (`apt_sync.py:2310`) — still exactly one per run across both refresh paths (`_ensure_metadata_refreshed`, `apt_sync.py:2059`)
8. §2.4's batched origin verification
9. package installs and removals
10. holds (`_ITEM_CLASS_ORDER`, `apt_sync.py:191`, rank 4)

`_ITEM_CLASS_ORDER` keeps `APT_PIN`/`APT_CONFIG` at rank 1 and `APT_SOURCE` at rank 2 for the reviewed items; the derived writes are not diffs and take their order from the list above, inside the group.

### 3.4 The second review and the pin echo are deleted together (ruling 15)

`089ea985` added `_rereview_repo_invalidated_packages` (`apt_sync.py:1881`) for two cases:

- A pin the user is deleting was still forcing its packages into `HELD_OR_PINNED`/`REPORT_ONLY` at plan time (`apt_sync.py:366-376`), suppressing their real diff.
- A repository installed this run supplies a package apt had no candidate for at plan time.

Under this model **both reasons are gone**.

The second case is gone because repository adds are derived *from* the package decision: the package is classified from the SOURCE's origins (§2.3), not from the target's current candidate set, so its actionability never depends on a repository this run has not written yet. Row N13's scenario now resolves inside a single review.

The first case is gone because **the pin echo is deleted**. The `pinned` branch of `_diff_apt_packages` (`apt_sync.py:352-355, 366-376`) fires whenever a package present on the target is named by any target-side pin stanza, and it is a net negative: it turns a no-op (same version on both, pinned) into review noise, and it makes a package that exists only on the target and is named by any pin **impossible to remove and impossible to silence**. That is the open defect the handover records at `02-HANDOVER-package-review.md:62`; ruling 15 closes it by deleting the branch. Pins now do exactly one job in this model, which is deciding which origin wins, and that is checked against the real post-update state in §2.4 rather than guessed at plan time.

What goes is only the per-package echo item reporting that a pin exists. **Pins themselves still replicate, as files** (§1, §5) — the echo was never the mechanism, only a report about it.

What that deletion simplifies, concretely: `_rereview_repo_invalidated_packages` (`apt_sync.py:1881-1971`), `_replanned_package_diffs` (`1973-1995`), `_merge_replanned_diffs` (`1997-2017`), `_rereview_groups` (`2019-2026`), `_repository_work_approved` (`1872-1879`), the `self._plan_source_items` cache (`1044`), the `apply()` override's re-review half (`1867-1869`), the `_RecordingReviewer` fixture (`tests/unit/jobs/test_apt_sync.py:3990`) and eight tests. It also removes the untested-by-construction hazard the handover flags first (`02-HANDOVER-package-review.md:31`): a `questionary` prompt firing mid-`execute()` with the Rich Live display paused at a point nothing has ever exercised. After this change **every apt prompt happens before the first mutating command**, which is what ADR-020 D-24 wanted in the first place.

Residual staleness after the deletion: none that a review could answer. The dry-run preview still shows a plan-time classification, but under this model that classification no longer depends on anything the run would change — it depends on the source's origins, which the run never mutates. That is a genuine improvement over the note at `02-SCENARIO-COVERAGE.md:370`.

## 4. The machine-specific follow-up (ruling 3)

### 4.1 The shared computation

One helper, generalised out of `_source_removal_details` (`apt_sync.py:1408`), which already does exactly this for the removal direction:

```python
async def _machine_specific_packages_by_source_file(
    self, target_run: Callable[[str], Awaitable[CommandResult]], filenames: frozenset[str]
) -> dict[str, list[str]]:
```

Reads the target's decision file for `apt:package:` ids (`apt_sync.py:1444`), runs **one** batched `apt-cache policy` over those names, maps each package's installed-version origins through `installed_origins_by_package`, and inverts against the target's own source-file URIs (`self._target_source_refs`, `apt_sync.py:1018`). Gated on `filenames` being non-empty so an ordinary run pays nothing. Scope stays machine-specific packages only, for the reason already recorded at `apt_sync.py:1418-1426`: an ordinary package is at least eligible for its own removal diff, and keying off the whole manual set would name a hundred packages on a base-repository change.

Called with two different filename sets, producing two different prompts.

### 4.2 Trigger A — repository removal (ruling 5, unchanged)

Filenames: the extra-on-target source files. Result: `detail` text via `build_orphaned_packages_detail` (`apt_sync.py:588`). **This is not a confirmation** — it is disclosure on a review line the user is already answering. Answers: remove or skip once. No skip-always.

### 4.3 Trigger B — repository conflict (ruling 6)

Filenames: source files present on both machines with different digests. When the result for a file is non-empty, that file becomes a screen-3 entry instead of a silent overwrite.

Shown per entry: the filename, the machine-specific packages it feeds, then the **target's current content** and the **source's content**, each read with `_read_file_content` (`apt_sync.py:764`, the existing sudo-qualified single-file read) and each rendered as a Rich `Text` inside its own `Panel` — never as a bare `str`, per the markup-crash rule already encoded at `review.py:234`. No unified diff: the user said the diff is not readable.

Asked: overwrite the target with the source's version, or skip once. Two answers. Skip-always is deliberately absent — the user's position is that they have effectively only the choice to delay until the next sync and should consolidate the two files themselves.

Recorded: `Decision.APPLY` → the file is in the write set; `Decision.SKIP_ONCE` → it is not, and every approved package install whose derived file set contains it fails with a message naming the file (same mechanism as §3.2's failed derived write). A conflict-skipped repository is not silently ignored: a package the user ticked whose origin depends on that file cannot be delivered, and pretending otherwise would install from the wrong origin.

Never recorded in a decision file (ruling 5's "no registry" extends here by the same reasoning: a two-way answer has no third state to persist).

### 4.4 One mechanism or two

**Two prompts, one computation.** Trigger A produces text on an existing line; trigger B produces its own screen. They share `_machine_specific_packages_by_source_file` and nothing else.

The D-30 collateral prompt (`review.py:389`, `COLLATERAL_REVIEW_ACTION`) is a **third, separate** mechanism and keeps its shape: its subject is a package apt's own simulation would remove or downgrade, not a repository file, and its answers stay install-anyway / skip / abort, not overwrite / skip-once.

Its **trigger narrows** (ruling 13). `_protected_manual_set` (`apt_sync.py:1628`) today returns the union of the target's and the source's `apt-mark showmanual` sets; it now returns the target's set alone. `self._source_manual_set` (`apt_sync.py:1050`, assigned at `1091`) loses its only consumer and is deleted with it.

This is a **knowingly accepted loss**, not an oversight. The case given up is the source-intent one: a package the user manually installed on the source, which arrives on the target as an automatically-installed dependency and is then removed as collateral of some later approved install. The rule that replaces it is simpler and easier to defend — if the target's apt installed a package automatically, the target's apt owns it, and reclaiming it as a user choice on the strength of the *other* machine's bookkeeping is a guess. The narrower set is also the one apt itself consults, which makes "manually installed" mean the same thing to pc-switcher and to apt on the machine being changed.

Two things stay true after the narrowing: a package that is manual on the target is still protected, in both the plan-time and the apply-time guard, and the machine-specific decision list is still not consulted for collateral (matrix row D20's accepted limitation, unchanged).

### 4.5 Review plumbing

Two new sentinel `ReviewGroup.action` values in `review.py`, siblings of `COLLATERAL_REVIEW_ACTION` (`review.py:127`):

```python
REPO_REMOVAL_REVIEW_ACTION = "repo_removal"
REPO_CONFLICT_REVIEW_ACTION = "repo_conflict"
```

`review.py:100` and `review.py:108` currently derive one set from the other (`_ACTIONABLE_ACTIONS = _REMOVAL_ACTIONS | {…}`), which makes "unticked by default" and "offered permanence" impossible to separate. Split them into two independent frozensets:

- `_REMOVAL_ACTIONS` — decides the default-unticked state. Add `REPO_REMOVAL_REVIEW_ACTION`.
- `_PROMOTABLE_ACTIONS` — decides whether `_offer_permanent_skips` (`review.py:340`) runs. Contains `{"install", "add", "enable", "change", "remove", "delete", "disable"}` and **not** either new sentinel.

`REPO_REMOVAL_REVIEW_ACTION` carries **both** repository-file and pin-file removals (ruling 12). They still reach the user as two separate screens, because `_build_review_groups` keys on `(action, item_class)` (`sync_core.py:236`) — one sentinel, two groups, two titles.

`/etc/apt/apt.conf.d` uses **none** of this plumbing. Its diffs keep the plain `DiffAction` values, so they fall into the ordinary checkbox path, the ordinary `_PROMOTABLE_ACTIONS` membership and the ordinary registry with no special-casing anywhere (ruling 11). The only change apt config needs is vocabulary (§6).

`REPO_REMOVAL_REVIEW_ACTION` groups render as ordinary checkbox lists (unticked) with no permanence pass. `REPO_CONFLICT_REVIEW_ACTION` groups get their own per-entry flow, a two-choice sibling of `_review_collateral_group` (`review.py:389`):

```python
async def _review_repo_conflict_group(
    group: ReviewGroup, *, console: Console, decisions: dict[str, Decision]
) -> None:
```

Choices: "Overwrite the target's file with the source's" → `Decision.APPLY`; "Skip for now" → `Decision.SKIP_ONCE`. Ctrl-C / EOF raises `SyncAbortedByUser` naming the file, matching every other screen (`review.py:511`). Non-interactive: every entry `SKIP_ONCE`, nothing recorded, no entry in `ReviewOutcome.unresolved` (which stays reserved for unreproducible items, `review.py:473`).

The conflict entry needs both file contents, which `ReviewEntry` (`review.py:138`) cannot carry. Add one optional field rather than a second entry type:

```python
@dataclass(frozen=True)
class ReviewEntry:
    ...
    versions: tuple[str, str] | None = None  # (target's current content, source's content)
```

Defaulted, so every existing construction site is unaffected.

## 5. Never-removed source files (ruling 7)

### 5.1 The set, and how it is matched

Matched by **exact filename** against a module-level frozenset in `apt_sync.py`, plus one absolute path:

```python
_DISTRO_SOURCE_FILENAMES = frozenset({
    "ubuntu.sources", "ubuntu-esm-apps.sources", "ubuntu-esm-infra.sources",
})
_ALWAYS_SYNCED_ABSOLUTE = ("/etc/apt/sources.list",)
```

Exact names, not a `ubuntu-esm-*` glob: a glob would also swallow a file a user named `ubuntu-esm-mine.sources`, and the set is short enough to enumerate. Verified on the development machine this session — `/etc/apt/sources.list.d` holds exactly `ubuntu.sources`, `ubuntu-esm-apps.sources` and `ubuntu-esm-infra.sources` from this family.

These files: written when the target lacks them, overwritten when the digests differ (subject to §4.3's conflict prompt like any other file), **never emitted as a removal diff**, never given a review line of any other kind. They are derived file operations, not items — they need no `item_id`, exactly as signing keys need none (`d27337da`).

This **revises** the handover's line "`/etc/apt/sources.list` and `ubuntu.sources` are never collected" (`02-HANDOVER-package-review.md:42`). That sentence's concern was deletion, and deletion is still forbidden. What changes is that they are now also *written* and *updated*, which the handover never addressed. The two do not conflict; the handover was silent on the add direction, not opposed to it.

`/etc/apt/sources.list` is currently outside the item model entirely (`apt_sync.py:161-165`: "NOT an item class — this file is never captured, diffed or written"). It now needs a digest on both machines. It is a single file, not a directory, so `_capture_dir_digests` (`apt_sync.py:753`) does not fit: add one `sudo sha256sum /etc/apt/sources.list` per machine, parsed with the existing `_parse_sha256sum` (`apt_sync.py:666`), tolerant of the file being absent (`sha256sum` on a missing path prints nothing to stdout and fails; treat as "no digest"). It stays in the keyring-reference scan it is already part of (`apt_sync.py:807`).

### 5.2 A related correctness fix, in scope because §5.1 touches the same capture

`_capture_dir_digests` uses `find <dir> -maxdepth 1 -type f` (`apt_sync.py:760`), which captures every file in `sources.list.d`. Observed on the development machine this session: that directory also holds `ubuntu.sources.save`, `ubuntu.sources.curtin.orig`, `ubuntu-esm-apps.sources.save` and `ubuntu-esm-infra.sources.save`. **apt does not read those** — it only reads `*.list` and `*.sources` — so today pc-switcher would offer four files apt ignores as review items. Restrict the `sources.list.d` capture to `-name '*.list' -o -name '*.sources'`. `preferences.d` and `apt.conf.d` keep the current unfiltered capture: apt reads extensionless files there, and the development machine's `preferences.d` holds six of them (`gh-github`, `nodejs`, `no-esm-docker`, `nsolid`, `ubuntu-pro-esm-apps`, `ubuntu-pro-esm-infra`).

### 5.3 The ESM / Pro attachment gate

#### 5.3.1 What the gate is for, measured

Writing the two ESM sources to an unattached target does **not** break `apt-get update`, and does not roll the transactional `/etc/apt` group back. Measured in a stock `ubuntu:24.04` container carrying both real ESM source files copied from a Pro-attached host: `apt-get update` **exits 0** with the ESM sources present, no credentials and `/etc/apt/auth.conf.d/` empty, because `esm.ubuntu.com` serves its repository *index* publicly (HTTP 200 on `.../dists/noble-apps-security/InRelease`); the suites are fetched and marked `Trusted: yes`. Only the *pool* is 401. Measured in the same container, a source that genuinely fails also does not abort the others: with the ESM keyrings removed the refresh exits 100 with `E: The repository ... is not signed.` and still writes all 19 other lists, and against a synthetic index-level 401 it exits 100 and writes all 27 others. A non-zero `apt-get update` is an aggregate signal, never an abort.

The real hazard comes later, at install time. The ESM versions enter candidate selection at priority 500 — above `noble/universe` (`apt-cache policy 7zip`) — so the target's next install of an ESM-covered package fails when it fetches the `.deb`: installing `7zip` exits 100 with `401 Unauthorized`. The container had 0 of 13 upgradable packages with an ESM candidate; that a desktop with a large `universe` set has many more is **inferred from the priority ordering, not measured**.

pc-switcher cannot resolve this itself. `pro attach` requires a subscription token from the user's Pro dashboard or an interactive browser short-code flow; the source's own credentials are root-only (`/var/lib/ubuntu-advantage/private/` is unreadable to the ordinary user), a machine's token is not reusable for another machine, and holding the user's token would put a secret on a command line. So the tool asks (ruling 14).

#### 5.3.2 Trigger and placement

Trigger, computable at plan time: the source has `ubuntu-esm-apps.sources` or `ubuntu-esm-infra.sources` and the target's digest for that file is absent or different — i.e. the always-sync bucket would write it. These files are not derived from approved packages (§1), so the trigger needs no review outcome; the two machines' `sources.list.d` digests are enough. No ESM write pending means no probe and no prompt.

Placement: in `plan()`, immediately after `await self._capture_origin_state()`, before `_plan_packages()` and before any review group is built. Three reasons, each load-bearing: one answer ends the job, so it must precede the expensive planning and the review the user would otherwise answer for nothing; the probe is a read, and `plan()` is the last read-only phase; and it puts the question and its copy-paste remediation on screen before anything is approved or written, which is as much of the standing "validate environment assumptions early" rule as this question can satisfy.

As shipped, `_capture_origin_state` also owns the `sources.list.d` digest capture and the `/etc/apt/sources.list` file digest. They were `_plan_repo_diffs`'s, which runs after `_plan_packages` — so the trigger was unreadable at the placement above until they moved. Same commands, same count, one position earlier; `_plan_repo_diffs` reads the cached values.

Not `validate()`: every `ValidationError` is fatal. `orchestrator.py:1019-1025` collects them across all jobs and raises `RuntimeError`; `ValidationError` (`models.py:103`) has no severity field, so there is no non-fatal form and no way to express "the user answered, carry on".

#### 5.3.3 The probe

```python
async def _target_pro_attached(self) -> bool
```

Runs `pro status --format json` on the target via `self.target.run_command(..., login_shell=False)` — a read, so no `mutates=` — and returns the top-level `attached` boolean. Measured this session on a Pro-attached host: exit 0 for an ordinary unprivileged user, `attached` present in the top-level object. A non-zero exit, a missing `pro` binary, or unparseable output returns False: False asks a question, True writes files, and the question is the recoverable error.

The payload also carries an `account` object naming the subscriber. Only the parsed boolean may be logged, shown in the prompt, or put in a `JobSkipped` reason; the raw stdout must never reach the log or the console.

#### 5.3.4 The gate

```python
async def _gate_esm_writes(self, esm_files: Sequence[str]) -> bool
```

Two real outcomes: **True** — attached, or attached after a re-check; the ESM files travel with the rest of the always-sync bucket. **Raises `JobSkipped`** — the user chose to skip, or nobody could be asked. **False** survives only for the dry-run branch (rule 2), where nothing is written on either path; as shipped, `plan()` records the two filenames and `_compute_derived_writes` drops them, so the preview does not list writes no real run would make.

Rules:

1. Attached on the first probe: return True with no prompt.
2. Dry run (`self.context.dry_run`): never prompt. Log one WARNING naming both files and the unattached state, and return False. A rehearsal must not make the user perform a real attachment, and ADR-014 wants the preview to say what the run would carry. The WARNING must also state that a real run would skip `apt_sync` entirely (rule 3/5), or the preview describes an outcome no real run produces. *(That last clause is derived from rule 3, not separately ruled.)*
3. Non-interactive (`ask_gate` returns None): raise `JobSkipped`. **Ruled by the user**, replacing the earlier derived fallback that withheld the two files and let the rest of `apt_sync` proceed. Withholding is not a coherent partial outcome: `/etc/apt/preferences.d` is in the always-sync bucket with no derivation predicate (§2.4), so the source's ESM pins — `ubuntu-pro-esm-apps` and `ubuntu-pro-esm-infra` were both read on the development machine (§5.2) — are written to the target whether or not the sources they name arrive. The target ends up with the source's pin state over a different repository set, so its candidate selection matches neither machine. Skipping the whole job leaves `/etc/apt` untouched instead, which is a state the user can reason about.
4. Interactive and unattached: ask. "Attach now" re-probes; attached ends the loop with True, still-unattached says so and re-asks. The loop is **unbounded** — **ruled by the user**: re-check as many times as wanted, and the exit is choosing to skip. No answer count is kept and no bound is enforced.
5. "Skip apt_sync": raise `JobSkipped` immediately.

Prompt wording constraints. Exactly two answers, labelled "I have attached the target — re-check and continue" and "Skip apt_sync this run (other jobs continue)". The body must name both files; state that the target reports no Ubuntu Pro attachment; state in one clause what writing them would do (the ESM indexes are public and win candidate selection, so the target's next install of an ESM-covered package fails with `401 Unauthorized` when it fetches the `.deb`); give the two copy-paste commands to run **on the target** — `sudo pro attach <token from https://ubuntu.com/pro/dashboard>`, then `sudo pro enable esm-apps esm-infra`; and state that skipping leaves every other job running. No apology, no history, no reference to this document, nothing from the probe payload beyond the boolean. Untrusted content stays out of Rich markup (T-02-02) as everywhere else.

#### 5.3.5 The prompt seam

The question is not a review item: it precedes the review, and one of its answers means there is no review. It therefore does not go through `ReviewGroup`. It also does not go through `Confirmer`, whose non-interactive contract is an `--allow-*` flag this question has none of. `packages/review.py` already owns pause-the-live-UI-ask-resume, interactivity detection (`review.py:476`) and the Ctrl-C-aborts rule (`review.py:522`), so the gate reuses that module:

```python
# packages/review.py, sibling of review_items
async def ask_gate(
    *,
    title: str,
    message: str,
    proceed_label: str,
    stop_label: str,
    console: Console,
    ui: PausableUI,
    logger: logging.Logger | None = None,
) -> bool | None

# Reviewer protocol, and TerminalUIReviewer forwarding to the above
async def ask_gate(self, *, title: str, message: str, proceed_label: str, stop_label: str) -> bool | None
```

True is the proceed answer, False the stop answer, None means non-interactive — nobody was asked, and the caller owns the fallback. Ctrl-C raises `SyncAbortedByUser`, matching the checkbox screens. Every `Reviewer` double in the tests gains the method; the two interactive branches are unit-tested through a fake `Reviewer`, and a TTY-less run exercises the None branch without any automation hook.

#### 5.3.6 What skipping the job means

The `JobSkipped` exception and the orchestrator arm that records it are **not** ESM-specific and are specified in **S8a** (§9), which this gate depends on. That stage also converts the existing call sites that report `SUCCESS` for work they did not do.

What is specific to this gate: skipping `apt_sync` does not touch `snap_sync`, `flatpak_sync`, `manual_installs_sync` or `folder_sync`; it writes no decision-file entry; and it leaves the target's `/etc/apt` exactly as it was, ESM files included. The gate sits in `plan()` (§5.3.2), before any mutating command, so S8a's before-first-mutation rule holds by construction.

Because rule 3 makes the non-interactive path a skip, `JobSkipped` can now be raised with nobody watching. The reason string must therefore stand alone in a log file: name both ESM filenames, the unattached target, and that no TTY was available to ask. Nothing from the probe payload beyond the boolean (§5.3.3). The WARNING survives to the end-of-run summary (`ui.py:319` `add_warning`, `ui.py:345` `collected_warnings`), so an interactive user sees the reason after the sync as well as during.

## 6. What gets deleted

`src/pcswitcher/jobs/apt_sync.py`:

| Region | Fate |
| --- | --- |
| `HoldPinFact` (254-275) | delete |
| `build_held_or_pinned_detail` (277-283) | delete |
| `build_repo_unavailable_detail` (286-291) | replace with an origin-aware builder (§2.5) |
| pinned-package branch of `_diff_apt_packages` (352-355, 366-376) | delete (§3.4) |
| `unavailable_item_ids` parameter and its branch (326, 383-393) | replace with `origin_plan` (§2.3) |
| `_PIN_PACKAGE_RE` (627), `_pin_packages` (644-655), `_parse_pin_file` (726-734) | delete — nothing consumes package names from a pin file any more |
| `AptPinItem.pinned_packages` (510) and its hydration (1607, 1613, 1620) | delete |
| `_travelling_keyrings_detail` (1570-1589) | delete as review text; keys are reported by the derived-write FULL log line and the dry-run preview instead (§7 docs) |
| `_diff_apt_sources` missing branch (1499-1526) and changed branch (1543-1566) | delete as diff emission; the same parse feeds the derived write set and the §4.3 conflict test |
| `_diff_apt_sources` extra branch (1528-1541) | keep, retargeted to `REPO_REMOVAL_REVIEW_ACTION` |
| `_diff_apt_pins` (1591-1626) | delete entirely; pins are always-sync in the add/change direction, and their removal direction is built by the shared removal builder |
| `_diff_apt_configs` (857-872) | keep, all three branches, unchanged (ruling 11) |
| `_protected_manual_set` (1628) | narrow to the target's manual set alone (ruling 13, §4.4) |
| `self._source_manual_set` (1050) and its assignment (1091) | delete — the narrowing leaves it with no consumer |
| `collect_hold_pin_facts` (1155-1166), `_scan_target_pin_lines` (1168-1182), `_pin_facts_from_scan` (1184-1192) | delete |
| `collect_unavailable_item_ids` (1205-1217) | delete; replaced by the origin classification |
| `_repository_work_approved` (1872-1879) | delete |
| `_rereview_repo_invalidated_packages` (1881-1971) | delete |
| `_replanned_package_diffs` (1973-1995) | delete |
| `_merge_replanned_diffs` (1997-2017) | delete |
| `_rereview_groups` (2019-2026) | delete |
| `self._plan_source_items` (1044-1046) | delete |
| `_require_keyrings_ready` (2813-2840) | delete; its refusal moves onto the package item (§3.2) |
| `apply()` override (1841-1870) | rewritten: converge the derived group, run the §2.4 verification, call `super().apply()` |

`src/pcswitcher/jobs/packages/apt_policy.py`:

| Region | Fate |
| --- | --- |
| `packages_with_no_candidate` (41-69) and its `__all__` entry (28) | delete — its only caller is `collect_unavailable_item_ids`; `manual_installs_sync` uses `packages_installed_from_no_repository` |
| new `candidate_origins_by_package` | add (§2.1) |

`src/pcswitcher/jobs/packages/items.py`:

| Region | Fate |
| --- | --- |
| `DiffClass.HELD_OR_PINNED` (67) | delete — apt was its only producer (verified by grep across `src/`) |
| `DiffClass.REPO_UNAVAILABLE` (68) | keep, redefined (§2.3 class 4); update the docstring |
| new `DiffClass.ORIGIN_MISMATCH` | add (§2.6) |
| `ItemClass.APT_SOURCE`, `APT_PIN`, `APT_CONFIG` (46-48) | keep — `APT_SOURCE` and `APT_PIN` identify reviewed **removals** only; `APT_CONFIG` stays a full item in all three directions |

`src/pcswitcher/jobs/packages/sync_core.py`:

| Region | Fate |
| --- | --- |
| `_ACTION_VOCABULARY` entry `(APT_SOURCE, REMOVE): "delete repository"` (111) | keep; add `(APT_PIN, REMOVE)` plus all three `APT_CONFIG` pairs (INSTALL, CHANGE, REMOVE) so neither a pin removal nor an apt-config line reads "Remove apt packages" |
| everything else | unchanged — the base class carries no repository knowledge |

The group's `action` value stays the raw `DiffAction` (`sync_core.py:277`), so the vocabulary only supplies display verbs; the default-unticked test and the promotion test key off that raw value, which is what makes apt config's three-way behaviour fall out with no plumbing.

`src/pcswitcher/jobs/packages/review.py`:

| Region | Fate |
| --- | --- |
| `_REMOVAL_ACTIONS` / `_ACTIONABLE_ACTIONS` derivation (100, 108) | split into two independent sets (§4.5) |
| `ReviewEntry` (138) | one optional `versions` field |
| new sentinels + `_review_repo_conflict_group` | add (§4.5) |

Ruling 8 (no backwards compatibility): nothing reads a legacy `apt:source:` or `apt:pin:` decision-file entry, nothing migrates one, and no compatibility shim is written. Package sync has never run outside the test environment, so no such entry exists anywhere. `apt:config:` is the exception and needs no migration either — it keeps writing and reading exactly the entries it writes and reads today (ruling 11).

## 7. Blast radius

### 7.1 Unit tests that must change or die

`tests/unit/jobs/test_apt_sync.py`:

| Line | Test | Fate |
| --- | --- | --- |
| 581 | `test_collect_hold_pin_facts_returns_pins_only_no_holds` | die |
| 601 | `test_preferences_d_pin_surfaces_with_pin_mechanism_and_filename` | die |
| 722 | `test_pin_still_yields_report_only_echo_alongside_a_hold_item` | die |
| 1010-1065 | `TestUnavailableCapture` (both) | rewrite around origin classification |
| 1765 | `test_deb822_and_legacy_source_each_record_own_format` | rewrite: format is now recorded for a derived write, not a diff |
| 1816 | `test_source_with_key_present_on_source_yields_plain_install` | rewrite as a derived-write assertion |
| 1845 | `test_source_whose_key_the_target_already_has_names_no_key` | die (no source review line to name keys on) |
| 1872, 1895 | dangling-keyring `REPORT_ONLY` downgrade tests | rewrite: the dangling reference now makes the **package** `REPO_UNAVAILABLE` (§2.3 class 4) |
| 1250 | `test_excluded_package_still_counts_as_source_manual_for_collateral_protection` | invert: a source-manual-only package is no longer protected (ruling 13) |
| 1923 | `test_pin_and_config_diff_missing_extra_and_changed` | split: the pin half keeps only the extra direction; the config half is unchanged and stays (ruling 11) |
| 2868-2930 | `TestSourceOnlyCollateral` (2) | invert both: the plan-time item is not raised and the apply-time guard does not refuse (ruling 13) |
| 2073, 2094 | no-candidate / never-heard-of classification | rewrite around §2.3 |
| 2829 | `test_apply_on_a_report_only_source_writes_nothing_and_refreshes_nothing` | die (no `REPORT_ONLY` source item) |
| 2995-3190 | `TestRepoRemovalNamesMachineSpecificPackages` (7) | keep; extend with the conflict direction |
| 3724-3760 | `TestPinStanzaParsing` (3) | die with `_pin_packages` |
| 3990 | `_RecordingReviewer` | die if no other test uses it after the second-review deletion |
| 4037-4172 | `TestSecondReviewAfterRepositoryChanges` (6) | die |
| 4174-4232 | `TestNewRepositoryMakesAPackageAvailable` | die; replaced by §10's single-review equivalent |
| 4302, 4313 | `TestHoldPinFactAndDetails` (2) | die |
| 4339 | `test_build_repo_unavailable_detail_names_the_package` | rewrite for the new detail |
| 4428 | `test_pin_fact_yields_held_or_pinned_distinguishable_from_a_hold_item` | die |
| 4444 | `test_missing_and_unavailable_yields_repo_unavailable_not_install` | rewrite |

`tests/unit/jobs/test_block_state_decisions.py`:

| Line | Test | Fate |
| --- | --- | --- |
| 219 | `test_declined_source_install_is_recorded_on_source_and_never_re_offered` | die — a source install is no longer offered, so it cannot be declined or recorded |
| 242 | `test_a_signing_key_is_never_offered_and_so_can_never_be_recorded` | keep; extend to assert that no `apt:source:` or `apt:pin:` id can reach a decision file in **any** direction. `apt:config:` must NOT be added to that assertion — it is recorded in all three directions (ruling 11), which is what `TestAptRepoItemDecisions` (C27) keeps covering |

`tests/unit/jobs/test_package_review.py`: add coverage for the two new sentinels; `test_install_group_defaults_checked_removal_group_defaults_unchecked` and the permanence tests (`TestPermanentSkipPromotion`, per `02-SCENARIO-COVERAGE.md:230`) must be extended to assert that a `repo_removal` group is unticked **and** never offered permanence.

`tests/unit/jobs/test_package_sync_core.py`: `_ACTION_VOCABULARY` fallback tests (`test_every_pair_without_a_vocabulary_entry_still_produces_a_usable_group`) still pass; add the two new vocabulary entries to whatever asserts the table.

### 7.2 Integration tests

`tests/integration/jobs/test_package_sync.py`:

| Line | Test | Fate |
| --- | --- | --- |
| 1009 | `test_apt_repository_state_dry_run_reviews_the_repo_and_carries_its_key` | rewrite — the dry run no longer *reviews* the repository; it previews a derived write |
| 1183-1288 | `test_continue_on_item_failure` (its `REPO_UNAVAILABLE` premise at 1197) | rewrite around §2.3 class 4 |
| 2237 | `test_apt_source_and_its_key_removed_together` | keep; the removal direction is unchanged |
| new | the Firefox scenario (§10.6) | add |

No `pytest.skip` may appear in this module (`02-HANDOVER-package-review.md:77`). Fixtures live in `tests/integration/scripts/internal/vm-test-fixtures.sh`, whose `FIXTURES_VERSION` (line 31) must be bumped together with `PCSWITCHER_TEST_FIXTURES_VERSION` in `tests/integration/scripts/internal/common.sh` whenever the baseline gains a subject.

### 7.3 Scenario matrix rows invalidated

`.planning/phases/02-package-management-sync/02-SCENARIO-COVERAGE.md`:

- **A2** — `REPO_UNAVAILABLE` no longer means "no candidate"; restate as §2.3 class 4.
- **A7, A7a, A9** — the pin echo and the `Package:`-stanza parse are gone. Delete; A7a's multi-name/wildcard concern has no consumer left.
- **A11** — already stale: `30a9eb6f` dropped bare-`.deb` packages at capture, so one package is no longer described twice. Mark resolved.
- **C1, C2, C3** — source INSTALL / dangling-key `REPORT_ONLY`: restate against derived writes and the package-level `REPO_UNAVAILABLE`.
- **C4, C5** — format and `.list`/`.sources` coexistence: still true, but they are no longer *review* facts. Restate.
- **C9** — pin/config missing/extra/changed: split into two rows. For pins only the extra direction survives as a diff; for apt config all three directions survive unchanged (ruling 11).
- **C13** — "key write fails → it is the SOURCE that fails": now the **package** fails (§3.2). Restate.
- **C27** — skip-always on a digest-derived repo item: narrow to `apt:config:` only; rulings 5 and 12 forbid the entry for `apt:source:` and `apt:pin:`.
- **D3** (`test_source_only_manual_collateral_removal_becomes_a_review_item`), **D9**'s source-only half and **N9** — the source half of the collateral union. Invert: ruling 13 makes a source-manual-only package unprotected. **D20** stays as written.
- **C28** — keyring provisioning trigger changes from "a source this run writes" to "a source the derived set writes". Restate.
- **D19** — "only a `REPORT_ONLY` repo item is decided APPLY": delete, no such item exists.
- **H19** — keep, but the enumerated `REPORT_ONLY` classes change (pin echo out, `ORIGIN_MISMATCH` in).
- **N5** — the key→source→update→install narrative survives but is now derived, not reviewed. Restate.
- **N12, N13, N14** — the whole second-review family. Delete; N13's outcome is now a single-review property (§10.4).
- **New rows** needed: origin capture and mapping, same-origin install, different-origin install (Firefox), unreplicable origin, post-update origin verification, `ORIGIN_MISMATCH`, always-sync bucket, `/etc/apt/sources.list` write, `.save`-file exclusion, the ESM attachment gate and its two answers, the two-answer repository removal, the two-answer pin removal, apt config keeping all three directions and its registry, and the conflict prompt.

### 7.4 Docs

- `docs/jobs/package-sync.md:42-56` — the review description gains origins and loses two of the three-way promises for repository items.
- `docs/jobs/package-sync.md:58-64` — the entire "A second apt review" section is deleted.
- `docs/jobs/package-sync.md:78-98` — "Signing keys" gains the pin as a second thing that travels invisibly, and loses the "a repository offered for install or change names the keys it would copy" paragraph (82).
- `docs/jobs/package-sync.md:167-177` — "Deletions" gains the two-answer rule for repository and pin removals and the never-removed set.
- New section in `docs/jobs/package-sync.md` — origins: what "from the same place" means, why the pin travels, what the post-update check refuses, and the ESM/Pro gate: why an unattached target is asked, what each of the two answers does, and the attachment commands.
- `docs/jobs/package-sync.md:76` — "apt collateral" loses the union: the protected set is the target's `apt-mark showmanual` alone, and the paragraph must say what that gives up (ruling 13).
- `docs/system/package-sync.md:63-68` — the `apt_sync` bullet list: item classes, what converges, and the new precondition-free Pro probe. Line 64's closing clause ("They stay in `_source_manual_set` regardless…") goes with the field.
- A short paragraph, wherever the review is described, stating the apt-config exception: `/etc/apt/apt.conf.d` is reviewed in all three directions with the full three-way decision, and why (ruling 11, §1).
- `docs/system/data-model.md:191` — the `DiffClass` enumeration comment.
- Module docstrings: `apt_sync.py:1-98` (the second-review paragraph at 87-94 and the key paragraphs at 44-85 both change), `packages/review.py:17-27` and `:48-55` (the three-way promise and the sentinel list), `packages/sync_core.py:1-29` (the "may review again" clause), `packages/items.py:59-70` (`DiffClass`), `packages/apt_policy.py:1-18` (two questions become three).

## 8. ADR

The decisions above are recorded in `docs/adr/adr-020-declarative-package-convergence.md`, which is `Status: Draft` and is edited in place as the model settles (`docs/adr/adr-001-adr.md:15` makes only *accepted* ADRs immutable). Where a decision here needs a citable identity, use the ADR's D-number:

| Decision | ADR-020 |
| --- | --- |
| Item model — only what the user can decide about is an item; mechanism is the job's own business | D-02 |
| Two-answer screens for repository removal, pin removal and the repository conflict | D-07 |
| The four `/etc/apt` buckets, the extension filter, and keys travelling byte-for-byte | D-11, D-12, D-13, D-14 |
| Exactly one review per job per run, before its first mutating command | D-24 |
| Diff taxonomy — `ORIGIN_MISMATCH`, `REPO_UNAVAILABLE`, and no pin echo | D-25 |
| Collateral protection on the target's manual set, and the class-3 rehearsal exclusion | D-30, D-40 |
| Origin replication and the four classes of §2.3 | D-34 |
| Origin enforcement against the target's post-refresh state | D-35 |
| Pins are mechanism and always sync (the epoch evidence) | D-36 |
| The review's scope and the `apt.conf.d` exception | D-37 |
| The distribution's own source files and the ESM/Pro attachment gate | D-38 |
| Derived-work failure attribution onto the packages that depended on it | D-39 |

## 9. Staged implementation plan

Every stage ends with `uv run ruff check . && uv run ruff format .`, `uv run basedpyright`, `uv run pytest` green, and its own tests meaningful (mutation-checked per §10).

**S0 — seams.** Extract `_scan_target_source_references` (`apt_sync.py:778`) into a machine-agnostic helper and call it for both machines; add the `/etc/apt/sources.list` digest capture; add the `sources.list.d` extension filter (§5.2); split `_REMOVAL_ACTIONS` from `_PROMOTABLE_ACTIONS` (`review.py:100,108`) with no behaviour change; add the optional `ReviewEntry.versions` field. Pure plumbing, no user-visible change, no test dies. Everything after this can be written against stable seams.

**S0b — collateral narrowing.** `_protected_manual_set` (`apt_sync.py:1628`) returns the target's set alone; `self._source_manual_set` and its assignment go; the two `TestSourceOnlyCollateral` tests and `test_excluded_package_still_counts_as_source_manual_for_collateral_protection` invert; `docs/jobs/package-sync.md:76` and `docs/system/package-sync.md:64` follow. Independent of everything else here — it may land at any point, and landing it early keeps it out of the lanes below.

**S1 — origin capture.** `candidate_origins_by_package` in `apt_policy.py`; `installed_origins_by_package` wired over the reused source policy output (`apt_sync.py:1117`); `_source_files_serving`; the distribution-origin set. Delivers: the plan holds every origin fact, nothing consumes them yet. Parser tests only.

**S2 — origin classification and the review label.** Replace `collect_unavailable_item_ids` (`apt_sync.py:1205`) and the `unavailable_item_ids` branch of `_diff_apt_packages` with §2.3's four outcomes; add `build_origin_detail`; delete `packages_with_no_candidate`. **This stage alone fixes the Firefox misclassification at plan time** — worth landing early even if later stages slip.

**S3 — origin enforcement.** The post-update batched verification and its `ConvergeItemFailed` (§2.4). Delivers: the wrong vendor's package can no longer be installed even if S4's derivation is wrong.

**S4 — derived `/etc/apt` writes.** Remove the INSTALL/CHANGE diff emission from `_diff_apt_sources` and `_diff_apt_pins` — `_diff_apt_configs` is untouched (ruling 11); build the write set in `accept_review`; the always-sync bucket including `/etc/apt/sources.list` and the distribution files; derived-write failure attribution onto packages (§3.2); delete `_require_keyrings_ready` and `_travelling_keyrings_detail`.

**S5 — two-answer removals.** The `REPO_REMOVAL_REVIEW_ACTION` sentinel carrying both repository and pin removals, the removal group builder, the four new `_ACTION_VOCABULARY` entries, and the proof that no `apt:source:` or `apt:pin:` id can ever reach a decision file — while `apt:config:` still can, in all three directions.

**S6 — the conflict prompt.** `_machine_specific_packages_by_source_file` generalised from `_source_removal_details` (`apt_sync.py:1408`), `REPO_CONFLICT_REVIEW_ACTION`, `_review_repo_conflict_group`, and the wiring that turns a skipped conflict into a named package failure. Depends on S4 (the write set) and S5 (the review plumbing).

**S7 — delete the pin echo and the second review.** The whole §6 deletion list for `HoldPinFact`, the pinned branch, and `_rereview_*`. Depends on S2 (the classification must already be origin-driven before the echo goes) and S3.

**S8a — honest job outcomes.** Depends on nothing; touches no apt logic; may land first. Audited this session: `JobStatus.SKIPPED` (`models.py:263`) is constructed nowhere, and several jobs already stop early or do nothing while the run records `SUCCESS` (`orchestrator.py:1230-1237`). This stage builds the mechanism the ESM gate needs and corrects those call sites in the same change, so `SKIPPED` means one thing everywhere.

The exception, beside `SyncAbortedByUser` (`models.py:131`):

```python
class JobSkipped(Exception):
    def __init__(self, job_name: str, reason: str) -> None: ...
```

The orchestrator gains an `except JobSkipped` arm in the job loop, beside the `PackageItemFailures` one (`orchestrator.py:1252`): record `JobResult(status=JobStatus.SKIPPED, ..., error_message=reason)`, log once at WARNING, and **do not re-raise**, so the loop moves to the next job exactly as the item-failure arm does. `_summarize_job_outcomes` (`orchestrator.py:202-216`) already treats `SKIPPED` as a non-failure, so a skipped job leaves the session `COMPLETED` and the exit code unchanged — no change needed there. `JobSkipped` may only be raised **before** the job's first mutating command; raised later, the partial state it left behind goes unreported.

Call sites that adopt it, each currently `SUCCESS`:

| Site | Condition | What it does today |
| --- | --- | --- |
| `PackageSyncJob.execute` (`packages/sync_core.py:493-497`) | the review came back non-interactive (`ReviewOutcome.was_interactive` False, `review.py:476-487`) **and** `plan.groups` is non-empty | every item is forced `SKIP_ONCE`, `apply()` logs "No … changes to apply" (`sync_core.py:357-359`) and returns; all four package jobs report SUCCESS having converged nothing. Raise `JobSkipped` after the review returns, before `after_review()` — so `manual_installs_sync` does not push its registry either. An **empty** plan on the same path stays SUCCESS: the target already matches. |
| `VSCodeStateSyncJob.execute` (`vscode_state_sync.py:298-302`) | no handled state DB exists on the source | logs "nothing to sync" and returns SUCCESS. Not applicable ≠ synced. |
| `FolderSyncJob.execute` (`folder_sync.py:874`) | `_active_folders()` (`folder_sync.py:256-266`) is empty — the schema requires `minItems: 1` (`folder_sync.py:201`) but every entry may be `enabled: false` | the loop body never runs and the job reports SUCCESS. |
| `Orchestrator._discover_and_validate_jobs` (`orchestrator.py:996-998`) | an enabled job name resolves to no `SyncJob` class (`_resolve_sync_job_class` returns None, `orchestrator.py:698-703`, `715-721`) | a WARNING is logged and the job produces **no `JobResult` at all** — worse than a wrong status. No exception is involved here: the orchestrator constructs the `SKIPPED` result directly, at discovery time, and appends it to the run's results. |

Deliberately **not** converted, so the boundary stays legible: a package job whose plan is empty (the target already matches — that is the goal, met); a `folder_sync` pass that transfers nothing because filters excluded everything (the mirror is correct); dry-run (`tests/unit/test_dry_run.py:6` states SUCCESS/FAILED-not-SKIPPED as an existing decision, and a rehearsal that completes did succeed); per-item exclusions inside an otherwise-working job (sideloaded snaps, `snap_sync.py:492-507`; `REPORT_ONLY` diffs, `sync_core.py:348-352`) — a job-level status cannot express those, and the review and the warnings already do; a job that raised `SyncAbortedByUser` (`orchestrator.py:1244-1251`, no result recorded) — the run stops there, so a per-job record is moot.

Note for scope: `JobResult` is currently read only by `_summarize_job_outcomes` and the CLI's exit code (`cli.py:387`) — nothing renders per-job outcomes, so `CORE-FR-SUMMARY` (`docs/system/core.md:540`, which names SUCCESS/SKIPPED/FAILED explicitly) is unimplemented. S8a makes the status honest; rendering it is a separate piece of work and is **not** in this stage.

**S8 — ESM and Pro. SHIPPED.** The attachment probe (`_target_pro_attached`), the two-answer gate in `plan()` (`_gate_esm_writes`) with its unbounded re-check, `review.ask_gate` and its `Reviewer` method. Depended on S4 — the always-sync write set is what the gate guards — and on S8a for `JobSkipped`. Its blocking VM check was **DONE**: measured in a stock `ubuntu:24.04` container, `apt-get update` exits 0 with the ESM sources and no credentials, one failing source never aborts the others, and the real failure is `apt-get install` exiting 100 on a 401 for the `.deb` (§5.3.1).

One deviation from §5.3.2, forced by today's code: the trigger's digests were captured by `_plan_repo_diffs`, which runs after `_plan_packages`, so the gate could not sit "immediately after `_capture_origin_state`" and still read them. The `sources.list.d` digest capture and the `/etc/apt/sources.list` file digest moved INTO `_capture_origin_state` — same commands, same count, earlier position — and `_plan_repo_diffs` now reads the cached values. Everything §5.3.2 gives as the reason for the placement holds unchanged.

The VM test covers the skip arm only (`tests/integration/jobs/test_package_sync.py`, `TestTheESMAttachmentGateOnVMs`), and that is a statement about the fixtures rather than a gap: neither VM can be attached to Ubuntu Pro without putting the user's subscription token in CI, so the "attach now" arm has no VM to prove itself on. What the VM does prove, and no mocked-executor test can, is that the skip costs the whole job: the source's `ubuntu-pro-esm-apps` pin is in the always-sync bucket and must not reach the target either.

**S9 — docs and scenario matrix.** ADR-020 and `docs/adr/_index.md` are already written; what is left is §7.4's documentation list and §7.3's matrix rows. Depends on everything; can be drafted alongside S4-S8 and finished last.

Parallelism. After S0 lands, two lanes touch disjoint regions of `apt_sync.py`:

- **Package lane** — S1, S2, S3, S7 — works in the diff region (`apt_sync.py:235-457`), the capture/query region (`1074-1267`) and the converge-install region (`2093-2166`).
- **Repository lane** — S4, S5, S6, S8 — works in the `/etc/apt` shapes (`460-546`), the repo capture and diff region (`616-930`, `1347-1626`) and the group convergence region (`2183-2550`).

They collide in exactly two functions, `plan()` (`1269-1314`) and `accept_review()` (`1782-1836`). Give those two one owner for the duration, or run the lanes sequentially. S9 runs alongside either lane, and so does S8a — it touches `models.py`, `orchestrator.py`, `packages/sync_core.py`, `folder_sync.py` and `vscode_state_sync.py`, none of which either lane edits.

## 10. Test plan

Every test below must be mutation-checked: break the named line, confirm the named assertion fails. A test that stays green under its mutation is vacuous and does not count (`02-HANDOVER-package-review.md:76`).

### 10.1 Origin parsing (`tests/unit/jobs/test_package_items.py` or a new `test_apt_policy.py`)

`test_candidate_origins_come_from_the_candidate_row_not_the_installed_one` — a policy block whose installed version (`***`) is from vendor A and whose candidate is from vendor B; assert the returned set is B's URI only. Mutation: make `candidate_origins_by_package` reuse the `***` row; the assertion flips to A.

`test_a_name_apt_printed_no_block_for_reaches_no_key` — mirrors `df48cd07`'s rule. Mutation: seed the key with an empty set; the `assert name not in result` fails.

`test_candidate_none_yields_an_empty_origin_set_not_a_missing_key` — the distinction class 3 and class 4 turn on. Mutation: return no key for `(none)`; the classification test in 10.2 then misroutes.

### 10.2 Classification (`tests/unit/jobs/test_apt_sync.py`)

`test_same_origin_install_derives_no_repository_write` — source origin and target candidate origin identical; assert `INSTALL` and an empty derived write set. Mutation: make `_source_files_serving` return every source file; the empty-set assertion fails.

`test_different_origin_install_derives_the_sources_own_repository` — the Firefox shape at unit level: source `firefox` from `https://packages.mozilla.org/apt`, target candidate `1:1snap1-0ubuntu5` from the Ubuntu archive. Assert the diff is `INSTALL`, the detail names `packages.mozilla.org/apt`, and the derived write set contains the Mozilla `.sources` file plus its keyring. Mutation: restore name-only matching (`collect_unavailable_item_ids`'s old logic); the derived-write assertion fails.

`test_unreplicable_origin_is_report_only_naming_the_origin` — source origin declared by no source file. Assert `REPO_UNAVAILABLE`/`REPORT_ONLY` and that no `apt-get install` command is issued. Mutation: fall back to `INSTALL`; the zero-install-commands assertion fails.

`test_a_dangling_keyring_makes_the_package_unavailable_not_the_repository_report_only` — replaces the two tests at 1872/1895. Mutation: keep the old source-level `REPORT_ONLY`; the package-level assertion fails.

`test_origin_detail_is_omitted_for_a_distribution_origin` and `test_origin_detail_strips_the_scheme_and_names_the_full_path` — ruling 9's naming. Mutation: emit the bare host; the `ppa.launchpadcontent.net/git-core/ppa/ubuntu` assertion fails.

`test_two_machines_on_different_ubuntu_mirrors_produce_no_origin_mismatch` — the suppression that keeps §2.6 usable. Mutation: drop the distribution-origin filter; every package reports mismatched.

`test_divergent_vendor_provenance_reports_origin_mismatch` — same name, same version, one from vendor A and one from vendor B. Mutation: drop the class; the diff count assertion fails.

### 10.3 Enforcement

`test_install_is_refused_when_the_post_update_candidate_is_from_the_wrong_origin` — after the group's `apt-get update`, the target's candidate still comes from the Ubuntu archive; assert `ConvergeItemFailed` naming both origins and **zero** `apt-get install` commands for that package. Mutation: skip the verification; an `apt-get install firefox` appears in the command log.

`test_the_origin_verification_costs_one_batched_policy_call` — regardless of how many installs are approved. Mutation: move the call inside the per-package loop; the call-count assertion fails.

`test_a_distribution_origin_package_is_not_origin_verified` — no extra refusal for a mirror difference. Mutation: verify unconditionally; the install is refused.

### 10.4 Derivation, ordering and the single review

`test_a_repository_never_appears_as_a_review_entry_in_the_add_or_change_direction` — the ruling-4 property, asserted across all three item classes. Mutation: re-emit the INSTALL diff; an entry appears.

`test_the_keyring_the_derived_repository_needs_lands_before_the_repository_file` — ordering fact, already covered in spirit by `test_key_then_source_then_update_then_package_install` (`tests/unit/jobs/test_apt_sync.py:2029`); rewrite it against the derived path. Mutation: reorder the write loop; the index comparison fails.

`test_a_package_the_target_had_no_candidate_for_is_installed_in_one_review` — the replacement for `TestNewRepositoryMakesAPackageAvailable` (`4174`). Assert exactly **one** call to the reviewer for the whole `execute()`. Mutation: reintroduce a second `reviewer.review(...)` call; the call-count assertion fails.

`test_a_failed_derived_repository_write_fails_the_package_that_needed_it` — and only that package. Mutation: swallow the derived-write failure; the install proceeds.

`test_pins_travel_without_a_review_line_and_land_before_the_sources` — the §2.4 requirement. Mutation: move pins after sources; the ordering assertion fails.

### 10.5 The two follow-ups

`test_repo_removal_is_unticked_and_never_offered_permanence` (in `tests/unit/jobs/test_package_review.py`) — assert the checkbox defaults to unchecked **and** that `_offer_permanent_skips` is never invoked for that group. Mutation: put `REPO_REMOVAL_REVIEW_ACTION` back into `_PROMOTABLE_ACTIONS`; the second assertion fails.

`test_no_repository_or_pin_id_can_reach_a_decision_file` — extends `test_a_signing_key_is_never_offered_and_so_can_never_be_recorded` (`tests/unit/jobs/test_block_state_decisions.py:242`). Mutation: allow the promotion; a `DecisionEntry` with an `apt:source:` id is written.

`test_a_pin_only_on_the_target_is_offered_for_removal_with_two_answers` — ruling 12's shape, and the pin group is distinct from the repository group even though both carry the sentinel. Mutation: put pin removals back in the ordinary checkbox path; the permanence assertion fails.

`test_an_apt_config_file_is_reviewed_in_all_three_directions_and_can_be_skipped_always` — ruling 11, asserted against `_diff_apt_configs`'s three branches plus a written `apt:config:` `DecisionEntry`. Mutation: move apt config into the always-sync bucket; the add and change lines vanish from the review.

`test_a_changed_repository_with_no_machine_specific_package_is_overwritten_silently` — zero prompts, one write. Mutation: prompt unconditionally; the prompt-count assertion fails.

`test_a_changed_repository_feeding_a_machine_specific_package_asks_and_shows_both_versions` — assert the entry carries `versions` with the target's content first and the source's second, and that only two choices are offered. Mutation: pass a unified diff instead; the content assertion fails.

`test_skipping_a_repository_conflict_fails_the_package_that_needed_it` — the coupling in §4.3. Mutation: proceed with the install anyway; a wrong-origin install lands.

`test_a_bracketed_filename_in_a_conflict_panel_renders_without_markup_error` — the standing Rich rule (`review.py:234`). Mutation: pass the content as a bare `str`; `MarkupError`.

### 10.6 The Firefox scenario, on VMs

`tests/integration/jobs/test_package_sync.py::TestAptOriginReplication::test_the_target_never_installs_a_different_vendors_package`.

Fixture work in `tests/integration/scripts/internal/vm-test-fixtures.sh` (bump `FIXTURES_VERSION`, line 31, and `PCSWITCHER_TEST_FIXTURES_VERSION` in `internal/common.sh`): the test needs **two repositories offering the same package name**, one of them present on the source only. Do not use Firefox itself — its Ubuntu package is a snap transition and its Mozilla repository is large. Build the divergence from a tiny package instead: create a local signed apt repository on the source machine (a one-package `.deb` built in the fixture, a generated GPG key, `apt-ftparchive` or a hand-written `Packages`/`Release`), serving a package whose **name already exists in the Ubuntu archive** and whose version is **lower** than the archive's. Add a `preferences.d` pin on the source giving that local origin priority 1000, exactly the Mozilla shape. The target gets neither the repository nor the pin.

The test then runs `apt_sync` with the package approved and asserts, on the target:

1. the repository file and its keyring exist under `/etc/apt`;
2. the pin file exists under `/etc/apt/preferences.d`;
3. `apt-cache policy <pkg>` reports the **installed** version's origin as the replicated repository's URI, not the Ubuntu archive's;
4. `dpkg-query --show` reports the source's version, which is the lower one — so the assertion cannot pass by accident through ordinary version resolution.

Mutation that must turn it RED: delete the derived pin write. Assertion 3 then reports the Ubuntu archive as the origin (the archive's higher version wins on equal priority), and assertion 4 reports the wrong version. A second mutation: restore name-only matching in the classification. Then assertion 1 fails — no repository is written at all.

A companion, cheaper case in the same class: `test_a_package_only_the_sources_repository_offers_is_installed_after_the_repository_lands` — the class-3 path, one review, no second screen.

### 10.7 ESM and Pro

`test_an_unattached_target_is_asked_about_before_anything_is_written` (unit, mocked `pro status --format json` output, fake `Reviewer`) — assert `ask_gate` is called exactly once, with a message naming both filenames and `pro attach`, and that no mutating command was issued before it. Mutation: write the files without asking; the call-count assertion fails.

`test_choosing_skip_raises_job_skipped_and_writes_nothing` — the stop answer. Assert `JobSkipped` names `apt_sync`, that zero commands with a `mutates=` marker were issued, and that no review group was ever presented. Mutation: return instead of raising; the orchestrator records SUCCESS and the exception assertion fails.

`test_the_orchestrator_records_a_skipped_job_and_runs_the_next_one` (in the orchestrator's own suite) — assert `JobResult.status is JobStatus.SKIPPED`, the session is `COMPLETED`, and the following job still executed. Mutation: re-raise `JobSkipped`; the next-job assertion fails. Belongs to **S8a**, not S8 — it exercises the arm, not the gate.

Three more S8a tests, one per converted call site beyond the gate. `test_a_non_interactive_package_review_skips_the_job_instead_of_applying_nothing` — a plan with groups, a reviewer that returns `was_interactive=False`; assert `JobSkipped` and that `after_review()` never ran. Mutation: keep today's behaviour; the exception assertion fails. Paired with `test_an_empty_plan_is_still_a_success` on the same path, whose mutation (raise for the empty plan too) makes it fail. `test_a_job_with_no_active_folders_is_skipped` and `test_a_source_with_no_editor_state_dbs_is_skipped` — same shape against `folder_sync` and `vscode_state_sync`. `test_an_unresolvable_enabled_job_is_recorded_skipped` — an enabled `sync_jobs` name with no matching class; assert a `SKIPPED` `JobResult` exists for it (today there is none at all) and the run still completes.

`test_attach_now_re_probes_and_continues_when_the_target_became_attached` — the proceed answer with a probe that flips to attached; assert exactly two probes and that both ESM files are in the write set. Mutation: trust the answer without re-probing; the probe-count assertion fails.

`test_attach_now_can_be_answered_any_number_of_times` — a fake `Reviewer` scripted with N "attach now" answers (N well above any plausible bound, say 10) then the skip answer, against a probe that always reports unattached; assert all N answers were consumed, N+1 probes ran, and only the final skip answer produced `JobSkipped`. Mutation: reintroduce a bound; the answer-count assertion fails.

`test_esm_sources_are_written_to_an_attached_target` — attached on the first probe; assert no prompt and no warning. Mutation: invert the probe; the prompt-count assertion fails.

`test_an_unreadable_pro_probe_is_treated_as_unattached` — a missing binary, a non-zero exit and unparseable output, parametrised. Mutation: default to attached; the prompt assertion fails.

`test_a_non_interactive_run_skips_the_whole_job` — `ask_gate` returns None; assert `JobSkipped` names `apt_sync`, that its reason names both filenames and the absent TTY, that zero commands with a `mutates=` marker were issued, and that no review group was presented. Mutation: withhold the two files and continue instead of raising; the exception assertion fails.

`test_the_probe_payload_is_never_logged` — a probe payload carrying an `account` block; assert no log record and no prompt message contains any of its values. Mutation: log the raw stdout; the assertion fails.

`test_a_dry_run_never_prompts_about_attachment` — assert zero `ask_gate` calls and one WARNING. Mutation: prompt in dry-run; the call-count assertion fails.

`test_ubuntu_sources_is_never_offered_for_removal` and `test_a_save_file_in_sources_list_d_is_never_captured` (§5.2) — both assert on the diff set. Mutations: drop the never-removed guard; drop the extension filter.

### 10.8 The collateral narrowing (ruling 13)

`test_a_package_manual_only_on_the_source_is_not_protected_from_collateral` — the inverted `TestSourceOnlyCollateral` (`tests/unit/jobs/test_apt_sync.py:2868`). Assert no `apt:collateral:` diff and no apply-time refusal. Mutation: restore the union in `_protected_manual_set`; a collateral item appears.

`test_a_package_manual_on_the_target_is_still_protected` — the half that must NOT move, at both guards. Mutation: narrow to the empty set; the collateral item disappears.

## 11. Residual risks and unverified reasoning

Nothing here is an open question — every decision is made. What follows is what this design rests on that has not been measured, and what would settle each one.

1. **ESM on an unattached target (§5.3) — settled by measurement, no longer a hypothesis.** The old premise (an unattached target's `apt-get update` exits non-zero on `esm.ubuntu.com` and rolls the transactional `/etc/apt` group back) is **refuted**: the refresh exits 0, and a refresh that does fail still writes every other list. What is left unmeasured is the blast radius: the test container had 0 of 13 upgradable packages with an ESM candidate, and that a real desktop with a large `universe` set has many more ESM candidates — and so more installs that would 401 — is **inferred from the priority ordering, not measured**. It would be settled by running `apt-cache policy` over a real desktop's manual set with the ESM sources present and unattached, and counting the candidates at priority 500 from `esm.ubuntu.com`. The gate does not depend on the count: one failing install is already a failure the user cannot trace back to the sync.

   The non-interactive path is no longer derived: the user has ruled that it skips the whole job (§5.3.4 rule 3), and that the interactive re-check loop is unbounded (rule 4). What remains derived there is one clause of the dry-run WARNING (rule 2), which is a wording consequence of rule 3, not a behaviour choice.

2. **The apt-config decision arity is derived, not stated (§1, screen 4).** Ruling 11 says apt config is reviewed in all three directions; it does not say with how many answers. This spec gives it the ordinary three-way decision and the registry, on the reasoning that the two no-registry rulings were both justified by consequences an apt-config file does not have (changing where packages come from, remediable by consolidating files), and that D-07's three-way is the default a departure needs a reason for. If that is wrong, apt config joins screen 2's shape and `_PROMOTABLE_ACTIONS` loses nothing — the change is small, but it is a change.

3. **The origin-verification cost at scale.** §2.4 adds one batched `apt-cache policy` over the approved install names after `apt-get update`. Measured cost: none — no run of this design exists yet. The batching shape is the same one `collect_unavailable_item_ids` (`apt_sync.py:1205`) already uses over a comparable name set, so the expectation is one command of similar cost, not a regression; that is inference, not measurement.

4. **`sha256sum` on a missing `/etc/apt/sources.list` (§5.1).** The spec treats a failing `sha256sum` as "no digest". The exact stdout/exit behaviour on an absent path is assumed from the tool's documented contract, not observed on a target this session; the parser must tolerate empty stdout with a non-zero exit either way.

5. **What ruling 13 gives up is unmeasured.** Nobody has counted how often a package is manual on the source and auto on the target — the case §4.4 knowingly abandons. The decision does not depend on the count (the rationale is about who owns the package, not how common the case is), but the frequency is unknown, so "rare" is not a claim this document makes.
