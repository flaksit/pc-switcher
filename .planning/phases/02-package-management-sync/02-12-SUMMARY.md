---
phase: 02-package-management-sync
plan: 12
subsystem: infra
tags: [documentation, adr-011, adr-012, living-specs, apt, snap, flatpak, package-sync]

requires:
  - phase: 02-package-management-sync
    provides: "The complete package-sync subsystem (plans 02-01 through 02-11, 02-13): apt_sync/snap_sync/flatpak_sync, PackageSyncJob/PackagePhaseCoordinator, package_state.py's DecisionFile/SnippetRegistry, config_sync's SYNCED_CONFIG_FILENAMES, folder_sync's exclusion wiring"
provides:
  - "docs/configuration.md Package Sync section: the three jobs, the batched cross-manager review, apt collateral-effect reporting, machine-specific packages, install snippets, folder_sync interaction, version policy, non-interactive behavior, sudo prerequisites"
  - "README.md: one-line entry per package job plus a pointer to the new configuration section"
  - "docs/system/architecture.md Package Sync Subsystem section: job-execution ordering, the plan/review/apply pipeline, why the PackagePhaseCoordinator exists, the source/target split against ADR-002, a Mermaid pipeline diagram"
  - "docs/system/core.md Package Sync Subsystem section: shared PackageSyncJob abstract-hook contract and per-job responsibilities/validate()/item classes/convergence verbs/first-sync scope for all three jobs"
  - "docs/system/data-model.md Package Sync Entities section: the item-identity scheme and the decision-file/snippet-registry shapes"
  - "REQUIREMENTS.md: REQ-sync-scope-packages and REQ-conflict-detection-no-resolution marked complete (checkbox + traceability table)"
affects: []

tech-stack:
  added: []
  patterns: []

key-files:
  created: []
  modified:
    - docs/configuration.md
    - README.md
    - docs/system/architecture.md
    - docs/system/core.md
    - docs/system/data-model.md
    - .planning/REQUIREMENTS.md

key-decisions:
  - "Documented the code as it exists, not as the original plans described it: SnapSyncJob/FlatpakSyncJob overriding plan() entirely (not a third generic hook) is stated explicitly in core.md's shared-core-contract paragraph, and data-model.md's SNAP_CHANNEL note explains why snap_sync never tags a diff with that item class despite the enum member existing."
  - "PCSWITCHER_PACKAGE_REVIEW_AUTOMATION is not named anywhere in the new documentation (D-26) — verified by grep, not by memory, after every edit."
  - "'Machine-specific package' is the only phrase used for the decision-file mechanism throughout docs/configuration.md; 'exclusion' is used only for folder_sync's unrelated always-excluded-paths list, never for the decision file."
  - "The install-snippet worked example in docs/configuration.md reproduces package_review.py's _SNIPPET_AUTHORING_NOTE text verbatim (the DEBIAN_FRONTEND=noninteractive dpkg -i / apt-get install -y -f shape), so a user reads identical guidance whether they hit it in the review prompt or in the docs."
  - "Marked REQ-sync-scope-packages and REQ-conflict-detection-no-resolution complete after reading all 12 prior SUMMARYs: the implementation is comprehensive (three real jobs, shared core, coordinator, 974 passing unit tests) and VM-level integration tests exist and are correctly wired — their 'pending_ci' status (no local VM access in any plan's execution environment) matches this project's established ADR-008 pattern of running integration tests in GitHub Actions on a PR to main, not a gap in the phase's own delivery."

patterns-established: []

requirements-completed: [REQ-sync-scope-packages, REQ-conflict-detection-no-resolution]

coverage:
  - id: D1
    description: "docs/configuration.md documents apt_sync/snap_sync/flatpak_sync, their sync_jobs keys, the decision-file and snippet-registry paths (naming which is synced), and states that enabling a package job excludes that ecosystem's store from folder_sync"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: other
        ref: "grep -c 'apt_sync\\|snap_sync\\|flatpak_sync' docs/configuration.md; grep -q '.decisions.yaml' docs/configuration.md; grep -q 'package-snippets.yaml' docs/configuration.md"
        status: pass
      - kind: other
        ref: "Every config key named in docs/configuration.md's Package Sync section (apt_sync, snap_sync, flatpak_sync) verified present in src/pcswitcher/schemas/config-schema.yaml"
        status: pass
    human_judgment: false
  - id: D2
    description: "docs/configuration.md uses 'machine-specific package' rather than 'exclusion' for the decision-file mechanism"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: other
        ref: "grep -c 'machine-specific' docs/configuration.md returns 5+; the decision-file paragraph never uses the word 'exclusion'"
        status: pass
    human_judgment: false
  - id: D3
    description: "The batched review's cross-manager scope (one review for every enabled package job) is documented in docs/configuration.md"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: other
        ref: "docs/configuration.md 'The batched review' subsection states 'one review covering every enabled package job at once — not one review per manager'"
        status: pass
    human_judgment: false
  - id: D4
    description: "PCSWITCHER_PACKAGE_REVIEW_AUTOMATION appears nowhere in docs/ or README.md (D-26)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: automated
        ref: "grep -ril 'PCSWITCHER_PACKAGE_REVIEW_AUTOMATION' docs/ README.md — returns nothing (verified this session)"
        status: pass
    human_judgment: false
  - id: D5
    description: "docs/system/ living specs describe the package-sync subsystem: job placement/ordering, the plan-review-apply pipeline with the PackagePhaseCoordinator, and the item model / decision-file / snippet-registry data shapes"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: automated
        ref: "uv run codespell docs/system/ — exits 0; grep -q 'mermaid' docs/system/architecture.md — found"
        status: pass
      - kind: other
        ref: "Every item class named in docs/system/data-model.md (AptPackageItem, AptSourceItem, AptKeyItem, AptPinItem, AptConfigItem, SnapItem, FlatpakItem, FlatpakRemoteItem, UnreproducibleItem) verified present as a dataclass in src/pcswitcher/jobs/package_items.py"
        status: pass
    human_judgment: false
  - id: D6
    description: "A user reading only docs/configuration.md can enable apt_sync, understand what will happen on their target, and know how to mark a package machine-specific"
    verification: []
    human_judgment: true
    rationale: "Whether the documentation is genuinely sufficient for a first-time reader is a judgment call about clarity and completeness that automated checks (codespell, grep, key-existence checks) cannot make. Every fact stated was individually verified against the source in this session (see key-decisions and the coverage entries above), but overall readability/sufficiency needs a human read-through."
  - id: D7
    description: "A human confirms the phase's three roadmap success criteria (package replication, conflicts/version-mismatches reported before any destructive change, machine-specific packages never forced onto the target) on real machines, per the plan's Task 3 checkpoint"
    verification: []
    human_judgment: true
    rationale: "Task 3 is a type=checkpoint:human-verify, gate=blocking task requiring interactive TUI checkbox review, real apt/snap/flatpak state divergence across two physical machines, and manual inspection of ~/.config/pc-switcher/*.decisions.yaml and package-snippets.yaml on both ends. This autonomous run has no interactive terminal or access to the user's two real machines. See 'Deferred human verification' below for the exact procedure."

duration: ~35min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 12: Package-Sync Documentation Summary

**docs/configuration.md gained a Package Sync section covering all three jobs, the batched cross-manager review, machine-specific packages and install snippets; docs/system/architecture.md, core.md and data-model.md now describe the subsystem as living specs per ADR-011/ADR-012; REQ-sync-scope-packages and REQ-conflict-detection-no-resolution are marked complete after verifying the phase's 13 plans genuinely deliver them.**

## Performance

- **Duration:** ~35 min
- **Completed:** 2026-07-23T12:43:19Z
- **Tasks:** 2 automated (documentation) + 1 checkpoint (deferred to human, see below)
- **Files modified:** 6 (5 docs, 1 requirements)

## Accomplishments

- `docs/configuration.md`: new "Package Sync" section — the three jobs and their `sync_jobs` keys (all ship disabled, run before `folder_sync`); what each job covers (apt manually-installed set + `/etc/apt` repo state, snap revision/channel convergence, flatpak scoped refs/remotes); the batched review (one review for every enabled manager, grouped by manager and action, removals separate and unticked by default, apt's collateral-effect reporting and simulate-before-execute refusal); machine-specific packages (decision-file path, semantics, example file, un-marking); install snippets (registry path, that it IS synced unlike decision files, the verbatim `DEBIAN_FRONTEND=noninteractive` worked example matching the review prompt's own text, mandatory-registration failure behavior); version policy (float-by-name, reported not forced, pins replicate); non-interactive behavior (D-26); passwordless-sudo prerequisites per job. The "Always excluded" list under `folder_sync` gained the decision-file and conditional snap/flatpak exclusions.
- `README.md`: one-or-two-line entry per package job in the top-level Configuration sections list, plus a pointer to the new `docs/configuration.md#package-sync` section; step 10's job-order description now names the package jobs running ahead of `folder_sync`.
- `docs/system/architecture.md`: new "Package Sync Subsystem" section — job placement in phase 9 (job execution) ahead of `folder_sync` and why that ordering is load-bearing (D-17); the `plan()`/`apply()` split and why the `PackagePhaseCoordinator` exists (three independently-executing jobs cannot share one batched review without it); the source/target split matching ADR-002's stateless-target model; a Mermaid flowchart showing the plan fan-out, single review, and apply fan-in.
- `docs/system/core.md`: new "Package Sync Subsystem" section — the shared `PackageSyncJob` abstract-hook contract (`capture_source_items`/`query_target_items`/`converge`) and what the base guarantees (read-only planning, review-before-changes, per-item continue-on-failure, dry-run, FULL/INFO logging split); per-job responsibilities, `validate()` checks, item classes, convergence verbs and first-sync scope for `apt_sync`, `snap_sync`, `flatpak_sync` — including the two convergence-safety behaviors (apt's `apt-get -s` simulate-before-execute guard, and `/etc/apt` writes staged under the target's own home and promoted with `sudo install`, with full repository-group rollback on a failed `apt-get update`).
- `docs/system/data-model.md`: new "Package Sync Entities" section — the item-identity scheme as a table (why `scope`/`origin` fold into `item_id` rather than staying a sibling field, for every item class), the shared `ItemDiff` dataclass, and the `DecisionEntry`/`Snippet` dataclass shapes with an explicit statement of which file is synced and which is not.
- `.planning/REQUIREMENTS.md`: `REQ-sync-scope-packages` and `REQ-conflict-detection-no-resolution` marked complete (checkbox + traceability table row) via `requirements mark-complete`, after reading all 12 prior plans' SUMMARYs and confirming the delivered scope genuinely matches both requirements' text.

## Task Commits

1. **Task 1: User documentation for the three package jobs** - `ca59835` (docs)
2. **Task 2: Living-spec updates for the package-sync subsystem** - `9c15908` (docs)
3. **Task 3: Confirm the phase's three success criteria on real machines** — checkpoint, `gate="blocking"`, deferred to human (see "Deferred human verification" below); no code change

**Plan metadata:** (this commit)

## Files Created/Modified

- `docs/configuration.md` - Package Sync section; extended "Always excluded" list
- `README.md` - package-job entries in the Configuration sections list and step 10 description
- `docs/system/architecture.md` - Package Sync Subsystem section with Mermaid pipeline diagram
- `docs/system/core.md` - Package Sync Subsystem section: shared contract + three per-job specs
- `docs/system/data-model.md` - Package Sync Entities section: item identity + decision-file/snippet-registry shapes
- `.planning/REQUIREMENTS.md` - REQ-sync-scope-packages and REQ-conflict-detection-no-resolution marked complete

## Decisions Made

See `key-decisions` in frontmatter: documenting the code as it actually exists (SnapSyncJob/FlatpakSyncJob's `plan()` override, `SNAP_CHANNEL`'s unused-for-diffs status), the D-26 grep-verified absence of the automation env var, consistent "machine-specific package" phrasing throughout, the verbatim-matching snippet worked example, and the requirement-completion decision after reading every prior SUMMARY.

## Deviations from Plan

None — plan executed exactly as written for both automated tasks. Task 3's checkpoint was deferred per explicit orchestrator directive #7 for this autonomous run rather than blocking the session (see below).

## Issues Encountered

None.

## Deferred Human Verification

Task 3 (`gate="blocking"`) requires a human to confirm the phase's three roadmap success criteria on real machines — package replication, conflicts/version-mismatches reported before any destructive change, and machine-specific packages never forced onto the target. This is inherently a two-real-machine, interactive-TUI check that no autonomous run can perform. Per the orchestrator's explicit directive for this run, this check is deferred rather than blocked on.

**Setup:** enable all three package jobs on the source by setting `apt_sync: true`, `snap_sync: true` and `flatpak_sync: true` in `~/.config/pc-switcher/config.yaml`.

**1. Preview first:**
```bash
pc-switcher sync <target> --dry-run
```
Check that ONE review appears covering all three managers (not three separate reviews) — the phase's central promise. Check it appears before anything else, shows every difference grouped by manager and action, that installs and removals are visibly separate groups with the removal group naming removal (not "apply"), that any apt collateral effects appear as their own review entries, that version mismatches show both versions, that held/pinned packages are called out, and that the four no-repository-candidate apt packages appear as unreproducible items. Confirm nothing changed on the target:
```bash
# On the target, before and after, compare:
apt-mark showmanual | wc -l
snap list | wc -l
flatpak list | wc -l
```

**2. Real run:**
```bash
pc-switcher sync <target>
```
Tick a few installs, leave removals unticked, mark one package skip-always, author a snippet for one unreproducible item. Afterward:
```bash
# On the TARGET — ticked packages installed, unticked ones untouched:
apt-mark showmanual
snap list
flatpak list

# On the SOURCE — the machine-specific mark landed:
cat ~/.config/pc-switcher/apt.decisions.yaml

# On the SOURCE — the authored snippet is there verbatim:
cat ~/.config/pc-switcher/package-snippets.yaml

# On the TARGET — the registry travelled (proves it is actually synced):
cat ~/.config/pc-switcher/package-snippets.yaml

# On the TARGET — no decision file ever arrived (machine-local, never synced):
ls ~/.config/pc-switcher/*.decisions.yaml   # should NOT exist on the target
```

**3. Second run (idempotence):**
```bash
pc-switcher sync <target> --dry-run
```
Check the machine-specific item produces no diff at all, and the snippet-resolved item no longer needs resolution.

**4. Auto-refresh guard:**
```bash
# On BOTH machines, before and after step 2:
snap get system refresh.hold
```
Should read identically before and after — snap_sync must never leave a snap held.

**5. Mirror interaction:** confirm `~/.var/app` content still syncs and `~/.local/share/flatpak` is no longer mirrored by `folder_sync`, then delete the now-redundant flatpak/snap lines from `~/.config/pc-switcher/home-janfr.filter` and re-run a dry-run to confirm nothing changed.

## User Setup Required

None - no external service configuration required. (Task 3's real-machine verification above is separate from external-service setup — it is the phase's own acceptance check, deferred per orchestrator directive.)

## Next Phase Readiness

Phase 2 (Package Management Sync) is now complete: all 13 plans executed, documented in both user-facing and living-spec form, and both phase requirements marked complete in `.planning/REQUIREMENTS.md`. The one outstanding item is Task 3's real-machine confirmation above — genuinely unverifiable without interactive access to two physical machines, deferred per this run's explicit autonomous-mode directive, not a gap in the phase's delivered code or tests.

---
*Phase: 02-package-management-sync*
*Completed: 2026-07-23*

## Self-Check: PASSED

- FOUND: docs/configuration.md
- FOUND: README.md
- FOUND: docs/system/architecture.md
- FOUND: docs/system/core.md
- FOUND: docs/system/data-model.md
- FOUND: .planning/REQUIREMENTS.md
- FOUND: commit ca59835
- FOUND: commit 9c15908
