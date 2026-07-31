# Sweep — B: apt holds

Machines: **Atlas** = source, **Vega** = target.

Terminology used below, from the articles rather than the code: a **real hold** is one naming a package the machine has installed; a **bookkeeping hold** is one naming a package the machine does not have (`PKG-FR-APT-HELD-TARGET`: "suppresses nothing"; the code calls it a stale hold).

## B.1 The hold is its own item (articles: PKG-FR-BLOCKS-REPLICATE — apt half, PKG-FR-APT-HOLD-ITEM)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| B1 | Atlas holds `pkg-a`; Vega has `pkg-a` at the same version and does not hold it | One item, `pkg-a (hold)`, in a group whose verb is "hold"; approving it registers the hold on Vega and issues no install or upgrade | U | `test_apt_packages:TestAptHold::test_source_held_yields_install_hold_item_and_converge_runs_apt_mark_hold`; `test_apt_diffing:TestDiffEngine::test_source_hold_only_yields_apt_hold_install` |
| B2 | Vega holds `pkg-a` (and has it); Atlas has `pkg-a` and does not hold it | One item, `pkg-a (hold)`, in an "unhold" group; approving it removes the hold from Vega | U | `test_apt_packages:TestAptHold::test_target_held_only_yields_remove_unhold_item`; `test_apt_diffing:TestDiffEngine::test_target_hold_only_yields_apt_hold_remove_and_suppresses_package_action` |
| B3 | Both machines hold `pkg-a` and both have it | No hold item at all | U | `test_apt_packages:TestAptHold::test_held_on_both_yields_no_hold_diff`; `test_apt_diffing:TestDiffEngine::test_held_on_both_yields_no_diff` |
| B4 | Neither machine holds anything | No hold item at all | U | `test_apt_diffing:TestDiffEngine::test_equal_versions_yields_no_diff` |
| B5 | One run carrying an install, a removal, a hold add and a hold removal | The two hold items sit in their own groups reading "hold"/"unhold"; the package items keep "install"/"remove", and no hold appears under a package group | U | `test_apt_job:TestHoldReviewVerbs::test_hold_items_get_their_own_group_with_hold_and_unhold_verbs` |
| B6 | The same run's hold group and unhold group | The unhold group is removal-direction (rows start unticked); the hold group is not | U | `test_apt_job:TestHoldReviewVerbs::test_unhold_group_is_removal_direction_and_the_hold_group_is_not` |
| B7 | Atlas holds `pkg-a`, Vega lacks it; the user approves the install and declines the hold | `pkg-a` is installed on Vega at Atlas's version and is left unheld — the two decisions are independent in that direction too | — | none |
| B8 | A hold-only run (no package work) | No `apt-get --dry-run` is issued at plan time or at apply time — a hold is selection state, not a transaction | U | `test_apt_packages:TestHoldsDriveNoSimulation::test_hold_only_run_issues_zero_apt_get_simulations` |
| B9 | The approved hold names something apt has never heard of, so `apt-mark` exits non-zero | That item alone fails; every other approved item in the run still converges | U | `test_apt_packages:TestHoldOnAnAbsentPackage::test_failed_apt_mark_hold_fails_only_that_item` |
| B10 | A hold add offered in the review | The permanent answer is available for it ("never hold") | U | `test_review_skip_always:TestBlockStateItemsArePromotable::test_hold_add_direction_can_be_made_permanent` |

## B.2 A package Vega has and holds (article: PKG-FR-APT-HELD-TARGET, first half)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| B11 | `pkg-a` on both, Vega holds it, versions differ | No package item of any kind — not an install, not an upgrade, not a version-difference report; the hold is the only item | U | `test_apt_packages:TestAptHold::test_held_package_yields_hold_item_not_a_duplicate_package_report`; `test_apt_diffing:TestDiffEngine::test_target_hold_only_yields_apt_hold_remove_and_suppresses_package_action` |
| B12 | `pkg-a` on both, both hold it, Vega has it | The run proposes nothing at all | U | `test_apt_packages:TestAStaleTargetHoldDoesNotStrandThePackage::test_a_hold_on_a_package_the_target_has_still_suppresses_its_install` |
| B13 | Vega holds `pkg-a`, which apt installed there automatically (so it is outside Vega's manual set); Atlas has it manually | Still no install item; the hold is still an item | U | `test_apt_diffing:TestDiffEngine::test_a_held_package_outside_the_targets_manual_set_is_still_not_proposed` |
| B14 | Vega has and holds `pkg-a`; Atlas does not have it at all | No removal item for `pkg-a` (it produces no package-level item in any direction); the unhold is offered | — | none |
| B15 | A version difference on a package Vega holds | Never converged and never reported as a package item — the hold item is the whole of what the user sees | U | same as B11 |

## B.3 A hold naming a package the machine does not have (article: PKG-FR-APT-HELD-TARGET, second half)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| B16 | Vega records a hold for `pkg-a` but does not have `pkg-a`; Atlas has it and holds it | `pkg-a` is offered for install like any other package, and its hold is offered as a second item that lands after the install | U | `test_apt_diffing:TestDiffEngine::test_a_hold_for_a_package_the_target_lacks_still_proposes_the_install` |
| B17 | Same, but Atlas does not hold `pkg-a` | `pkg-a` is offered for install, and Vega's own bookkeeping hold is offered for removal | U | `test_apt_diffing:TestDiffEngine::test_a_stale_hold_the_source_does_not_share_proposes_install_and_unhold` |
| B18 | Vega records a hold for `pkg-a`; neither machine has `pkg-a` installed | No package item; the hold is still offered for removal (Vega carries selection state Atlas does not) | — | none |
| B19 | Approved install of a package Vega holds without having, run through | Vega's bookkeeping hold is cleared, then the package is installed, then the hold Atlas asked for is registered — in that order | U | `test_apt_packages:TestAStaleTargetHoldDoesNotStrandThePackage::test_the_stale_hold_is_cleared_the_package_installed_and_the_hold_restored` |
| B20 | Planning a run whose install batch contains a name Vega holds without having | Planning still produces its collateral findings instead of ending on apt's refusal of the whole batch | U | `test_apt_collateral:TestTheRehearsalSurvivesAStaleTargetHold::test_the_install_rehearsal_asks_apt_to_allow_the_held_name` |
| B21 | Planning a run with no such name | Nothing in the run asks apt to move held packages | U | `test_apt_collateral:TestTheRehearsalSurvivesAStaleTargetHold::test_an_ordinary_run_never_asks_for_it` |

## B.4 The exact-version obligation (article: PKG-FR-APT-HOLD-VERSION)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| B22 | Atlas holds `pkg-a` at 1.0; Vega lacks it and its repositories offer 1.0 | The install asks for 1.0 by name, not for whatever Vega's repositories currently offer | U | `test_apt_packages:TestAHeldPackageIsInstalledAtTheSourcesVersion::test_the_install_names_the_sources_version` |
| B23 | Atlas holds `pkg-a` at 1.0; Vega's repositories offer only 2.0 | That install fails as its own item, naming 1.0 as Atlas's version and 2.0 as what Vega offers; the rest of the run continues | U | `test_apt_packages:TestAHeldPackageIsInstalledAtTheSourcesVersion::test_a_version_the_target_cannot_supply_fails_naming_both` |
| B24 | Same | No install of `pkg-a` at any other version is attempted | P | same test — it asserts the absence of the *version-pinned* command only, so a fallback to an unpinned `apt-get install pkg-a` would pass |
| B25 | Same, but Vega's apt offers no candidate for `pkg-a` at all (or the candidate read comes back unusable) | The refusal still names Atlas's version and says Vega offers no other | P | `test_apt_commands:TestCandidateVersion::test_apt_saying_it_will_install_nothing_reads_as_no_version` and `::test_a_name_apt_printed_no_block_for_reads_as_no_version` assert the parser; no test reaches the wording of the refusal |
| B26 | The run is done and both items were approved | `pkg-a` is on Vega at Atlas's version and Vega records the hold | U | `test_apt_packages:TestAHeldPackageIsInstalledAtTheSourcesVersion::test_the_install_names_the_sources_version` (asserts both the pinned install and `apt-mark hold`) |
| B27 | Atlas holds `pkg-a`, Vega lacks it, and `pkg-a` was earlier marked machine-specific on Atlas | The package does not travel — and neither does its hold, since there is nothing on Vega to freeze | ‼ | none. The hold item is still emitted and, if approved, `apt-mark hold` runs on Vega for a package it does not have. See Gaps. |
| B28 | Atlas holds `pkg-a` but the version capture yields nothing for it | The install is refused rather than floated onto whatever Vega offers | ‼ | none; the version pin is silently dropped and the install floats. See Gaps. |

## B.5 Replicating a hold changes no version (article: PKG-FR-APT-HOLD-INERT, first sentence)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| B29 | Atlas holds `pkg-a` at 1.0; Vega has `pkg-a` at 2.0 and does not hold it | The version difference is reported, the hold is registered on Vega, and no install, upgrade or downgrade of `pkg-a` runs — the two machines end held at different versions | — | none (only the equal-version shape is covered, by B1) |
| B30 | Any approved hold add | The converge issues `apt-mark` and nothing else | U | `test_apt_packages:TestAptHold::test_source_held_yields_install_hold_item_and_converge_runs_apt_mark_hold`; `test_apt_packages:TestHoldsDriveNoSimulation::test_hold_only_run_issues_zero_apt_get_simulations` |

## B.6 The four outcomes for a hold whose package did not arrive (article: PKG-FR-APT-HOLD-INERT)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| B31 | Atlas holds `pkg-a`, Vega lacks it; the user approves the hold and declines the install at the review | The hold is reported as declined, not as a failure; no `apt-mark hold` runs; the job does not fail | U | `test_apt_packages:TestAHoldNeedsItsPackage::test_a_hold_whose_install_was_skipped_is_declined_not_failed` |
| B32 | Same, but the install is withdrawn by a collateral answer given while planning (the user kept a package on Vega that installing `pkg-a` would have removed) | Same outcome: declined, no failure, nothing logged as an error | U | `test_apt_packages:TestAHoldNeedsItsPackage::test_a_hold_whose_install_a_collateral_answer_cancelled_is_declined_too` |
| B33 | Same, but the collateral question is only answerable after `/etc/apt` has converged (the repository `pkg-a` needs is one this run writes), and the user keeps the other package | The hold is declined for that reason and named as such; not a failure | — | none |
| B34 | The install was approved and then failed | The hold fails too, and both the package and the hold are named as failed items | U | `test_apt_packages:TestAHoldNeedsItsPackage::test_a_hold_whose_install_failed_fails_too` |
| B35 | Atlas holds `pkg-a` and gets it from a repository this run cannot reproduce, so `pkg-a` is reported rather than installed; the user approves the hold | The hold fails alone, saying `pkg-a` is not on Vega and the run cannot reproduce the repository it comes from | — | none |
| B36 | Atlas holds `pkg-a`; Vega already has `pkg-a`, so there is no install item at all | The hold applies normally — the guard must not touch the ordinary case | U | `test_apt_packages:TestAHoldNeedsItsPackage::test_a_hold_on_a_package_the_target_already_has_still_runs` |
| B37 | A run in which one hold was declined for any of B31–B33 | The run's report separates "not applied, by the user's answer" from failed items, and the job's outcome is not a failure | U | `test_apt_packages:TestAHoldNeedsItsPackage::test_a_hold_whose_install_was_skipped_is_declined_not_failed` (asserts no record at ERROR or above) |

## B.7 Ordering against the package's own install (article: PKG-FR-APT-HOLD-VERSION / PKG-FR-APT-HOLD-INERT — the hold must follow what it freezes)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| B38 | `pkg-a` missing on Vega and held on Atlas; both items approved; the run has a repository item that is left unapproved | The install runs before the hold | U | `test_apt_packages:TestInstallBeforeHoldOrdering::test_hold_follows_install_on_the_plain_plan_sort_path` |
| B39 | Same, but the run also writes a repository, so `/etc/apt` work is scheduled ahead of the packages | Key, then `apt-get update`, then the install, then the hold | U | `test_apt_packages:TestInstallBeforeHoldOrdering::test_hold_follows_install_on_the_accept_review_reorder_path` |
| B40 | A bookkeeping hold on Vega for a package this run installs | Clear, install, hold — and the plan-time/apply-time rehearsal also comes after the clear | U | `test_apt_packages:TestAStaleTargetHoldDoesNotStrandThePackage::test_the_stale_hold_is_cleared_the_package_installed_and_the_hold_restored` |
| B41 | Atlas holds `pkg-a` but does not have it in its manual set, while Vega has `pkg-a` manually; the user approves both the removal and the hold | `pkg-a` is removed from Vega and no hold is left recorded for it | — | none; the hold is applied after the removal. See Notes. |

## B.8 Which machine records a declined hold (articles: PKG-FR-APT-HOLD-ITEM "both when it is added and when it is removed", with PKG-FR-MACHINE-SPECIFIC)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| B42 | The hold add for `pkg-a` is marked machine-specific | The mark is written on Atlas (the machine that holds the hold), not on Vega, and `pkg-a`'s hold is never offered again | U V | `test_block_state_decisions:TestAptHoldDecisions::test_declined_hold_is_recorded_on_source_and_never_re_offered`; `test_package_sync:TestBlockStateDecisionRoundTrip::test_skip_always_on_an_apt_hold_is_inert_next_run` |
| B43 | The unhold for `pkg-a` (a real hold on Vega) is marked machine-specific | The mark is written on Vega, not on Atlas, and the unhold is never offered again | U | `test_block_state_decisions:TestAptHoldDecisions::test_declined_unhold_is_recorded_on_target_and_never_re_offered` |
| B44 | The same decision file content is present on the machine that does *not* hold the item | The hold is still offered — the mark only counts on its holding machine | U | `test_block_state_decisions:TestAptHoldDecisions::test_recorded_hold_is_read_back_from_the_machine_that_holds_it_only` |
| B45 | An unhold was marked machine-specific; the held package's version still differs between the machines | The held package's upgrade is not re-proposed in a later run — silencing the unhold must not un-silence the package | U | `test_block_state_decisions:TestAptHeldPackageSuppression::test_declined_unhold_does_not_re_propose_the_held_packages_upgrade` |
| B46 | One hold among several is marked machine-specific | Only that one goes quiet; the other holds keep being offered | U | `test_block_state_decisions:TestAptHeldPackageSuppression::test_unrelated_recorded_decision_leaves_the_hold_set_intact` |
| B47 | A marked hold add, on a later run whose review answers everything "apply" | The hold still does not land on Vega | V | `test_package_sync:TestBlockStateDecisionRoundTrip::test_skip_always_on_an_apt_hold_is_inert_next_run` |

## B.9 Reading the hold sets (overlaps ADR-022; listed here because the reads are hold-specific)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| B48 | Both machines' hold sets are read | Each machine's holds are attributed to that machine | U | `test_apt_probe:TestHoldPinCapture::test_hold_sets_from_both_machines_surface` |
| B49 | The hold read fails or is refused on either machine | The job fails naming the command, rather than treating the silence as "holds nothing" | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_hold_read_that_did_not_answer_fails_the_job` |
| B50 | A machine genuinely holds nothing | That is data, not a failure | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_an_empty_hold_set_is_data_not_a_failure` |
| B51 | Vega holds nothing and the run found no target-only repository | Vega's installed-package set is never read (the real/bookkeeping split has nothing to decide) | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_machine_holding_nothing_is_never_asked_what_it_has_installed` |

## Gaps

**B7 — package approved, hold declined.** A unit test can assert it with mocks: source `apt-mark showhold` returns `pkg-a`, target lacks it, review answers `apt:package:pkg-a` APPLY and `apt:hold:pkg-a` SKIP_ONCE; assert the version-pinned install ran and no `apt-mark hold` did. Same fixture shape as `TestAHeldPackageIsInstalledAtTheSourcesVersion`.

**B14 — a target-only package the target holds.** Unit, pure diff: `diff_apt_packages([], [AptPackageItem("pkg-a", "1.0")], {}, MACHINES, frozenset(), frozenset({"pkg-a"}))` must yield exactly `("apt:hold:pkg-a", REMOVE)` and no `EXTRA_ON_TARGET` removal. Verified by reading `diffing.py` that this is what happens; nothing asserts it.

**B18 — a bookkeeping hold on a package neither machine has.** Unit, pure diff: same call with empty source and target item lists and `target_stale_holds={"pkg-a"}`; expect the unhold item alone.

**B25 — the "offers no other" wording.** Unit: same shape as `test_a_version_the_target_cannot_supply_fails_naming_both`, but the target's `apt-cache policy` answers with `Candidate: (none)` or a non-zero exit. Assert the failure names Atlas's version and says Vega offers no other. The parser side is covered; the message is not.

**B24 — no fallback install.** Strengthen the existing assertion from "the pinned command did not run" to "no command containing `apt-get install` and `pkg-a` ran". Unit, one line.

**B27 — a hold surviving its package's machine-specific mark (‼).** `filter_inert` drops a marked package from the source manifest, but the hold sets are read raw from `apt-mark showhold` and `_drop_inert_diffs` only consults the hold item's *own* id. So a package marked machine-specific on Atlas still produces a hold item, and `PackageConverger._hold_refusal` finds no package diff, reads that as "the target already has it", and applies `apt-mark hold` on Vega for a package Vega does not have — the bookkeeping hold `PKG-FR-APT-HELD-TARGET` calls out as harmful. Unit-testable: record `apt:package:pkg-a` in the source decision file, hold `pkg-a` on the source, target lacks it, approve the hold, assert no `apt-mark hold` reaches the target. Fixing it means either withholding the hold item when its package is marked, or having `_hold_refusal` consult the target's installed set rather than inferring presence from the absence of an item.

**B28 — a held package with no captured version (‼, narrow).** `AptSyncJob._plan_packages` builds the version pin only `if name in source_versions`, and `source_versions` skips any item with a falsy version. A held package whose captured version is empty is therefore installed unpinned — the "MUST NOT fall back to another version" case, reached by a different route than B24. Unit-testable by returning an empty version field from the source `dpkg-query` stub. Low likelihood (the capture read is itself guarded), so this may be judged acceptable rather than fixed — but it is currently silent either way.

**B29 — hold replication with a version difference.** Unit: Atlas `pkg-a` 1.0 held, Vega `pkg-a` 2.0 unheld. Assert the plan carries both a `VERSION_MISMATCH` report and the hold item, and that applying both issues only `apt-mark hold` — no install, upgrade or downgrade. This is the article's own first sentence and nothing asserts it in the version-differing shape.

**B33 — declined by a late collateral answer.** Reachable today (`PackageConverger._declined_installs` is set by `install` when `LateCollateral.declined` fires, and `_hold_refusal` checks it first), but no test combines a held package with the late question. Unit: take `_late_collateral_context` from `test_apt_collateral.py`, add `apt-mark showhold` returning `pkg-a` on the source, approve `apt:package:pkg-a` and `apt:hold:pkg-a`, and answer the late collateral question "keep the package". Assert no `apt-mark hold`, no failed item, and a log line saying the hold was not applied because its install was withdrawn to keep a package on Vega. Mocks suffice.

**B35 — repository unreproducible.** Unit: source holds `pkg-a`, the source's own origin for it is declared by no repository the source has (the `REPO_UNAVAILABLE` fixture already exists in `test_apt_diffing`), and the hold is approved. Assert one failed item, `apt:hold:pkg-a`, whose message says the repository cannot be reproduced, and that the package itself is reported rather than failed. This is one of the two grounds the article says must *fail*, and it is the one with no coverage.

**B41 — hold applied after an approved removal.** See Notes; needs a decision before a test is written.

**No VM coverage of hold replication at all.** The only integration test touching apt holds is the skip-always round trip (B42/B47). Nothing on a real machine asserts that a hold replicates, that a held package is installed at the source's exact version, or that a bookkeeping hold is cleared and re-registered. These need a VM: the whole design rests on measured `apt-mark`/`apt-get` behaviour (exit 0 for an uninstalled package, `E: Held packages were changed`, `E: Version 'x' was not found`), and mocks assert only that we send the commands we think we send. Highest value: (a) Atlas holds a package Vega lacks at a version Vega can still obtain → Vega ends with that exact version and the hold; (b) the same where Vega can only obtain a different version → one failed item naming both versions and no package installed.

## Notes for the assembler

- **B20/B21 overlap area D.** The `--allow-change-held-packages` rehearsal flag is collateral machinery, but it exists only because of bookkeeping holds and is meaningless without them. Placed here; drop if area D claims it.
- **B48–B51 overlap the read-integrity area (ADR-022).** Kept because the article's whole real/bookkeeping distinction depends on which set the read produced.
- **B41 is genuinely ambiguous.** A hold add and a package removal for the same name can both be approved in one run (the source can hold a package that is not in its manual set, so the source manifest lacks it while the hold set carries it). The requirements say the hold is decided separately from its software, which makes approving both legitimate; they do not say what the result should be. The code applies the removal and then the hold, leaving Vega holding a package it no longer has — precisely the bookkeeping state `PKG-FR-APT-HELD-TARGET`'s rationale calls harmful. Either the articles should say the add direction is refused when the same run removes the package, or the guard in `PackageConverger._hold_refusal` should cover the REMOVE-direction package item as well as the INSTALL-direction one. Flagged rather than resolved.
- **`PKG-FR-APT-HOLD-INERT`'s four outcomes map to three code sites**, not four: the review decline and the plan-time collateral decline are one branch (the collateral answer is rewritten into an ordinary skip before converge sees it), the late collateral decline is a second, and the two failure grounds are a third and fourth. B31/B32 therefore assert the same code path from two user-visible situations; both rows are kept because the article names both situations and a reviewer sets them up differently.
- **Rows split from the old matrix's single "hold replicates" line:** B1–B4 (the four membership combinations), B16–B18 (the bookkeeping-hold combinations), B31–B36 (the outcome fan-out). Nothing was merged.
- The `PKG-FR-BLOCKS-REPLICATE` snap and flatpak halves are areas E and F; only the apt clause is enumerated here.
