# Phase 02 — SPEC: repo-and-key derivation for snap and flatpak

Answers one request: *"snap and flatpak must have similar repo&key-follow-package automation so user doesn't need to bother with repos&keys for them either (in so far possible)."*

Every factual claim about snap or flatpak behaviour below is tagged **measured** (observed this session, on this machine or in a stock `ubuntu:24.04` container) or **inferred** (reasoned, not observed). Code claims cite `path:line`.

## 0. Settled premises, carried from apt

These were decided for apt this session (ADR-021, commits `c5f34462`, `33b3da33`, `c253355a`) and carry over unless this document says otherwise:

- A package replicates as (name, origin), not name alone.
- The review lists packages only. A line names its origin.
- Repository adds are **derived** from the packages approved from them, never tickable. "Package ticked, its source unticked" must be unrepresentable.
- Keys are derived too and travel byte-for-byte from the source; never re-fetched from a vendor (D-12).
- Removal stays reviewed, with exactly two answers (remove / skip once), recorded nowhere.
- The distribution's own sources are written and updated but never removed.
- No backwards compatibility anywhere.

## 1. Verdict, per ecosystem

### 1.1 flatpak — symmetry is achievable, and half of it already exists

flatpak already treats origin as first-class, and `flatpak_sync` already uses it. What is missing is the *derivation*: remotes are still tickable review lines.

Already correct, do not disturb:

- A ref's origin is recoverable from the installed ref. `flatpak list --columns=origin` prints it per ref (**measured**: every ref on this machine reports `flathub`), and `flatpak info --show-origin <app>` prints it for one ref in 11 ms with no network (**measured**). `flatpak_sync` captures it into `FlatpakItem.origin` (`src/pcswitcher/jobs/flatpak_sync.py:177`) via `_FLATPAK_LIST_CMD` (`flatpak_sync.py:102`).
- The install command already names the remote positionally (`flatpak_sync.py:1052-1055`), so the ref is installed *from that remote* or not at all.
- The review line already names the origin: `FlatpakItem.label()` (`flatpak_sync.py:187-189`) renders `app (version, origin, scope)`. ADR-021's "each line names its origin" is satisfied with no change.
- Keys already travel byte-for-byte and are staged, imported and discarded (#215, `flatpak_sync.py:1097-1150`). This is verified working and must not regress.

### 1.1.0 DISPROVEN: "flatpak refuses to guess, so no origin verification is needed"

This document originally claimed that the silent wrong-vendor install motivating ADR-021 D-34 was *structurally unreachable* for flatpak, on the grounds that `flatpak install` refuses to choose between remotes. **That claim is false**, and everything derived from it — in particular §2.5's "flatpak chooses nothing, so there is no candidate to re-read" — is wrong. Replaced by the following, all **measured** in a stock `ubuntu:24.04` container on Flatpak 1.14.6 against the real Flathub and Flathub-beta:

1. Flatpak refuses to guess **only when two or more registered remotes offer the ref**. With exactly one registered remote offering it, it resolves silently at exit 0, in both scopes. `--noninteractive`, bare and `--assumeyes` behave identically; `--assumeyes` does not auto-pick a remote.
2. **The wrong-vendor install is live, and silent.** A target remote with the same NAME and a different URL installs the other vendor's binary: remote `flathub` pointed at `https://dl.flathub.org/beta-repo/`, then `flatpak install --assumeyes flathub app/org.mozilla.firefox/x86_64/stable` → **exit 0**, `Version: 148.0`, `Collection: org.flathub.Beta`, against real Flathub's `153.0` / `org.flathub.Stable`. Different commit, different binary, no warning. `flatpak list --columns=origin` reports `flathub` in BOTH cases, so a name-only origin check cannot see it.
3. **The availability bug was live too.** `FlatpakItem` carried no branch, and Flathub-beta carries `stable` and `beta` for `org.mozilla.firefox`, so `flatpak install --assumeyes flathub-beta org.mozilla.firefox` exits 1 with `Multiple branches available for org.mozilla.firefox` — every run, for every app from a multi-branch remote. Naming the full ref (`org.mozilla.firefox/x86_64/beta`, exactly what `flatpak list --columns=ref` prints) fixes it: exit 0, correct branch.
4. **A fully-qualified ref does not pin the origin.** It carries arch and branch, never a remote. Origin can only be pinned by remote name — and, per (2), a remote name is not enough on its own.
5. Positional remote qualification works, and fails cleanly when that remote lacks the ref (`error: Nothing matches ... in remote ...`, exit 1). A remote name the target does not have fails loudly too (`error: No remote refs found for 'flathub'`, exit 1).
6. `flatpak info` prints `Collection:` (`org.flathub.Stable` vs `org.flathub.Beta`) and `Commit:`, which DO distinguish the two repositories. Neither is in the columns `flatpak_sync` queries; the shipped check compares the remote's **URL** instead, because the URL is already captured on `FlatpakRemoteItem` and is the thing that actually differs.

Shipped (commits on `gsd/phase-02-package-management-sync`): branch folded into `FlatpakItem.item_id` and the full ref used by both `flatpak install` and `flatpak uninstall`; `_origin_refusal` comparing the target's re-read remote URL and verification state; `_installed_origin_refusal` reading the landed origin back after the install; and the derivation itself.

Three real gaps remain, and all three are in scope:

1. **Remotes are review lines.** `_install_remote_diff` (`flatpak_sync.py:525`), `_change_remote_diff` (`flatpak_sync.py:611`) and `_diff_flatpak_remotes` (`flatpak_sync.py:657`) put every remote add and every URL/trust change in front of the user as a tickable entry. That is exactly the unrepresentable pairing ADR-021 deleted for apt: a remote ticked without its refs does nothing, a ref ticked without its remote is refused at converge (`flatpak_sync.py:1045-1051`).
2. **Origin is not part of ref identity.** `FlatpakItem.item_id` is `flatpak:ref:<scope>:<application>` (`flatpak_sync.py:185`). A ref installed on both machines from *different* remotes therefore produces no diff at all when the versions match, and a bare `VERSION_MISMATCH` that says nothing about provenance when they differ (`_version_mismatch_ref_diff`, `flatpak_sync.py:479`). This is the flatpak `ORIGIN_MISMATCH`.
3. **The install is issued by app id, not by ref.** `flatpak_sync.py:1052-1055` interpolates the application id alone. **Measured**: `flatpak install --assumeyes --noninteractive flathub-beta org.mozilla.firefox` — a remote carrying two branches of one id — prints `Similar refs found for "org.mozilla.firefox" in remote "flathub-beta"` with a stable/beta menu and aborts. The full ref is available from the same listing command (`flatpak list --columns=ref`, **measured**) and is not captured today. This is the same class of defect as name-only matching: the identity is not precise enough to name one thing.

### 1.2 snap — there is no repository or key decision to derive

The honest answer is that the user's request does not apply to snap, and forcing symmetry would invent a screen with nothing behind it.

- **One store, and the device is on the generic one.** `snap known model` reports `brand-id: generic`, `model: generic-classic`, `authority-id: generic` (**measured**). `snap known store` returns nothing — no store assertion exists on this device (**measured**). A brand store is selected by a `store` assertion referenced from the model assertion, both set at image-build time; moving a running device to another store is `snap remodel` against a brand-signed model assertion (**inferred** from `snap remodel`'s existence and the assertion chain; not exercised).
- **Name→publisher is cryptographically pinned store-side.** `snap known snap-declaration series=16 snap-name=chromium` returns `authority-id: canonical`, `snap-id: XKEcBqPM06H1Z7zGOdG5fbICuf8NWK5R` (**measured**), and `snap info firefox` reports `publisher: Mozilla**`, `snap-id: 3wdHCAVyZEmYsCMFDE9qt92UV8rC8Wdk` (**measured**). One name resolves to one snap-id resolves to one publisher, enforced by an assertion snapd validates itself. There is no second `firefox` for the target to install by accident. The (name, origin) treatment buys nothing because name *is* origin-determining.
- **Keys are snapd's, not the user's.** `snap known account-key` lists 4 keys, all store-side (**measured**). There is no per-repository key material a user configures, so there is nothing to copy byte-for-byte. `snap ack` exists for adding assertions, but every assertion it accepts must already chain to a key snapd trusts.
- **No proxy configured here.** `snap get system proxy` and `snap get system store` both return `error: snap "core" has no ... configuration option` (**measured**), i.e. no Snap Store Proxy is pointed at.

So snap's answer to "spare the user repo and key decisions" is: there were never any, and the two mechanisms that could make two machines draw from different stores — a brand store in the model assertion, and `snap set system proxy.store=<id>` — are device-provisioning facts, not per-snap facts. Neither is replicable: a remodel needs a brand-signed assertion pc-switcher cannot produce, and a proxy store id is a site configuration with its own CA and auth.

What snap *should* gain instead is one honest **detection**: if the two machines' provenance is not the same, say so once and refuse to pretend. See §4.

### 1.3 Sideloaded snaps belong to `manual_installs_sync`

`snap_sync` drops a snap whose revision is `x<N>` from the diff input and warns once (`snap_sync.py:230-247`, `snap_sync.py:492-507`). That is precisely the bare-`.deb` shape: `apt_sync` drops such packages at capture and `manual_installs_sync` owns them (`docs/system/package-sync.md:61`, `manual_installs_sync.py:1-20`). Yes, it should move, for the same reason: a snippet *can* reproduce a sideloaded snap (`snap install --dangerous <file>`), which converts a permanent per-run warning into a resolvable `UNREPRODUCIBLE` item with the three-way resolution D-21 already provides.

It is however orthogonal to the repos-and-keys request and touches a different job. Treated as its own stage (S6) and its own open question (§9 Q4).

## 2. Design — flatpak

### 2.1 What becomes derived

A remote travels because a ref approved this run comes from it. Nothing else makes a remote travel: a source remote feeding no ref this run syncs stays where it is, exactly as ADR-021 rules for an apt repository.

Two sources feed the derived set, both computed on the source, both local and network-free:

- **Direct.** For each approved `FLATPAK_REF` INSTALL, the source ref's own `origin` (already held in `_source_refs_by_id`, `flatpak_sync.py:795`), paired with the ref's scope.
- **Runtime completion.** An app install auto-pulls its runtime and the runtime's related refs, and flatpak resolves those from whatever remotes are configured — so deriving from apps alone can leave the install unable to resolve its runtime. **Measured** in the container: installing `io.github.fragglet.sdl_sopwith` from `flathub` pulled `org.freedesktop.Platform/25.08` plus five related refs, every one recording `origin=flathub`. On this machine every installed ref, app and runtime alike, reports `flathub` (**measured**) — so the hole is rare, but it is real whenever an app on remote X is built against a runtime the source holds from remote Y (**inferred**; not constructed). Closing it costs two cheap reads: `flatpak info --show-runtime <app>` on the source is local and instant (**measured**, 11 ms), and a second source-side `flatpak list` *without* `--app` gives every installed ref's origin and scope (**measured**), so the runtime's own remote is a dictionary lookup.

Derivation runs in `accept_review()`, not in `plan()` — same placement as apt's `_build_derived_writes` (`apt_sync.py:2287`), and for the same reason: the input is the set of *approved* items, which does not exist until the review returns. `plan()` gains only the two extra source reads (full ref listing, per-app runtime) so the data is in hand.

Signing keys are derived with their remote and at the same moment. `_stage_source_key`/`_discard_staged_key` (`flatpak_sync.py:1097-1150`) are unchanged; only their caller moves from "converge a `FLATPAK_REMOTE` diff" to "converge a derived remote write". The `--gpg-import` bytes still come from the source's own `<installation>/repo/<name>.trustedkeys.gpg` and are never re-fetched. **Measured**: `flatpak remote-add --if-not-exists --gpg-import=<file> <name> <plain url>` exits 0 with no `.flatpakrepo` involved, so the derived path needs no vendor round trip.

Exact shape:

```python
@dataclass(frozen=True)
class _DerivedRemote:
    """One remote this run must provision because an approved ref needs it."""
    remote_id: str                  # flatpak:remote:<scope>:<name>
    scope: Literal["user", "system"]
    name: str
    reason: Literal["ref_origin", "runtime_origin"]

def _derive_remotes(
    approved_ref_ids: frozenset[str],
    source_refs_by_id: Mapping[str, FlatpakItem],
    source_all_refs_by_scope_ref: Mapping[tuple[str, str], str],   # (scope, full ref) -> origin
    source_runtime_by_ref_id: Mapping[str, str],                   # ref item_id -> full runtime ref
) -> tuple[_DerivedRemote, ...]: ...
```

`FlatpakSyncJob` gains `self._derived_remotes: tuple[_DerivedRemote, ...]` and `self._ref_derived_remote_ids: dict[str, frozenset[str]]` (the D-39 attribution map: which remote provisioning each approved ref depended on), both set in `accept_review()`, both consumed in `apply()`.

### 2.2 What stays reviewed

| Direction | Item | Answers | Recordable |
| --- | --- | --- | --- |
| Ref install / removal | `FLATPAK_REF` | three-way (apply / skip once / skip always) | yes |
| Ref origin divergence | `FLATPAK_REF`, `ORIGIN_MISMATCH` | report only | n/a |
| Mask add / remove | `FLATPAK_MASK` | three-way | yes |
| Remote add / URL or trust change | — | **not reviewed at all** (derived) | no |
| Remote removal | `FLATPAK_REMOTE` | **two** (delete / skip once) | **no** |
| Remote change that re-points target-only refs | `flatpak:conflict:` | **NOT SHIPPED** — the change is derived and silent; see §9 Q3 | n/a |

Masks keep the three-way decision. A mask is the flatpak analogue of an apt hold — a standing user preference about updating, not mechanism serving a ref — so ADR-021's `apt.conf.d` reasoning applies: nothing about an approved ref implies whether a mask should travel, so the only honest source of the answer is the user.

Remote removal drops from three answers to two, reusing the existing `REPO_REMOVAL_REVIEW_ACTION` sentinel (`packages/review.py:120`) and the `_REMOVAL_ACTIONS`/`_PROMOTABLE_ACTIONS` split already in place (`review.py:126,140`). Rationale carries verbatim from ADR-021's D-07 exception: a permanent machine-local mark on a remote whose whole purpose is to feed refs would silently and permanently change where those refs come from, and the user's remedy is consolidating the two configurations, not recording a preference. `build_orphaned_refs_detail` (`flatpak_sync.py:264`) and the dependent-naming behaviour (#214) are unchanged — they are what makes the two-way answer informed.

The change direction becomes the apt conflict prompt (`c253355a`'s shape). A remote present on both sides with a differing URL, `gpg_verify` or `key_digest` is overwritten **silently** as derived mechanism, *unless* the target's own version of that remote is the origin of target refs that are not being removed this run — in which case re-pointing it changes where those refs update from. Then, and only then, it becomes a two-answer `flatpak:conflict:<scope>:<name>` entry showing both URLs / both key digests, answered overwrite or skip once, recorded nowhere. `_remote_change_detail` (`flatpak_sync.py:556`) is reused verbatim as the conflict body; `_target_refs_by_origin_remote` (`flatpak_sync.py:642`) already computes the dependent set.

Everything above is one review, before the job's first mutating command. ADR-021 retired D-24's "may review again" clause for apt; the same holds here for the same reason — nothing this run does changes the *source's* origins, and the classification depends only on those.

### 2.3 Origin as ref identity, and the `ORIGIN_MISMATCH` diff

Origin does **not** go into `item_id`. Putting it there would turn "same app, different remote" into an install plus a removal, and **measured**: `flatpak install <other remote> <ref>` on a ref already present exits with `error: <ref> is already installed from remote flathub` — so the install half could never run, and the removal half would propose deleting the app the user has. It is a divergence to report, not to converge.

**BRANCH goes the other way, and does live in `item_id`** (shipped; supersedes §2.4's "field, not identity"). The argument above does not carry over: two branches of one application id coexist in one installation — that is what branches are for — so the install half of a branch change is never refused and the pair converges to exactly the source's set. Two further reasons, both decisive on their own: `(scope, application)` is not a unique key for a machine's own listing, so folding the captured items into a `{item_id: item}` map silently dropped one of two rows; and both `flatpak install` and `flatpak uninstall` need the full ref anyway (§1.1.0 item 3), so identity and command argument are one string rather than two facts that can drift.

Instead, mirroring `_is_origin_mismatch`/`_diff_apt_packages` (`apt_sync.py:575,682`) exactly: when a ref is present on both machines in the same scope and the two origins differ, emit

```python
ItemDiff(
    item_class=ItemClass.FLATPAK_REF,
    diff_class=DiffClass.ORIGIN_MISMATCH,
    action=DiffAction.REPORT_ONLY,
    item_id=item_id,
    label=target_item.label(),
    detail=build_flatpak_origin_mismatch_detail(source_item.origin, target_item.origin),
)
```

and it takes precedence over the version-mismatch branch, exactly as apt's does. There is no flatpak distribution-origin exemption to apply (§2.6), so the comparison is a plain inequality of two remote names within one scope.

`build_flatpak_origin_mismatch_detail(source_origin: str, target_origin: str) -> str` lives in `flatpak_sync.py` beside `build_orphaned_refs_detail`, per `packages/items.py:11-16`'s rule that a detail only one job writes stays in that job.

### 2.4 Install by ref, not by app id

`FlatpakItem` gains `ref: str` (the `flatpak list --columns=ref` value, e.g. `org.mozilla.firefox/x86_64/stable`), captured by widening `_FLATPAK_LIST_CMD` to `application,version,origin,installation,ref` and widening `_parse_flatpak_list`'s field count from 4 to 5. **It is identity, not merely a field** — see §2.3's branch paragraph for why this reverses the original ruling; `item_id` is `flatpak:ref:<scope>:<application>/<arch>/<branch>`.

`_converge_ref`'s INSTALL command (`flatpak_sync.py:1052-1055`) interpolates `source_item.ref` in place of the bare application id. `flatpak install <remote> <full ref>` is unambiguous by construction. `_converge_ref`'s REMOVE uses the full ref too: the original "unambiguous within one installation" reasoning was **inferred and is wrong** — `flatpak uninstall <app>` is ambiguous exactly when two branches of that id are installed, which is the same condition §1.1.0 item 3 measures on the install side. Measured: `flatpak uninstall --assumeyes --user <id>/<arch>/<branch>` parses the full ref (`error: No installed refs found for ... with arch ... with branch ...` for an absent one).

### 2.5 Ordering, failure attribution, and the enforcement point

`plan()` (`flatpak_sync.py:881-934`) stops ordering remotes ahead of refs, because there are no longer remote diffs to order: `diffs` becomes `(*ref_diffs, *mask_diffs)`. The ordering guarantee moves into `apply()`, which converges the derived remote writes **before** the first ref install, then refs, then masks — the same relocation apt made when `/etc/apt` writes stopped being diffs (`apt_sync.py:2461`).

Failure attribution follows D-39. A derived remote-add that fails has no item of its own, so it fails **every approved ref whose `_ref_derived_remote_ids` names it**, quoting the remote, the scope and flatpak's own stderr. A conflict answered "skip once" does the same to the refs that depended on that remote's new URL. `_remote_ready_on_target` (`flatpak_sync.py:1162-1170`) survives as the last-line guard but its message changes: it no longer says "not among this run's successfully-added remotes", it says the remote this ref needs could not be provisioned.

Does the apt "verify the origin against the target's real state before the first install" guarantee (D-35) have a flatpak analogue, and is it needed? **A strong one, and yes** — the "flatpak chooses nothing" reasoning this paragraph originally rested on is disproven (§1.1.0 item 2): naming the remote pins a NAME, and a same-named remote pointing elsewhere serves another vendor's build at exit 0.

As shipped, the check is per ref rather than the batched per-scope read sketched below, because it has to answer a question about one item: `_origin_refusal` re-reads the target's remotes (cached per run, discarded on every remote write) and requires the ref's origin remote to carry the source remote's URL and verification setting, and `_installed_origin_refusal` re-reads `flatpak list` after the install and resolves the landed origin to a URL again. `flatpak info --show-origin` is deliberately not used for the read-back: it exits 1 both for a ref that is not installed and for an installation that cannot be opened, and ADR-022 D-03 forbids an ambiguous discriminator; the listing answers "not installed" as an absent row at exit 0.

That also subsumes the sketch below, whose case **D** it covers:

```python
async def _verify_derived_remotes(self) -> dict[str, str]:
    """`{remote_id: refusal reason}` for derived remotes the target does not report with
    the source's URL after the derived writes. One `flatpak remotes --columns=name,url`
    per scope, cached; runs after the last derived write and before the first ref install.
    """
```

A remote that fails this check fails its dependent refs through the same D-39 attribution, without any ref install being attempted. This is cheap (two commands) and closes the case measured as **D**: `flatpak remote-add --if-not-exists <name> <different url>` exits **0** and leaves the old URL in place — a silent no-op that a naive "exit code 0 means it worked" reading would accept.

### 2.6 The distribution-never-removed analogue: there is none

apt's D-38 bucket exists because `ubuntu.sources` defines the origins every unremarkable package comes from, and a machine without it has a broken apt. flatpak has no counterpart. Flathub is not shipped or blessed by Ubuntu; `flatpak` installs with **zero** remotes configured (**measured**: a fresh `ubuntu:24.04` container after `apt-get install flatpak` lists no remotes until one is added), and a machine with no remotes is a perfectly valid flatpak machine with no refs.

So: no always-synced remote bucket, no never-removed set, and no ESM-style attachment gate. Flathub travels if and only if a ref approved this run comes from it — which on any real desktop is every run, but as a consequence rather than as a rule.

### 2.7 Scope does not change the derivation

Scope is already identity for refs, remotes and masks (`flatpak_sync.py:185,230,255`), and remotes are per-installation even when the URL is byte-identical (**measured**: `flathub` appears twice in `flatpak remotes --columns=name,url,options` on this machine, once `system` and once `user`, same URL). Derivation therefore keys on `(scope, name)` throughout: a user-scope ref derives the user-scope remote and never the system-scope one, and the `sudo` prefix stays a pure function of the derived item's own scope (`_sudo_prefix`, `flatpak_sync.py:288`). Nothing about derivation changes `validate()`'s system-scope sudo gate (`flatpak_sync.py:1172-1189`) except that `_system_scope_in_play` no longer needs to be true merely because a system remote exists that no ref uses — it may keep that broad test; narrowing it is not worth a behaviour change.

### 2.8 Known limit: remote filters do not travel

`_FLATPAK_REMOTES_CMD_TEMPLATE` (`flatpak_sync.py:112`) reads `name,url,options` only. **Measured**: `flatpak remote-modify --filter=/tmp/f.filter flathub-beta` puts the *local path* `/tmp/f.filter` in the `filter` column and adds a `filtered` token to `options`; the filter's content lives at that path, outside the ostree store. `collection`, `subset` and `priority` are likewise uncaptured (**measured**: all `-` / `1` on this machine and in the container, so no divergence exists here today).

Two consequences. First, the `filtered` token is harmless to the existing parse — `_parse_flatpak_remotes` (`flatpak_sync.py:400-435`) tests membership of `no-gpg-verify` in `options.split(",")`, and `filtered` is a distinct token. Second, a filtered source remote replicates as an **unfiltered** remote on the target, silently. That is out of scope for this work (the filter file is arbitrary user content at an arbitrary path, not repo-and-key material), but it must not be claimed as replicated: `flatpak_sync` should log one WARNING per derived remote whose source `options` carries `filtered`, naming the remote and the path. One line, no item.

## 3. Design — snap: nothing derived, one thing detected

No derivation, no new item class, no new review line. Snap's whole answer to the request is §1.2: there is no repository or key for the user to be bothered by.

The one thing worth adding is a validation-time provenance check, in the spirit of ADR-021's refusal to replicate a name when the provenance would invert — and placed in `validate()` per the standing rule that environment assumptions are checked with copy-paste remediation, never mid-execute:

```python
async def _store_identity(self, executor: Executor) -> tuple[str, str, str | None]:
    """`(brand_id, model, proxy_store_id)` for one machine.

    `snap known model` gives brand-id and model (measured: `generic`/`generic-classic` on
    a stock Ubuntu 24.04 desktop). `snap get system proxy.store` gives the Snap Store Proxy
    id, or `None` — measured, an unset one exits non-zero with
    `snap "core" has no "proxy" configuration option`, which is not an error condition.
    """
```

`validate()` compares the two machines' tuples and appends a `ValidationError` when they differ, stating that the two machines draw snaps from different stores, that pc-switcher cannot converge that (a remodel needs a brand-signed model assertion; a proxy store id is site configuration), and that `snap_sync` should be disabled or the machines re-provisioned to match. This is a **blocking** validation error rather than a warning, because a name that resolves to different bytes on the two machines is the exact failure ADR-021 exists to prevent, and unlike apt there is no per-item origin to fall back on.

On a pair of ordinary Ubuntu desktops this check is always satisfied and costs two commands. It is the entire "in so far possible" for snap.

Everything else snap already reviews is unchanged and is *not* the analogue of repos and keys: channel and revision converge deliberately (ADR-020 D-06, `snap_sync.py:272-289`, `snap_sync.py:581-622`) because snap embeds the revision in `~/snap/<app>/<rev>`, and per-snap holds are a standing user preference like apt holds (`snap_sync.py:292-329`).

Could a snap's provenance differ between machines in a Firefox-like way, with the store constant? **No** (**measured**, §1.2): one name, one snap-id, one publisher, enforced by a canonical-signed `snap-declaration`. The only remaining provenance variable is which *revision* of that one snap is installed, and D-06 already converges it exactly.

## 4. The four-jobs rule is unaffected

ADR-020 D-15/D-16, carried forward by ADR-021, keeps `apt_sync`, `snap_sync`, `flatpak_sync` and `manual_installs_sync` as four independent `SyncJob`s. Nothing here changes that: flatpak derivation is entirely internal to `flatpak_sync`, and the snap check is entirely internal to `snap_sync`. Neither job imports the other, and neither consults the other's result. The shared plumbing they touch (`packages/review.py`'s sentinels, `packages/items.py`'s `DiffClass.ORIGIN_MISMATCH`, `packages/sync_core.py`'s `_ACTION_VOCABULARY`) is already shared and already carries what apt needed; flatpak reuses it rather than extending it, with one exception (§7 S3's vocabulary entries).

S6 (sideloaded snaps → `manual_installs_sync`) moves a *responsibility* between two jobs. That is the same kind of move D-18 already made for bare-`.deb` packages and does not create a dependency: `manual_installs_sync` will run its own `snap list --all`, exactly as it runs its own `dpkg`/`apt-cache` rather than sharing `apt_sync`'s (`manual_installs_sync.py:16-19`).

## 5. Blast radius

### 5.1 Unit tests that change or die

`tests/unit/jobs/test_flatpak_sync.py`:

| Line | Test / class | Fate |
| --- | --- | --- |
| 118 | `TestCapture` | rewrite — `_FLATPAK_LIST_CMD` gains a fifth column, every fixture line gains a ref field |
| 173 | `TestPlanDiff` | rewrite — remote install/change diffs no longer exist; the ref half survives |
| 220 | `test_flathub_present_in_both_scopes_yields_two_remote_items` | rewrite as two *derived* remotes, one per scope |
| 237 | `test_every_remote_diff_precedes_every_ref_diff` | die — there are no remote diffs. Replaced by S4's derived-write-before-install ordering test |
| 250 | `TestRemoteUrlChange` (incl. 314) | rewrite — silent derived overwrite in the no-dependents case, two-answer conflict otherwise. `test_converge_uses_remote_modify_with_source_url_and_scope_flag` survives as a derived-write assertion |
| 375 | `TestRemoteTrustCapture` (8) | keep unchanged — capture is untouched |
| 487 | `TestRemoteTrustDiff` (4) | rewrite — trust divergence drives a derived write, not a `CHANGE` diff |
| 560 | `TestRemoteTrustConverge` (7, incl. 603/657/678/692/716/758/779) | keep the assertions, rewrite the driver: they assert the command and the staging, which are unchanged; what changes is that a derived remote rather than an approved diff triggers them |
| 816 | `TestPlanReadOnly` | keep; extend to cover the two new source reads (full ref list, `--show-runtime`) |
| 831 | `TestConverge` (833, 896) | rewrite — `test_remotes_converge_before_refs_that_depend_on_them` becomes a derived-write ordering test; `test_ref_with_missing_origin_remote_is_skipped_with_named_failure` keeps its shape with the new message |
| 1174 | `test_ref_and_remote_groups_keep_their_own_verbs_and_exclude_masks` | rewrite — no remote group in the add direction |
| 1192 | `TestRemoteRemovalOrphansRefs` (5, incl. 1251/1267/1309) | keep; extend with the two-answer assertion and the no-decision-file assertion |
| 1504 | `TestFlatpakRemoteItem` (1508, 1515) | keep — the item shape is unchanged, only its route into the plan |

`tests/unit/jobs/test_block_state_decisions.py`: `TestFlatpakMaskDecisions` (377) is unaffected — masks keep the registry. Add a sibling asserting that no `flatpak:remote:` or `flatpak:conflict:` id can reach a decision file in **any** direction, mirroring the apt assertion at 242 that `TestAptRepoItemDecisions` (210) complements.

`tests/unit/jobs/test_snap_sync.py`: no test dies. Add the store-identity validation tests (§8). If S6 lands, `TestSideloadedSnaps` (per `02-SCENARIO-COVERAGE.md` E17) moves to `test_manual_installs_sync.py` and the source-side half of `snap_sync.py:492-507` goes with it.

`tests/unit/jobs/test_package_sync_core.py`: extend whatever asserts `_ACTION_VOCABULARY` with the new `(FLATPAK_REMOTE, REMOVE)` entry.

`tests/unit/test_mutates_audit.py`: the derived remote writes are new `run_command` call sites and must carry `mutates=`. Existing coverage catches an omission; no change expected, listed so the stage owner checks.

### 5.2 Integration tests

`tests/integration/jobs/test_package_sync.py`:

| Line | Test | Fate |
| --- | --- | --- |
| 1382 | `test_flatpak_installs_into_source_scope_after_remote` | rewrite. Its premise — pc2's Flathub is deleted, the sync re-adds it with the source's key, then installs — is exactly the derived path, so the *assertions* survive; what changes is that no remote appears in the review. The name should follow: `test_flatpak_derives_the_remote_its_ref_needs_and_carries_its_key` |
| new | the two-remote scenario (§8.4) | add; needs a fixture bump |

No `pytest.skip` may appear in this module. Fixtures live in `tests/integration/scripts/internal/vm-test-fixtures.sh`; its `FIXTURES_VERSION` (line 31) must be bumped together with `PCSWITCHER_TEST_FIXTURES_VERSION` (`tests/integration/scripts/internal/common.sh:156`) whenever the baseline gains a subject.

**Fixture bump required** for §8.4. The baseline provisions real Flathub and one app on pc1 only (`vm-test-fixtures.sh:65-82,190-215`). The two-remote test needs a second remote on pc1 that pc2 lacks and that is the origin of one ref. Cheapest honest subject: add `flathub-beta` (`https://dl.flathub.org/beta-repo/flathub-beta.flatpakrepo`) on pc1 only and install nothing from it, which alone proves the *negative* — a remote feeding no synced ref does not travel. Proving the positive needs a ref installed from it, so also install one small beta ref on pc1. Both remotes carry a byte-identical keyring (**measured**: `flathub.trustedkeys.gpg` and `flathub-beta.trustedkeys.gpg` have the same sha256, `c504fa5d…`), so the key-divergence assertions must key on the remote name, never on the digest.

### 5.3 Scenario matrix rows affected

`.planning/phases/02-package-management-sync/02-SCENARIO-COVERAGE.md`, section F:

- **F1** — restate: the ref installs from its origin *by full ref*, and the remote is derived rather than converged as a preceding item.
- **F5** — delete as written ("Remote missing on target → INSTALL … before any ref"). Replace with a derived-write row.
- **F5a, F5b, F5c, F5d** — keep the claims, restate the trigger: the key travels with a *derived* remote, not with an approved remote item. F5b (capture) is untouched.
- **F6** — restate: two derived remotes, one per scope, not two review items.
- **F7, F7a** — split. Silent derived overwrite when the target remote feeds no target-only refs; two-answer conflict when it does.
- **F8** — keep.
- **F9** — keep, new message; the guard survives.
- **F10** — keep; add that a *derived* system-scope remote write is likewise `sudo`-prefixed.
- **F12–F20** (masks) — untouched.
- **F21** — keep; add the two-answer and never-recorded assertions.
- **F22** — untouched.
- New F rows: origin captured per ref including runtimes; a remote feeding no synced ref does not travel; runtime-origin completion; `ORIGIN_MISMATCH` on a ref; install issued by full ref; derived-remote verification before the first install; derived-write failure attributed to its dependent refs; conflict answered "skip once" fails the dependent refs; no `flatpak:remote:` id in any decision file; filtered source remote warns.
- Section E: new row for the store-identity validation check, and — if S6 lands — E17 moves to section G.
- The **Findings** subsection "Snap and flatpak end-to-end coverage was vacuous" gains the two-remote fixture note and the reminder that a missing subject is an assertion failure, never a skip.

### 5.4 Docs

- `docs/jobs/package-sync.md:157` — "A flatpak remote is replicated as its own review item" is the sentence this work falsifies. Rewrite: remotes are derived from the refs approved from them and never appear in the review in the add or change direction.
- `docs/jobs/package-sync.md:159` — trust paragraph: keep the byte-for-byte rule, retarget it from "re-adds the remote on the target" to "provisions the derived remote".
- `docs/jobs/package-sync.md:161` — the same-name-different-URL paragraph becomes the conflict rule.
- `docs/jobs/package-sync.md:175,177` — removals: add the two-answer rule for a remote removal and state it is recorded nowhere.
- `docs/jobs/package-sync.md:145` — the version-float paragraph should mention that a flatpak ref's *origin* is now compared and reported.
- New short section in `docs/jobs/package-sync.md`: what "from the same remote" means for flatpak, why there is no Flathub equivalent of `ubuntu.sources`, and why snap has no repository question at all (the user asked; the answer belongs where they will look).
- `docs/system/package-sync.md:81-85` — the `flatpak_sync` bullets: item classes lose `FLATPAK_REMOTE` from the reviewed set, "Converges by" gains derivation and the pre-install remote verification, and "First-sync scope" (`flatpak_sync.py:1240-1250`) drops "configured flatpak remotes (per scope)" as a *reviewed* scope item while keeping it as a stated consequence.
- `docs/system/package-sync.md:77` — the `snap_sync` "Preconditions" bullet gains the store-identity check.
- Module docstrings: `flatpak_sync.py:1-73` (the remote-as-item-class paragraph at 16-29 and the ordering claim both change), `snap_sync.py:1-43` (add the store-identity check).
- ADR: see §6.

## 6. ADR

This does not need a new ADR. ADR-021 is `Accepted` and immutable (`docs/adr/adr-001-adr.md:15`), and its rules are written for apt specifically — D-34 through D-40 all say "apt". The flatpak work *applies* ADR-021's principle (D-02's carried-forward "mechanism the user has no basis to judge is not an item") to a second ecosystem without contradicting any ADR-021 rule.

Two ADR-021 lines do need checking against the result, and both hold: line 15's "the flatpak OSTree store MUST NOT be rsynced or otherwise file-mirrored" (unchanged — the derived path still shells out to `flatpak`), and line 25's four-jobs rule (§4).

If the reviewer disagrees and wants the ruling recorded as a decision rather than as an application of one, the cheapest form is a short **ADR-022: repository derivation across ecosystems**, stating the general rule once and enumerating per-ecosystem what it does and does not reach — including the two negatives (snap has no repository; flatpak has no distribution remote), which are the most valuable thing here to write down, because a future reader will otherwise re-derive them. Flagged as §9 Q1.

## 7. Staged plan

Every stage ends with `uv run ruff check . && uv run ruff format .`, `uv run basedpyright` and `uv run pytest` green, and its own tests mutation-checked (§8).

**S0 — capture widening.** `_FLATPAK_LIST_CMD` gains `ref`; `FlatpakItem` gains the `ref` field; `_parse_flatpak_list` accepts 5 fields. Add the source-side full ref listing (`flatpak list --columns=ref,origin,installation`, no `--app`) and the per-app `flatpak info --show-runtime` read, both cached on the job, both unconsumed. Pure plumbing; only `TestCapture` changes. Everything after this writes against stable seams.

**S1 — install by full ref.** `_converge_ref`'s INSTALL interpolates `source_item.ref`. Standalone bug fix, independent of derivation, fixes the two-branch ambiguity measured as **A**. Land early.

**S2 — `ORIGIN_MISMATCH` for refs.** `build_flatpak_origin_mismatch_detail`, the new branch in `_diff_flatpak_refs` (`flatpak_sync.py:495-522`) ahead of the version branch. Independent of S3–S5. Delivers the divergence report on its own.

**S3 — derivation.** `_DerivedRemote`, `_derive_remotes`, the `accept_review()` override, the `apply()` relocation of remote provisioning ahead of ref installs, `_ref_derived_remote_ids` attribution, and the removal of `_install_remote_diff`/`_change_remote_diff` from `_diff_flatpak_remotes`. `_ACTION_VOCABULARY` gains `(FLATPAK_REMOTE, REMOVE): "delete"`. The single largest stage; the one that answers the user's request.

**S4 — pre-install verification.** `_verify_derived_remotes`, its batched per-scope read, and the D-39 fan-out of its failures onto dependent refs. Depends on S3.

**S5 — two-answer removal and the conflict prompt.** Remote removal routed through `REPO_REMOVAL_REVIEW_ACTION`; `flatpak:conflict:<scope>:<name>` through `REPO_CONFLICT_REVIEW_ACTION` (`review.py:171`); `_record_permanent_skips` overridden in `FlatpakSyncJob` to filter `flatpak:remote:`/`flatpak:conflict:` prefixes, mirroring `apt_sync.py:2438-2458`. Depends on S3 (the conflict only exists once change is derived).

**S6 — sideloaded snaps to `manual_installs_sync`.** A third detector on the source; the `snap_sync.py:492-507` drop keeps its target-side withholding but loses the WARNING. Depends on nothing here; may land at any point; see §9 Q4 before starting.

**S7 — snap store identity.** `_store_identity` and the `validate()` comparison. Depends on nothing; may land first.

**S8 — filtered-remote warning.** One WARNING per derived remote whose source options carry `filtered`. Trivial; depends on S3.

**S9 — docs and matrix.** §5.3 and §5.4. Depends on everything; drafted alongside, finished last.

Parallelism and collisions in `src/pcswitcher/jobs/flatpak_sync.py`:

- **Ref lane** — S0, S1, S2 — works in the item shapes (`164-262`), the parse region (`374-455`) and the ref diff/converge regions (`457-522`, `1027-1063`).
- **Remote lane** — S3, S4, S5, S8 — works in the remote diff region (`525-698`), `plan()` (`881-934`), `converge()`/`_converge_remote` (`937-1025`) and the staging helpers (`1097-1160`).

They collide in exactly two places: `plan()` (`881-934`), which both lanes edit, and `FlatpakSyncJob.__init__` (`789-806`), where both add cached state. Give those two one owner for the duration, or run the lanes sequentially. S6 touches only `snap_sync.py` and `manual_installs_sync.py`; S7 touches only `snap_sync.py`; both are safe to run alongside either lane. S9 runs alongside anything.

## 8. Test plan

Every test below must be mutation-checked: break the named line, confirm the named assertion fails. A test that stays green under its mutation is vacuous.

### 8.1 Capture and ref identity (`tests/unit/jobs/test_flatpak_sync.py`)

`test_list_command_requests_the_ref_column` — assert `_FLATPAK_LIST_CMD` names `ref` and `_parse_flatpak_list` populates `FlatpakItem.ref` from the fifth field. Mutation: drop `ref` from the column list; the parse falls to 4 fields and the assertion on `.ref` fails.

`test_ref_field_is_not_part_of_identity` — two items differing only in `ref` (branch `stable` vs `beta`) share one `item_id`. Mutation: fold `ref` into `item_id`; the equality assertion fails.

`test_install_command_names_the_full_ref_not_the_application_id` — the converge command contains `org.example.App/x86_64/stable`, not the bare id. Mutation: revert `_converge_ref` to `application`; the substring assertion fails. This is the regression test for measurement **A**.

### 8.2 Origin mismatch

`test_same_ref_from_two_remotes_yields_origin_mismatch_report_only` — same app, same scope, `origin=flathub` vs `origin=flathub-beta`, equal versions; assert exactly one diff, `DiffClass.ORIGIN_MISMATCH`, `DiffAction.REPORT_ONLY`, detail naming both remotes. Mutation: delete the new branch; the diff count drops to zero.

`test_origin_mismatch_outranks_a_version_mismatch` — same pair with differing versions too; assert the diff class is `ORIGIN_MISMATCH`, not `VERSION_MISMATCH`. Mutation: move the new branch after the version branch; the class assertion flips.

`test_same_origin_different_scope_is_still_two_items_not_a_mismatch` — guards the scope-as-identity rule against the new branch. Mutation: compare origins across scopes; the item count assertion fails.

### 8.3 Derivation (`tests/unit/jobs/test_flatpak_sync.py`)

`test_no_remote_appears_in_any_review_group` — a plan with a source-only remote and a source-only ref; assert `plan.groups` contains no entry whose `item_id` starts `flatpak:remote:`. Mutation: restore `_install_remote_diff`'s emission; the assertion fires. This is the user's request made falsifiable.

`test_remote_is_provisioned_because_a_ref_was_approved` — approve the ref, assert one `remote-add` for the ref's origin in the ref's scope, issued before the `flatpak install`. Mutation: derive from source remotes instead of approved refs; the ordering or the scope assertion fails.

`test_a_remote_no_approved_ref_needs_does_not_travel` — source has two remotes, only one is a ref's origin; assert exactly one `remote-add`. Mutation: derive the full source remote set; the count assertion fails.

`test_declining_the_ref_declines_its_remote` — the only ref from a remote is answered skip-once; assert zero `remote-add` commands. Mutation: derive before applying the decisions; a `remote-add` appears.

`test_runtime_origin_is_derived_alongside_the_apps` — approved app on remote X, its `--show-runtime` runtime installed on the source from remote Y; assert both X and Y are provisioned. Mutation: drop the runtime-completion pass; Y is absent.

`test_derived_remote_carries_the_sources_key` — the #215 assertions, re-driven through derivation: `--gpg-import` naming a path under the target's own home, and the staged copy discarded. Mutation: pass `staged_key=None`; the flag assertion fails. Explicitly a **regression guard on #215**.

`test_user_scope_ref_derives_only_the_user_scope_remote` — a remote present in both scopes, a user-scope ref approved; assert one `remote-add --user`, no `--system`, no `sudo`. Mutation: key derivation on the remote name alone; the `--system` command appears.

`test_every_derived_write_carries_mutates` — each derived `run_command` names a `mutates=` phrase. Mutation: drop one; the assertion fires (and `tests/unit/test_mutates_audit.py` independently).

### 8.4 Verification, attribution, removal, conflict

`test_a_derived_remote_the_target_does_not_report_fails_its_refs_not_itself` — stub the post-write `flatpak remotes` read to omit the remote; assert the dependent ref is a failure naming the remote, and that no `flatpak install` was issued. Mutation: skip `_verify_derived_remotes`; the install is attempted.

`test_remote_add_exit_zero_that_changed_nothing_is_caught` — the measurement-**D** case: `remote-add --if-not-exists` returns 0, the target still reports the old URL; assert the dependent refs fail. Mutation: trust the exit code; the refs are installed against the wrong URL.

`test_a_failed_derived_write_fails_only_its_own_dependent_refs` — two refs from two remotes, one write fails; assert the other ref installs. Mutation: fail all approved refs; the second assertion fails.

`test_remote_removal_offers_exactly_two_answers` — assert the group's action is `REPO_REMOVAL_REVIEW_ACTION` and the group is not promotable (`review.py:140`). Mutation: leave it in `_PROMOTABLE_ACTIONS`; the assertion fires.

`test_no_flatpak_remote_id_can_reach_a_decision_file` (in `test_block_state_decisions.py`) — hand-assemble a `ReviewOutcome` marking a `flatpak:remote:` and a `flatpak:conflict:` id `SKIP_ALWAYS`; assert nothing is written. Mutation: drop the `_record_permanent_skips` override; an entry appears. The mask sibling must still pass, proving masks keep the registry.

`test_change_with_no_dependent_target_refs_is_silent` — differing URL, no target refs naming the remote; assert `remote-modify` runs and no review entry exists. Mutation: always raise the conflict; a group appears.

`test_change_that_repoints_target_only_refs_asks_two_answers` — same, with a target-only ref naming it; assert one `flatpak:conflict:` entry showing both URLs. Mutation: ignore the dependents; the entry is absent.

`test_conflict_answered_skip_once_fails_the_refs_that_needed_the_new_url` — mirrors apt's ruling-6 behaviour. Mutation: drop the seeded failure; the refs install against the old URL.

### 8.5 snap (`tests/unit/jobs/test_snap_sync.py`)

`test_matching_store_identity_produces_no_validation_error` — both machines `generic`/`generic-classic`, no proxy. Mutation: invert the comparison; the error appears.

`test_divergent_brand_store_is_a_blocking_validation_error` — differing `brand-id`; assert one `ValidationError` naming both brands and stating pc-switcher cannot converge it. Mutation: downgrade to a log line; the error-list assertion fails.

`test_divergent_proxy_store_is_a_blocking_validation_error` — one machine with `proxy.store` set. Mutation: compare only brand/model; the error is absent.

`test_unset_proxy_store_is_not_an_error` — the measured non-zero-exit case (`snap "core" has no "proxy" configuration option`) parses to `None` on both machines and produces no error. Mutation: treat non-zero as a probe failure; a spurious error appears on every ordinary desktop.

### 8.6 Integration (`tests/integration/jobs/test_package_sync.py`)

`test_flatpak_derives_the_remote_its_ref_needs_and_carries_its_key` — the rewrite of the test at line 1382. Same setup (delete Flathub and the app on pc2), new assertions: the review contains no remote line, the sync provisions Flathub on pc2 with the source's key, and the app installs. Mutation: revert S3; the review-content assertion fails while the install still succeeds — which is why the review assertion must be present, not merely the outcome.

`test_a_flatpak_remote_no_synced_ref_needs_does_not_travel` — needs the **fixture bump** (§5.2): `flathub-beta` on pc1 only, feeding nothing. Assert pc2 does not gain it. Mutation: derive the full source remote set; pc2 gains it.

`test_a_ref_from_a_second_remote_brings_that_remote_across` — same fixture, one small beta ref on pc1. Assert pc2 gains `flathub-beta` and the ref, and that `flatpak info --show-origin` on pc2 reports `flathub-beta`. Mutation: derive only the first remote; the install fails.

No `pytest.skip`. A missing fixture subject is an assertion failure naming `vm-test-fixtures.sh`.

## 9. Open questions

**Q1 — ADR or no ADR.** §6 argues this is an application of ADR-021's principle to a second ecosystem, not a new decision, so no ADR is needed. The counter-argument is that the two *negatives* — snap has no repository question, flatpak has no distribution remote — are exactly the kind of finding a future reader will re-derive expensively, and an ADR is where they would look. Materially different code either way: none. Materially different *artifact*: yes. Decide before S9.

**Q2 — derived-remote precision vs. always-sync.** This spec derives remotes from approved refs (the apt *repository* rule). The alternative is to always-sync every source remote (the apt *pin* rule, ADR-021 D-36), justified there by "a pin naming an absent origin is inert, so the precision buys nothing and the derivation has a wrong-answer mode that always-syncing does not". A flatpak remote is *nearly* inert — it costs one summary fetch per `flatpak update` and one line in `flatpak remotes` — and the derivation does have a wrong-answer mode (the runtime hole, §2.1, which S3 closes with two extra reads). If the answer is always-sync, §2.1's runtime completion, S3's derivation function and four of §8.3's tests all disappear and the stage shrinks by more than half. **My recommendation is derive**, because "the two machines' remote lists are converged for what refs need, not made identical" is the property ADR-021 chose for apt and a user should not have to learn two rules. But this is a real fork.

**Q3 — the flatpak conflict prompt's trigger.** apt's conflict trigger (`c253355a`) is "the file feeds *machine-specific* packages on the target", deliberately narrower than "any target package", for the reasons at `02-SCENARIO-COVERAGE.md`'s "Accepted scope limits". §2.2 proposes the flatpak trigger as "the target's remote is the origin of target refs not being removed this run" — which is broader, because a flatpak machine has a handful of refs per remote rather than hundreds of packages, so the narrow version would almost never fire. If the reviewer wants exact parity with apt, the trigger becomes "target refs recorded skip-always in `flatpak.decisions.yaml`" and the prompt becomes rare. Different code in `_derive_remotes`'s conflict branch and in two §8.4 tests.

**Q4 — S6's scope.** Moving sideloaded snaps to `manual_installs_sync` is right by symmetry (§1.3) and converts a permanent warning into a resolvable item, but it is not about repos or keys and was not what was asked for. Should it ride along in this work, become its own GitHub issue, or be dropped? If it rides along it needs its own scenario rows and moves E17's six tests between modules.

## 10. Residual risks and unverified reasoning

- The **runtime hole** (§2.1) is inferred, not constructed. Every ref on this machine and every ref pulled in the container came from one remote, so a cross-remote runtime dependency was never observed. If Q2 resolves to always-sync, the hole is moot; if it resolves to derive, S3's completion pass is insurance against a case nobody has seen.
- The snap **brand store / remodel** path (§1.2) is reasoned from the assertion chain and `snap remodel`'s existence, not exercised. The check in §3 only compares what `snap known model` and `snap get system proxy.store` report; if a third mechanism can make two machines draw from different stores, the check will not see it.
- `flatpak remotes --columns=filter` reporting a **local path** rather than content (§2.8) was measured on flatpak 1.14.6 only. A future flatpak that inlines the filter would make S8's warning wrong rather than merely incomplete.
- The two-remote integration fixture (§5.2) adds a network dependency on `dl.flathub.org/beta-repo/` to baseline provisioning. Flathub beta's availability is not something this project controls, and `assert_app_runtime_unchanged`'s tolerance pattern (`vm-test-fixtures.sh:151-177`) should be extended to it.
