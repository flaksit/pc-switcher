# Sweep — D: collateral damage from an approved apt change

Machines: `Atlas` (source), `Nomad` (target). Test modules are named by their symbol path; `apt/collateral` = `tests/unit/jobs/apt/test_apt_collateral.py`, `apt/packages` = `tests/unit/jobs/apt/test_apt_packages.py`, `apt/probe` = `tests/unit/jobs/apt/test_apt_probe.py`, `review` = `tests/unit/jobs/test_package_review.py`, `state` = `tests/unit/jobs/test_package_state.py`, `skip_always` = `tests/unit/jobs/test_review_skip_always.py`.

Code lives in `src/pcswitcher/jobs/apt_sync/` (not `jobs/apt/` — the package was renamed away from the brief's path): `collateral.py` (`Collateral`, `LateCollateral`), `packages.py` (`PackageConverger.install`/`.remove` guards), `messages.py` (group title, trigger phrase), `review.py` (`_collateral_options`, `_review_collateral_group`).

## D.1 Collateral that touches only automatically-installed packages (articles: PKG-FR-COLLATERAL-AUTO)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| D1 | Nomad's apt would remove `auto-dep` — a package apt installed automatically there — to install an approved `pkg-a` | No question anywhere in the review; no collateral entry; `pkg-a` stays an ordinary approvable install | U | apt/collateral:`TestPlanTimeCollateral::test_auto_collateral_removal_produces_no_review_item` |
| D2 | Same run reaches apply | The real install runs; the guard does not refuse it | U | apt/packages:`TestTransactionGuard::test_install_whose_only_collateral_is_auto_deps_proceeds` |
| D3 | Same run's log is read afterwards | A line names `auto-dep`, the change (`would remove auto-dep`), and that it is installed automatically on Nomad so nobody was asked | U | apt/collateral:`TestAutoCollateralIsLogged::test_auto_collateral_removal_is_named_in_the_log` |
| D4 | An approved removal of `pkg-a` also removes the auto-installed `pkg-b` | The removal runs; no question | U | apt/packages:`TestRemovalGuard::test_auto_reverse_dep_removal_proceeds` |
| D5 | Same, log read afterwards | A line names `pkg-b` and the removal that took it (`Removing …`) | — | nothing asserts the `Removing` direction's auto log line |
| D6 | An approved install would downgrade `auto-dg`, automatically installed on Nomad | No collateral entry, no question | U | apt/collateral:`TestPlanTimeCollateral::test_manual_downgrade_becomes_item_auto_downgrade_does_not` |
| D7 | Same at apply time (the drift case: the real transaction downgrades an auto package) | The install proceeds, no refusal | U | apt/packages:`TestDowngradeGuard::test_guard_allows_auto_downgrade` |
| D8 | An approved install would change auto-installed `auto-dep` from 1.0 to 2.0 | Log names both versions; the run spends no `dpkg --compare-versions` on it (direction is on the page without a command) | U | apt/collateral:`TestAutoCollateralIsLogged::test_an_auto_version_change_is_logged_without_a_version_comparison` |
| D9 | The real (apply-time) transaction drifts and takes an auto package plan time did not predict | That change is named in the log too — it is the transaction that actually happened | — | `Collateral.unapproved` calls `_log_auto`; no test reads the log on an apply-time path |

## D.2 Collateral that touches a package installed by hand on Nomad (articles: PKG-FR-COLLATERAL-MANUAL)

### The question exists and says the three things

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| D10 | Approved install of `pkg-a` would remove `other-manual`, which Nomad's apt has marked manually installed | Exactly one collateral item, in its own group, not on the install checkbox screen; `pkg-a` remains an approvable install | U | apt/collateral:`TestPlanTimeCollateral::test_manual_collateral_removal_becomes_a_collateral_review_item` |
| D11 | Approved install would downgrade `manual-dg` 2.0 → 1.0 | Its own item, worded as a downgrade | U | apt/collateral:`TestPlanTimeCollateral::test_manual_downgrade_becomes_item_auto_downgrade_does_not` |
| D12 | Approved install would upgrade `manual-up` 1.0 → 2.0 | Its own item: "Installing pkg-a on Nomad would upgrade manual-up from 1.0 to 2.0" — an unasked-for upgrade is the same imposition as a downgrade | U | apt/collateral:`TestCollateralUpgrade::test_manual_upgrade_becomes_a_collateral_item` |
| D13 | Approved removal of `going` would also remove the manually-installed `victim` | Its own item naming the removal as the cause | U | apt/collateral:`TestOnePackageTwoConsequences::test_each_consequence_is_its_own_item_with_its_own_cause` |
| D14 | apt reports a "version change" for a protected package whose old and new versions compare equal | No item, no question — nothing is actually moved | — | `Collateral.classify` returns early on `order == 0`; nothing asserts it |
| D15 | The question is read | It names the affected package and what the approved change would do to it, as the first line of the detail | U | apt/collateral:`TestPlanTimeCollateral::test_manual_collateral_removal_becomes_a_collateral_review_item`, `TestTheReasonNamesTheGroundThatApplies::test_a_manually_installed_package_says_apt_has_it_marked_manual` |
| D16 | Same question, second line | It says why the package is protected — that apt on Nomad has it marked manually installed, i.e. something asked for it there rather than it arriving as a dependency | U | apt/collateral:`TestTheReasonNamesTheGroundThatApplies::test_a_manually_installed_package_says_apt_has_it_marked_manual` |
| D17 | Several collateral packages in one run, protected on different grounds | One heading over all of them, naming both grounds ("installed on Nomad or marked as its own") | U | apt/collateral:`TestTheReasonNamesTheGroundThatApplies::test_the_group_title_names_both_grounds` |
| D18 | Two collateral packages in one group | One decision screen each — the causes and effects differ per item | U | review:`TestCollateralGroupResolution::test_each_package_gets_a_decision_screen_of_its_own` |

### The three answers

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| D19 | The screen is shown | Three answers: go ahead, keep the package, stop the sync — and only those three; no "never offer again" | U | skip_always:`TestGroupsNeverOfferedPermanence::test_collateral_group_is_never_offered_permanence` |
| D20 | The go-ahead answer is read | It states its own effect, naming the causing change and the consequence: "install sl on nomad, so fortunes is removed as well"; the word on the row is the effect verb (`remove`/`downgrade`/`upgrade`) | P | review:`TestCollateralPromptWording::test_every_answer_names_the_machine_and_its_own_effect` asserts the rendering of hand-written hints; nothing asserts that `Collateral._item` composes those sentences |
| D21 | The keep answer is read | It states its own effect: keep the package on Nomad, the causing change will not be installed/removed, will be asked again next sync | P | same as D20 — the item's own `answer_hints` are never asserted |
| D22 | The stop answer is read | It says how far it reaches: nothing more is changed on Nomad, and what earlier jobs already did stays done | U | review:`TestCollateralPromptWording::test_every_answer_names_the_machine_and_its_own_effect` |
| D23 | The user answers "go ahead" | The causing install runs, and the apply-time guard lets the collateral removal through | U | apt/collateral:`TestCollateralFlow::test_install_anyway_proceeds_and_guard_allows_the_collateral_removal` |
| D24 | The user answers "keep the package" | The causing install is left unapplied — no install command is issued | U | apt/collateral:`TestCollateralFlow::test_skip_leaves_the_triggering_install_unapproved` |
| D25 | Same run's outcome is read | The withdrawn change is reported as not applied, not as a failure ("leaving the changes unapplied rather than failing later") | P | apt/packages:`TestAHoldNeedsItsPackage::test_a_hold_whose_install_a_collateral_answer_cancelled_is_declined_too` asserts no ERROR-level record; no test asserts the item's own status on the plan-time path |
| D26 | The user answers "stop the sync" | `SyncAbortedByUser` naming the package and Nomad; the whole sync ends, not just the apt job | U | review:`TestCollateralGroupResolution::test_abort_raises_sync_aborted_by_user_naming_the_collateral_package`, `TestCollateralPromptWording::test_stopping_names_the_package_and_the_machine_in_the_abort` |
| D27 | A collateral package name contains bracket characters | The screen renders; no Rich markup error | U | review:`TestCollateralGroupResolution::test_bracketed_collateral_label_renders_without_markup_error` |
| D28 | The run has no interactive terminal | No screen is drawn; the entry comes back skipped for this run; it is not flagged unresolved | U | review:`TestCollateralGroupResolution::test_non_interactive_collateral_entries_skip_once_and_are_not_unresolved` |

### What counts as "manually installed on Nomad"

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| D29 | `src-only` is manual on Atlas and automatic on Nomad; an approved install would remove it | No question — the target's own apt owns it (deliberate loss under ADR-020 D-40) | U | apt/collateral:`TestSourceOnlyCollateral::test_source_only_manual_collateral_removal_is_not_a_review_item` |
| D30 | Same, at apply time | The guard also lets it go | U | apt/collateral:`TestSourceOnlyCollateral::test_apply_time_guard_allows_source_only_manual_collateral` |
| D31 | `code` was installed on Atlas from a bare `.deb` (dropped from the manifest) and is automatic on Nomad; an approved install would remove it | No question, no item | U | apt/probe:`test_an_excluded_bare_deb_package_is_not_protected_from_collateral` |
| D32 | Nomad's `apt-mark showmanual` (the protection read, the second of the two) does not answer | The job fails rather than proceeding with an empty protection set, which would classify every collateral package as automatic | U | apt/probe:`test_a_collateral_protection_read_that_did_not_answer_fails_the_job` |

### A package that is itself under review for removal

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| D33 | `old-tool` is on Nomad only (so it is a removal candidate) and manually installed there; installing approved `pkg-a` would take it | The user is still asked — being offered for removal is not consent to be removed. Its own removal item is untouched | U | apt/collateral:`TestRemovalCandidateKeepsItsProtection::test_a_removal_candidate_taken_by_an_install_is_still_asked_about` |
| D34 | The user skips `old-tool`'s removal for this run and keeps it at the collateral question | Neither the install nor the removal runs | U | apt/collateral:`TestRemovalCandidateKeepsItsProtection::test_skipping_that_removal_leaves_the_install_unapplied` |
| D35 | `old-tool` is a removal candidate and the removal batch's own rehearsal reports it | It is not collateral of its own batch — no question about itself | U | apt/collateral:`TestRemovalCandidateKeepsItsProtection::test_a_removal_candidate_is_not_collateral_of_its_own_batch` |
| D36 | The user APPROVED removing both `pkg-a` and `pkg-b`; removing `pkg-a` also removes `pkg-b` | Both removals run — an approved removal exempts its package from the protection | U | apt/packages:`TestRemovalGuard::test_both_removals_approved_the_first_proceeds` |
| D37 ‼ | The user SKIPPED `pkg-b`'s removal for this run; the approved removal of `pkg-a` would carry `pkg-b` off anyway | The requirement says a skipped candidate keeps its protection, so the user must be ASKED. The tool tells instead: `plan_time` exempts every removal candidate from the removal batch, so no question is built, and `PackageConverger.remove`'s guard refuses `pkg-a`'s transaction naming `pkg-b`. Nothing is lost; the answer is never offered. Accepted cost: closing it needs one `apt-get --dry-run` per candidate on every run with removals | — | no test sets this up at all — neither the refusal nor the missing question |
| D38 ‼ | Nomad holds `pkg-a` without having it installed, and `pkg-a` is in this run's install batch | `Collateral.plan_time` rehearses with `--allow-change-held-packages`, which the real install withholds, so apt may report changes to OTHER held packages that the real command would refuse outright — the question can be about collateral that would never happen. Accepted: over-asks, never under-asks | U (for the flag; the over-ask itself is unasserted) | apt/collateral:`TestTheRehearsalSurvivesAStaleTargetHold::test_the_install_rehearsal_asks_apt_to_allow_the_held_name`, `::test_an_ordinary_run_never_asks_for_it` |

### The guard behind the question

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| D39 | The real install's transaction drifts after plan time and would remove a manually-installed package nobody saw | The install is refused as its own failed item naming that package; no install command runs | U | apt/packages:`TestTransactionGuard::test_guard_refuses_drifted_manual_removal_not_seen_at_plan_time` |
| D40 | The real removal's transaction drifts and would remove a manually-installed package nobody saw | The removal is refused naming that package | U | apt/packages:`TestRemovalGuard::test_drifted_manual_reverse_dep_removal_refused` |
| D41 | The real transaction would downgrade a manually-installed package nobody saw | Refused naming the package | U | apt/packages:`TestDowngradeGuard::test_guard_refuses_drifted_manual_downgrade` |

## D.3 The collateral package is marked machine-specific (articles: PKG-FR-COLLATERAL-MARKED)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| D42 | `vendor-tool` is marked as Nomad's own and is NOT in Nomad's manual set; an approved install would remove it | A question is asked, and it says explicitly that the package is marked as Nomad's own — and claims nothing about apt's bookkeeping | U | apt/collateral:`TestTheReasonNamesTheGroundThatApplies::test_a_package_only_a_mark_protects_says_so_and_claims_nothing_about_apt` |
| D43 | Same, with `ghost-tool` marked and automatic on Nomad | The mark alone protects it; the collateral group exists | U | state:`TestDecisionScopeReachesCollateral::test_a_mark_protects_a_package_apt_considers_auto_installed` |
| D44 | `vendor-tool` is both marked as Nomad's own and in Nomad's manual set | The question states both grounds and says either alone would protect it | U | apt/collateral:`TestTheReasonNamesTheGroundThatApplies::test_a_package_both_grounds_cover_states_both` |
| D45 | A marked package appears in a run | It produces no diff and no review line of its own anywhere — the collateral question is the only place its mark is ever named | U | state:`TestDecisionScopeReachesCollateral::test_manual_set_membership_protects_the_same_item_on_its_own` |
| D46 | The user answers "never offer again" to `pkg-y`'s removal earlier in THIS run, then an approved removal's cascade would take `pkg-y` | The mark counts from that moment: `pkg-y` is protected, and the transaction that would take it is refused rather than proceeding | — | `Collateral.resolve` builds `_run_marked` from same-run `SKIP_ALWAYS` removal decisions and `protected()` unions it in; no test drives a transaction after such a mark |
| D47 ‼ | Same as D46, but read as a question rather than a guard | The article wants the mark to count; the tool counts it only at the apply-time guard. Plan-time items — and therefore every question and every "why it is protected" sentence — are built before any answer exists, and `Collateral._reason` deliberately does not consult `_run_marked`. So a same-run mark yields a refusal, never a question naming the mark | — | inferred from `Collateral._reason`'s docstring and `resolve`'s ordering; no test, and the criteria's gap list does not name this one |

## D.4 Which approved changes a decline cancels (articles: PKG-FR-COLLATERAL-ATTRIBUTION)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| D48 | Two approved removals, `pkg-x` and `pkg-y`; only `pkg-x`'s own transaction takes `other-manual`. The user keeps `other-manual` | Only `pkg-x` is cancelled; `pkg-y` is still removed | U | apt/collateral:`TestCollateralAttribution::test_skip_cancels_only_the_candidate_whose_transaction_causes_it` |
| D49 | Same setup, question read before answering | The question names `pkg-x`, not "the selected packages" | U | apt/collateral:`TestCollateralAttribution::test_the_narrowing_names_the_causing_candidate_in_the_question` |
| D50 | Neither `pkg-x` nor `pkg-y` alone drops `other-manual`; only both together do. The user keeps `other-manual` | The whole set is cancelled — neither removal runs | U | apt/collateral:`TestCollateralAttribution::test_collateral_no_single_candidate_reproduces_is_blamed_on_the_whole_batch` |
| D51 | Same, question read before answering | The question says so: it refers to the whole batch ("the packages listed earlier") rather than naming one package | U | apt/collateral:`TestCollateralAttribution::test_joint_causation_names_the_whole_batch_rather_than_one_package` |
| D52 | A run whose batched rehearsal finds no manual collateral | No per-candidate narrowing is paid for — one rehearsal per direction | U | apt/collateral:`TestCollateralAttribution::test_a_clean_batch_costs_no_extra_rehearsal`, `TestPlanTimeCollateral::test_at_most_two_apt_get_dash_s_commands_regardless_of_package_count` |
| D53 | A run whose batch found manual collateral, with a single candidate in that direction | The single candidate is its own answer and is not rehearsed a second time | — | `for_direction` guards the narrowing with `len(candidates) > 1`; no test counts rehearsals on a single-candidate collateral run |
| D54 | `victim` is manually installed on Nomad; installing approved `pkg-a` takes it AND removing approved `going` takes it | Two separate questions, each naming its own cause and its own consequence | U | apt/collateral:`TestOnePackageTwoConsequences::test_each_consequence_is_its_own_item_with_its_own_cause` |
| D55 | The user lets the install's consequence go ahead and keeps `victim` against the removal's | The install runs; the removal is cancelled. Consenting to one consequence does not exempt the package from the other | U | apt/collateral:`TestOnePackageTwoConsequences::test_letting_the_installs_casualty_go_ahead_does_not_release_the_removals` |
| D56 | Same, but the removal's transaction only drifts onto `victim` after plan time, so nothing cancelled it | The apply-time guard refuses the removal naming `victim`, while the install whose consequence WAS consented to still runs — the consent is matched on the consequence, not the package | U | apt/collateral:`TestOnePackageTwoConsequences::test_the_apply_time_guard_matches_the_consequence_not_the_package` |
| D57 | A consequence already let go ahead at plan time comes up again in the late (post-`/etc/apt`) question | It is not asked twice — the id is the consequence, so the earlier answer covers this cause | — | `LateCollateral.ensure_asked` filters on `Collateral.approved`; the late tests never seed a plan-time approval for the same id |
| D58 | A consequence DECLINED at plan time recurs as a late question about different changes | It is asked again — the earlier answer cancelled the changes it was about, and these are other changes | — | same code path; no test |

## D.5 A cancellation must not overwrite a decision the user gave (articles: PKG-FR-COLLATERAL-KEEPS-MARKS)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| D59 | The user answers "never offer again" to `pkg-y`'s removal, and a kept collateral package cancels `pkg-y` too (both candidates really cause it) | `pkg-y` is not removed, and its machine-specific mark is still recorded on Nomad | U | apt/collateral:`TestCollateralAttribution::test_a_collateral_skip_does_not_discard_a_trigger_own_skip_always` |
| D60 | Same mark on a package that is NOT a trigger of the collateral | Untouched — nothing about it is re-decided, and the mark is recorded | U | apt/collateral:`TestCollateralAttribution::test_a_collateral_skip_does_not_discard_an_unrelated_skip_always` |
| D61 | A change the user already declined is among the collateral's triggers | It is not re-decided; only an APPLY is ever overridden | P | covered only through D59 (the `SKIP_ALWAYS` case). A trigger already `SKIP_ONCE` is unobservable — overriding it to `SKIP_ONCE` is a no-op |

## D.6 When the question is asked (articles: PKG-FR-COLLATERAL-MANUAL with PKG-FR-ASK-AGAIN, PKG-FR-BATCHED, PKG-FR-CONSENT-BEFORE-CHANGE, PKG-FR-NO-TERMINAL, PKG-FR-LOG-DECISIONS)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| D62 | Every approved install is a package Nomad's apt can already resolve | The collateral question is put while planning, in the one review; the converge loop asks nothing more and issues no extra rehearsal | U | apt/collateral:`TestCollateralForARepositoryThisRunWrites::test_the_question_costs_nothing_on_a_run_with_no_late_install` |
| D63 | `pkg-a` comes from a repository this run writes, so Nomad's apt has never heard the name | Nothing is asked at plan time and no rehearsal is issued — the facts genuinely do not exist yet | U | apt/collateral:`TestCollateralForARepositoryThisRunWrites::test_the_question_is_absent_from_the_plan_time_review` |
| D64 | Same run, once `/etc/apt` has converged and the run's `apt-get update` has run | The question is put then, with the same three answers and the same heading, in a second review round | U | apt/collateral:`TestCollateralForARepositoryThisRunWrites::test_keeping_the_package_leaves_the_install_unapplied_and_unfailed` (asserts exactly two review calls, the second carrying the collateral entry) |
| D65 | Two such installs in one run | The question is asked ONCE, over both together, before the first of them converges — no package transaction has happened when the last of them is answered | P | `LateCollateral.ensure_asked` is idempotent by design; `_two_late_installs_context` exists but the run it drives withdraws `pkg-a`, so nothing asserts the ordering against a surviving install |
| D66 | The user keeps the package at the late question | The install does not run, the package survives, and the job reports no failed item | U | apt/collateral:`TestCollateralForARepositoryThisRunWrites::test_keeping_the_package_leaves_the_install_unapplied_and_unfailed` |
| D67 | The user lets it go ahead at the late question | The install runs and the guard allows the collateral removal | U | apt/collateral:`TestCollateralForARepositoryThisRunWrites::test_going_ahead_installs_and_the_guard_allows_the_collateral_removal` |
| D68 | The user stops the sync at the late question | The whole sync ends — the stopping answer reaches as far mid-apply as it does at plan time — and no install runs | U | apt/collateral:`TestCollateralForARepositoryThisRunWrites::test_stopping_ends_the_whole_sync` |
| D69 | The late question is reached on a run with nobody to ask | Every such item is declined for this run: the install is withheld, not pushed through and not failed | U | apt/collateral:`TestCollateralForARepositoryThisRunWrites::test_a_run_with_no_terminal_declines_it` |
| D70 | The late question's answer is read back from the log | The log names the item and the answer it got ("reviewed other-manual (collateral): skip now"), and names the install that was not applied — the plan's own decision pass cannot see this question | U | apt/collateral:`TestCollateralForARepositoryThisRunWrites::test_the_decision_is_named_in_the_log` |

## Gaps

D5 — the auto-collateral log line in the REMOVE direction. `Collateral._log_auto` is called from `for_direction` for both verbs and from `unapproved` at apply time, but only the install direction's line is asserted. Unit-testable: the existing removal fixtures plus a `caplog` assertion for `Removing … would remove …`.

D9 — auto collateral produced by the REAL transaction (apply time). `unapproved` logs it, and this is the transaction that actually happened, so it is the one the log most needs. Unit-testable: reuse `apt/packages:TestTransactionGuard::test_install_whose_only_collateral_is_auto_deps_proceeds` and assert the log record.

D14 — a reported version change that compares equal produces no item. Unit-testable: an `Inst manual-x [1.0] (1.0)` line plus a `dpkg --compare-versions` fixture returning equality; assert no `apt:collateral:` diff and no question.

D20, D21 — the act and keep sentences the collateral item itself composes (`Collateral._item`'s `answer_hints`). Today the review renders them faithfully and `Collateral` builds them, but no test connects the two: `review:TestCollateralPromptWording` feeds hand-written hints. The requirement "each of those three MUST state its own effect" is therefore asserted about strings no production path produces. Unit-testable: `plan()` the `_manual_collateral_context` fixture and assert the collateral diff's `answer_hints` pair verbatim, for an install cause and for a removal cause (the preposition and the verb both flip: "install … on Nomad" vs "remove … from Nomad", "will not be installed" vs "will not be removed").

D25 — the plan-time keep answer's outcome status. Asserting "unapplied, not failed" needs the job's own reported outcome for the withdrawn install, not just the absence of a command. Unit-testable: assert no `PackageItemFailures` and that the item is reported skipped.

D37 ‼ — accepted gap 1 (named in the criteria). Two things are unasserted: that the guard refuses and names the package, and that no question was asked. The refusal half is unit-testable now (two removal candidates, one approved and one skipped, batch rehearsal exempting both, per-item rehearsal showing the cascade); write it so the day the gap closes the test flips from "refused by name" to "asked about".

D38 ‼ — accepted gap 2 (named in the criteria). The flag's presence and absence are covered; the over-ask itself — a question about another held package that the real install would refuse outright — is not. A unit test can assert it (rehearsal output naming a second held manual package → a collateral item exists), and doing so pins the accepted cost rather than leaving it as prose.

D46 — a same-run "never offer again" mark protecting its package from a later transaction's cascade. This is the operative half of PKG-FR-COLLATERAL-MARKED's second sentence and nothing exercises it. Unit-testable: `pkg-y` removal answered `SKIP_ALWAYS`, `pkg-x` removal approved, `pkg-x`'s apply-time rehearsal showing `Remv pkg-y`; assert `pkg-x`'s removal is refused naming `pkg-y`. Note `_run_marked` is built only from `REMOVE`-action package diffs — an `INSTALL` item's `SKIP_ALWAYS` is recorded on Atlas and must not appear.

D47 ‼ — a same-run mark never reaches a question, only the guard. Whether this satisfies "a mark recorded earlier in the same run MUST count" is a judgement call for the requirements owner: the protection counts, the explicit "this package is marked machine-specific" wording cannot, because items are built before answers exist. Same shape as D37 (told, not asked) but not listed among the accepted gaps. Flagging rather than resolving.

D53 — no second rehearsal for a single candidate. Unit-testable by counting `apt-get --dry-run` calls on a one-candidate run that found manual collateral (the existing `_manual_collateral_context`): expect exactly one.

D57, D58 — the late question against a plan-time answer for the same consequence id. Two tests, both unit-testable on `_late_collateral_context` plus a plan-time collateral item for the same package: (a) seed an `APPLY` for the id at plan time and assert the late round asks nothing; (b) seed a `SKIP_ONCE` and assert the late round asks again. (b) is the subtler one — a decline is about specific changes, and the late changes are different ones.

D61 — a trigger already declined is not re-decided. Only the `SKIP_ALWAYS` case is observable; the `SKIP_ONCE` case is a no-op by construction. No test needed unless `resolve` ever gains a decision that would be destroyed by an override.

D65 — the late question is asked once, before the first install command. Unit-testable: two late installs both let go ahead, and assert the reviewer was called before any `sudo … apt-get install` appears in the target's call sequence (the mock records order).

None of these need a VM: every branch here turns on what `apt-get --dry-run` printed and on the target's `apt-mark showmanual` set, both of which the existing mock fixtures supply exactly. A VM test would add value only for the two accepted gaps (D37, D38), where the point is what REAL apt does that the rehearsal does not — real cascade behaviour and real `--allow-change-held-packages` semantics.

## Notes for the assembler

Integration coverage is zero for this whole area. `tests/integration/jobs/test_package_sync.py` mentions collateral once, in a comment about apt_sync's own guards, and asserts nothing about it. Every `V` cell in this section would be new work.

Crossings, deliberately left to their owners:
- A hold whose install a collateral answer withdrew is area B's. It is covered at both moments — plan-time answer (apt/packages:`TestAHoldNeedsItsPackage::test_a_hold_whose_install_a_collateral_answer_cancelled_is_declined_too`) and late answer (`PackageConverger._declined_installs`, checked first in `_hold_refusal`) — and both report the hold DECLINED, not failed.
- The `/etc/apt` file this run wrote for an install a late collateral answer then withdrew is PKG-FR-REPO-STRANDED, not mine. It is well covered: apt/collateral:`TestARepositoryWrittenForADeclinedInstall::{test_the_repository_is_named_by_url_and_filename, test_it_does_not_read_as_something_broken, test_a_repository_a_surviving_install_still_needs_is_not_named}`. Whoever owns the repository section should take those three rows.
- Which installs are simulable at plan time is `OriginClassifier.target_resolvable` — origin/repository area. D63's "asked late because the repository is not there yet" depends on it.
- The whole-job "no terminal means every package job with a non-empty review is skipped" rule (PKG-FR-NO-TERMINAL) belongs to the review area; D28 and D69 only assert the collateral entry's own behaviour.

Rows I split: PKG-FR-COLLATERAL-MANUAL's single sentence about the three answers became D19–D26, because each answer has its own effect, its own observable outcome, and (for stop) its own reach. PKG-FR-COLLATERAL-ATTRIBUTION's three sentences became D48–D58 because "cancels only the causing changes", "joint causation cancels the set AND says so", and "consent is keyed to the consequence" are independently falsifiable.

Rows I did not place: the article's phrase "the request MUST name the affected package" is asserted only via the detail's first line and the entry label; there is no row for a package name that would need escaping in the DETAIL (as opposed to the label) — D27 covers the label only. Probably not worth a row; noting it in case the assembler disagrees.

Genuinely ambiguous: PKG-FR-COLLATERAL-MARKED's "A mark recorded earlier in the same run MUST count" — see D47. Counting for the guard is clearly required; whether the question must be able to name a same-run mark is not decidable from the text, and the implementation cannot do it without rebuilding items after the review.

Path note for the assembler: the brief says the code is in `src/pcswitcher/jobs/apt/`; it is in `src/pcswitcher/jobs/apt_sync/`. Tests are where the brief says.
