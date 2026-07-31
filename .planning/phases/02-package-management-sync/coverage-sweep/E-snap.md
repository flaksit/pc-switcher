# Sweep — E, snap

Articles decomposed: `PKG-FR-SNAP-SCOPE`, `PKG-FR-SNAP-IDENTITY`, `PKG-FR-SNAP-REVISION`, `PKG-FR-SNAP-CASES`, `PKG-FR-SNAP-CONFINEMENT`, `PKG-FR-SNAP-REMOVE-SNAPSHOT`, `PKG-FR-SNAP-SIDELOAD`, `PKG-FR-SNAP-FAIL-ITEM`, `PKG-FR-SNAP-HOLD`, `PKG-FR-SNAP-REFRESH-PAUSE`, `PKG-FR-SNAP-DATA-BOUNDARY`, `PKG-NG-SNAP-ORIGIN`, and the snap half of `PKG-FR-BLOCKS-REPLICATE`.

Test-module shorthand: `test_snap_sync` = `tests/unit/jobs/test_snap_sync.py`; `test_snap_autorefresh_hold` = `tests/unit/orchestrator/test_snap_autorefresh_hold.py`; `test_block_state_decisions` = `tests/unit/jobs/test_block_state_decisions.py`; `test_folder_sync` = `tests/unit/jobs/test_folder_sync.py`; `test_package_sync` = `tests/integration/jobs/test_package_sync.py`.

## E.1 Scope and identity (articles: PKG-FR-SNAP-SCOPE, PKG-FR-SNAP-IDENTITY, PKG-NG-SNAP-ORIGIN)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| E1 | Atlas has snaps installed; run `snap_sync` | Each snap is captured with its name, its installed revision and the channel it tracks | U | `test_snap_sync:TestCapture::test_capture_source_items_parses_name_rev_tracking_by_header` |
| E2 | Atlas has a classic snap and a devmode snap | The confinement mode of each is captured alongside its revision and channel | U | `test_snap_sync:TestParseConfinement::test_classic_note_sets_item_classic`, `::test_devmode_note_sets_item_devmode` |
| E3 | Atlas has a snap with a per-snap refresh hold set | The hold state is captured as part of that machine's snap state | U | `test_snap_sync:TestParseHeld::test_held_note_sets_item_held` |
| E4 | The same snap name is installed on Atlas and Nomad from listings whose Publisher columns differ | The two are one item; the publisher plays no part in matching them | — | nothing asserts the publisher column is ignored |
| E5 | A full snap review is presented for a plan with installs, changes, removals and holds | No question, group or item ever asks the user where a snap comes from — no store, publisher, remote or key item exists | — | no test asserts the absence of an origin item for snap |
| E6 | A snap item is written to a decision file or shown in the review | Its identity is the name alone (`snap:<name>`), stable across runs | U | `test_snap_sync:TestSnapItem::test_reports_its_item_class` (asserts `item_id == "snap:firefox"`) |

## E.2 Presence, revision and channel cases (articles: PKG-FR-SNAP-CASES, PKG-FR-SNAP-REVISION)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| E7 | `alpha` is on Atlas and not on Nomad | Offered for install on Nomad | U | `test_snap_sync:TestDiff::test_missing_on_target_yields_install_diff` |
| E8 | Approving that install | Nomad gets Atlas's exact revision, named in the command | U | `test_snap_sync:TestNoHold::test_install_command_contains_an_explicit_revision` |
| E9 | Approving that install, where Atlas tracks `latest/edge` | Nomad ends the run tracking `latest/edge` too — the channel is set as part of the install | P | `test_snap_sync:TestNoHold::test_install_change_retrack_and_removal_never_set_a_hold` converges the install but never asserts the following `snap switch --channel=...` |
| E10 | `delta` is on Nomad only | Offered for removal from Nomad, in a group of its own separate from the installs | U | `test_snap_sync:TestDiff::test_extra_on_target_yields_remove_diff_in_its_own_group` |
| E11 | `beta` is revision 20 on Atlas and 15 on Nomad, same channel | One change naming both revisions | U | `test_snap_sync:TestDiff::test_revision_change_yields_change_diff_naming_both_revisions` |
| E12 | Approving E11 | Nomad is moved to revision 20; no channel switch is issued, because the channel already matches | P | `test_snap_sync:TestSideloadedSnaps::test_store_snaps_in_the_same_listing_still_diff_and_converge` asserts the `--revision=20` refresh; nothing asserts the absence of a switch on the success path |
| E13 | `gamma` is revision 30 on both but `latest/edge` on Atlas and `latest/stable` on Nomad | One change naming both channels | U | `test_snap_sync:TestDiff::test_same_revision_different_channel_yields_change_diff_naming_both_channels` |
| E14 | Approving E13 | Nomad is retracked to Atlas's channel and no revision refresh is issued | P | `test_snap_sync:TestHoldAndRevisionFailuresArePerItem::test_unfetchable_revision_is_a_clean_per_item_failure_not_a_crash` asserts `snap switch --channel=latest/stable gamma`; that no `--revision` refresh accompanies it is unasserted |
| E15 | `beta` differs in BOTH revision and channel | One single item, whose line names both pairs of values | U | `test_snap_sync:TestDiff::test_revision_and_channel_both_differing_names_both_pairs` |
| E16 | Approving E15 | Both the revision move and the channel retrack happen for that one item | — | no test converges a both-differ item and asserts both commands |
| E17 | `epsilon` is at the same revision and channel on both machines | No item at all | U V | `test_snap_sync:TestDiff::test_identical_snap_yields_no_diff`; `test_package_sync:TestPackageSyncIdempotency::test_second_consecutive_sync_has_nothing_to_do` (snap revisions in the before/after state witness) |
| E18 | A revision/channel difference reaches the review | It is a change to apply, never a report-only finding (unlike an apt version difference) | U | `test_snap_sync:TestDiff::test_revision_change_yields_change_diff_naming_both_revisions` (asserts `DiffAction.CHANGE`) |
| E19 | A plan carrying a revision/channel change is reviewed | The change group reads as a change, distinct from the install and remove groups | — | `TestHoldReviewVerbs` asserts install/remove/hold/unhold group titles; no CHANGE group title is asserted for snap |
| E20 | Neither machine has any snap | No items, no failure | — | not tested; each empty half is (E17/E26) |
| E21 | A real sync converges a snap divergence between two VMs | Nomad ends on Atlas's revision | V | `test_package_sync:TestPackageSyncWholeRunContracts::test_snap_revision_converges_without_hold` |

## E.3 Reading `snap list` (articles: PKG-FR-SNAP-SCOPE, PKG-FR-READ-FAILS-JOB as it binds snap)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| E22 | `snap list` output in the stock column order | Name, revision, channel and notes read from the columns those headers name | U | `test_snap_sync:TestCapture::test_capture_source_items_parses_name_rev_tracking_by_header` |
| E23 | A future snapd emits the same columns in a different order | Values are still correct — nothing is read by position | U | `test_snap_sync:TestCapture::test_column_reordered_header_still_parses_correctly` |
| E24 | A snap has a retained older revision, whose line is marked `disabled` | Only the active revision becomes an item | U | `test_snap_sync:TestCapture::test_disabled_revision_line_produces_no_item` |
| E25 | The disabled older line ALSO carries `classic` in the same Notes list | It is still skipped; the active classic line is the item | U | `test_snap_sync:TestParseConfinement::test_disabled_classic_line_is_still_skipped` |
| E26 | A machine with no snaps answers "No snaps are installed yet." at exit 0 | Read as an empty machine, not a crash and not a failure; the other machine's snaps are offered for removal | U | `test_snap_sync:TestCapture::test_no_snaps_installed_yields_empty_list_not_a_crash`, `TestAProbeThatDidNotAnswer::test_a_source_with_no_snaps_installed_is_data_not_a_failure` |
| E27 | snapd is unreachable on Atlas and `snap list` exits non-zero | `snap_sync` fails naming the command that did not answer; the silence is never read as "no snaps" | U | `test_snap_sync:TestAProbeThatDidNotAnswer::test_a_source_list_that_did_not_answer_fails_the_job` |
| E28 | Same on Nomad | Same | U | `test_snap_sync:TestAProbeThatDidNotAnswer::test_a_target_list_that_did_not_answer_fails_the_job` |
| E29 | A `snap list` line has fewer columns than the header declares | The line is ignored rather than producing a wrong revision | — | nothing asserts the short-line skip |

## E.4 Confinement (article: PKG-FR-SNAP-CONFINEMENT)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| E30 | Atlas has a classic snap Nomad lacks; the install is approved | Nomad's install carries the classic confirmation, so snapd does not refuse it | U | `test_snap_sync:TestConvergeConfinement::test_install_of_classic_snap_passes_classic` |
| E31 | Atlas has a devmode snap Nomad lacks | The install carries the devmode confirmation and never the classic one | U | `test_snap_sync:TestConvergeConfinement::test_install_of_devmode_snap_passes_devmode_and_never_classic` |
| E32 | Atlas has a strictly confined snap Nomad lacks | The install carries neither confirmation flag | U | `test_snap_sync:TestConvergeConfinement::test_install_of_strict_snap_passes_no_confinement_flag` |
| E33 | A revision change where Atlas's revision is classic and Nomad's current one is strict | The refresh carries Atlas's confinement, not Nomad's | U | `test_snap_sync:TestConvergeConfinement::test_refresh_passes_classic_when_target_is_strict` |
| E34 | The reverse skew: Atlas strict, Nomad classic, revision differs | The refresh carries no confinement flag and Nomad's confinement is left as it is | — | no test converges the reverse skew |
| E35 | Same name, revision and channel on both, but the Notes disagree about confinement | No item — confinement alone is nothing to converge | U | `test_snap_sync:TestConvergeConfinement::test_confinement_difference_alone_produces_no_diff` |

## E.5 Removal (article: PKG-FR-SNAP-REMOVE-SNAPSHOT)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| E36 | An approved removal of a snap from Nomad | The snap is removed and snapd's own pre-removal snapshot is left in place | P | `test_snap_sync:TestConvergeRemoval::test_removal_never_passes_purge` asserts the command carries no purge; nothing observes the snapshot surviving |
| E37 | After a real removal through a sync, the user runs `snap saved` on Nomad | The snapshot for the removed snap is listed | — | no VM test removes a snap through a sync |

## E.6 Sideloaded snaps (article: PKG-FR-SNAP-SIDELOAD)

A sideload is a snap whose bytes came from a local `.snap` file; `snap list` shows it at an `x`-prefixed revision. All rows here assert the same outcome shape: no item in any direction, no command, one named warning per machine.

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| E38 | Atlas has a sideloaded snap Nomad lacks | No install is offered; the run names it as unmanaged | U | `test_snap_sync:TestSideloadedSnaps::test_sideloaded_source_snap_produces_no_diff`, `::test_one_warning_names_the_skipped_sideloaded_snaps` |
| E39 | Nomad has a sideloaded snap Atlas lacks | No removal is offered; the run names it | U | `test_snap_sync:TestSideloadedSnaps::test_target_only_sideloaded_snap_is_not_offered_for_removal` |
| E40 | Both machines have a sideload of the same name, at different revisions | No item in either direction | U | `test_snap_sync:TestSideloadedSnaps::test_sideloaded_snap_present_on_both_is_not_proposed_for_removal` |
| E41 | Both machines have sideloads (different names, one each) | Each machine's sideloads are named — two findings, one per machine | P | `::test_one_warning_names_the_skipped_sideloaded_snaps` covers a source-only machine; no test has sideloads on both and asserts a warning per machine |
| E42 | `beta` is a store snap on Atlas and a sideload of the same name on Nomad | Nothing at all: no install of `beta` on Nomad, no removal | U | `test_snap_sync:TestSideloadedSnaps::test_store_snap_the_target_sideloaded_under_the_same_name_produces_no_diff` |
| E43 | `beta` is a sideload on Atlas and a store snap on Nomad | Nothing at all: `beta` is not offered for removal from Nomad | — | the withholding is symmetric in the code, but only the E42 direction is tested |
| E44 | Atlas's sideload also carries a per-snap hold | No hold item is proposed for it | U | `test_snap_sync:TestSideloadedSnaps::test_sideloaded_snap_that_is_held_produces_no_hold_diff_either` |
| E45 | Nomad holds a sideload whose name is a store snap on Atlas | No hold item either, because the name is withheld on both machines | — | untested |
| E46 | A sideload the user previously marked as this machine's own | Still named as unmanaged, still no item — the mark silences items, not the finding | U | `test_snap_sync:TestSideloadedSnaps::test_a_marked_sideloaded_snap_is_still_named_and_still_produces_no_diff` |
| E47 | A run whose listing mixes a sideload and ordinary store snaps | The store snaps converge normally; no command ever names the sideload | U | `test_snap_sync:TestSideloadedSnaps::test_store_snaps_in_the_same_listing_still_diff_and_converge` |
| E48 | Two sideloads on one machine | One finding names both, rather than one line each | U | `test_snap_sync:TestSideloadedSnaps::test_one_warning_names_the_skipped_sideloaded_snaps` (asserts a single record naming both) |
| E49 | A real run on a VM carrying a sideloaded snap | The run finishes naming it, and the sideload is neither installed nor removed on either machine | — | no VM coverage of sideloads |

## E.7 A snap that cannot be converged (article: PKG-FR-SNAP-FAIL-ITEM)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| E50 | Two approved snap changes; Nomad's snapd cannot fetch the revision the first one names | That snap fails alone, naming it; the second still converges | U | `test_snap_sync:TestHoldAndRevisionFailuresArePerItem::test_unfetchable_revision_is_a_clean_per_item_failure_not_a_crash` |
| E51 | The same failing item also needed a channel retrack | The retrack is not attempted after the revision move failed | U | same test (asserts no `snap switch` for the failed snap) |
| E52 | An approved snap removal fails on Nomad | It fails as its own item and the rest of the run continues | — | the per-item loop is generic (`sync_core`); no snap removal failure is asserted |
| E53 | A real run on VMs where one snap item fails | The run reports that item as failed and everything else it approved still landed | — | `test_package_sync:TestPackageSyncWholeRunContracts::test_continue_on_item_failure` is apt-only |

## E.8 Per-snap refresh holds (articles: PKG-FR-SNAP-HOLD, PKG-FR-BLOCKS-REPLICATE)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| E54 | Atlas holds `alpha`; Nomad does not | A hold item of its own, separate from `alpha` itself, proposing to add the hold on Nomad | U | `test_snap_sync:TestHolds::test_source_held_yields_install_hold_diff_and_converges_hold_forever` |
| E55 | Approving E54 | Nomad's `alpha` ends the run held | U V | same test (asserts `snap refresh --hold=forever alpha`); `test_package_sync:TestSnapHoldCaptureTiming::test_per_snap_hold_replicates_through_a_real_sync_window` |
| E56 | Nomad holds `alpha`; Atlas does not | A hold item proposing to lift Nomad's hold | U | `test_snap_sync:TestHolds::test_target_held_only_yields_remove_hold_diff_and_converges_unhold` |
| E57 | Approving E56 | Nomad's hold on `alpha` is lifted | U | same test (asserts `snap refresh --unhold alpha`) |
| E58 | Both machines hold `alpha` | No hold item | U | `test_snap_sync:TestHolds::test_both_held_yields_no_hold_diff` |
| E59 | Neither machine holds `alpha` | No hold item | U | implicit in every non-hold fixture, e.g. `TestDiff::test_identical_snap_yields_no_diff` |
| E60 | Nomad holds `orphan`, which Atlas no longer has at all | No hold item; `orphan` itself is still offered for removal | U | `test_snap_sync:TestHoldIntentIsSourceAuthoritative::test_hold_on_a_snap_the_source_does_not_have_yields_no_hold_diff` |
| E61 | A plan carrying snap installs, snap removals and holds in both directions | Hold items sit in their own groups reading "hold"/"unhold", never inside the install or remove groups | U | `test_snap_sync:TestHoldReviewVerbs::test_hold_install_group_reads_hold_never_install`, `::test_hold_remove_group_reads_unhold_and_is_removal_direction`, `::test_snap_groups_keep_their_own_verbs_and_exclude_hold_items` |
| E62 | The group that lifts a hold is presented | It is a removal-direction group, so it is not approved by not choosing | U | `::test_hold_remove_group_reads_unhold_and_is_removal_direction` |
| E63 | `alpha` is new on Nomad AND held on Atlas | The install is settled and applied before the hold, so the hold lands on a snap that exists | U | `test_snap_sync:TestHolds::test_hold_diff_emitted_after_presence_diffs` |
| E64 | The user declines `alpha`'s install but approves its hold | The hold fails as its own item; the other approved holds still land | U | `test_snap_sync:TestHoldAndRevisionFailuresArePerItem::test_hold_for_a_snap_absent_on_target_fails_only_that_item` |
| E65 | A run that installs, changes, retracks and removes snaps | Not one of those commands sets a refresh hold as a side effect | U | `test_snap_sync:TestNoHold::test_install_change_retrack_and_removal_never_set_a_hold` |
| E66 | The hold-add command is built for a snap | It always names the snap; the bare form that would hold every snap on the machine is never issued | U | `test_snap_sync:TestHolds::test_hold_converge_never_emits_bare_hold` |
| E67 | A real sync converges a snap revision on VMs | Neither machine's system-wide refresh policy is different afterwards from what it was before | V | `test_package_sync:TestPackageSyncWholeRunContracts::test_snap_revision_converges_without_hold` |
| E68 | The user answers "never ask again" to a hold Atlas holds | The mark is recorded on Atlas, and the hold is never offered again | U V | `test_block_state_decisions:TestSnapHoldDecisions::test_declined_hold_is_recorded_on_source_and_never_re_offered`; `test_package_sync:TestBlockStateDecisionRoundTrip::test_skip_always_on_a_snap_hold_is_inert_next_run` |
| E69 | The user answers "never ask again" to an unhold, where Nomad holds it | The mark is recorded on Nomad, and it is never offered again | U | `test_block_state_decisions:TestSnapHoldDecisions::test_declined_unhold_is_recorded_on_target_and_never_re_offered` |
| E70 | A hold was permanently declined; the snap itself is still missing on Nomad | The snap is still offered for install — the hold decision is about the hold only | U | `test_block_state_decisions:TestSnapHoldDecisions::test_recorded_hold_does_not_silence_the_snaps_own_presence_diff` |
| E71 | The sync's own auto-refresh pause is in force while holds are captured | A per-snap hold still reads as held, and a snap without one does not acquire the note | V | `test_package_sync:TestSnapHoldCaptureTiming::test_system_refresh_hold_does_not_mask_a_per_snap_held_note` |

## E.9 The auto-refresh pause (article: PKG-FR-SNAP-REFRESH-PAUSE)

Driven by the orchestrator around the whole job window; listed here because the obligation is snap's.

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| E72 | `snap_sync` is enabled and the run is real | Automatic snap refreshes are suspended on BOTH machines for the run | U | `test_snap_autorefresh_hold:TestHoldEngaged::test_hold_set_on_both_hosts_when_snap_sync_enabled` |
| E73 | The suspension is about to be set | Each machine's existing refresh policy is read first, and reading it changes nothing | U | `test_snap_autorefresh_hold:TestHoldEngaged::test_capture_is_read_only_and_precedes_the_set`, `TestConfirmEachCommandGate::test_apply_and_restore_declare_mutations_on_both_hosts` |
| E74 | The policy is read on a machine where reading it needs privilege | The read is privileged, so a permission failure is never mistaken for "no policy set" | U | `test_snap_autorefresh_hold:TestCaptureIsPrivileged::test_capture_reads_under_sudo_on_both_hosts` |
| E75 | Atlas's policy cannot be read; Nomad's is genuinely unset | The two are told apart | U | `test_snap_autorefresh_hold:TestCaptureIsPrivileged::test_a_denied_read_is_not_reported_as_no_hold` |
| E76 | Atlas had its own refresh hold set to a date; the run ends | Atlas's own hold is back, exactly as it was | U | `test_snap_autorefresh_hold:TestUserHoldIsNeverDestroyed::test_a_prior_hold_survives_the_sync_window[2026-07-24T18:00:00Z]` |
| E77 | Atlas had an INDEFINITE hold of its own; the run ends | The indefinite hold is back, not a timed one and not none | U | `::test_a_prior_hold_survives_the_sync_window[forever]` |
| E78 | Neither machine had a hold; the run ends | The suspension the run set is cleared and nothing else is | U | `test_snap_autorefresh_hold:TestUserHoldIsNeverDestroyed::test_only_a_genuinely_absent_hold_is_cleared`, `TestRestore::test_restore_unsets_when_no_prior_hold` |
| E79 | Different prior policies on the two machines (a date on Atlas, indefinite on Nomad) | Each machine gets its own value back | U | `test_snap_autorefresh_hold:TestRestore::test_restore_preserves_prior_hold_per_host` |
| E80 | A machine's prior policy cannot be read | Nothing is written there — the run does not pause that machine at all | U | `test_snap_autorefresh_hold:TestUserHoldIsNeverDestroyed::test_an_unreadable_hold_is_never_written_in_the_first_place` |
| E81 | Same machine at the end of the run | Its policy is left exactly as found, never cleared | U | `::test_an_unreadable_hold_is_left_alone_rather_than_cleared` |
| E82 | Atlas's policy is unreadable, Nomad's is fine | Nomad is still paused; one machine's failure does not cost the other its guard | U | `::test_the_readable_host_is_still_paused_when_the_other_is_not` |
| E83 | Setting the suspension fails on a machine | The run says so, names the machine, and continues with that machine unpaused | U | `test_snap_autorefresh_hold:TestWarningsNameTheMachines::test_a_failed_pause_names_the_machine` |
| E84 | The set command succeeds but the policy still reads unchanged afterwards | The run says the machine is NOT paused, names it, and continues | U | `test_snap_autorefresh_hold:TestApplyIsVerified::test_warns_when_the_hold_did_not_stick`, `TestWarningsNameTheMachines::test_a_hold_that_did_not_stick_names_the_machine` |
| E85 | The confirmation read is refused | The run says it could not confirm the pause, names the machine, and continues | U | `test_snap_autorefresh_hold:TestApplyIsVerified::test_warns_when_the_read_back_is_denied`, `TestWarningsNameTheMachines::test_a_pause_that_cannot_be_confirmed_names_the_machine` |
| E86 | The confirmation read raises (connection lost) | The run continues; the pause stays engaged so it is still restored at the end | U | `test_snap_autorefresh_hold:TestApplyIsVerified::test_a_read_back_that_raises_never_fails_the_sync` |
| E87 | Everything works | No warning is emitted at all | U | `test_snap_autorefresh_hold:TestApplyIsVerified::test_no_warning_when_the_hold_took_effect` |
| E88 | The suspension is written | It is a timed value computed on that machine's own clock, so it lapses on its own | P | `test_snap_autorefresh_hold:TestHoldEngaged::test_hold_set_on_both_hosts_when_snap_sync_enabled` asserts the value is date-computed and that the indefinite verb is never used; nothing observes the expiry |
| E89 | A run dies without cleaning up | Neither machine is left with automatic refreshes suspended once the timed value lapses | — | untested |
| E90 | The pause is in force while `snap_sync` converges revisions | It does not block the run's own revision moves — only automatic refreshes are gated | U V | `test_snap_autorefresh_hold:TestHoldDoesNotBlockConvergence::test_hold_only_writes_refresh_hold_never_a_snap_refresh_command`; `test_package_sync:TestPackageSyncWholeRunContracts::test_snap_revision_converges_without_hold` |
| E91 | A dry run | Nothing is suspended on either machine | U | `test_snap_autorefresh_hold:TestHoldEngaged::test_hold_skipped_in_dry_run` |
| E92 | `snap_sync` is not enabled | Nothing is suspended on either machine | U | `test_snap_autorefresh_hold:TestHoldEngaged::test_hold_not_set_when_snap_sync_disabled` |
| E93 | A run that never engaged the pause reaches cleanup | No command is issued | U | `test_snap_autorefresh_hold:TestRestore::test_restore_is_noop_when_no_hold_engaged` |
| E94 | Cleanup runs twice | The restore happens once | U | `test_snap_autorefresh_hold:TestRestore::test_restore_is_idempotent` |
| E95 | The run is started with per-command confirmation | Both the pause and the restore are shown to the user as changes; the policy read is not | U | `test_snap_autorefresh_hold:TestConfirmEachCommandGate::test_apply_and_restore_declare_mutations_on_both_hosts` |
| E96 | The restore is shown for confirmation on a machine whose own hold was overwritten | The prompt names the value being written back | U | `test_snap_autorefresh_hold:TestConfirmEachCommandGate::test_restore_names_the_prior_value_it_is_writing_back` |
| E97 | The user declines the restore at that prompt | The write does not happen, and the run still releases the lock on Nomad and its connection | U | `test_snap_autorefresh_hold:TestConfirmEachCommandGate::test_abort_at_restore_is_not_swallowed_by_the_best_effort_handler`, `::test_cleanup_honours_the_abort_but_still_releases_resources` |
| E98 | The restore command fails, or the connection is already gone | The run says so, names the machine, and finishes tearing down | U | `test_snap_autorefresh_hold:TestWarningsNameTheMachines::test_a_failed_restore_names_the_machine`, `::test_a_restore_that_raises_names_the_machine` |
| E99 | The restore for Nomad is attempted | It happens while the connection it needs is still up | — | ordering is only implicit in `test_cleanup_honours_the_abort_but_still_releases_resources` |
| E100 | Any of the pause's warnings is read by the user | Each names the machine by hostname; neither role word appears | U | `test_snap_autorefresh_hold:TestWarningsNameTheMachines` (`_named` asserts the absence of both role words across every warning) |
| E101 | Atlas cannot run privileged commands without a password | Validation fails naming Atlas, saying the pause is why the source needs it | U | `test_snap_sync:TestValidate::test_source_without_passwordless_sudo_yields_validation_error` |
| E102 | Nomad cannot run privileged commands without a password | Validation fails naming Nomad | U | `test_snap_sync:TestValidate::test_target_without_passwordless_sudo_yields_validation_error` |
| E103 | snap is absent on either machine | Validation fails naming that machine | U | `test_snap_sync:TestValidate::test_snap_unavailable_on_source_yields_validation_error`, `::test_snap_unavailable_on_target_yields_validation_error` |

## E.10 The `~/snap` data boundary (article: PKG-FR-SNAP-DATA-BOUNDARY)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| E104 | Atlas's `~/snap/firefox` holds the active revision's data directory and a retained older one | Only the active revision's directory is mirrored; the older one is not | U | `test_snap_sync:TestExcludePaths::test_excludes_old_revisions_keeps_current_common_and_current_symlink` |
| E105 | The same app's revision-independent data directory and the pointer to the active revision | Both are mirrored | U | same test |
| E106 | The pointer to the active revision is dangling | No revision directory for that app is mirrored | U | `test_snap_sync:TestExcludePaths::test_dangling_current_falls_back_to_excluding_all_revisions` |
| E107 | The pointer is missing entirely | Same | U | `test_snap_sync:TestExcludePaths::test_missing_current_symlink_falls_back_to_excluding_all_revisions` |
| E108 | Atlas has no `~/snap` at all | Nothing is excluded and nothing fails | U | `test_snap_sync:TestExcludePaths::test_no_snap_directory_returns_empty` |
| E109 | `folder_sync` builds the transfer for the folder holding `~/snap` with `snap_sync` enabled | The retained revision directories are excluded from the transfer | U | `test_folder_sync:TestSnapSyncExcludeFilters::test_old_revision_excluded_current_kept`, `TestPackageJobExcludeFiltersGating::test_snap_sync_enabled_includes_revision_exclusion` |
| E110 | `snap_sync` is not enabled | `folder_sync` excludes nothing on snap's account | U | `test_folder_sync:TestPackageJobExcludeFiltersGating::test_snap_sync_disabled_excludes_nothing`, `::test_missing_enabled_sync_jobs_omits_both_exclusions_without_raising` |
| E111 | The folder being synced does not contain `~/snap` | No snap exclusion is added to that transfer | U | `test_folder_sync:TestSnapSyncExcludeFilters::test_revision_dir_outside_synced_folder_is_skipped`, `::test_no_snap_directory_yields_no_filters` |
| E112 | The transfer carries both the snap exclusions and the user's own filter file | The exclusions take effect ahead of the user's filter | U | `test_folder_sync:TestPackageJobExcludeFiltersGating::test_both_package_exclusions_precede_merge_filter` |
| E113 | A real sync of two VMs with `snap_sync` and `folder_sync` both on | Nomad ends with no data directory for a revision its own snapd never installed | — | no VM coverage of the `~/snap` boundary |

## Gaps

Ordered by row id. "unit" means a mocked unit test can assert it reliably; "VM" means it needs real snapd semantics.

- **E4** (publisher is not identity) — unit. Give the two machines listings whose Publisher column differs for the same name, same revision, same channel, and assert no diff; then differ the revision and assert one CHANGE, not an install-plus-removal pair.
- **E5** (`PKG-NG-SNAP-ORIGIN`, no origin question) — unit. Over a plan carrying installs, changes, removals and both hold directions, assert every review group's item class is `SNAP` or `SNAP_HOLD` and that no item id or group title mentions a store, publisher or key. This is the only check standing between the article and a future regression that adds one.
- **E9** (an install lands the source's channel) — unit. Converge an INSTALL and assert the commands are, in order, the `--revision=` install and then `snap switch --channel=<source channel>`; today only the install half is asserted.
- **E12** (revision-only change issues no channel switch) — unit. Converge a CHANGE whose channels match and assert no `snap switch` command at all. The existing near-miss asserts absence only because the refresh failed first.
- **E14** (channel-only change issues no revision refresh) — unit. Converge a CHANGE whose revisions match and assert no `--revision=` command.
- **E16** (both facets differ) — unit. Converge the both-differ item and assert both the `--revision=` refresh and the `snap switch` land, in that order.
- **E19** (the change group's own verb) — unit. Assert the group title and `action_label` for a snap CHANGE, the way `TestHoldReviewVerbs` does for the other four.
- **E20** (both machines empty) — unit, cheap: both listings "No snaps are installed yet.", assert `plan.diffs == ()` and no failure.
- **E29** (short line) — unit. Feed a body line with fewer fields than the header and assert it is dropped rather than producing an item with a wrong revision.
- **E34** (reverse confinement skew) — unit. Source strict, target classic, revisions differ: assert the refresh carries neither flag.
- **E36/E37** (snapd's pre-removal snapshot survives) — E36's remaining half and E37 both need a VM: install a fixture snap on Nomad only, sync, and assert `snap saved` lists a snapshot for it afterwards. The unit test proves only the command shape. Note the fixture snap must be outside `_SNAP_REMOVAL_DENYLIST`.
- **E41** (a warning per machine when both hold sideloads) — unit. Sideloads on both machines with different names; assert two warning records, each naming only its own machine's sideloads.
- **E43** (source sideload / target store snap of the same name) — unit. Mirror of `test_store_snap_the_target_sideloaded_under_the_same_name_produces_no_diff`, asserting no removal is offered for Nomad's store copy.
- **E45** (a held sideload name on the other machine) — unit. Nomad holds `beta` which is a sideload there, Atlas has `beta` from the store unheld: assert no `snap:hold:` item.
- **E49** (sideloads through a real run) — VM. Build a trivial `.snap` (or `snap try` a directory) on one VM, run a sync, assert the run named it and that both machines' `snap list` still show it unchanged. Worth it only if the fixture cost is acceptable; the unit coverage of the branch set is already dense.
- **E52** (a snap removal that fails) — unit. Make `snap remove` exit non-zero for one of two approved removals and assert only that item is in `PackageItemFailures` and the other command still ran.
- **E53** (per-item failure through a real run) — VM. The apt equivalent exists (`test_continue_on_item_failure`); the snap version needs a snap whose revision Nomad cannot fetch, which is awkward to arrange reliably — approving a hold for a snap the run also declines to install is the cheaper VM trigger.
- **E88/E89** (self-expiry) — E88's remaining half is VM-only and slow (the pause is six hours). A cheaper substitute: assert on a VM that the value snapd stores after the pause parses as a future timestamp rather than the indefinite literal. E89 (a run that dies) can be a VM test that kills the sync mid-window and asserts the stored value is timed, not indefinite — that is the observable consequence, since waiting out the expiry is not testable.
- **E99** (restore precedes connection teardown) — unit. In `_cleanup`, record the order of the restore call and the connection close and assert the restore comes first; today only the abort path touches `_cleanup`.
- **E113** (`~/snap` boundary end to end) — VM. Put a stale revision directory under `~/snap/<app>/` on Atlas, run a sync with both jobs enabled, and assert Nomad has the active revision's directory and `common` but not the stale one.

## Notes for the assembler

- **E.9 is the orchestrator's code, not the job's.** Every row there exercises `Orchestrator._hold_snap_autorefresh` / `_apply_snap_hold` / `_verify_snap_hold` / `_restore_snap_hold` / `_restore_snap_autorefresh` in `src/pcswitcher/orchestrator.py` — there is no separate module. If area K (orchestration ordering) also claims the pause, keep the ordering claim there (pause engaged before the job window, restored first in cleanup) and the policy claims here.
- **E101–E103 overlap the preconditions area.** They are `PKG-FR-SUDO-PRECONDITION` rows, but snap's source-side sudo requirement exists solely because of the refresh pause, so they belong to whichever section keeps that causal link. Drop them here if the preconditions area covers them.
- **E109–E112 overlap folder_sync.** They are the `folder_sync` half of `PKG-FR-SNAP-DATA-BOUNDARY`; the source of the excluded paths (`snap_sync_exclude_paths`) is snap's. Merge with any `PKG-FR-DATA-BOUNDARY` rows another area emits rather than duplicating.
- **E68–E70 overlap the machine-specific-mark area.** They are `PKG-FR-MACHINE-SPECIFIC` applied to a snap hold, kept here because the hold's identity (`snap:hold:<name>` being a strict superstring of `snap:<name>`) is what makes them non-generic.
- **A requirements-level ambiguity worth flagging.** `PKG-FR-SNAP-SIDELOAD` says a run "MUST name the ones it found so the user knows they are unmanaged". The code names them as a job WARNING (`SnapSyncJob._warn_sideloaded`), not as a review line or a report entry. Whether a warning satisfies "a run MUST name" is a judgement the article does not settle; I recorded the rows as covered on that reading. If the intended reading is the report, E38/E39 become ‼ rather than U.
- **One split worth knowing about.** `PKG-FR-SNAP-CASES` names four cases; I split each into the item it produces (E7, E10, E11/E13/E15, E17) and the command that item produces when approved (E8/E9, E12, E14, E16), because the requirement's "at the source's revision and channel" is an outcome the diff alone does not deliver. Most of the unasserted halves are on the command side.
- **The `held`-capture-timing question is settled against a real snapd** (E71) — the source comment in `_parse_snap_list` that says "if a VM integration test ever shows a system hold flipping this token" now has that test, and it does not.
