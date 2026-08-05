# Issue #208 — replicate hold/block/mask intent across managers

Status: Implemented (2026-07-24)

Notes/deviations: the apt hold diff is built from a dedicated `AptHoldItem` dataclass (`packages/items.py`), while the snap hold diff (`_diff_snap_holds` in `snap_sync.py`) constructs each `ItemDiff` inline with `item_id=f"snap:hold:{name}"` and no separate `SnapHoldItem` dataclass. A minor, consistent shape difference — both emit the same `<mgr>:hold:<name>` identity and the same INSTALL/REMOVE membership diff — not a behavior change.

Replicate the user's deliberate per-package block state source→target as review Items: apt `apt-mark hold`, snap per-snap `snap refresh --hold`, flatpak `flatpak mask`. (apt pins already travel as `/etc/apt/preferences.d` files.) Per-item review choice is the existing three-way: apply (= overwrite source>target) / skip-once / skip-always. Changes are rare — reuse the existing pipeline, no new UI.

## Locked design (uniform across the three managers)

D1. Each block is a boolean-membership Item. New `ItemClass`: `APT_HOLD`, `SNAP_HOLD`, `FLATPAK_MASK`. item_ids: `apt:hold:<pkg>`, `snap:hold:<name>`, `flatpak:mask:<scope>:<pattern>`. These are DISTINCT identities from the package/ref item, so a package and its block are two separate review items.

D2. Diff = membership difference: source-has & target-lacks → `DiffAction.INSTALL` (add the block on target); target-has & source-lacks → `DiffAction.REMOVE` (remove the block on target); present/absent on both → no diff. No `CHANGE` (there is no value to change, only presence).

D3. Review verbs via `_ACTION_VOCABULARY` (sync_core.py): `(APT_HOLD,INSTALL)="hold"`/`(…,REMOVE)="unhold"`; `(SNAP_HOLD,INSTALL)="hold"`/`(…,REMOVE)="unhold"`; `(FLATPAK_MASK,INSTALL)="mask"`/`(…,REMOVE)="unmask"`. Add-direction is default-checked; remove-direction lands in its own removal group, unticked — the right friction for undoing a block the user deliberately set. Three-way apply/skip-once/skip-always inherited unchanged; skip-always writes the machine-local `DecisionFile` and `filter_inert` drops it next run.

D4. Converge routed by `item_id` PREFIX, not by action (so it never collides with the package install/remove/change dispatch). Commands: apt `sudo apt-mark hold|unhold <pkg>`; snap `sudo snap refresh --hold=forever <name>` / `sudo snap refresh --unhold <name>` (NEVER bare `snap refresh --hold` with no snap name — that is the global-hold pitfall); flatpak `[sudo] flatpak --user|--system mask [--remove] <pattern>` (sudo iff system scope, via existing `_sudo_prefix`). All idempotent for the add direction; hold/unhold/mask exit 0 on no-op. `shlex.quote` every interpolated value. No apt-get simulation for holds (selection state only, not a transaction).

D5. apt `HELD_OR_PINNED` reshape (the crux): today a held package is surfaced as `HELD_OR_PINNED`/`REPORT_ONLY` and its hold is never written. Split the mechanism: PINS keep their `REPORT_ONLY` echo on the package item; HOLDS move out of the `held_or_pinned` guard into their own `apt:hold:` item. The target's hold set still SUPPRESSES the package's install/version action (a held package is never proposed for install/upgrade), but the hold FLAG is now carried by the `apt:hold:` item, not the package report. Suppress the package-level hold report when an `apt:hold:` diff exists for that name, so a held package no longer double-reports. `collect_hold_pin_facts`: keep the pin read as a target-only fact; convert the two `apt-mark showhold` reads into source-hold-set + target-hold-set feeding the `AptHoldItem` diff.

D6. Absent-package blocks (apt/snap): if the user skips a package's install but applies its hold, the hold hits an absent package and fails — treat as a normal per-item failure (D-27 continue-and-report). No gating machinery. flatpak masks are patterns independent of installed refs, so always replicate.

D7. Sudo: add `/usr/bin/apt-mark` to apt `_TARGET_SUDO_COMMANDS`; snap already covers `/usr/bin/snap`; flatpak — extend `_system_scope_in_play()` to also return True when a system-scope mask exists on either machine (then target sudo is required, existing gate covers the binary).

D8. Ordering: within each job emit block-item diffs AFTER presence diffs (`apply()` preserves original diff order, so install lands before its hold for the same item). flatpak order: remotes → refs → masks.

D9. snap capture-timing assumption (verify in VM integration): per-snap `held` in `snap list` Notes is separate snapstate from the system `refresh.hold` option the sync-window hold sets, so capturing inside the sync window does not mask per-snap holds. Reasoning is strong (different snapd namespaces) but not doc-explicit. Fail-safe: even if the system hold flipped Notes, it does so symmetrically on both hosts → both-held → no spurious diff (a no-op, never a wrong action). Document the assumption in code; add a VM check. Both exist: the comment in `snap_sync._parse_snap_list`, and `TestSnapHoldCaptureTiming` in tests/integration/jobs/test_package_sync.py (snapd semantics in both directions, plus the end-to-end replication through a real sync window).

D10. flatpak: replicate masks as pure patterns (not filtered to installed refs); a pattern edit reads as remove-old + add-new, and a user/system scope-split reads as add + remove — reported as found, not normalized (consistent with existing ref/remote behavior).

## Work packages

- WP-1 (core + apt) — FIRST, owns all shared-core edits so the parallel managers don't collide:
  - `packages/items.py`: three new `ItemClass` values; `AptHoldItem`, `FlatpakMaskItem` dataclasses; `held: bool = False` field on `SnapItem`.
  - `packages/sync_core.py`: six `_ACTION_VOCABULARY` entries; apt hold diff + the D5 `HELD_OR_PINNED` reshape.
  - `apt_sync.py`: source/target hold-set capture, converge by item_id prefix, `_TARGET_SUDO_COMMANDS` += apt-mark.
  - apt unit tests.
- WP-2 (snap) — after WP-1: `snap_sync.py` (parse `held` from Notes → `SnapItem.held`; second diff pass emitting `snap:hold:` items; converge by prefix) + tests.
- WP-3 (flatpak) — after WP-1: `flatpak_sync.py` (per-scope mask capture, `_diff_flatpak_masks`, `_converge_mask`, extend `_system_scope_in_play`) + tests.

Then: docs (package-sync.md "Holds and masks" section; ADR-020 note; this .planning record), label #208 `status: done`, commits reference `Closes #208`.
