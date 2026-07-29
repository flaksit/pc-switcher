# Phase 02 — package sync scenario/coverage matrix

Requirement-derived scenario enumeration for the four package jobs, mapped to pytest coverage. Branches come from ADR-020 (D-01…D-42), ADR-022, 02-208-HOLD-MASK-REPLICATION.md and `docs/planning/package-sync-conformance-criteria.md` — not from code paths, though code was read to decide what actually happens.

## Legend

| Mark | Meaning |
| --- | --- |
| U | covered by a unit test that asserts this branch |
| V | a VM integration test asserts this branch — claimed, never observed to pass (see Findings); `V` is a claim about the test, not about a passing run |
| P | partial — a neighbouring test exists, this branch is not asserted |
| — | no coverage |
| ‼ | open defect, or a requirement that is not implemented |

Test file shorthand: `apt` = tests/unit/jobs/test_apt_sync.py · `snap` = test_snap_sync.py · `flat` = test_flatpak_sync.py · `man` = test_manual_installs_sync.py · `core` = test_package_sync_core.py · `rev` = test_package_review.py · `state` = test_package_state.py · `items` = test_package_items.py · `blk` = test_block_state_decisions.py · `perm` = test_review_skip_always.py · `fold` = test_folder_sync.py · `hold` = tests/unit/orchestrator/test_snap_autorefresh_hold.py · `gate` = tests/unit/test_step_gate.py · `audit` = tests/unit/test_mutates_audit.py · `cli` = tests/unit/cli/test_commands.py · `cfg` = tests/unit/orchestrator/test_config_system.py · `exit` = test_session_status_from_job_results.py · `INT` = tests/integration/jobs/test_package_sync.py

## A. apt packages — presence and version diff (D-03, D-04, D-25)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| A1 | On source's manual set, absent on target, candidate exists | MISSING_ON_TARGET → INSTALL, default-ticked | U V | core:`test_missing_on_target_yields_install`, apt:`test_diff_yields_exactly_two_missing_items`, INT:`test_apt_sync_installs_missing_package` |
| A2 | On source, absent on target, the source's origin is declared by no writable source file on the source | REPO_UNAVAILABLE → REPORT_ONLY, never an install. This is `REPO_UNAVAILABLE`'s only meaning: it is a statement about provenance, not about whether apt printed a candidate | U | apt:`TestDiffEngine::test_an_origin_no_source_file_declares_yields_repo_unavailable_not_install`, `TestOriginClassification::test_unreplicable_origin_is_report_only_naming_the_origin`, `TestUnavailableCapture::test_a_package_no_repository_can_supply_is_reported_not_installed` |
| A2a | On source, absent on target, apt on the target has never heard the name, but a source file declares the origin | ordinary INSTALL with the repository derived — a target candidate is not a precondition | U | apt:`TestOriginOutcome::test_apt_silence_on_the_target_does_not_condemn_a_package`, `test_a_package_apt_has_never_heard_of_prints_no_block_and_is_still_offered` |
| A3 | On target's manual set only | EXTRA_ON_TARGET → REMOVE, own unticked group | U | core:`test_extra_on_target_yields_remove`, apt:`test_extra_on_target_yields_extra_on_target_remove` |
| A4 | Present both, equal version | no diff at all | U | core:`test_equal_versions_yields_no_diff` |
| A5 | Present both, versions differ | VERSION_MISMATCH → REPORT_ONLY naming both versions; never force-converged | U | core:`test_version_mismatch_yields_report_only_with_both_versions` |
| A6 | Version ordering (epoch, tilde, revision) | decided by `dpkg --compare-versions`, never string compare | U | items:`TestCompareDebVersions` (incl. real-dpkg cross-check) |
| A7 | Package named by a target-side `preferences.d` stanza | no per-package echo of any kind; the diff takes no pin input at all, so a target-only pinned package is offered for removal like any other | U | apt:`TestAPinNeverSpeaksForAPackage` (4), `TestDiffEngine::test_the_diff_takes_no_pin_input_at_all` |
| A8 | Package held on target | install/upgrade action suppressed, no package-level report — including after the unhold was permanently declined | U | core:`test_target_hold_only_yields_apt_hold_remove_and_suppresses_package_action`, apt:`test_held_package_yields_hold_item_not_a_duplicate_package_report`, blk:`TestAptHeldPackageSuppression` (2) |
| A9 | Same name and version on both, installed from two different vendors | ORIGIN_MISMATCH → REPORT_ONLY naming both origins; outranks the version branch; never fires for two Ubuntu mirrors | U | apt:`test_divergent_vendor_provenance_reports_origin_mismatch`, `test_two_machines_on_different_ubuntu_mirrors_produce_no_origin_mismatch`, `TestOriginDetailWording::test_the_mismatch_detail_names_both_sides` |
| A10 | Dependency-only package (not in `apt-mark showmanual`) | never in the manifest → never installed, never removed, never reported | U | apt:`TestManifestIsShowmanualOnly::test_auto_installed_dependency_produces_no_diff_of_any_kind` — the mechanism example 1 rests on |
| A11 | Package installed from no configured repository on the source | `manual_installs_sync`'s exclusively: dropped at `apt_sync`'s capture, so it reaches no diff, no review group, no simulation and no origin classification | U | apt:`TestBareDebPackagesAreNotAptSyncsBusiness` (10) |
| A12 | `apt-mark showmanual` empty on a machine | empty manifest, no crash, every target package offered as an unticked removal | U | apt:`test_empty_source_manifest_offers_every_target_package_as_an_unticked_removal` |
| A13 | Version resolution source | `dpkg-query`, never `apt list --installed` | U | apt:`test_dpkg_query_used_not_apt_list_installed` |
| A14 | Where the source's own origins come from | the one batched source-side `apt-cache policy` already issued for the bare-`.deb` exclusion, parsed a second time; never a second policy call. The installed (`***`) row, never the candidate row | U | apt:`test_the_source_policy_call_answers_both_questions_asked_of_it`, `test_the_source_origin_map_holds_the_installed_row_not_the_candidate_one` |
| A15 | Which origins count as the distribution's | computed per machine from that machine's own never-removed files; a user-named `ubuntu-esm-mine.sources` is not one | U | apt:`test_distribution_origins_come_from_the_machines_own_distribution_files`, `test_a_user_named_esm_lookalike_is_not_a_distribution_file` |
| A16 | Review line names the origin | full URI path with the scheme stripped, comma-separated and sorted for several vendors; omitted entirely when every origin is a distribution origin | U | apt:`TestOriginDetailWording` (4), `test_a_distribution_origin_install_names_no_origin` |
| A17 | Post-refresh origin verification (D-35) | ONE batched `apt-cache policy` after the group's single `apt-get update` and before the first install; a candidate from none of the source's origins refuses that install alone, naming both; a distribution-origin package is never verified; a skipped install is never named | U | apt:`TestOriginEnforcement` (8), `TestOriginRefusalWording` (2) |

## B. apt holds (#208 D1–D6)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| B1 | Held on source, not target | `apt:hold:<pkg>` INSTALL → `sudo apt-mark hold` | U | apt:`test_source_held_yields_install_hold_item_and_converge_runs_apt_mark_hold`, core:`test_source_hold_only_yields_apt_hold_install` |
| B2 | Held on target, not source | REMOVE → `sudo apt-mark unhold`, unticked removal group | U | apt:`test_target_held_only_yields_remove_unhold_item` |
| B3 | Held on both / neither | no hold diff | U | apt:`test_held_on_both_yields_no_hold_diff`, core:`test_held_on_both_yields_no_diff` |
| B4 | Hold identity is distinct from package identity | two separate review items for one package | U | apt:`test_held_package_yields_hold_item_not_a_duplicate_package_report` |
| B5 | Review verb | group/entry read "hold"/"unhold", never "install"/"remove" | U | apt:`TestHoldReviewVerbs` (2), snap:`TestHoldReviewVerbs` (3), flat:`TestMaskReviewVerbs` (3) — the #208 D3 promise, asserted per manager |
| B6 | Hold converges after the package install in the same run (D8) | install lands before its hold | U | apt:`TestInstallBeforeHoldOrdering` (plain sort path and `accept_review` reorder path) |
| B7 | Hold approved for a package whose install was skipped (D6) | `apt-mark hold` on an absent package → normal per-item failure | U | apt:`TestHoldOnAnAbsentPackage::test_failed_apt_mark_hold_fails_only_that_item` |
| B8 | skip-always on a hold item | DecisionEntry written on the holder's machine | U | apt:`test_skip_always_on_a_hold_writes_the_decision_file` |
| B9 | skip-always on a hold item, next run | item inert, no diff | U V | blk:`TestAptHoldDecisions` (3, both directions + holder-machine read-back), INT:`test_skip_always_on_an_apt_hold_is_inert_next_run`. Enforced by `_drop_inert_diffs` post-diff, the only correct place: filtering the hold set on the way in would re-propose upgrading a held package |
| B10 | Holds drive no `apt-get -s` simulation | selection state only | U | apt:`TestHoldsDriveNoSimulation::test_hold_only_run_issues_zero_apt_get_simulations` |
| B11 | `/usr/bin/apt-mark` in the target sudo grant | named in the hint | U | apt:`test_apt_mark_is_in_the_target_sudo_command_list` |

## C. apt repository config — sources, keys, pins, apt.conf (D-11, D-12, D-13, D-27, D-34, D-36, D-37, D-38, D-39)

Under `/etc/apt` only three things are reviewed: an `apt.conf.d` file in all three directions with the full three-way decision, and the REMOVAL of a repository file or a pin file on a two-answer screen that records nothing. Everything else is derived mechanism with no `item_id` — repository adds and overwrites derived from the packages approved from them, every `preferences.d` pin, the distribution's own source files, and every signing key. A derived write cannot fail as an item; its failure is charged to the packages that depended on it.

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C1 | A repository never appears as a review entry in the add or change direction | no `apt:source:`/`apt:pin:` entry in any group, for any of the three item classes | U | apt:`test_a_repository_never_appears_as_a_review_entry_in_the_add_or_change_direction`, `TestPinsStillTravelAsFiles` (3) |
| C1a | Approved install whose origin the target already offers | INSTALL, and an empty derived write set | U | apt:`test_same_origin_install_derives_no_repository_write` |
| C1b | Approved install whose origin the target offers from another vendor, or not at all | INSTALL carrying the source's declaring file, its keyring and the always-sync pins as derived writes | U | apt:`test_different_origin_install_derives_the_sources_own_repository`, `test_one_writable_serving_file_is_enough` |
| C2 | Every source file declaring a package's origin names a key absent on the source | the PACKAGE is REPO_UNAVAILABLE → REPORT_ONLY; no repository is written and no install is issued | U | apt:`test_a_dangling_keyring_makes_the_package_unavailable`, `test_a_genuinely_missing_key_is_still_reported_dangling` (all three key directories searched first) |
| C3 | A package served by several origins on the source | the derived set is the union of every file declaring any of them; every non-distribution origin is named in the detail | U | apt:`test_source_files_serving_is_the_union_of_every_file_declaring_an_origin`, `TestOriginDetailWording::test_several_vendors_are_named_comma_separated` |
| C4 | deb822 `.sources` vs legacy `.list` | format recorded per file for the derived write, never normalised; identity is the filename | U | apt:`test_deb822_and_legacy_source_each_record_own_format` |
| C5 | Same repo described by both a `.list` and a `.sources` file | two distinct files, both derivable | P | identity-by-filename tested; the coexistence case itself is not |
| C6 | Any key, any direction (missing / differing / target-only) | never a diff, never a review entry, never a decision | U | apt:`TestKeysAreNotItems::test_no_key_reaches_a_diff_or_a_review_group_in_any_direction`, blk:`test_a_signing_key_is_never_offered_and_so_can_never_be_recorded` |
| C7 | Key digests identical on both machines | no transfer, no promotion, no `apt-get update` | U | apt:`test_a_matching_keyring_is_never_written` |
| C8 | Key content differs while its source file is byte-identical (vendor rotation) | refreshed anyway, bytes copied verbatim | U | apt:`test_rotated_keyring_is_refreshed_although_its_source_file_is_identical` |
| C9 | `apt.conf.d` file missing / extra / changed | INSTALL / REMOVE / CHANGE, each with the ordinary three-way decision and the registry — the one non-package exception to "only packages are reviewed" | U | apt:`test_pin_and_config_diff_missing_extra_and_changed`, `TestAptConfigVocabulary` (2), blk:`TestAptRepoItemDecisions::test_declined_config_install_is_recorded_on_source_and_never_re_offered` |
| C9a | Pin file missing or changed on the target | written / overwritten silently, with no review line and no read of its contents; only the extra direction is a diff | U | apt:`TestPinsStillTravelAsFiles` (3) |
| C10 | Convergence order | key → pin/apt-config → distribution + derived sources → approved removals → unused-key collection → `apt-get update` → origin verification → packages → holds | U | apt:`test_key_then_source_then_update_then_package_install`, `test_pins_travel_without_a_review_line_and_land_before_the_sources`, `test_key_of_a_derived_repo_is_provisioned_with_no_decision_of_its_own`, `test_key_left_unreferenced_by_an_approved_removal_is_deleted` (asserts source-rm < key-rm < update) |
| C11 | Several repo items approved | exactly one `apt-get update` | U | apt:`test_apt_get_update_runs_exactly_once_for_three_repo_items` |
| C12 | Keys never re-fetched from a vendor | no command contains a URL | U | apt:`test_no_key_command_contains_a_url` |
| C13 | A derived write fails — the key, or the repository file's own promotion | the file has no item to fail, so every approved PACKAGE whose origin depended on it fails naming the file; other packages proceed | U | apt:`test_a_failed_derived_repository_write_fails_the_package_that_needed_it`, `test_a_repository_whose_own_promotion_fails_also_fails_its_package`, `test_directory_preparation_failure_fails_the_item_not_the_run` |
| C14 | `/etc/apt` write mechanism | stage under `~/.cache`, `sudo install -o root -g root -m 0644`, never `mv`, never `send_file` outside home | U | apt:`test_promotion_uses_sudo_install_with_owner_group_mode_never_mv`, `test_send_file_destinations_start_with_home_never_contain_etc` |
| C15 | Staging copy lifecycle | removed on success and on failure | U | apt:`test_staging_file_removed_after_success_and_after_failure` |
| C16 | `/etc/apt/keyrings` absent (fresh 24.04) | `sudo mkdir -p -m 0755` before `install` | U | apt:`test_promotion_ensures_keyrings_directory_before_install` |
| C17 | Directory preparation fails | fails that item only, not the run | U | apt:`test_directory_preparation_failure_fails_the_item_not_the_run` |
| C18 | Repo file extra on target | single `sudo rm -f` naming that file | U | apt:`test_remove_source_issues_single_rm_naming_that_file` |
| C19 | `apt-get update` fails after the group's writes | back up → restore changed → delete created → every group item recorded FAILED | U | apt:`test_failed_update_restores_changed_deletes_created_records_group_failures` |
| C20 | A rollback step itself fails | named in the summary, backup dir kept | U | apt:`test_failed_rollback_step_warns_and_keeps_the_backup` |
| C21 | `apt-get update` succeeds | no restore command issued, backup discarded | U | apt:`test_successful_update_issues_no_restore_command` |
| C22 | Rollback happened | package items still attempted (D-27) | U | apt:`test_rollback_does_not_prevent_package_items_from_being_attempted` |
| C23 | Backup itself fails | every group item fails, no `KeyError` crash | U | apt:`test_backup_failure_fails_every_group_item_without_crashing` |
| C24 | Source file extra on target, its key left unreferenced by the approved removal | both deleted, key deletion after the source deletion, one `apt-get update` after both | U V | apt:`test_source_and_its_key_both_removed_with_one_update_after_both`, `test_key_left_unreferenced_by_an_approved_removal_is_deleted`, INT:`test_apt_source_and_its_key_removed_together` (proven against `/etc/apt` and a working `apt-get update` afterwards, with only the REPOSITORY decided) |
| C25 | Content reads for hydration use sudo | matches the `sudo find … sha256sum` privilege | U | apt:`test_content_hydration_reads_use_sudo_matching_the_digest_capture` |
| C26 | Target has a repo the source lacks, still needed by a target-side machine-specific package | removal still offered (unticked), its `detail` naming the machine-specific packages | U | apt:`TestRepoRemovalNamesMachineSpecificPackages` (7) |
| C27 | skip-always, per `/etc/apt` item class | `apt:config:` is recordable and inert next run; no `apt:source:` or `apt:pin:` id can reach a decision file in any direction, because neither is ever offered permanence | U | blk:`TestAptRepoItemDecisions` (2, incl. `test_no_repository_or_pin_id_can_reach_a_decision_file`) |
| C28 | Keyring referenced by a source file this run's DERIVED set writes | provisioned from the source machine before that write, whether the file is new on the target or overwritten | U | apt:`test_key_of_a_derived_repo_is_provisioned_with_no_decision_of_its_own`, `test_key_of_an_overwritten_repo_is_provisioned_too` |
| C29 | Keyring on the source machine that no target-side source references | not copied — `/etc/apt/keyrings` is never mirrored wholesale | U | apt:`test_an_unreferenced_source_keyring_is_not_copied_to_the_target` |
| C30 | One rotated key referenced by several repositories (1-n) | exactly one write | U | apt:`test_one_rotated_key_serving_three_repos_is_written_once` |
| C31 | `/etc/apt/trusted.gpg.d` key missing or differing on the target | copied/refreshed on its own content (nothing references it), never collected | U | apt:`test_global_trust_keys_are_replicated_whether_missing_or_differing`, `test_a_global_trust_key_is_never_collected` |
| C32 | Keyring still referenced by a surviving source — one with no diff, one the user unticked, one recorded machine-specific, one in `/etc/apt/sources.list` | kept in every case | U | apt:`test_key_still_referenced_by_a_surviving_repo_is_kept`, `test_key_referenced_by_a_repo_whose_removal_was_declined_is_kept`, `test_key_referenced_by_a_machine_specific_repo_is_kept`, `test_key_referenced_only_by_a_file_pc_switcher_never_syncs_is_kept` |
| C33 | Keyring the source machine still has, unreferenced on the target | never collected (it is configuration this sync replicates, not litter) | U | apt:`test_a_key_the_source_machine_still_has_is_never_collected` |
| C34 | Run that removes no source file | collection pass does not run at all — not even its re-scan | U | apt:`test_no_source_removed_means_no_collection_pass_at_all` |
| C35 | Keyring whose only referent is the repository this run removes | not refreshed first, then collected | U | apt:`test_a_key_only_the_departing_repo_needs_is_not_refreshed_first`, `test_key_left_unreferenced_by_an_approved_removal_is_deleted` |
| C36 | Collected keyring | backed up into the group's backup dir before deletion, deletion carries `mutates=` | U | apt:`test_a_collected_key_is_backed_up_and_gated_as_a_modification` |
| C37 | deb822 `Signed-By:` holding an inline armored key — block on continuation lines, or its first line on the field line as `add-apt-repository` writes it | yields no reference: no invented dependency, no real keyring made to look referenced, and the repository installs normally | U | apt:`test_inline_armored_signed_by_names_no_keyring`, `TestInlineArmoredSignedBy` (2) |
| C38 | `Signed-By:` pointing into `/usr/share/keyrings` — where `add-apt-repository`, `ubuntu.sources` and most vendor `.deb`s put their key | resolves; the file is replicable, so the package it serves is an INSTALL rather than REPO_UNAVAILABLE | U | apt:`test_a_usr_share_keyrings_reference_resolves_and_the_repo_is_replicable` |
| C39 | Referenced keyring the target LACKS, whatever owns it on either machine | copied — including one the target's dpkg owns, which is the only way a repository whose key ships inside a package it hosts can bootstrap | U | apt:`test_a_hand_placed_key_the_target_lacks_is_provisioned`, `test_a_package_owned_key_the_target_is_missing_is_copied_anyway` |
| C40 | Referenced keyring the target HAS with different bytes, owned by a target package | left alone; the repository is still written (the refusal in C13 does not apply to a difference this run chose not to touch) | U | apt:`test_a_package_owned_key_present_with_different_bytes_is_not_overwritten` |
| C41 | Keyring ownership probe | ONE batched `dpkg -S` over every key in all three directories, exit code ignored (it is non-zero as soon as any path is unowned) | U | apt:`test_ownership_is_probed_once_for_every_key_directory`, mutation-checked against an exit-code guard |
| C42 | `/usr/share/keyrings` key no source references | not copied (the directory is mostly the distro's own) and never collected | U | apt:`test_a_shared_keyring_no_source_references_is_never_copied` |
| C43 | Files apt itself does not read (`.save`, `.curtin.orig`, editor backups) in `sources.list.d` | never captured, so never a diff and never a derived write. `preferences.d` and `apt.conf.d` keep no extension filter — apt reads extensionless files there | U | apt:`TestWhatAptItselfReads::test_a_save_file_in_sources_list_d_is_never_captured`, `test_preferences_d_and_apt_conf_d_keep_no_extension_filter` |
| C44 | `/etc/apt/sources.list` | digested on both machines and always-synced, but never an item; an absent file yields no digest rather than an error | U | apt:`test_sources_list_is_digested_on_both_machines_and_is_still_not_an_item`, `test_an_absent_sources_list_yields_no_digest_rather_than_an_error` |
| C45 | The distribution's own source files | written when missing, overwritten when different, never emitted as a removal diff and never offered for removal | U | apt:`test_ubuntu_sources_is_never_offered_for_removal`, `test_the_distribution_files_are_written_when_they_differ` |
| C46 | Repository file and pin file extra on the target | two separate two-answer screens, both unticked, neither offered permanence, neither recordable; the repository is deleted before the pin that prefers it | U | apt:`TestTwoAnswerRemovals` (4), rev:`test_repo_removal_is_unticked_and_never_offered_permanence` |
| C47 | Repository present on both with different content, feeding no machine-specific package | overwritten silently: zero prompts, one write | U | apt:`test_a_changed_repository_with_no_machine_specific_package_is_overwritten_silently` |
| C48 | Same, but it feeds a package the TARGET recorded skip-always | a two-answer conflict entry carrying both whole file contents, target first, never a unified diff; found with one batched policy call | U | apt:`test_a_changed_repository_feeding_a_machine_specific_package_asks_and_shows_both_versions`, `test_the_conflict_computation_costs_one_batched_policy_call`, rev:`TestRepoConflictGroupResolution` (7) |
| C49 | Conflict answered overwrite / skip once | overwrite writes the source's version; skip once writes nothing AND fails every approved package whose origin depended on that file, naming it | U | apt:`test_overwriting_a_conflict_writes_the_sources_version`, `test_skipping_a_conflict_writes_nothing_and_fails_the_package_that_needed_it` |
| C50 | ESM sources pending and the target reports no Pro attachment | asked before any mutating command, exactly two answers, both named; "attach now" re-probes rather than trusting the answer, any number of times; "skip" raises `JobSkipped` for `apt_sync` alone | U | apt:`TestTheESMAttachmentGate::test_an_unattached_target_is_asked_about_before_anything_is_written`, `test_the_gate_offers_exactly_two_answers_and_names_both_of_them`, `test_attach_now_re_probes_and_continues_when_the_target_became_attached`, `test_attach_now_can_be_answered_any_number_of_times`, `test_choosing_skip_raises_job_skipped_and_writes_nothing` |
| C50a | Same, no TTY | `JobSkipped` naming both files, the unattached target and the absent TTY; nothing written, no review presented | U | apt:`test_a_non_interactive_run_skips_the_whole_job` |
| C50b | Target attached, or no ESM write pending, or the target's ESM files already match | no probe at all in the last two cases, no prompt in any | U | apt:`test_esm_sources_are_written_to_an_attached_target`, `test_a_source_with_no_esm_sources_never_probes_at_all`, `test_an_esm_file_the_target_already_matches_is_not_gated` |
| C50c | Pro probe unreadable — no binary, non-zero exit, unparseable output | treated as unattached, because that answer asks a question the user can act on | U | apt:`test_an_unreadable_pro_probe_is_treated_as_unattached` |
| C50d | Probe payload carrying the subscriber's account | never logged, never in the prompt, never in the skip reason | U | apt:`test_the_probe_payload_is_never_logged` |
| C50e | Dry run against an unattached target | zero gate prompts, one WARNING naming both files and stating a real run would skip the job | U | apt:`test_a_dry_run_never_prompts_about_attachment` |
| C51 | Dry run with derived `/etc/apt` work | the writes are previewed even though they have no review line, and none is issued | U | apt:`test_a_dry_run_previews_the_derived_writes_and_issues_none` |
| C52 | The source-file scan | runs against BOTH machines, from one always-present start point with `-path` selectors | U | apt:`test_the_source_file_scan_runs_against_both_machines`, `test_the_source_file_scan_selects_both_locations_from_one_start_point` |

## D. apt collateral (D-30) and metadata refresh (decision 1)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| D1 | Approved installs, simulation removes an auto-installed package | proceeds silently, no review item | U | apt:`test_auto_collateral_removal_produces_no_review_item`, `test_install_whose_only_collateral_is_auto_deps_proceeds` |
| D2 | Simulation removes a target-manual package | own COLLATERAL group item naming the trigger | U | apt:`test_manual_collateral_removal_becomes_a_collateral_review_item` |
| D3 | Simulation removes a package manual only on the SOURCE | not protected — the protected set is the target's `apt-mark showmanual` alone. Knowingly given up: if the target's apt installed it automatically, the target's apt owns it | U | apt:`TestSourceOnlyCollateral::test_source_only_manual_collateral_removal_is_not_a_review_item` |
| D4 | Simulation downgrades a manual package | collateral item; auto downgrade produces nothing | U | apt:`test_manual_downgrade_becomes_item_auto_downgrade_does_not`, `test_guard_allows_auto_downgrade` |
| D5 | Collateral resolved "install anyway" | install proceeds, guard permits the removal | U | apt:`test_install_anyway_proceeds_and_guard_allows_the_collateral_removal`, rev:`test_install_anyway_records_apply` |
| D6 | Collateral resolved "skip" | every triggering install left unapproved | U | apt:`test_skip_leaves_the_triggering_install_unapproved`, rev:`test_skip_records_skip_once` |
| D7 | Collateral resolved "abort" | `SyncAbortedByUser` naming the package | U | rev:`test_abort_raises_sync_aborted_by_user_naming_the_collateral_package` |
| D8 | Collateral prompt never happens mid-apply | classification is plan-time | U | apt:`TestPlanTimeCollateral` + `test_at_most_two_apt_get_dash_s_commands_regardless_of_package_count` |
| D9 | Real transaction drifted since plan time (manual removal) | apply-time guard refuses the item; source-manual-only collateral is allowed through at that guard too, matching D3 | U | apt:`test_guard_refuses_drifted_manual_removal_not_seen_at_plan_time`, `TestSourceOnlyCollateral::test_apply_time_guard_allows_source_only_manual_collateral` |
| D10 | Drifted manual downgrade | refused | U | apt:`test_guard_refuses_drifted_manual_downgrade` |
| D11 | `apt-get -s` itself fails | fail closed, never read as a clean preview | U | apt:`test_failed_simulation_raises_instead_of_returning_empty_preview`, `test_apply_time_simulation_failure_fails_the_item_not_silently_clean` |
| D12 | Approved removal whose transaction removes auto reverse-deps | proceeds | U | apt:`test_auto_reverse_dep_removal_proceeds` |
| D13 | Approved removal whose transaction removes an unreviewed manual reverse-dep | refused | U | apt:`test_drifted_manual_reverse_dep_removal_refused` |
| D14 | Two removals both approved, each removing the other | both proceed | U | apt:`test_both_removals_approved_the_first_proceeds` |
| D15 | Install-only run (no repo item changed) | exactly one `apt-get update` before the first install | U | apt:`test_install_only_run_refreshes_metadata_once_before_first_install` |
| D16 | That refresh fails | every install aborts, still only one `apt-get update` | U | apt:`test_failed_metadata_refresh_aborts_installs_with_a_single_update` |
| D17 | Repo item changed *and* installs approved | group's own update is the run's single refresh | U | apt:`test_repo_group_refresh_is_not_repeated_by_the_install_path` |
| D18 | Rollback's re-probe succeeded | later installs need no further refresh | U | apt:`test_post_rollback_install_issues_no_further_apt_get_update` |
| D19 | Approved install whose repository this run derives (D-34 class 3) | excluded from the plan-time rehearsal on the evidence of the target's `apt-cache policy`, never on the simulation's exit code, because apt refuses the whole batch on one unlocatable name. The resolvable candidates in the same run are still rehearsed and still protected | U | apt:`TestAPackageTheTargetCannotResolveYet` (3) |
| D19a | That package's manual collateral | discovered by the apply-time per-item rehearsal after `/etc/apt` converged, and fails that one item — the user is told afterwards rather than asked beforehand | U | apt:`TestTransactionGuard::test_guard_refuses_drifted_manual_removal_not_seen_at_plan_time` (the same guard); the class-3-specific path is asserted only through D19's exclusion |
| D20 | Machine-specific list is *not* consulted for collateral protection | accepted limitation (decision 8): manual-set membership alone decides protection | U | state:`TestDecisionScopeIsDiffFilteringOnly` (2) |

## E. snap (D-06, D-14, D-29, #208)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| E1 | Snap on source only | INSTALL at the source's exact revision, then channel switch | U V | snap:`test_missing_on_target_yields_install_diff`, `test_install_command_contains_an_explicit_revision`, INT:`test_snap_revision_converges_without_hold` |
| E2 | Snap on target only | REMOVE, own group, `snap remove` never `--purge` | U | snap:`test_extra_on_target_yields_remove_diff_in_its_own_group`, `test_removal_never_passes_purge` |
| E3 | Revision differs | CHANGE naming both revisions → `snap refresh --revision` | U | snap:`test_revision_change_yields_change_diff_naming_both_revisions` |
| E4 | Same revision, different channel | CHANGE naming both channels → `snap switch` only | U | snap:`test_same_revision_different_channel_yields_change_diff_naming_both_channels` |
| E5 | Identical revision + channel | no diff | U | snap:`test_identical_snap_yields_no_diff` |
| E6 | No command ever sets a standing hold (RESEARCH Pitfall 1) | install/change/retrack/remove hold-free | U V | snap:`test_install_change_retrack_and_removal_never_set_a_hold`, INT:`test_snap_revision_converges_without_hold` |
| E7 | `snap list --all` column order changes | parsed by header name | U | snap:`test_column_reordered_header_still_parses_correctly` |
| E8 | Disabled older-revision line | only the active revision becomes an item | U | snap:`test_disabled_revision_line_produces_no_item` |
| E9 | No snaps installed | empty list, no crash | U | snap:`test_no_snaps_installed_yields_empty_list_not_a_crash` |
| E10 | `held` in Notes | `SnapItem.held` set | U | snap:`test_held_note_sets_item_held` |
| E11 | Held on source only / target only / both | INSTALL `--hold=forever` / REMOVE `--unhold` / no diff | U | snap:`test_source_held_yields_install_hold_diff_and_converges_hold_forever`, `test_target_held_only_yields_remove_hold_diff_and_converges_unhold`, `test_both_held_yields_no_hold_diff` |
| E12 | Hold ordering after presence diffs | hold lands after its snap's install | U | snap:`test_hold_diff_emitted_after_presence_diffs` |
| E13 | Hold command never degenerates to bare `--hold` | snap name always interpolated | U | snap:`test_hold_converge_never_emits_bare_hold` |
| E14 | Hold recorded for a snap the source no longer has | not proposed (source is hold authority) | U | snap:`TestHoldIntentIsSourceAuthoritative::test_hold_on_a_snap_the_source_does_not_have_yields_no_hold_diff` |
| E15 | skip-always on `snap:hold:<name>`, next run | item inert, no diff | U V | blk:`TestSnapHoldDecisions` (3, incl. that the snap's own presence diff stays live), INT:`test_skip_always_on_a_snap_hold_is_inert_next_run`. Same `_drop_inert_diffs` pass as B9: the id first exists on the `ItemDiff`, so `filter_inert` cannot reach it |
| E16 | Classic- or devmode-confinement snap (`Notes: classic`/`devmode`) | `--classic`/`--devmode` threaded from the SOURCE item, on install and on refresh; confinement alone is never a diff | U | snap:`TestParseConfinement` (3), `TestConvergeConfinement` (5). The flag is passed unconditionally from the source because `snap refresh` preserves the TARGET's confinement — a strict target would otherwise stay strict forever |
| E17 | Sideloaded snap on the source (revision `x<N>`, `Notes: try`) | dropped from the diff input — no install, change or `snap:hold:` diff — and named in one WARNING; a target-only sideloaded snap is still a removal candidate | U | snap:`TestSideloadedSnaps` (6). Reproducing a sideloaded snap is deliberately not implemented (no mechanism carries the `.snap` bytes); the target's copy of a source-sideloaded snap is withheld too, so the drop cannot turn into a removal proposal |
| E18 | Snap absent on target but hold approved (D6) | per-item failure, loop continues | U | snap:`test_hold_for_a_snap_absent_on_target_fails_only_that_item` |
| E19 | `~/snap/<app>/<rev>` exclusion export | old revisions excluded, current-rev dir + `common` + `current` kept | U | snap:`test_excludes_old_revisions_keeps_current_common_and_current_symlink`, fold:`test_old_revision_excluded_current_kept` |
| E20 | `current` dangling or missing | all revision dirs excluded (safe default) | U | snap:`test_dangling_current_falls_back_to_excluding_all_revisions`, `test_missing_current_symlink_falls_back_to_excluding_all_revisions` |
| E21 | No `~/snap` at all | no filters | U | snap:`test_no_snap_directory_returns_empty`, fold:`test_no_snap_directory_yields_no_filters` |
| E22 | Revision converges but the target's snapd cannot fetch that revision | clean per-item failure, that snap's channel switch skipped, other snaps still converge | U | snap:`test_unfetchable_revision_is_a_clean_per_item_failure_not_a_crash` |

## F. flatpak (D-06, D-14, decision 7, #208 D-10)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| F1 | Ref on source only | INSTALL into the source's scope, from its origin, by FULL ref | U V | flat:`test_full_diff_taxonomy`, `test_install_names_the_full_ref_after_the_remote`, INT:`test_flatpak_derives_the_remote_its_ref_needs_and_carries_its_key` |
| F2 | Ref on target only | REMOVE (`uninstall -y`) by FULL ref, no source lookup needed | U | flat:`test_ref_removal_never_needs_source_lookup`, `test_uninstall_names_the_full_ref` |
| F3 | Same app, different scope on each machine | two items: one install + one removal, never a change | U | flat:`test_same_application_both_scopes_yields_two_distinct_identities`, items:`test_same_application_different_scope_yields_distinct_item_ids` |
| F4 | Same app, same scope, same branch, version differs | REPORT_ONLY (floats, D-04) | U | flat:`test_full_diff_taxonomy` |
| F4a | Same app, same scope, DIFFERENT branch | two items (install + removal), never a version mismatch; two branches of one id are two identities | U | flat:`test_a_branch_change_reads_as_install_plus_removal_never_a_version_mismatch`, `test_two_branches_of_one_application_in_one_scope_are_two_items` |
| F4b | Same app installed from two different remotes | origin stays OUT of identity (the install half of the pair could never run) | U | flat:`test_origin_stays_out_of_the_identity` |
| F5 | Remote missing on target | DERIVED from the approved ref, written before any ref install, never a review line | U V | flat:`test_no_remote_appears_in_any_review_group`, `test_a_remote_is_provisioned_before_the_ref_that_needed_it`, INT:`test_flatpak_derives_the_remote_its_ref_needs_and_carries_its_key` |
| F5e | Source remote that feeds no approved ref | does not travel; no distribution-remote exemption exists | U V | flat:`test_a_remote_no_approved_ref_needs_does_not_travel`, `test_declining_the_ref_declines_its_remote`, INT:`test_a_flatpak_remote_no_synced_ref_needs_does_not_travel` |
| F5f | Approved app whose runtime comes from another remote | that remote is derived too | U | flat:`test_the_runtime_an_approved_app_needs_brings_its_own_remote` |
| F5g | Derived write fails | fails every approved ref that named it, quoting flatpak's stderr; other refs still install | U | flat:`test_a_failed_derived_write_fails_only_the_ref_that_needed_it`, `test_a_failed_derived_write_names_the_remote_and_its_own_stderr` |
| F5a | Signed remote replicated to a machine that never had it | the source's own keyring travels byte-for-byte and is imported with `--gpg-import`, staged under the target's `~/.cache/pc-switcher` | U V | flat:`TestRemoteTrustTravelsWithTheDerivedWrite` (`test_signed_remote_is_added_with_the_sources_own_key`, `test_staging_stays_under_the_targets_own_home`, `test_every_staging_write_carries_mutates`, `test_staged_key_is_discarded_even_when_remote_add_fails`), INT:`test_flatpak_derives_the_remote_its_ref_needs_and_carries_its_key` |
| F5b | Trust capture per scope | verification read from the `options` column, key digest from that scope's own `<repo>/<remote>.trustedkeys.gpg` | U | flat:`TestRemoteTrustCapture` (8) |
| F5c | Unverified source remote | replicated with `--no-gpg-verify`; a verified one is never downgraded | U | flat:`test_unverified_source_remote_replicates_as_unverified`, `test_verified_source_remote_is_never_downgraded_even_if_the_target_is_unverified`, `test_change_to_an_unverified_source_remote_disables_verification` |
| F5d | Verified remote with no per-remote key (machine-level anchor) | added plainly, nothing invented; a key captured but missing at the write fails the ref that needed it | U | flat:`test_verified_remote_without_a_key_of_its_own_adds_plainly`, `test_missing_source_keyring_fails_the_ref_rather_than_provisioning_a_dead_remote` |
| F6 | `flathub` in both scopes | derived once per scope, never one shared provisioning | U | flat:`test_the_same_remote_in_two_scopes_is_derived_once_per_scope`, `test_a_user_scope_ref_derives_only_the_user_scope_remote` |
| F7 | Same-name, same-scope remote with a different URL, no machine-specific ref taking it as origin | repointed as derived mechanism (`remote-modify --url`), silently, with no review line | U | flat:`test_a_differing_url_is_repointed_with_no_review_line`, `test_a_remote_the_target_already_matches_is_not_written_at_all`, `test_a_target_only_ref_is_not_machine_specific_and_the_repoint_stays_silent` |
| F7e | Same repoint, but a ref the TARGET recorded skip-always takes that remote as its origin in that scope | a `flatpak:conflict:<scope>:<name>` entry on the two-answer screen carrying both configurations, target first, one differing facet per line and never a diff; detail names the machine-specific refs that are the reason; costs no command of its own | U | flat:`TestARepointThatMovesAMachineSpecificRefIsAsked` (6, incl. `test_the_entry_shows_both_configurations_target_first_and_never_a_diff`, `test_finding_the_conflict_costs_no_command_of_its_own`) |
| F7f | Conflict answered overwrite / skip once / not at all | overwrite repoints and installs; skip-once leaves the target's remote untouched and fails the dependent refs quoting the decision; an undecided entry is a skip, never a silent overwrite | U | flat:`test_overwrite_repoints_the_remote_and_installs_the_ref`, `test_skip_once_leaves_the_targets_remote_exactly_as_it_was`, `test_skip_once_fails_the_ref_that_needed_the_source_url_naming_the_decision`, `test_an_undecided_conflict_is_a_skip_not_a_silent_overwrite` |
| F7g | What is NOT a conflict | a remote the target lacks (that is an add), a remote no approved ref could need, and a signing-key-only difference — `--gpg-import` merges rather than replaces, so it can neither move an origin nor withdraw trust | U | flat:`test_a_remote_the_target_lacks_is_an_add_and_never_a_conflict`, `test_a_remote_no_approved_ref_could_need_is_never_a_conflict`, `test_a_signing_key_difference_alone_stays_silent` |
| F7h | Conflict screen's decision plumbing | neither a removal direction nor promotable; no `flatpak:conflict:` or `flatpak:remote:` id reaches a decision file in any direction, while masks keep the registry | U | flat:`test_the_conflict_screen_is_neither_a_removal_direction_nor_promotable`, `test_a_conflict_id_marked_skip_always_reaches_no_decision_file`, blk:`TestFlatpakMaskDecisions` (2) |
| F7b | Same-name remote pointing at ANOTHER repository, ref install approved | refused per ref naming both URLs, no install issued — the live wrong-vendor case | U | flat:`test_a_same_named_remote_pointing_elsewhere_refuses_the_install`, `test_the_derived_write_repointing_the_remote_lets_the_same_install_through` |
| F7c | `remote-add --if-not-exists` exits 0 and changes nothing | caught by re-reading the target, not by the exit code | U | flat:`test_a_remote_add_that_exited_zero_and_changed_nothing_refuses_the_install`, `test_a_remote_written_after_a_refusal_is_seen_on_the_next_attempt` |
| F7d | Ref lands from a repository that is not the source's | caught after the install by reading the origin back and resolving it to a URL | U | flat:`test_a_ref_that_landed_from_another_repository_fails_after_the_install`, `test_an_install_that_exited_zero_and_installed_nothing_fails_the_item` |
| F7a | Same-name, same-scope remote whose signing key or verification setting differs | one derived `remote-modify` carrying url + trust; a verification mismatch also refuses the ref install until it is fixed | U | flat:`test_verified_source_remote_is_never_downgraded_even_if_the_target_is_unverified`, `test_a_target_remote_that_does_not_verify_signatures_refuses_the_install` |
| F8 | Same URL and same trust | no write at all | U | flat:`test_a_remote_the_target_already_matches_is_not_written_at_all` |
| F9 | Ref whose origin remote is not on the target after this run's writes | refused with a named per-item failure, no doomed install | U | flat:`test_ref_with_missing_origin_remote_is_skipped_with_named_failure` |
| F10 | user vs system scope privilege | `sudo` iff system scope, for every verb including a derived remote write | U | flat:`test_user_scope_ref_install_has_no_sudo_and_carries_user_flag`, `test_system_scope_ref_install_uses_sudo_and_system_flag`, `test_system_scope_add_uses_sudo_and_still_stages_in_the_user_home` |
| F11 | Third named installation (neither user nor system) | line skipped, never guessed | U | flat:`test_unrecognized_installation_value_is_skipped` |
| F12 | Mask parsing (2-space prefix, wildcards, blank lines) | patterns per scope | U | flat:`test_parses_two_leading_space_format_and_wildcard_patterns`, `test_blank_lines_skipped_and_scope_is_the_passed_argument`, `test_no_masks_yields_empty_list` |
| F13 | Mask on source only / target only / both | INSTALL `mask` / REMOVE `mask --remove` / no diff | U | flat:`test_source_user_mask_absent_on_target_yields_install`, `test_target_only_system_mask_yields_removal`, `test_mask_present_on_both_yields_no_diff` |
| F14 | Mask ordering | derived remote writes → refs → masks | U | flat:`test_masks_ordered_after_refs_in_diffs_tuple`, `test_a_remote_is_provisioned_before_the_ref_that_needed_it` |
| F15 | Mask pattern edited on source | reads as remove-old + add-new, never a CHANGE | U | flat:`test_edited_pattern_reads_as_two_membership_diffs_never_a_change` |
| F16 | Mask scope moved user→system | add + remove | U | flat:`test_scope_move_reads_as_add_system_plus_remove_user` |
| F17 | Mask replicated whether or not a matching ref is installed | pure pattern | U | flat:`test_mask_replicates_even_when_its_pattern_matches_no_installed_ref` |
| F18 | System-scope mask on either machine | target sudo required | U | flat:`test_system_scope_mask_requires_target_sudo` |
| F19 | User-scope-only diff | sudo never checked | U | flat:`test_user_scope_only_mask_never_checks_sudo`, `test_user_scope_only_never_checks_sudo` |
| F20 | skip-always on a mask, next run | inert | U | flat:`TestMaskSkipAlways::test_recorded_mask_produces_no_diff_on_the_next_run`, blk:`TestFlatpakMaskDecisions` (2) |
| F21 | Remote removed while a target ref still uses it | removal offered on its own TWO-answer screen with the dependent target refs named in its `detail` (same scope only); never recordable; not refused | U | flat:`TestRemoteRemovalOrphansRefs` (6, incl. `test_removal_offers_exactly_two_answers_and_is_never_recordable`) |
| F22 | `~/.local/share/flatpak` exclusion, `~/.var/app` never excluded | store owned by the job, data by folder_sync | U | flat:`test_returns_flatpak_data_dir_excludes_var_app`, fold:`test_flatpak_data_dir_included_var_app_never_mentioned` |
| F23 | Same ref, same scope, same branch, installed from two DIFFERENT remotes on the two machines | `ORIGIN_MISMATCH` → REPORT_ONLY naming both remotes and both URLs, ahead of the version branch; never converged | U V | flat:`TestRefOriginMismatch` (10) — `test_two_differently_named_remotes_yield_one_report_only_diff`, `test_origin_mismatch_outranks_a_version_mismatch`, `test_the_mismatch_is_reported_and_never_converged`; INT:`test_one_ref_from_two_vendors_is_reported_with_both_urls` (same-named remote repointed at the beta URL, so only the URL comparison can see it) |
| F24 | Source remote carrying a `filtered` option | one WARNING per DERIVED remote whose source is filtered, naming the remote, its scope and the re-apply command; fires in a dry run too; the remote still travels, unfiltered | U V | flat:`TestFilteredRemoteWarning` (6) — `test_a_derived_remote_whose_source_is_filtered_warns_once`, `test_the_warning_fires_in_a_dry_run_too`, `test_a_filtered_remote_no_approved_ref_needs_never_warns`; INT:`test_a_filtered_source_remote_warns_once_and_travels_unfiltered` (asserts the real `filtered` token on the VM before the run) |

## G. manual installs and snippets (D-18…D-23, decision 9, corrected D-23)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| G1 | Source-manual package with no apt candidate on the source | unreproducible item | U | man:`test_no_candidate_source_package_becomes_unreproducible_diff` |
| G2 | Unowned paths under `/usr/local`, `/opt`, `/usr/local/{bin,lib}` | unreproducible items; scan bounded to those roots | U | man:`test_scan_unowned_installs_yields_two_items_from_four_candidates`, `test_unowned_scan_queries_only_usr_local_and_opt` |
| G3 | Item with a snippet in the **source** registry | INSTALL → replayed on the target | U V | man:`test_item_with_snippet_plans_install_and_converges_by_replaying_it`, `test_source_snippet_classifies_install`, INT:`test_manual_installs_sync_pushes_registry_and_replays_snippet` |
| G4 | Snippet only on the **target** | still REPORT_ONLY (source is the authority) | U | man:`test_target_only_snippet_stays_report_only` |
| G5 | Item with no snippet | REPORT_ONLY in its own resolution group | U | man:`test_item_without_snippet_is_report_only_and_grouped_separately` |
| G6 | Snippet authored on the fly during review | persisted to source registry → pushed → replayed the same run | U | man:`test_on_the_fly_snippet_is_replayed_the_same_run`, `test_snippet_authored_in_review_is_persisted_before_the_push`, `test_push_runs_after_review_and_before_replay_in_execute` |
| G7 | Snippet body stored verbatim (whitespace preserved) | never parsed or trimmed | U | rev:`test_add_snippet_choice_captures_body_verbatim_including_whitespace`, state:`test_add_then_get_round_trips_body_verbatim_including_whitespace` |
| G8 | Empty snippet body submitted | re-prompt the three-way choice, never fall through | U | rev:`test_empty_snippet_body_reprompts_until_a_real_choice`, `test_empty_snippet_then_real_snippet_is_captured` |
| G9 | Ctrl-C / EOF at the resolution choice | aborts the whole sync | U | rev:`test_cancelled_select_aborts_the_entire_sync` |
| G10 | "Skip for now" | a real resolution; run stays clean | U | rev:`test_explicit_skip_once_is_a_resolution_not_unresolved`, man:`test_run_whose_only_items_were_skipped_once_passes` |
| G11 | "Record as machine-specific" | DecisionEntry on the source; no snippet | U | rev:`test_skip_always_choice_yields_skip_always_decision_and_no_snippet`, core:`test_skip_always_on_unreproducible_item_records_on_source` |
| G12 | Item already recorded machine-specific | no diff next run | U | man:`test_machine_specific_item_is_filtered_before_becoming_a_diff` |
| G13 | Snippet vanished between plan and replay | failed CommandResult, per-item failure, no crash | U | man:`test_missing_snippet_at_converge_is_a_failed_result_not_a_crash`, state:`test_replay_with_no_registered_snippet_returns_a_failed_result_not_a_raise` |
| G14 | Snippet replay | `bash -c '<body>'`, unprivileged, `login_shell=False`, no stdin, exit code decides | U | state:`test_replay_passes_body_as_one_quoted_argument_with_login_shell_false`, `test_replay_exit_code_alone_decides_success` |
| G15 | Registry push destination | under the SSH user's home, never `/etc` | U | man:`test_push_sends_source_registry_under_the_user_home_never_etc` |
| G16 | No source registry file | push is a no-op | U | man:`test_absent_source_registry_makes_push_a_noop` |
| G17 | Push is purely additive (target ⊆ source, identical bodies) | proceeds silently | U | man:`test_additive_overwrite_proceeds_without_confirming`, `test_identical_target_entry_is_additive` |
| G18 | Target holds an entry absent from source | show entries, require confirmation; approve → push | U | man:`test_lost_target_entry_prompts_and_proceeds_on_confirm` |
| G19 | Same, declined | abort the run, send nothing | U | man:`test_lost_target_entry_aborts_on_decline` |
| G20 | Target entry with a differing body | non-additive → prompt | U | man:`test_changed_body_is_non_additive_and_prompts` |
| G21 | Non-interactive run, non-additive push | abort (no override flag exists) | U | man:`test_non_interactive_non_additive_aborts` |
| G22 | No confirmer injected on a non-additive push | job failure, nothing sent | U | man:`test_non_additive_push_without_a_confirmer_fails_and_sends_nothing` |
| G23 | Job runs with `apt_sync` disabled/absent | independent enable flag | U | man:`test_plan_runs_with_apt_absent_from_config_and_manual_enabled`, cfg:`test_manual_installs_sync_is_an_accepted_job_name` |
| G24 | Install-only: never proposes removals | no target manifest | U | man:`TestInstallOnly` (2 — empty target query by design, and no removal diff or group even when the target holds items) |
| G25 | Empty detection | no group, nothing applied | U | man:`test_empty_detection_produces_no_group_and_applies_nothing` |
| G26 | Failed snippet replay | per-item failure, job continues | U | man:`test_failed_snippet_replay_is_a_per_item_failure_and_does_not_stop_the_job` |
| G27 | Malformed / empty registry file | degrades to no snippets + WARNING | U | state:`test_malformed_registry_returns_empty_mapping_and_warns_naming_the_path`, `test_empty_file_returns_empty_mapping` |
| G28 | Registry write | atomic tmp-then-`mv`, unrelated entries preserved | U | state:`test_write_is_atomic_temp_then_move`, `test_add_preserves_an_unrelated_pre_existing_entry` |
| G29 | Registry excluded from `config_sync` | `config.yaml` only | U | state:`test_copy_config_to_target_sends_only_config_yaml` |
| G30 | Snippet that prompts (needs stdin) | fails rather than hangs | U | man:`TestPromptingSnippetCannotHang::test_replay_supplies_no_stdin_and_a_prompting_snippet_is_a_plain_item_failure` |

## H. Three-way decision, direction, and where it is recorded (D-07, D-08, D-08a)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| H1 | APPLY on an INSTALL-direction item | converge; SKIP_ONCE items reach no command | U | apt:`test_only_apply_decision_installs_skip_once_never_sent`, core:`test_ticking_only_install_group_yields_zero_removal_commands` |
| H2 | APPLY on REMOVE / CHANGE | routed to `converge()` | U | core:`test_remove_diff_produces_exactly_one_target_converge_call`, `test_change_diff_reaches_converge_alongside_install_and_remove` |
| H3 | APPLY on REPORT_ONLY | never converged | U | core:`test_report_only_diff_produces_zero_target_commands` |
| H4 | skip-always on an INSTALL item | recorded on the **source** | U | state:`test_skip_always_on_install_writes_to_source_not_target` |
| H5 | skip-always on a REMOVE item | recorded on the **target**, through the remote executor | U | state:`test_skip_always_on_remove_writes_to_target_not_source`, `test_target_side_write_issues_no_local_filesystem_write` |
| H6 | skip-always on a CHANGE item | recorded on the source | U | state:`test_skip_always_on_change_writes_to_source_not_target` |
| H7 | skip-always is reachable from the review at all | a second "never offer again on this machine?" checkbox per actionable group, over the entries the apply list left unticked (D-24: a list to tick, not a per-item question queue) | U | perm:`TestPermanentSkipPromotion` (6 — install/change/remove groups, per-group pass, empty and fully-ticked cases), `TestBlockStateItemsArePromotable` (2 — hold add, mask remove) |
| H8 | Recorded item, this machine as source | not pushed | U V | state:`test_source_held_inert_item_absent_from_the_plans_diffs`, INT:`test_skip_always_is_inert_in_both_roles` |
| H9 | Recorded item, this machine as target | never installed/removed here | U V | state:`test_target_held_inert_item_absent_even_though_source_also_differs`, INT:`test_skip_always_is_inert_in_both_roles` |
| H10 | Nothing recorded during `plan()` | writes happen only in `apply()` | U | state:`test_plan_issues_no_decision_file_write`, `test_every_record_call_originates_from_apply_not_plan` |
| H11 | Dry run | no decision written | U | state:`test_no_record_call_when_dry_run` |
| H12 | Non-interactive outcome | no decision written | U | state:`test_no_record_call_when_outcome_was_not_interactive` |
| H13 | Decision file absent / empty / malformed | degrade to "no decisions"; only malformed warns | U | state:`TestDecisionFileLoad` (6 tests) |
| H14 | Re-recording the same item | no duplicate; other entries preserved | U | state:`test_recording_same_item_id_twice_does_not_duplicate`, `test_recording_a_second_distinct_item_preserves_the_first` |
| H15 | Decision file write | atomic tmp-then-`mv` | U | state:`test_write_is_atomic_temp_then_move` |
| H16 | One file per manager, under `~/.config/pc-switcher` | template + glob | U | state:`TestRelpathConstants` |
| H17 | No default machine-specific entry hardcoded in Python | example YAML only | U | state:`test_no_default_machine_specific_package_hardcoded` |
| H18 | User deletes an entry by hand | item live again next run | U | state:`TestHandEditedDecisionFile` (2 — one entry deleted, whole file deleted) |
| H19 | skip-always on a REPORT_ONLY item (version mismatch, repo-unavailable, origin mismatch) | never offered: D-08a has no holder machine for an item with no converge verb, and a recorded skip-always on a VERSION_MISMATCH would drop the package from syncing entirely rather than stop reporting the drift. Resolved by fixing the underlying condition (ADR-020 D-07) | U | perm:`test_report_only_group_is_never_offered_permanence`; `_drop_inert_diffs` passes REPORT_ONLY diffs through untouched for the same reason |
| H20 | Two-answer screens (repository removal, pin removal, repository conflict, flatpak remote removal, flatpak remote conflict) | unticked where they are checkbox groups, never offered permanence, and structurally unable to reach a decision file in any direction | U | rev:`test_repo_removal_is_unticked_and_never_offered_permanence`, `TestRepoConflictGroupResolution` (7), blk:`test_no_repository_or_pin_id_can_reach_a_decision_file`, flat:`test_removal_offers_exactly_two_answers_and_is_never_recordable`, `test_a_conflict_id_marked_skip_always_reaches_no_decision_file` |

## I. Review presentation and interaction (D-24, D-25, D-26)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| I1 | One group per action, fixed order install→change→remove→report | stable regardless of diff order | U | core:`test_four_diffs_produce_four_groups_keyed_by_action`, `test_group_emission_order_is_install_change_remove_report` |
| I2 | Removal groups never share a screen with installs | separate prompts | U | rev:`test_no_group_mixes_install_and_removal_entries_in_one_prompt` |
| I3 | Install/change default-ticked, removal unticked | bulk tick cannot delete | U | rev:`test_install_group_defaults_checked_removal_group_defaults_unchecked` |
| I4 | Removal group title names the verb, never "apply" | concrete action wording | U | rev:`test_removal_group_title_names_concrete_verb`, core:`test_removal_group_title_names_a_removal_verb_never_apply` |
| I5 | Verb fallback for a class with no vocabulary entry | reads "report"; every uncovered `(item_class, action)` pair still reaches review with a usable verb | U | core:`test_report_only_falls_back_to_report_for_a_class_with_no_vocabulary_entry`, `test_every_pair_without_a_vocabulary_entry_still_produces_a_usable_group` |
| I6 | Hold/mask groups keep their own verb when sharing an action with packages | "Hold …" not "Install …"; package/ref/remote groups exclude them | U | see B5 — apt/snap/flat all assert both the own-verb and the exclusion side |
| I7 | Ctrl-C / EOF at a checkbox screen | abort the whole sync | U | rev:`test_checkbox_ctrl_c_aborts_the_entire_sync` |
| I8 | Live display around a blocking prompt | pause before, resume in `finally`, even on raise | U | rev:`test_ui_resumed_when_prompt_raises`, `test_pause_and_resume_both_run_when_the_underlying_prompt_raises`, `test_ui_resumed_when_snippet_capture_raises` |
| I9 | Blocking `.ask()` off the event loop | `asyncio.to_thread` | U | rev:`test_synchronous_sleep_in_ask_does_not_block_loop` |
| I10 | Untrusted labels containing `[...]` | wrapped in `Text`, no MarkupError | U | rev:`test_bracketed_collateral_label_renders_without_markup_error` |
| I11 | Non-interactive run | prompt nothing, everything SKIP_ONCE, groups printed, warning with count | U V | rev:`TestNonInteractive`, INT:`test_non_interactive_skip_all` |
| I12 | Non-interactive + unreproducible items | all reported unresolved, nothing recorded | U | rev:`test_non_interactive_offers_no_capture_and_marks_every_item_unresolved` |
| I13 | Non-interactive + collateral entries | SKIP_ONCE, not "unresolved" | U | rev:`test_non_interactive_collateral_entries_skip_once_and_are_not_unresolved` |
| I14 | Automation env var | mapped decisions, no prompt, absent from `--help` | U | rev:`test_automation_env_returns_mapped_decisions_without_prompting`, `test_env_var_not_mentioned_in_cli_help` |
| I15 | Malformed automation JSON, or a decision value it does not know | fails loudly, prompts nothing | U | rev:`test_malformed_automation_json_fails_loudly_and_prompts_nothing`, `test_unknown_decision_value_in_automation_json_fails_loudly` |
| I16 | Unreproducible/collateral groups never rendered as checkboxes | sentinel actions take their own flow | U | rev:`test_unreproducible_group_never_offered_as_a_checkbox`, `test_collateral_group_never_offered_as_a_checkbox` |
| I17 | Each manager reviews before its own first mutation | per-manager, never cross-manager | U V | core:`test_call_order_is_plan_review_accept_review_apply`, INT:`test_each_manager_reviews_before_its_own_mutation` |
| I18 | Zero-diff run | review still called once | U | core:`test_zero_diff_run_still_calls_review_once` |
| I19 | No reviewer injected | loud failure, no converge | U | core:`test_missing_reviewer_raises_and_issues_no_converge`, apt:`test_execute_without_a_reviewer_raises_and_issues_no_command` |
| I20 | `plan()` issues no mutating command | read-only | U | apt:`test_plan_issues_no_mutating_command`, snap:`test_plan_issues_no_mutating_snap_command`, flat:`test_plan_issues_no_mutating_flatpak_command` |
| I21 | `plan()` failure | propagates out of `execute()` unchanged | U | core:`test_plan_failure_propagates_out_of_execute_unchanged` |
| I22 | Real-TTY questionary rendering with the Rich Live panel | clean hand-back, no corruption | — | UAT-only (02-UAT.md test 1); no automated coverage |
| I23 | Ctrl-C, a raising prompt, or `[...]` in a label at the skip-always checkbox | abort the whole sync / resume the UI in `finally` / render literally | U | perm:`TestPromotionAbortAndTeardown` (3); non-interactive runs prompt nothing (`test_non_interactive_run_prompts_nothing`), and the unreproducible and collateral groups keep their own flows (`TestGroupsNeverOfferedPromotion`) |

## J. Failure isolation, exit code, dry run

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| J1 | Item 2 of 3 fails | all attempted, one failure raised at the end | U V | apt:`test_second_of_three_fails_all_attempted_one_failure_raised`, INT:`test_continue_on_item_failure` |
| J2 | One package job's items fail | that job FAILED, remaining jobs still run | U | core:`test_failing_package_job_does_not_cancel_remaining_jobs` |
| J3 | Any other exception from a job | aborts the run | U | core:`test_other_exception_types_still_abort_the_run` |
| J4 | Session status / exit code | derived from `job_results`, non-zero when items failed | U V | exit:`TestSessionStatusReflectsJobResults`, `TestCliExitCodeFromSessionStatus`, INT:`test_continue_on_item_failure` |
| J5 | Dry run | zero mutating commands across all four action types | U V | apt:`test_dry_run_issues_no_mutating_command`, core:`test_dry_run_zero_mutating_commands_across_all_four_action_types`, INT:`test_apt_sync_dry_run_changes_nothing` |
| J6 | Dry run + on-the-fly snippet | previewed as an install, no replay, no registry write | U | man:`test_dry_run_previews_on_the_fly_install_without_replay_or_write` |
| J7 | Dry run + registry push | nothing transferred | U | man:`test_dry_run_pushes_nothing` |
| J8 | Dry run + finalize hooks | no writes | U | core:`test_no_finalize_writes_during_dry_run` |
| J9 | Unresolved items never fail an interactive run | decision 10 | U | man:`test_interactive_unresolved_no_longer_fails_the_run`, rev:`TestUnresolvedNeverFailsTheJob` |
| J10 | Second consecutive run after a successful convergence | no diffs (idempotency) | U V | core:`TestIdempotency::test_identical_source_and_target_produce_no_diff_no_group_and_no_mutation` (shared pipeline, holds included, zero `mutates=` commands), INT:`test_second_consecutive_sync_has_nothing_to_do` — full apt/snap/flatpak target state unchanged, and the converged item's SKIP_ALWAYS is never recorded (proof it was never presented) |
| J11 | Non-interactive run, non-empty plan | `JobSkipped` before `after_review()` — so no registry push either — and the job records SKIPPED rather than a SUCCESS it did not earn. No dry-run exemption: a rehearsal nobody could answer decided nothing either | U | core:`test_a_non_interactive_package_review_skips_the_job_instead_of_applying_nothing` |
| J12 | Non-interactive run, EMPTY plan | still SUCCESS: the target already matches | U | core:`test_an_empty_plan_is_still_a_success` |
| J13 | A job raising `JobSkipped` | orchestrator records SKIPPED, does not re-raise, and the next job still executes; the session still completes | U | tests/unit/orchestrator/test_skipped_jobs.py:`TestSkippedJobArm` |
| J14 | Enabled job name that resolves to no class | a SKIPPED `JobResult` exists for it rather than no result at all | U | tests/unit/orchestrator/test_skipped_jobs.py:`TestUnresolvableEnabledJob` (2) |
| J15 | Other jobs with nothing applicable | `folder_sync` with no enabled folders, `vscode_state_sync` with no handled state DB on the source | U | fold:`test_a_job_with_no_active_folders_is_skipped`, tests/unit/jobs/test_vscode_state_sync.py — both asserting `JobSkipped` |

## K. Validation / preflight (validate-not-mid-execute rule)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| K1 | apt: `apt-mark` missing on either end | validation error | U | apt:`test_apt_mark_unavailable_yields_validation_error` |
| K2 | apt: no passwordless sudo on target | error naming the binaries | U | apt:`test_target_without_passwordless_sudo_yields_validation_error_naming_the_binaries` |
| K3 | apt: no passwordless sudo on **source** | error (else capture silently degrades) | U | apt:`test_source_without_passwordless_sudo_yields_validation_error` |
| K4 | apt: dpkg frontend lock held | distinct error | U | apt:`test_dpkg_lock_held_yields_distinct_validation_error` |
| K5 | snap: binary missing on either end | error | U | snap:`test_snap_unavailable_on_source/target_yields_validation_error` |
| K6 | snap: sudo missing on source or target | error on each | U | snap:`test_source_/test_target_without_passwordless_sudo_yields_validation_error` |
| K7 | snap: pre-existing `refresh.hold` | logged, never an error, never mutated by validate | P | code logs it; no test asserts the read-only informational path |
| K8 | flatpak: binary missing | error, never an exception | U | flat:`test_flatpak_unavailable_on_source/target...` |
| K9 | flatpak: sudo only when system scope in play | gated check | U | flat:`test_system_scope_item_present_without_sudo_yields_validation_error`, `test_user_scope_only_never_checks_sudo` |
| K10 | manual: `apt-cache`/`dpkg` missing on source | error | U | man:`test_apt_cache_unavailable_on_source_...`, `test_dpkg_unavailable_on_source_...` |
| K11 | Sudo hint content | names binaries, visudo, drop-in, verification | U | tests/unit/test_sudoers.py |
| K12 | First-sync scope self-description per job | each package job contributes one scope, in order | U | tests/unit/orchestrator/test_first_sync_scope.py |

## L. Orchestration, config, folder_sync overlap (D-17, D-29, D-32)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| L1 | Package job listed after `folder_sync` | ConfigError naming the job | U | cfg:`TestPackageJobsBeforeFolderSyncStructuralCheck` (5 tests) |
| L2 | Shipped default config | all four jobs disabled, no empty sections, still validates | U | cfg:`test_package_jobs_ship_disabled`, `test_shipped_config_omits_empty_package_sections`, `test_config_omitting_package_sections_validates` |
| L3 | Job discovery resolves each name to its class | 4/4 | U | apt/snap/flat/man:`TestJobDiscovery` |
| L4 | Decision files excluded from folder_sync non-overridably, before user filters | glob emitted global-first | U | fold:`TestDecisionFileExcludeFilters` (6 tests, incl. a user `+` rule) |
| L5 | snap/flatpak exclusions gated on their enable flags | present only when enabled | U | fold:`TestPackageJobExcludeFiltersGating` (6 tests) |
| L6 | snapd auto-refresh paused across the run when snap_sync enabled | timed `refresh.hold` on both hosts, capture first | U | hold:`test_hold_set_on_both_hosts_when_snap_sync_enabled`, `test_capture_is_read_only_and_precedes_the_set` |
| L7 | snap_sync disabled / dry run | no hold written | U | hold:`test_hold_not_set_when_snap_sync_disabled`, `test_hold_skipped_in_dry_run` |
| L8 | Prior hold restored exactly (timestamp or `forever`), or cleared | per host, idempotent, no-op when never engaged | U | hold:`TestRestore` (4 tests) |
| L9 | Hold never substitutes for or blocks `--revision` convergence | writes only `refresh.hold` | U V | hold:`test_hold_only_writes_refresh_hold_never_a_snap_refresh_command`, INT:`test_snap_revision_converges_without_hold` |
| L10 | Per-snap `held` capture inside the hold window (#208 D9) | system hold does not mask per-snap holds | V | INT:`TestSnapHoldCaptureTiming` — `test_system_refresh_hold_does_not_mask_a_per_snap_held_note` (snapd semantics, both directions) and `test_per_snap_hold_replicates_through_a_real_sync_window` (end to end). The D9 verdict is UNDETERMINED until these run against real snapd: the code's premise is that the two live in different snapd namespaces |
| L11 | Crash mid-run leaves no standing hold | timed value self-expires | P | timed value asserted; no test simulates a crash |

## M. `--confirm-each-command`

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| M1 | Read-only command | never gated | U | gate:`test_read_is_never_gated` |
| M2 | Write | gated with the verbatim command, including login-shell wrapping | U | gate:`test_write_is_gated_with_the_verbatim_command`, `test_gate_sees_the_login_shell_wrapped_command` |
| M3 | Abort | command never runs | U | gate:`test_abort_prevents_the_command`, `test_abort_raises_and_resumes_ui` |
| M4 | `send_file` | both paths shown, abort precedes the transfer | U | gate:`test_send_file_shows_both_paths_and_aborts_before_transfer` |
| M5 | Unanswerable prompt (EOF/Ctrl-C) | abort, never a silent proceed | U | gate:`test_unanswerable_prompt_aborts_never_proceeds` |
| M6 | Markup characters in the command | literal text, no MarkupError | U | gate:`test_command_with_markup_characters_does_not_raise` |
| M7 | Prompt labelled with the active job and host | context correct | U | gate:`test_active_job_labels_the_prompt`, `test_local_executor_reports_the_source_host` |
| M8 | No gate configured | pass-through | U | gate:`test_no_gate_configured_is_a_plain_pass_through` |
| M9 | In-process write escape hatch | gated | U | gate:`test_declare_modification_gates_an_in_process_write` |
| M10 | snapd hold apply/restore | declared as mutations on both hosts; restore names the value | U | hold:`TestConfirmEachCommandGate` (4 tests) |
| M11 | Abort at teardown | honoured but resources still released | U | hold:`test_cleanup_honours_the_abort_but_still_releases_resources` |
| M12 | Flag refused without a TTY | `cli.sync` exits 1 naming the flag, before config load or connect; an ordinary non-interactive run is unaffected | U | cli:`TestConfirmEachCommandFlag` (3) |
| M13 | Every write anywhere in `src/` carries `mutates=` | no ungated write | U | audit:`TestMutatesCoverage` (3) — an AST walk over every executor call site, each ungated one accounted for as a read or a listed exemption. `folder_sync`'s rsync pass is the only exemption (#209); the audit is a live guard, not an xfail, so a new ungated write fails it |
| M14 | Decision-file / registry writes on **both** machines gated | prompt per role, reads never prompt, declining leaves the file untouched | U | gate:`TestStateWritesReachTheGate` (5, parametrised over source and target) |

## N. Cross-run and two-machine compositions

These are the narrative scenarios. Each is a composition of the branches above; none of them is covered as a whole except where noted.

| # | Narrative | Expected | Cov |
| --- | --- | --- | --- |
| N1 | skip-always in run 1 → run 2 shows no diff, in both roles | inert both ways | V INT:`test_skip_always_is_inert_in_both_roles` |
| N2 | Converge in run 1 → run 2 is empty | idempotent | V INT:`test_second_consecutive_sync_has_nothing_to_do` (see J10) |
| N3 | skip-always on a **hold/mask** in run 1 → run 2 | inert | U V INT:`TestBlockStateDecisionRoundTrip` (apt and snap holds, each over two real runs); the mechanism is asserted per manager at unit level by B9, E15 and F20 |
| N4 | A→B installs P; B→A later removes P | removal propagates as an unticked review item | V INT:`test_install_propagates_then_reversed_removal_needs_approval` — three runs: install, reversed-direction removal left undecided (no effect), then approved |
| N5 | New apt source + key + package on A → A→B installs all three in order | key→source→update→install. The repository and key are DERIVED from the package's own approval, so only the package is ever ticked | U apt:`test_key_then_source_then_update_then_package_install` (single run, mocked), `test_a_package_apt_reports_no_candidate_for_is_withheld_from_the_first_pass`, `test_a_package_apt_has_never_heard_of_prints_no_block_and_is_still_offered` |
| N13 | Repository this run derives supplies a package the target's apt has no candidate for at plan time | installed in ONE review: the package is classified from the SOURCE's origins, which the run never mutates, so its actionability never depends on a repository not yet written | U apt:`TestOneReviewPerRun` (2 — `test_a_package_the_target_had_no_candidate_for_is_installed_in_one_review` asserts exactly one reviewer call for the whole `execute()`) |
| N6 | Package uninstalled + source removed on B → B→A | a package removal item and a two-answer repository removal on A, both unticked; the key the repository removal orphans goes with it, with no item and no prompt | P (C18 + C46 + A3 separately; not as a narrative, and there is **no** "this was the last package from that source, remove it too?" prompt — that requirement is not implemented) |
| N7 | Machine-specific package R on B needs source S; A removes S → A→B | S offered for removal, unticked, naming R in its detail so the user can decline knowingly | U apt:`TestRepoRemovalNamesMachineSpecificPackages` (C26) — single run, mocked; the two-machine composition is untested |
| N8 | Dependency-only package Q kept/removed by apt's own bookkeeping across a P install/remove round trip | pc-switcher never touches Q | P — the single-run half is apt:`test_auto_installed_dependency_produces_no_diff_of_any_kind` (A10); the round trip is asserted nowhere |
| N9 | Package manual on A, arriving on B as an automatic dependency, later removed as collateral | NOT protected. The protected set is B's own `apt-mark showmanual` alone — knowingly given up, since reclaiming a package on the strength of the other machine's bookkeeping is a guess | U apt:`TestSourceOnlyCollateral` (2 tests, both asserting the absence) |
| N10 | Snippet authored on A in run 1 → run 2 from B (registries diverge) | non-additive push guarded | U man:`TestSnippetRegistryOverwriteGuard` (6 tests) — single run only |
| N11 | Two machines, full four-job run, three success criteria end to end | — | — UAT-only (02-UAT.md test 2) |

## Findings

### Open defects and unimplemented requirements

- N6 — no "this was the last package from that source, remove it too?" prompt. Source removals propagate only because the source machine's own files disappeared, as unticked items; the key that removal orphans then goes on its own, with no item and no prompt. Example narrative 2 is not implemented.

### One review per job, and why nothing invalidates it

A package's classification depends on the SOURCE's origins, and no run mutates the source, so nothing a run writes to the target can make an answer already given wrong. The one fact that does depend on this run's writes — which origin actually ends up supplying a package — is not guessed at plan time at all: it is re-read after the group's single `apt-get update` and turned into a per-item refusal (A17), never a second question. Every apt prompt therefore precedes the job's first mutating command, the ESM gate included.

The one thing this costs is a plan-time collateral classification for a package whose repository the same run derives (D19): apt cannot say what it would remove for a name it cannot yet resolve, so that package's manual collateral is caught by the apply-time guard and reported afterwards instead.

### Coverage gaps in behaviour believed correct

- C5 — a repo described by both a `.list` and a `.sources` file on one machine. Identity-by-filename is asserted; the coexistence case is not.
- K7 — snap `validate()` logs a pre-existing system `refresh.hold` informationally and never mutates it. No test asserts the read-only path.
- L11 — the hold is written with a timestamp so a crashed run self-expires. The timed value is asserted; no test simulates the crash.
- N8 — the round trip in which apt's own bookkeeping keeps or drops a dependency-only package. The single-run half is covered (A10).
- I22/N11 — real-TTY review rendering and the two-machine walkthrough are UAT-only by nature.
- C38–C42 — the three-directory resolution and the `dpkg -S` ownership rule are asserted against mocked output only. The `dpkg -S` output shape and its non-zero exit on any unowned path were verified by hand on a real Ubuntu 24.04 machine, as was the survey that motivated the fix: 11 of that machine's 20 `sources.list.d` files classified as dangling before it (9 pointing into `/usr/share/keyrings`, 2 inline-armored PPAs), 0 after. No VM test exercises either against a live dpkg.
- C26 — the source removal impact is asserted against mocked `apt-cache policy` output only. The output shape was verified by hand against a real Ubuntu 24.04 machine carrying both deb822 and legacy repositories (every installed package with a repository origin resolved to a source file, no unmatched origin), but no VM test exercises the parse against a live apt.

### Accepted scope limits

- C26 names only **machine-specific** packages, not every target-installed package from the repository being removed. A skip-always package is structurally invisible — `filter_inert` keeps it out of the target manifest, so it can never produce an `ItemDiff` in any run — and skip-always is an explicit user statement that this machine keeps it; both make the silent repo deletion a broken promise. An ordinary package is at least eligible for its own removal diff, and keying off the whole manual set would make the detail's length a property of the machine (a base-repo deletion would name over a hundred packages on a normal desktop and inform nobody). The narrower scope also keeps the cost at two batched commands gated on a removal actually being offered.

### Snap and flatpak end-to-end coverage was vacuous

Every snap and flatpak `V` in this matrix was, until now, a test that could not run at all. Each one searched the VMs for a subject — a snap outside the removal denylist, a snap with an alternate installable revision, an installed flatpak ref with a configured remote — and called `pytest.skip` when it found none. The VM baseline contained only `snapd`, `core*` and `bare` (every one of them denylisted) and no flatpak whatsoever, so the search never succeeded and the tests skipped on every run. A skip is green, so nothing reported this: the last integration run passed 68 and skipped 8, seven of them for exactly this reason.

Rows E1, E6, E15, F1, I11, I17 and L9 therefore claimed VM evidence that had never been produced, and L10's #208 D9 verdict was blocked by it.

The fix is provisioning, not weaker tests: `tests/integration/scripts/internal/vm-test-fixtures.sh` creates two snaps (`hello`, `hello-world`) on both VMs, adds **the real Flathub** and its runtime to both, and installs one small Flathub app (`io.github.fragglet.sdl_sopwith`) on **pc1 only**, all baked into the baseline snapshot by `provision-test-infra.sh` and re-applied by the `vm_test_fixtures` conftest fixture. The discover-or-skip convention is gone from the module entirely: a missing subject is now an assertion failure naming the script that creates it, never a skip. Nothing in this matrix may reintroduce a skip as a way of tolerating an unprovisioned machine — that is what made these rows unfalsifiable in the first place.

The flatpak remote is Flathub itself rather than a locally built signed repository: a stand-in only ever tests this project's model of a remote, and the F5a–F5d trust rows are claims about a real remote's real trust configuration. The app lives on pc1 alone so the source→target ref divergence is part of the baseline instead of something a test manufactures; the runtime lives on both, so the install the sync performs is the 146 kB app and nothing else.

### Unverified pending CI

Every `V` row is a claim no passing run has confirmed. `integration-tests.yml` triggers only on PRs targeting `main`, so a stacked PR skips the VM suite entirely and these execute for the first time when this work reaches a PR against `main`. For the snap and flatpak rows above this is their first execution of any kind.

L10 carries a verdict that only that run can settle: whether a system-wide `refresh.hold` masks per-snap `held` notes (#208 D9) is UNDETERMINED. `TestSnapHoldCaptureTiming` encodes the premise that the two live in different snapd namespaces, and the capture is ordered before the hold set on that basis; real snapd has not yet answered.

F1's run now proves remote trust travels rather than depending on a pre-seeded anchor (#215, rows F5a–F5d): a remote carries its GPG-verification setting and its own keyring, and the target trusts Flathub only through the remote. `flatpak remote-delete` takes `flathub.trustedkeys.gpg` with it and Ubuntu ships no machine-level anchor for Flathub, so the ref install after the sync fails unless pc-switcher imported the source's key.

F23 and F24 carry the same status and each asserts, before the sync it wraps, the live fact its behaviour rests on: that both machines print the same `flathub` in `flatpak list --columns=origin` for a ref whose remotes point at different URLs (so only D-41's URL comparison can see the divergence), and that `flatpak remote-modify --filter=<file>` really adds a `filtered` token to `flatpak remotes --columns=options`. Both were measured in a container; neither has been observed on a VM. Both build their own divergence and need no fixture subject, so `FIXTURES_VERSION` stays at 4.

Three of that claim's mechanics were checked directly against the real Flathub in a container before this run existed (flatpak 1.14.6, x86_64): `flatpak remotes --columns=name,url,options` prints Flathub as **two** tab-separated fields with an empty options column, which `_parse_flatpak_remotes` reads as `gpg_verify=True`; `<installation>/repo/flathub.trustedkeys.gpg` is a 0644 file the batched `sha256sum` glob picks up; and re-adding the remote with `--gpg-import` of those bytes produces a **byte-identical** keyring, so the replicated remote's digest matches the source's and no perpetual `CHANGE` diff is manufactured. The replicated remote then verified both a summary signature (`remote-ls`) and a commit signature (`install`). What is still unverified is the same thing every other row here is: the VM run itself.
