# Phase 02 — package sync scenario/coverage matrix

Requirement-derived scenario enumeration for the four package jobs, mapped to pytest coverage. Branches come from ADR-020, 02-CONTEXT.md (D-01…D-33), 02-208-HOLD-MASK-REPLICATION.md and docs/jobs/package-sync.md — not from code paths, though code was read to decide what actually happens.

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
| A2 | On source, absent on target, target repos offer no candidate | REPO_UNAVAILABLE → REPORT_ONLY, never an install | U | core:`test_missing_and_unavailable_yields_repo_unavailable_not_install`, apt:`test_no_candidate_package_is_reported_not_installed` |
| A3 | On target's manual set only | EXTRA_ON_TARGET → REMOVE, own unticked group | U | core:`test_extra_on_target_yields_remove`, apt:`test_extra_on_target_yields_extra_on_target_remove` |
| A4 | Present both, equal version | no diff at all | U | core:`test_equal_versions_yields_no_diff` |
| A5 | Present both, versions differ | VERSION_MISMATCH → REPORT_ONLY naming both versions; never force-converged | U | core:`test_version_mismatch_yields_report_only_with_both_versions` |
| A6 | Version ordering (epoch, tilde, revision) | decided by `dpkg --compare-versions`, never string compare | U | items:`TestCompareDebVersions` (incl. real-dpkg cross-check) |
| A7 | Package pinned on target via preferences.d | HELD_OR_PINNED REPORT_ONLY echo on the package | U | core:`test_pin_fact_yields_held_or_pinned_distinguishable_from_a_hold_item`, apt:`test_preferences_d_pin_surfaces_with_pin_mechanism_and_filename` |
| A8 | Package held on target | install/upgrade action suppressed, no package-level report — including after the unhold was permanently declined | U | core:`test_target_hold_only_yields_apt_hold_remove_and_suppresses_package_action`, apt:`test_held_package_yields_hold_item_not_duplicate_held_or_pinned_report`, blk:`TestAptHeldPackageSuppression` (2) |
| A9 | Package pinned *and* a hold exists for another package | pin echo and hold item coexist, stay distinguishable | U | apt:`test_pin_still_yields_report_only_echo_alongside_a_hold_item` |
| A10 | Dependency-only package (not in `apt-mark showmanual`) | never in the manifest → never installed, never removed, never reported | U | apt:`TestManifestIsShowmanualOnly::test_auto_installed_dependency_produces_no_diff_of_any_kind` — the mechanism example 1 rests on |
| A11 | Same package is no-candidate on source *and* missing on target | surfaces twice: `manual_installs_sync` unreproducible item + `apt_sync` REPO_UNAVAILABLE report | — | no test; two managers describe one package in two reviews |
| A12 | `apt-mark showmanual` empty on a machine | empty manifest, no crash, every target package offered as an unticked removal | U | apt:`test_empty_source_manifest_offers_every_target_package_as_an_unticked_removal` |
| A13 | Version resolution source | `dpkg-query`, never `apt list --installed` | U | apt:`test_dpkg_query_used_not_apt_list_installed` |

## B. apt holds (#208 D1–D6)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| B1 | Held on source, not target | `apt:hold:<pkg>` INSTALL → `sudo apt-mark hold` | U | apt:`test_source_held_yields_install_hold_item_and_converge_runs_apt_mark_hold`, core:`test_source_hold_only_yields_apt_hold_install` |
| B2 | Held on target, not source | REMOVE → `sudo apt-mark unhold`, unticked removal group | U | apt:`test_target_held_only_yields_remove_unhold_item` |
| B3 | Held on both / neither | no hold diff | U | apt:`test_held_on_both_yields_no_hold_diff`, core:`test_held_on_both_yields_no_diff` |
| B4 | Hold identity is distinct from package identity | two separate review items for one package | U | apt:`test_held_package_yields_hold_item_not_duplicate_held_or_pinned_report` |
| B5 | Review verb | group/entry read "hold"/"unhold", never "install"/"remove" | U | apt:`TestHoldReviewVerbs` (2), snap:`TestHoldReviewVerbs` (3), flat:`TestMaskReviewVerbs` (3) — the #208 D3 promise, asserted per manager |
| B6 | Hold converges after the package install in the same run (D8) | install lands before its hold | U | apt:`TestInstallBeforeHoldOrdering` (plain sort path and `accept_review` reorder path) |
| B7 | Hold approved for a package whose install was skipped (D6) | `apt-mark hold` on an absent package → normal per-item failure | U | apt:`TestHoldOnAnAbsentPackage::test_failed_apt_mark_hold_fails_only_that_item` |
| B8 | skip-always on a hold item | DecisionEntry written on the holder's machine | U | apt:`test_skip_always_on_a_hold_writes_the_decision_file` |
| B9 | skip-always on a hold item, next run | item inert, no diff | U V | blk:`TestAptHoldDecisions` (3, both directions + holder-machine read-back), INT:`test_skip_always_on_an_apt_hold_is_inert_next_run`. Enforced by `_drop_inert_diffs` post-diff, the only correct place: filtering the hold set on the way in would re-propose upgrading a held package |
| B10 | Holds drive no `apt-get -s` simulation | selection state only | U | apt:`TestHoldsDriveNoSimulation::test_hold_only_run_issues_zero_apt_get_simulations` |
| B11 | `/usr/bin/apt-mark` in the target sudo grant | named in the hint | U | apt:`test_apt_mark_is_in_the_target_sudo_command_list` |

## C. apt repository config — sources, keys, pins, apt.conf (D-11, D-12, D-13, D-27)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C1 | Source file missing on target, key present on source | INSTALL | U | apt:`test_source_with_key_present_on_source_yields_plain_install` |
| C2 | Source file whose `Signed-By:`/`signed-by=` names a key absent on the source | REPORT_ONLY + dangling detail, never installable | U | apt:`test_source_with_dangling_keyring_reference_is_flagged_not_installable` |
| C3 | Same, but the source file is *changed* rather than missing | also downgraded to REPORT_ONLY | U | apt:`test_changed_source_with_dangling_keyring_reference_is_downgraded_to_report_only` |
| C4 | deb822 `.sources` vs legacy `.list` | format recorded per file, never normalised; identity is the filename | U | apt:`test_deb822_and_legacy_source_each_record_own_format` |
| C5 | Same repo described by both a `.list` and a `.sources` file | two distinct items, both visible | P | identity-by-filename tested; the coexistence case itself is not |
| C6 | Key present per-repo vs global-trust with the same filename | distinct item ids | U | apt:`test_per_repo_and_global_trust_keys_are_distinct_item_ids` |
| C7 | Key digests identical on both machines | no diff, no content fetch | U | apt:`test_key_matching_digest_on_both_sides_produces_no_diff` |
| C8 | Key content differs | VERSION_MISMATCH → CHANGE, bytes copied verbatim | U | apt:`test_changed_per_repo_key_is_staged_then_promoted_with_the_source_bytes` |
| C9 | Pin/config file missing / extra / changed | INSTALL / REMOVE / CHANGE | U | apt:`test_pin_and_config_diff_missing_extra_and_changed` |
| C10 | Convergence order | key → pin/config → source → `apt-get update` → packages | U | apt:`test_key_then_source_then_update_then_package_install` |
| C11 | Several repo items approved | exactly one `apt-get update` | U | apt:`test_apt_get_update_runs_exactly_once_for_three_repo_items` |
| C12 | Keys never re-fetched from a vendor | no command contains a URL | U | apt:`test_no_key_command_contains_a_url` |
| C13 | Key write fails | dependent source file left unwritten | U | apt:`test_failed_key_write_leaves_dependent_source_unwritten` |
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
| C24 | Source file *and* its key both extra on target, both approved | both deleted, one `apt-get update` after both writes | U V | apt:`test_source_and_its_key_both_removed_with_one_update_after_both`, INT:`test_apt_source_and_its_key_removed_together` (proven against `/etc/apt` and a working `apt-get update` afterwards) |
| C25 | Content reads for hydration use sudo | matches the `sudo find … sha256sum` privilege | U | apt:`test_content_hydration_reads_use_sudo_matching_the_digest_capture` |
| C26 | Target has a repo/key the source lacks, still needed by a target-side machine-specific package | removal offered with no awareness of that dependency | ‼ | no linkage exists in the code (user example 3, see N7) |
| C27 | skip-always on a digest-derived repo item (`apt:source:`/`apt:key:`/`apt:pin:`/`apt:config:`), next run | item inert, no diff | U | blk:`TestAptRepoItemDecisions` (2) |

## D. apt collateral (D-30) and metadata refresh (decision 1)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| D1 | Approved installs, simulation removes an auto-installed package | proceeds silently, no review item | U | apt:`test_auto_collateral_removal_produces_no_review_item`, `test_install_whose_only_collateral_is_auto_deps_proceeds` |
| D2 | Simulation removes a target-manual package | own COLLATERAL group item naming the trigger | U | apt:`test_manual_collateral_removal_becomes_a_collateral_review_item` |
| D3 | Simulation removes a source-manual-only package | protected too (union of both manual sets) | U | apt:`test_source_only_manual_collateral_removal_becomes_a_review_item` |
| D4 | Simulation downgrades a manual package | collateral item; auto downgrade produces nothing | U | apt:`test_manual_downgrade_becomes_item_auto_downgrade_does_not`, `test_guard_allows_auto_downgrade` |
| D5 | Collateral resolved "install anyway" | install proceeds, guard permits the removal | U | apt:`test_install_anyway_proceeds_and_guard_allows_the_collateral_removal`, rev:`test_install_anyway_records_apply` |
| D6 | Collateral resolved "skip" | every triggering install left unapproved | U | apt:`test_skip_leaves_the_triggering_install_unapproved`, rev:`test_skip_records_skip_once` |
| D7 | Collateral resolved "abort" | `SyncAbortedByUser` naming the package | U | rev:`test_abort_raises_sync_aborted_by_user_naming_the_collateral_package` |
| D8 | Collateral prompt never happens mid-apply | classification is plan-time | U | apt:`TestPlanTimeCollateral` + `test_at_most_two_apt_get_dash_s_commands_regardless_of_package_count` |
| D9 | Real transaction drifted since plan time (manual removal) | apply-time guard refuses the item | U | apt:`test_guard_refuses_drifted_manual_removal_not_seen_at_plan_time`, `test_apply_time_guard_refuses_source_only_manual_collateral` |
| D10 | Drifted manual downgrade | refused | U | apt:`test_guard_refuses_drifted_manual_downgrade` |
| D11 | `apt-get -s` itself fails | fail closed, never read as a clean preview | U | apt:`test_failed_simulation_raises_instead_of_returning_empty_preview`, `test_apply_time_simulation_failure_fails_the_item_not_silently_clean` |
| D12 | Approved removal whose transaction removes auto reverse-deps | proceeds | U | apt:`test_auto_reverse_dep_removal_proceeds` |
| D13 | Approved removal whose transaction removes an unreviewed manual reverse-dep | refused | U | apt:`test_drifted_manual_reverse_dep_removal_refused` |
| D14 | Two removals both approved, each removing the other | both proceed | U | apt:`test_both_removals_approved_the_first_proceeds` |
| D15 | Install-only run (no repo item changed) | exactly one `apt-get update` before the first install | U | apt:`test_install_only_run_refreshes_metadata_once_before_first_install` |
| D16 | That refresh fails | every install aborts, still only one `apt-get update` | U | apt:`test_failed_metadata_refresh_aborts_installs_with_a_single_update` |
| D17 | Repo item changed *and* installs approved | group's own update is the run's single refresh | U | apt:`test_repo_group_refresh_is_not_repeated_by_the_install_path` |
| D18 | Rollback's re-probe succeeded | later installs need no further refresh | U | apt:`test_post_rollback_install_issues_no_further_apt_get_update` |
| D19 | Only a REPORT_ONLY repo item is decided APPLY | marker inserted, no writes, no update | U | apt:`test_apply_on_a_report_only_source_writes_nothing_and_refreshes_nothing` |
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
| F1 | Ref on source only | INSTALL into the source's scope, from its origin | U V | flat:`test_full_diff_taxonomy`, INT:`test_flatpak_installs_into_source_scope_after_remote` |
| F2 | Ref on target only | REMOVE (`uninstall -y`), no source lookup needed | U | flat:`test_ref_removal_never_needs_source_lookup` |
| F3 | Same app, different scope on each machine | two items: one install + one removal, never a change | U | flat:`test_same_application_both_scopes_yields_two_distinct_identities`, items:`test_same_application_different_scope_yields_distinct_item_ids` |
| F4 | Same app, same scope, version differs | REPORT_ONLY (floats, D-04) | U | flat:`test_full_diff_taxonomy` |
| F5 | Remote missing on target | INSTALL (`remote-add --if-not-exists`) before any ref | U | flat:`test_every_remote_diff_precedes_every_ref_diff`, `test_remotes_converge_before_refs_that_depend_on_them` |
| F5a | Signed remote replicated to a machine that never had it | the source's own keyring travels byte-for-byte and is imported with `--gpg-import`, staged under the target's `~/.cache/pc-switcher` | U V | flat:`TestRemoteTrustConverge` (`test_signed_remote_is_added_with_the_sources_own_key`, `test_staging_stays_under_the_targets_own_home`, `test_every_staging_write_carries_mutates`, `test_staged_key_is_discarded_even_when_remote_add_fails`), INT:`test_flatpak_installs_into_source_scope_after_remote` |
| F5b | Trust capture per scope | verification read from the `options` column, key digest from that scope's own `<repo>/<remote>.trustedkeys.gpg` | U | flat:`TestRemoteTrustCapture` (8) |
| F5c | Unverified source remote | replicated with `--no-gpg-verify`; a verified one is never downgraded | U | flat:`test_unverified_source_remote_replicates_as_unverified`, `test_verified_source_remote_is_never_downgraded_even_if_the_target_is_unverified`, `test_change_to_an_unverified_source_remote_disables_verification` |
| F5d | Verified remote with no per-remote key (machine-level anchor) | added plainly, nothing invented; a key captured but missing at converge refuses | U | flat:`test_verified_remote_without_a_key_of_its_own_adds_plainly`, `test_missing_source_keyring_refuses_rather_than_provisioning_a_dead_remote` |
| F6 | `flathub` in both scopes | two independent remote items | U | flat:`test_flathub_present_in_both_scopes_yields_two_remote_items` |
| F7 | Same-name, same-scope remote with a different URL | CHANGE → `remote-modify --url`, default-ticked | U | flat:`test_changed_url_yields_one_change_diff`, `test_changed_url_lands_in_default_ticked_change_group`, `test_converge_uses_remote_modify_with_source_url_and_scope_flag` |
| F7a | Same-name, same-scope remote whose signing key or verification setting differs | CHANGE naming the differing facet → one `remote-modify` carrying url + trust | U | flat:`TestRemoteTrustDiff` (4), `test_verified_source_remote_is_never_downgraded_even_if_the_target_is_unverified` |
| F8 | Same URL and same trust | no diff | U | flat:`test_identical_url_yields_no_diff`, `test_identical_url_and_trust_yields_no_diff` |
| F9 | Ref whose origin remote exists on neither the target nor this run | refused with a named per-item failure, no doomed install | U | flat:`test_ref_with_missing_origin_remote_is_skipped_with_named_failure` |
| F10 | user vs system scope privilege | `sudo` iff system scope, for every verb | U | flat:`test_user_scope_ref_install_has_no_sudo_and_carries_user_flag`, `test_system_scope_ref_install_uses_sudo_and_system_flag`, `test_system_scope_url_change_uses_sudo_and_system_flag` |
| F11 | Third named installation (neither user nor system) | line skipped, never guessed | U | flat:`test_unrecognized_installation_value_is_skipped` |
| F12 | Mask parsing (2-space prefix, wildcards, blank lines) | patterns per scope | U | flat:`test_parses_two_leading_space_format_and_wildcard_patterns`, `test_blank_lines_skipped_and_scope_is_the_passed_argument`, `test_no_masks_yields_empty_list` |
| F13 | Mask on source only / target only / both | INSTALL `mask` / REMOVE `mask --remove` / no diff | U | flat:`test_source_user_mask_absent_on_target_yields_install`, `test_target_only_system_mask_yields_removal`, `test_mask_present_on_both_yields_no_diff` |
| F14 | Mask ordering | remotes → refs → masks | U | flat:`test_masks_ordered_after_refs_in_diffs_tuple`, `test_every_remote_diff_precedes_every_ref_diff` |
| F15 | Mask pattern edited on source | reads as remove-old + add-new, never a CHANGE | U | flat:`test_edited_pattern_reads_as_two_membership_diffs_never_a_change` |
| F16 | Mask scope moved user→system | add + remove | U | flat:`test_scope_move_reads_as_add_system_plus_remove_user` |
| F17 | Mask replicated whether or not a matching ref is installed | pure pattern | U | flat:`test_mask_replicates_even_when_its_pattern_matches_no_installed_ref` |
| F18 | System-scope mask on either machine | target sudo required | U | flat:`test_system_scope_mask_requires_target_sudo` |
| F19 | User-scope-only diff | sudo never checked | U | flat:`test_user_scope_only_mask_never_checks_sudo`, `test_user_scope_only_never_checks_sudo` |
| F20 | skip-always on a mask, next run | inert | U | flat:`TestMaskSkipAlways::test_recorded_mask_produces_no_diff_on_the_next_run`, blk:`TestFlatpakMaskDecisions` (2) |
| F21 | Remote removed while a target ref still uses it | removal offered with the dependent target refs named in its `detail` (same scope only); not refused | U | flat:`TestRemoteRemovalOrphansRefs` (5), items:`test_build_orphaned_refs_detail_names_the_remote_and_every_dependent` |
| F22 | `~/.local/share/flatpak` exclusion, `~/.var/app` never excluded | store owned by the job, data by folder_sync | U | flat:`test_returns_flatpak_data_dir_excludes_var_app`, fold:`test_flatpak_data_dir_included_var_app_never_mentioned` |

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
| H19 | skip-always on a REPORT_ONLY item (version mismatch, repo-unavailable, pin echo) | never offered: D-08a has no holder machine for an item with no converge verb, and a recorded skip-always on a VERSION_MISMATCH would drop the package from syncing entirely rather than stop reporting the drift. Resolved by fixing the underlying condition (ADR-020, D-07 scope amendment) | U | perm:`test_report_only_group_is_never_offered_permanence`; `_drop_inert_diffs` passes REPORT_ONLY diffs through untouched for the same reason |

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
| N5 | New apt source + key + package on A → A→B installs all three in order | key→source→update→install | U apt:`test_key_then_source_then_update_then_package_install` (single run, mocked) |
| N6 | Package uninstalled + source/key removed on B → B→A | three independent removal items on A, all unticked | P (C18 + A3 separately; not as a narrative, and there is **no** "this was the last package from that source, remove it too?" prompt — that requirement is not implemented) |
| N7 | Machine-specific package R on B needs source S; A removes S → A→B | user expects S kept | ‼ not implemented — S is offered for removal on B with no awareness of R (C26) |
| N8 | Dependency-only package Q kept/removed by apt's own bookkeeping across a P install/remove round trip | pc-switcher never touches Q | P — the single-run half is apt:`test_auto_installed_dependency_produces_no_diff_of_any_kind` (A10); the round trip is asserted nowhere |
| N9 | Same package machine-specific on A, arrives as collateral on B | protected by the source∪target manual union | U apt:`TestSourceOnlyCollateral` (2 tests) |
| N10 | Snippet authored on A in run 1 → run 2 from B (registries diverge) | non-additive push guarded | U man:`TestSnippetRegistryOverwriteGuard` (6 tests) — single run only |
| N11 | Two machines, full four-job run, three success criteria end to end | — | — UAT-only (02-UAT.md test 2) |

## Findings

### Open defects and unimplemented requirements

- C26/N7 — a repo or key removal has no awareness of a target-side machine-specific package that still needs it. No linkage exists between the decision store and the repo diff. Example narrative 3 is not implemented.
- N6 — no "this was the last package from that source, remove it too?" prompt. Source and key removals propagate only because the source machine's own files disappeared, as independent unticked items. Example narrative 2 is not implemented.

### Coverage gaps in behaviour believed correct

- A11 — one package that is unreproducible on the source and missing on the target is described twice, in two managers' reviews. Untested.
- C5 — a repo described by both a `.list` and a `.sources` file on one machine. Identity-by-filename is asserted; the coexistence case is not.
- K7 — snap `validate()` logs a pre-existing system `refresh.hold` informationally and never mutates it. No test asserts the read-only path.
- L11 — the hold is written with a timestamp so a crashed run self-expires. The timed value is asserted; no test simulates the crash.
- N8 — the round trip in which apt's own bookkeeping keeps or drops a dependency-only package. The single-run half is covered (A10).
- I22/N11 — real-TTY review rendering and the two-machine walkthrough are UAT-only by nature.

### Snap and flatpak end-to-end coverage was vacuous

Every snap and flatpak `V` in this matrix was, until now, a test that could not run at all. Each one searched the VMs for a subject — a snap outside the removal denylist, a snap with an alternate installable revision, an installed flatpak ref with a configured remote — and called `pytest.skip` when it found none. The VM baseline contained only `snapd`, `core*` and `bare` (every one of them denylisted) and no flatpak whatsoever, so the search never succeeded and the tests skipped on every run. A skip is green, so nothing reported this: the last integration run passed 68 and skipped 8, seven of them for exactly this reason.

Rows E1, E6, E15, F1, I11, I17 and L9 therefore claimed VM evidence that had never been produced, and L10's #208 D9 verdict was blocked by it.

The fix is provisioning, not weaker tests: `tests/integration/scripts/internal/vm-test-fixtures.sh` creates two snaps (`hello`, `hello-world`) on both VMs, adds **the real Flathub** and its runtime to both, and installs one small Flathub app (`io.github.fragglet.sdl_sopwith`) on **pc1 only**, all baked into the baseline snapshot by `provision-test-infra.sh` and re-applied by the `vm_test_fixtures` conftest fixture. The discover-or-skip convention is gone from the module entirely: a missing subject is now an assertion failure naming the script that creates it, never a skip. Nothing in this matrix may reintroduce a skip as a way of tolerating an unprovisioned machine — that is what made these rows unfalsifiable in the first place.

The flatpak remote is Flathub itself rather than a locally built signed repository: a stand-in only ever tests this project's model of a remote, and the F5a–F5d trust rows are claims about a real remote's real trust configuration. The app lives on pc1 alone so the source→target ref divergence is part of the baseline instead of something a test manufactures; the runtime lives on both, so the install the sync performs is the 146 kB app and nothing else.

### Unverified pending CI

Every `V` row is a claim no passing run has confirmed. `integration-tests.yml` triggers only on PRs targeting `main`, so a stacked PR skips the VM suite entirely and these execute for the first time when this work reaches a PR against `main`. For the snap and flatpak rows above this is their first execution of any kind.

L10 carries a verdict that only that run can settle: whether a system-wide `refresh.hold` masks per-snap `held` notes (#208 D9) is UNDETERMINED. `TestSnapHoldCaptureTiming` encodes the premise that the two live in different snapd namespaces, and the capture is ordered before the hold set on that basis; real snapd has not yet answered.

F1's run now proves remote trust travels rather than depending on a pre-seeded anchor (#215, rows F5a–F5d): a remote carries its GPG-verification setting and its own keyring, and the target trusts Flathub only through the remote. `flatpak remote-delete` takes `flathub.trustedkeys.gpg` with it and Ubuntu ships no machine-level anchor for Flathub, so the ref install after the sync fails unless pc-switcher imported the source's key.

Three of that claim's mechanics were checked directly against the real Flathub in a container before this run existed (flatpak 1.14.6, x86_64): `flatpak remotes --columns=name,url,options` prints Flathub as **two** tab-separated fields with an empty options column, which `_parse_flatpak_remotes` reads as `gpg_verify=True`; `<installation>/repo/flathub.trustedkeys.gpg` is a 0644 file the batched `sha256sum` glob picks up; and re-adding the remote with `--gpg-import` of those bytes produces a **byte-identical** keyring, so the replicated remote's digest matches the source's and no perpetual `CHANGE` diff is manufactured. The replicated remote then verified both a summary signature (`remote-ls`) and a commit signature (`install`). What is still unverified is the same thing every other row here is: the VM run itself.
