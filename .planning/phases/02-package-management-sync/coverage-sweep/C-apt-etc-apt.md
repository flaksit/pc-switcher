# Sweep — C: apt repositories, keys, pins, apt configuration, Ubuntu Pro, and applying repository changes

Articles decomposed: `PKG-FR-REPO-DERIVED` `PKG-FR-REPO-STRANDED` `PKG-FR-REPO-OVERWRITE` `PKG-FR-REPO-CONFLICT` `PKG-FR-REPO-DELETE` `PKG-FR-DISTRO-FILES` `PKG-FR-APT-IGNORES` `PKG-FR-KEY-NOT-ITEM` `PKG-FR-KEY-COPY` `PKG-FR-KEY-REFRESH` `PKG-FR-KEY-CLEANUP` `PKG-FR-PIN-ALWAYS` `PKG-FR-PIN-DELETE` `PKG-FR-PIN-NOT-INVENTORY` `PKG-FR-APTCONF` `PKG-FR-ESM-GATE` `PKG-FR-ESM-VERIFY` `PKG-FR-ESM-SKIP-WHOLE-JOB` `PKG-FR-ESM-NO-ASK` `PKG-FR-ESM-PRIVACY` `PKG-FR-APT-CONFIG-ATOMIC` `PKG-FR-DERIVED-FAILURE` `PKG-FR-DERIVED-VISIBLE` `PKG-NG-APT-LINE-CONTROL` `PKG-NG-PIN-LOCAL` `PKG-NG-ESM-PARTIAL`.

Machines: **Atlas** = source, **Nomad** = target. Unit modules are under `tests/unit/jobs/apt/` unless another path is given; the one integration module is `tests/integration/jobs/test_package_sync.py`, cited as `integration:`.

## C-A. The file classes under `/etc/apt` and what each may be (articles: PKG-FR-APT-IGNORES, PKG-FR-DISTRO-FILES, PKG-FR-KEY-NOT-ITEM, PKG-NG-APT-LINE-CONTROL)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C1 | `sources.list.d/foo.sources` on Atlas only, and an approved install comes from the repository it declares | the file lands on Nomad; the review showed the package and no line for the file | U | `test_apt_etc_apt:TestRepoGroupOrdering::test_key_then_source_then_update_then_package_install`; `test_apt_keyrings:TestKeysAreNotItems::test_key_of_a_derived_repo_is_provisioned_with_no_decision_of_its_own` |
| C2 | `sources.list.d` file on Atlas only that feeds no package this run syncs | not written to Nomad, and offered in no direction | U V | `test_apt_probe:TestRepoStateCapture::test_a_repository_never_appears_as_a_review_entry_in_the_add_or_change_direction`; `test_apt_probe:TestWhatAptItselfReads::test_the_distribution_files_are_written_when_they_differ`; integration:`TestAptSyncEndToEnd::test_apt_repository_state_dry_run_previews_derived_writes_and_reviews_no_repository` |
| C3 | repository file byte-identical on both machines | no item, no write, no key work, and no `apt-get update` at all | U | `test_apt_keyrings:TestKeysAreNotItems::test_a_matching_keyring_is_never_written` |
| C4 | repository file on Nomad only | reaches the user only as a deletion (rows C-D), never as add or change | U | `test_apt_probe:TestRepoStateCapture::test_deb822_and_legacy_source_each_record_own_format` |
| C5 | `vendor.list` and `vendor.list.save` both on Nomad, `vendor.list` absent on Atlas | only `vendor.list` becomes an item; the `.save` copy is never captured | U | `test_apt_probe:TestWhatAptItselfReads::test_a_save_file_in_sources_list_d_is_never_captured` |
| C6 | extensionless files in `preferences.d` and `apt.conf.d` | captured on both machines, with no `-name` narrowing, and diffed as items | U | `test_apt_probe:TestWhatAptItselfReads::test_preferences_d_and_apt_conf_d_keep_no_extension_filter` |
| C7 | `99-vendor.bak` / `99conf.dpkg-dist` in `preferences.d` or `apt.conf.d` — filenames apt's own ignore rules skip | should be invisible to the sync in every direction | ‼ | — (see Gaps) |
| C8 | `/etc/apt/sources.list` differs on the two machines | overwritten from Atlas, and never appears as a review item | U | `test_apt_probe:TestWhatAptItselfReads::test_sources_list_is_digested_on_both_machines_and_is_still_not_an_item`, `::test_the_distribution_files_are_written_when_they_differ` |
| C9 | `/etc/apt/sources.list` absent on Atlas | no digest is recorded, no write is derived, and the run does not fail | U | `test_apt_probe:TestWhatAptItselfReads::test_an_absent_sources_list_yields_no_digest_rather_than_an_error` |
| C10 | `/etc/apt/sources.list` on Nomad and not on Atlas | never offered for deletion | P | `test_apt_probe:TestWhatAptItselfReads::test_sources_list_is_digested_on_both_machines_and_is_still_not_an_item` asserts no `:sources.list` diff only for the both-present case |
| C11 | `ubuntu.sources` and `ubuntu-esm-apps.sources` on Nomad, absent on Atlas | neither is offered for removal | U | `test_apt_probe:TestWhatAptItselfReads::test_ubuntu_sources_is_never_offered_for_removal` |
| C12 | `ubuntu-esm-mine.sources` (a user file with a distribution-lookalike name) on Nomad only | treated as an ordinary repository and offered for removal | U | same test |
| C13 | distribution source file present on both with different bytes | overwritten from Atlas with no review line | U | `test_apt_probe:TestWhatAptItselfReads::test_the_distribution_files_are_written_when_they_differ` (via `/etc/apt/sources.list`) |
| C14 | `ubuntu.sources` on Atlas, absent on Nomad | written to Nomad with no review line | U | same test |
| C15 | keys present/absent/differing across `/etc/apt/keyrings`, `/etc/apt/trusted.gpg.d`, `/usr/share/keyrings` | no `apt:key:` id in any diff, any review group, or any decision file, in any direction | U | `test_apt_keyrings:TestKeysAreNotItems::test_no_key_reaches_a_diff_or_a_review_group_in_any_direction`; `tests/unit/jobs/test_block_state_decisions.py:TestAptRepoItemDecisions::test_a_signing_key_is_never_offered_and_so_can_never_be_recorded` |
| C16 | a machine has no `/etc/apt/preferences.d` (or any other captured directory) at all | answers "nothing" at exit 0; no error, no removal proposals against the other machine | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_an_absent_directory_answers_nothing_rather_than_failing` |
| C17 | a `/etc/apt` digest listing exits non-zero | the job fails naming the command; nothing is read as an empty directory | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_directory_digest_read_that_did_not_answer_fails_the_job` |
| C18 | the source-file/`Signed-By:` scan exits non-zero on either machine | the job fails naming the command | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_source_file_scan_that_did_not_answer_fails_the_job` |
| C19 | `apt.conf.d` file in any of the three directions | an ordinary reviewed item (rows C-G), never derived | U | `test_apt_probe:TestRepoStateCapture::test_pin_and_config_diff_missing_extra_and_changed` |

## C-B. Adding a repository — derived, never asked (articles: PKG-FR-REPO-DERIVED, PKG-NG-APT-LINE-CONTROL)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C20 | Atlas has one repository Nomad lacks, one that differs, one pin Nomad lacks and one pin that differs; no package diverges | the plan has zero diffs and zero review groups — the user is asked about none of the four | U | `test_apt_probe:TestRepoStateCapture::test_a_repository_never_appears_as_a_review_entry_in_the_add_or_change_direction` |
| C21 | an approved install whose origin the target already serves from a place Atlas also uses | no repository file travels for it | U | `test_apt_origins:TestOriginClassification::test_same_origin_install_derives_no_repository_write` |
| C22 | an approved install whose origin only Atlas's repository declares | exactly that repository file travels | U | `test_apt_origins:TestOriginClassification::test_different_origin_install_derives_the_sources_own_repository` |
| C23 | an install offered from a vendor repository, and the user declines it at the review | the repository is not written to Nomad at all | — | — (see Gaps) |
| C24 | a package that is `REPORT_ONLY` because its origin cannot be replicated | no repository is derived for it | U | `test_apt_origins:TestOriginClassification::test_unreplicable_origin_is_report_only_naming_the_origin` (asserts the report; `OriginPlan.derived_files` is empty by construction for `UNREPLICABLE`) |
| C25 | one repository file serving two approved installs | one write, not two, and both packages are attributed to it | P | `test_apt_collateral:TestARepositoryWrittenForADeclinedInstall::test_a_repository_a_surviving_install_still_needs_is_not_named` sets this shape up but asserts the stranding rule, not the write count |
| C26 | a repository the user could tick or untick | no such control exists — the only way to decline it is to decline the package | U | C20 + `test_apt_etc_apt:TestRepoGroupOrdering::test_pins_travel_without_a_review_line_and_land_before_the_sources` (`actionable_entry_ids` is exactly the package) |

## C-C. Overwriting a repository, and the one conflict question (articles: PKG-FR-REPO-OVERWRITE, PKG-FR-REPO-CONFLICT, PKG-FR-NO-MARK-ON-ORIGIN)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C27 | `vendor.list` differs on the two machines, an approved install needs it, and nothing on Nomad is marked machine-specific | overwritten with Atlas's version, silently, no question | U | `test_apt_job:TestRepositoryConflicts::test_a_changed_repository_with_no_machine_specific_package_is_overwritten_silently` |
| C28 | the same file, and Nomad installs a package it marked machine-specific from it | the user is asked before anything is written | U | `test_apt_job:TestRepositoryConflicts::test_a_changed_repository_feeding_a_machine_specific_package_asks_and_shows_both_versions` |
| C29 | that question's content | both whole file versions, Nomad's first, and a detail naming the marked packages and why they are protected | U | same test; `tests/unit/jobs/test_package_review.py:TestRepoConflictGroupResolution::test_both_whole_versions_are_shown_and_no_unified_diff` |
| C30 | the two version panels | each titled with the machine that holds it; the words "the target"/"the source" appear nowhere | U | `tests/unit/jobs/test_package_review.py:TestRepoConflictGroupResolution::test_each_version_panel_is_titled_with_the_machine_that_holds_it` |
| C31 | the question's answers | exactly two — overwrite, skip now — and the row starts on skip | U | `tests/unit/jobs/test_package_review.py:TestRepoConflictGroupResolution::test_only_two_answers_are_offered_and_the_row_starts_skipped` |
| C32 | two conflicting files in one run | each is answered right after its own two panels, never batched behind both | U | `tests/unit/jobs/test_package_review.py:TestRepoConflictGroupResolution::test_each_conflicting_file_is_answered_right_after_it_is_shown` |
| C33 | either answer to the conflict question | nothing is recorded — the file is offered again on the next sync | — | — (see Gaps) |
| C34 | the answer is "overwrite" | Atlas's version of the file is written to Nomad | U | `test_apt_job:TestRepositoryConflicts::test_overwriting_a_conflict_writes_the_sources_version` |
| C35 | the answer is "skip" | the file is not written, and every approved package whose origin depended on it fails naming the file; no install runs | U | `test_apt_job:TestRepositoryConflicts::test_skipping_a_conflict_writes_nothing_and_fails_the_package_that_needed_it` |
| C36 | a repository that differs and feeds a marked package, but that no install this run proposes would write | no question is raised, and the file is not written | — | — (see Gaps) |
| C37 | a conflicting file whose body contains a URL with embedded credentials | neither panel shows the credential | U | `tests/unit/jobs/test_package_review.py:TestCredentialsInPrintedFileBodies::test_neither_version_of_a_conflicting_repository_shows_the_credential` |
| C38 | reading either machine's copy of the conflicting file fails | the job fails naming the `cat` command rather than showing an empty panel | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_conflict_content_read_that_did_not_answer_fails_the_job` |
| C39 | a run with both a target-only repository to judge and a conflict to trigger | one batched `apt-cache policy` over Nomad's own packages, not two | U | `test_apt_job:TestRepositoryConflicts::test_the_conflict_computation_costs_one_batched_policy_call` |
| C40 | a bracketed filename or body in a conflict panel | renders without a markup error | U | `tests/unit/jobs/test_package_review.py:TestRepoConflictGroupResolution::test_a_bracketed_filename_in_a_conflict_panel_renders_without_markup_error` |
| C41 | Ctrl-C at the conflict screen | the whole sync aborts; it is not read as declining the file | U | `tests/unit/jobs/test_package_review.py:TestRepoConflictGroupResolution::test_ctrl_c_aborts_the_sync_naming_the_screen` |
| C42 | a non-interactive run with a conflict pending | the entry is `SKIP_ONCE`, not unresolved, and nothing is written | U | `tests/unit/jobs/test_package_review.py:TestRepoConflictGroupResolution::test_non_interactive_conflict_entries_skip_once_and_are_not_unresolved` |

## C-D. Deleting a repository, and the conditions that gate it (article: PKG-FR-REPO-DELETE)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C43 | `vendor.list` on Nomad only, and nothing installed on Nomad comes from its URLs | offered for deletion, with a detail naming the URLs the file declares | U | `test_apt_job:TestRepoRemovalWithheldWhileInUse::test_a_repository_nothing_installs_from_is_offered_with_its_urls` |
| C44 | that offer as the user reads it | reaches them as a review entry carrying the same URL text | U | `test_apt_job:TestRepoRemovalWithheldWhileInUse::test_detail_reaches_the_user_through_the_review_entry` |
| C45 | a target-only repository whose file declares no parsable URL | the detail says so rather than trailing off | U | `test_apt_messages:TestRepoRemovalWording::test_a_file_declaring_no_url_says_so_rather_than_trailing_off` |
| C46 | a target-only repository that declares several URLs | all of them are named | U | `test_apt_messages:TestRepoRemovalWording::test_every_url_the_file_declares_is_named` |
| C47 | Nomad still installs a package it marked machine-specific from the repository | the repository is not raised as an item at all — not offered with a warning | U | `test_apt_job:TestRepoRemovalWithheldWhileInUse::test_a_repository_a_machine_specific_package_uses_is_not_raised_at_all` |
| C48 | Nomad installs an ordinary package from it that both machines have (so it has no diff of its own) | withheld | U | `test_apt_job:TestRepoRemovalWithheldWhileInUse::test_a_repository_an_ordinary_target_package_uses_is_withheld_too` |
| C49 | only an automatically-installed package on Nomad comes from it | withheld — automatic packages count as usage | U | `test_apt_job:TestRepoRemovalWithheldWhileInUse::test_a_repository_only_an_automatic_package_uses_is_withheld` |
| C50 | the only packages coming from it are ones this run proposes to remove | offered, alongside the removals — usage is counted after this run's removal candidates | U | `test_apt_job:TestRepoRemovalWithheldWhileInUse::test_a_repository_only_this_runs_removals_use_is_offered` |
| C51 | a `.sources` file writing `URIs: https://…/apt/` while `apt-cache policy` prints the origin without the trailing slash | still recognised as in use, so the repository is withheld | U | `test_apt_job:TestRepoRemovalWithheldWhileInUse::test_deb822_uris_match_the_policy_origin_despite_the_trailing_slash` |
| C52 | the machine-specific package that caused a withholding | still produces no diff of its own — naming it in the counting does not re-propose it | U | `test_apt_job:TestRepoRemovalWithheldWhileInUse::test_the_machine_specific_package_itself_still_produces_no_diff` |
| C53 | twelve packages counted against one repository | one batched `apt-cache policy`, never one per package | U | `test_apt_job:TestRepoRemovalWithheldWhileInUse::test_one_apt_cache_policy_call_regardless_of_package_count` |
| C54 | a run with no target-only and no conflicting repository | no `apt-cache policy` over Nomad's own packages is issued at all | U | `test_apt_job:TestRepoRemovalWithheldWhileInUse::test_no_policy_call_when_nothing_is_offered_for_removal` |
| C55 | the usage probe (`apt-cache policy`) does not answer | the job fails naming the command rather than reading "this repository feeds nothing" | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_removal_impact_read_that_did_not_answer_fails_the_job` |
| C56 | reading a target-only repository's own body fails | the job fails naming the `cat` command | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_removal_content_read_that_did_not_answer_fails_the_job` |
| C57 | the deletion screen's shape | its own screen titled "Delete repositories <Atlas> no longer has (apt)", starting unticked, offering exactly two answers | U | `test_apt_job:TestTwoAnswerRemovals::test_each_two_answer_screen_is_titled_in_correct_english`, `::test_a_two_answer_group_is_unticked_and_never_offered_permanence` |
| C58 | a repository entry on that screen | carries its URL detail and no whole-file block | U | `test_apt_job:TestTwoAnswerRemovals::test_a_repository_offered_for_deletion_carries_no_content_block` |
| C59 | the deletion is approved | one `sudo rm --force` naming that file, and nothing else under `/etc/apt` | U | `test_apt_etc_apt:TestRepoGroupOrdering::test_remove_source_issues_single_rm_naming_that_file` |
| C60 | a repository and a pin are both approved for deletion | the repository goes first, the pin second — the reverse of the write order | U | `test_apt_job:TestTwoAnswerRemovals::test_the_repository_goes_before_the_pin_that_prefers_it` |
| C61 | the deletion is declined | nothing is recorded, so the file is offered again on the next sync | U | `tests/unit/jobs/test_block_state_decisions.py:TestAptRepoItemDecisions::test_no_repository_or_pin_id_can_reach_a_decision_file` (records nothing even when `SKIP_ALWAYS` is forced) |
| C62 | a legacy `.list` and a deb822 `.sources` both offered for deletion | each entry names its own format | U | `test_apt_probe:TestRepoStateCapture::test_deb822_and_legacy_source_each_record_own_format` |
| C63 | a real target-only repository with an unreachable host, deleted on a VM | the file and its key leave `/etc/apt`, and `apt-get update` stops naming that host and still exits 0 | V | integration:`TestCrossDirectionRoundTrips::test_apt_source_and_its_key_removed_together` |

## C-E. A repository stranded by a declined install (article: PKG-FR-REPO-STRANDED)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C64 | a repository was written for `pkg-a`, and the user then keeps a protected package, withdrawing `pkg-a` | the file stays on Nomad — it is not removed | U | `test_apt_collateral:TestARepositoryWrittenForADeclinedInstall::test_the_repository_is_named_by_url_and_filename` (no `rm` is issued for it) |
| C65 | the same run's account of that file | one line naming both the path and the URL, saying nothing on Nomad installs from it and that it was left in place | U | same test |
| C66 | the severity of that line | INFO — not a failure and not a warning; the run has no warning at all | U | `test_apt_collateral:TestARepositoryWrittenForADeclinedInstall::test_it_does_not_read_as_something_broken` |
| C67 | `pkg-a` is withdrawn but `pkg-b` from the same repository survives | the repository is not named at all | U | `test_apt_collateral:TestARepositoryWrittenForADeclinedInstall::test_a_repository_a_surviving_install_still_needs_is_not_named` |
| C68 | a repository whose own write failed, for an install then withdrawn | not named as stranded — nothing landed on Nomad | P | no test; `DerivedWrites.stranded` excludes `self._failed` by construction |
| C69 | a pin or a distribution file, on a run where an install is withdrawn | never named as stranded — they travel because Atlas has them, not because a package was approved | P | no test; `stranded` intersects `_repo_writes` only |
| C70 | an approved install that FAILS (rather than being declined) after its repository landed | the repository stays; the article does not require it to be named | — | — (deliberate: `stranded()` is keyed on the declined set only) |

## C-F. Keys (articles: PKG-FR-KEY-NOT-ITEM, PKG-FR-KEY-COPY, PKG-FR-KEY-REFRESH, PKG-FR-KEY-CLEANUP)

### Copying and refreshing

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C71 | a repository this run writes names a keyring Nomad lacks | the key is copied byte-for-byte from Atlas and lands before the repository file, which lands before the install | U | `test_apt_keyrings:TestKeysAreNotItems::test_key_of_a_derived_repo_is_provisioned_with_no_decision_of_its_own` |
| C72 | any key operation | not one command reaches for a URL | U | `test_apt_etc_apt:TestRepoGroupOrdering::test_no_key_command_contains_a_url` |
| C73 | a repository present on both that this run overwrites, whose `Signed-By:` is part of what differs | the newly-named key is provisioned too | U | `test_apt_keyrings:TestKeysAreNotItems::test_key_of_an_overwritten_repo_is_provisioned_too` |
| C74 | a keyring with identical bytes on both machines | never written, never transferred | U | `test_apt_keyrings:TestKeysAreNotItems::test_a_matching_keyring_is_never_written` |
| C75 | the vendor rotated a key: differing bytes, but the repository file is byte-identical so it produces no diff | the key is refreshed anyway, from Atlas's own file, promoted with `sudo install --owner=root --group=root --mode=0644` | U | `test_apt_etc_apt:TestRepoGroupRemovalAndKeyChange::test_rotated_keyring_is_refreshed_although_its_source_file_is_identical` |
| C76 | one rotated key named by three source files | exactly one write | U | `test_apt_keyrings:TestKeysAreNotItems::test_one_rotated_key_serving_three_repos_is_written_once` |
| C77 | `/etc/apt/trusted.gpg.d` keys — one missing on Nomad, one differing | both replicated: nothing references ambient trust, so content is the only signal | U | `test_apt_keyrings:TestKeysAreNotItems::test_global_trust_keys_are_replicated_whether_missing_or_differing` |
| C78 | an `/etc/apt/keyrings` file on Atlas that no source file references | never copied — the directory is not mirrored wholesale | U | `test_apt_keyrings:TestKeysAreNotItems::test_an_unreferenced_source_keyring_is_not_copied_to_the_target` |
| C79 | a `/usr/share/keyrings` key a written repository references | resolved and copied | U | `test_apt_keyrings:TestSharedKeyringsDirectory::test_a_usr_share_keyrings_reference_resolves_and_the_repo_is_replicable`, `::test_a_hand_placed_key_the_target_lacks_is_provisioned` |
| C80 | a `/usr/share/keyrings` key nothing references | never copied — mostly the distribution's own | U | `test_apt_keyrings:TestSharedKeyringsDirectory::test_a_shared_keyring_no_source_references_is_never_copied` |
| C81 | Nomad has the key with different bytes and Nomad's own dpkg owns that path | left alone; the repository is still written | U | `test_apt_keyrings:TestSharedKeyringsDirectory::test_a_package_owned_key_present_with_different_bytes_is_not_overwritten` |
| C82 | dpkg on Nomad owns the path but the file is absent (a vendor `.deb` shipping repository + keyring) | copied anyway — ownership gates the overwrite, never the copy | U | `test_apt_keyrings:TestSharedKeyringsDirectory::test_a_package_owned_key_the_target_is_missing_is_copied_anyway` |
| C83 | ownership determination across all three key directories | one batched `dpkg --search` naming every key Nomad has; its non-zero exit is not read as an answer | U | `test_apt_keyrings:TestSharedKeyringsDirectory::test_ownership_is_probed_once_for_every_key_directory` |
| C84 | a deb822 `Signed-By:` carrying an inline armored block on continuation lines | yields no key reference at all | U | `test_apt_keyrings:TestKeysAreNotItems::test_inline_armored_signed_by_names_no_keyring` |
| C85 | a PPA whose `Signed-By:` puts the armor's first line on the field line | yields no reference, the repository installs normally, no key is written | U | `test_apt_keyrings:TestInlineArmoredSignedBy::test_the_armor_first_line_on_the_field_line_yields_no_ref`, `::test_a_ppa_with_an_inline_key_installs_normally_and_needs_no_keyring` |

### Every way a key reference fails to resolve

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C86 | a source file's `Signed-By:` names a key that exists in none of Atlas's three key directories | the package that needed that repository is reported, not installed, naming the missing key path; no repository item echoes the same fact | U | `test_apt_keyrings:TestSharedKeyringsDirectory::test_a_genuinely_missing_key_is_still_reported_dangling`; `test_apt_origins:TestOriginClassification::test_a_dangling_keyring_makes_the_package_unavailable` |
| C87 | a package served by two files, one with a dangling reference and one sound | still replicable — one writable file is enough | U | `test_apt_origins:TestOriginClassification::test_one_writable_serving_file_is_enough` |
| C88 | the key's own promotion to Nomad fails | the repository is deliberately NOT written, and the failure lands on the package naming both the repository path and the key | U | `test_apt_etc_apt:TestRepoGroupOrdering::test_a_failed_derived_repository_write_fails_the_package_that_needed_it` |
| C89 | `/etc/apt/keyrings` does not exist on a fresh Nomad | the directory is created before the key is promoted into it | U | `test_apt_etc_apt:TestKeyringsDirectoryEnsured::test_promotion_ensures_keyrings_directory_before_install` |
| C90 | creating that directory fails | the package fails naming the key; the key promotion is never attempted | U | `test_apt_etc_apt:TestKeyringsDirectoryEnsured::test_directory_preparation_failure_fails_the_item_not_the_run` |

### Collecting unused keys

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C91 | this run removed no repository | the collection pass does not run at all — not even its re-scan | U | `test_apt_keyrings:TestUnusedKeyringCollection::test_no_source_removed_means_no_collection_pass_at_all` |
| C92 | an approved repository deletion leaves its `/etc/apt/keyrings` key referenced by nothing | the key is deleted, after the repository's own deletion and before the single `apt-get update` | U | `test_apt_keyrings:TestUnusedKeyringCollection::test_key_left_unreferenced_by_an_approved_removal_is_deleted` |
| C93 | another repository on Nomad that this run does not touch still names the key | kept | U | `test_apt_keyrings:TestUnusedKeyringCollection::test_key_still_referenced_by_a_surviving_repo_is_kept` |
| C94 | the key is named only by `/etc/apt/sources.list`, which this tool never syncs | kept | U | `test_apt_keyrings:TestUnusedKeyringCollection::test_key_referenced_only_by_a_file_pc_switcher_never_syncs_is_kept` |
| C95 | the repository naming the key had its deletion declined, while another repository's deletion was approved | kept | U | `test_apt_keyrings:TestUnusedKeyringCollection::test_key_referenced_by_a_repo_whose_removal_was_declined_is_kept` |
| C96 | the key is named only by a repository Nomad recorded machine-specific (so it appears in no review) | kept | U | `test_apt_keyrings:TestUnusedKeyringCollection::test_key_referenced_by_a_machine_specific_repo_is_kept` |
| C97 | Atlas still holds the same key | never collected, even with nothing on Nomad referencing it | U | `test_apt_keyrings:TestUnusedKeyringCollection::test_a_key_the_source_machine_still_has_is_never_collected` |
| C98 | an orphan key in `/etc/apt/trusted.gpg.d` on a run with an approved repository deletion | never collected — ambient trust is not reference-countable | U | `test_apt_keyrings:TestUnusedKeyringCollection::test_a_global_trust_key_is_never_collected` |
| C99 | an orphan key in `/usr/share/keyrings` on the same run | never collected — distribution-owned territory | — | — (see Gaps) |
| C100 | the key of a departing repository differs on the two machines | not refreshed first and then collected — no write at all | U | `test_apt_keyrings:TestUnusedKeyringCollection::test_a_key_only_the_departing_repo_needs_is_not_refreshed_first` |
| C101 | a collected key | backed up into the unit's backup directory before deletion, and its deletion declares `mutates=` | U | `test_apt_keyrings:TestUnusedKeyringCollection::test_a_collected_key_is_backed_up_and_gated_as_a_modification` |
| C102 | backing up an about-to-be-collected key fails | the key is kept rather than deleted unbacked-up, with a warning | — | — (see Gaps) |
| C103 | the `rm` of an unused key fails | a warning names the key; the run continues | — | — (see Gaps) |
| C104 | a key that this run collects, on a VM, after a real repository deletion | both files are gone from `/etc/apt` and `apt-get update` no longer reaches the repository | V | integration:`TestCrossDirectionRoundTrips::test_apt_source_and_its_key_removed_together` |

## C-G. Pins (articles: PKG-FR-PIN-ALWAYS, PKG-FR-PIN-DELETE, PKG-FR-PIN-NOT-INVENTORY, PKG-NG-PIN-LOCAL)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C105 | a `preferences.d` file Atlas has and Nomad lacks, on a run with no package diffs at all | the pin lands on Nomad, and the reviewer is handed nothing | U V | `test_apt_derived:TestPinsStillTravelAsFiles::test_a_pin_file_the_target_lacks_is_written_with_no_review_line`; integration:`TestAptSyncEndToEnd::test_apt_repository_state_dry_run_previews_derived_writes_and_reviews_no_repository` |
| C106 | a pin present on both with different bytes | overwritten with Atlas's version, with no review line | U | `test_apt_derived:TestPinsStillTravelAsFiles::test_a_differing_pin_is_overwritten_rather_than_reviewed`; `test_apt_probe:TestRepoStateCapture::test_pin_and_config_diff_missing_extra_and_changed` |
| C107 | pins and repositories both travelling in one run | the pin is written before the repository files and before the refresh | U | `test_apt_etc_apt:TestRepoGroupOrdering::test_pins_travel_without_a_review_line_and_land_before_the_sources` |
| C108 | a pin naming an origin Nomad does not have | still replicated — inert, so always-sync cannot get a derivation wrong | P | implied by C105 (nothing conditions the pin bucket on origins); no test names this case |
| C109 | a pin file that travels | its contents are never read on Atlas — only its digest and its bytes | U | `test_apt_derived:TestPinsStillTravelAsFiles::test_the_pin_file_needs_no_read_of_its_contents` |
| C110 | a `preferences.d` file on Nomad only | offered for deletion on its own screen titled "Delete pin files <Atlas> no longer has (apt)", separate from the repository screen | U | `test_apt_job:TestTwoAnswerRemovals::test_repository_and_pin_removals_get_two_separate_two_answer_screens`, `::test_each_two_answer_screen_is_titled_in_correct_english` |
| C111 | that entry's content | the pin file's whole body, printed under the machine that holds it, read with one `sudo cat` | U | `test_apt_job:TestTwoAnswerRemovals::test_a_pin_offered_for_deletion_carries_its_whole_content`; `tests/unit/jobs/test_package_review.py:TestRemovalGroupContent::test_a_pin_file_is_printed_whole_under_the_machine_that_holds_it` |
| C112 | that screen's answers | two — remove, leave it for now — starting on skip, never offering permanence | U | `test_apt_job:TestTwoAnswerRemovals::test_a_two_answer_group_is_unticked_and_never_offered_permanence`; `tests/unit/jobs/test_package_review.py:TestInteractive::test_repo_removal_starts_skipped_and_is_never_offered_permanence` |
| C113 | reading a pin file offered for deletion fails | the job fails naming the `cat` command rather than showing an empty block | U | `test_apt_job:TestTwoAnswerRemovals::test_a_pin_read_that_did_not_answer_fails_the_job` |
| C114 | approving a pin deletion | that file, and only that file, is removed | U | `test_apt_job:TestTwoAnswerRemovals::test_approving_a_pin_removal_deletes_the_file` |
| C115 | declining a pin deletion (or forcing `SKIP_ALWAYS` from an automation hook) | nothing is written to any decision file, so it is asked again next run | U | `tests/unit/jobs/test_block_state_decisions.py:TestAptRepoItemDecisions::test_no_repository_or_pin_id_can_reach_a_decision_file` |
| C116 | a pin body containing a credentialed URL, shown for deletion | the credential is withheld from the printed body | U | `tests/unit/jobs/test_package_review.py:TestCredentialsInPrintedFileBodies::test_a_pin_file_offered_for_deletion_shows_no_credential` |
| C117 | a package present only on Nomad that a Nomad pin names | offered for removal as an ordinary package item — the pin says nothing about it | U | `test_apt_job:TestAPinNeverSpeaksForAPackage::test_a_target_only_package_named_by_a_pin_is_offered_for_removal`, `::test_the_removal_reaches_the_user_as_an_actionable_review_entry`, `::test_approving_it_actually_removes_the_package` |
| C118 | any run with pins | no command asks which packages a pin file names | U | `test_apt_job:TestAPinNeverSpeaksForAPackage::test_no_command_asks_the_target_which_packages_its_pins_name` |
| C119 | a pin the user wants on Nomad only | impossible — it is re-offered every run until Atlas drops it | P | C115 covers "nothing recorded"; the re-offer on a second run is not asserted for a pin |

## C-H. apt's own configuration (article: PKG-FR-APTCONF)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C120 | `apt.conf.d/10add` on Atlas only | reviewed as an addition, in a group titled "Add apt configuration files" with the action word "add" | U | `test_apt_job:TestAptConfigVocabulary::test_each_direction_names_the_config_file_not_a_package` |
| C121 | `apt.conf.d/20update` present on both with different bytes | reviewed as a change ("Update apt configuration files" / "update") — never overwritten silently | U | same test; `test_apt_probe:TestRepoStateCapture::test_pin_and_config_diff_missing_extra_and_changed` |
| C122 | `apt.conf.d/30delete` on Nomad only | reviewed as a deletion ("Delete apt configuration files" / "delete") — the ordinary three-answer group, not the repository/pin two-answer one | U | same tests; `test_apt_job:TestTwoAnswerRemovals::test_repository_and_pin_removals_get_two_separate_two_answer_screens` (asserts the config group keeps the ordinary action) |
| C123 | any apt-config group's wording | never claims to be about packages | U | `test_apt_job:TestAptConfigVocabulary::test_no_apt_config_group_claims_to_be_about_packages` |
| C124 | an apt-config addition marked machine-specific | recorded on Atlas (the holding machine) and never offered again | U | `tests/unit/jobs/test_block_state_decisions.py:TestAptRepoItemDecisions::test_declined_config_install_is_recorded_on_source_and_never_re_offered` |
| C125 | an apt-config deletion marked machine-specific | recorded on Nomad; the same run's repository and pin ids are not | U | `tests/unit/jobs/test_block_state_decisions.py:TestAptRepoItemDecisions::test_no_repository_or_pin_id_can_reach_a_decision_file` |
| C126 | an approved apt-config write | staged under Nomad's home then promoted with `sudo install --owner=root --group=root --mode=0644`, never `mv`; the staging copy is removed on success and on failure; nothing is SFTP'd into `/etc` | U | `test_apt_etc_apt:TestRepoGroupOrdering::test_promotion_uses_sudo_install_with_owner_group_mode_never_mv`, `::test_staging_file_removed_after_success_and_after_failure`, `::test_send_file_destinations_start_with_home_never_contain_etc` |

## C-I. Ubuntu Pro and ESM (articles: PKG-FR-ESM-GATE, PKG-FR-ESM-VERIFY, PKG-FR-ESM-SKIP-WHOLE-JOB, PKG-FR-ESM-NO-ASK, PKG-FR-ESM-PRIVACY, PKG-NG-ESM-PARTIAL)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C127 | Atlas carries `ubuntu-esm-apps.sources` and `ubuntu-esm-infra.sources`, Nomad reports no Pro attachment | the user is asked before the job's first write and before any other apt question | U | `test_apt_esm_gate:TestTheESMAttachmentGate::test_an_unattached_target_is_asked_about_before_anything_is_written` (asserts zero `mutates=` commands at the moment of the gate, and that no review was presented) |
| C128 | the gate's message | names both ESM files, the `pro attach` and `pro enable` commands, and the Ubuntu tutorial link | U | same test |
| C129 | the gate's shape | a title naming Nomad, and exactly two answers: "I have attached <Nomad> — check again and continue" / "Skip apt_sync this run (every other job still runs)" | U | `test_apt_esm_gate:TestTheESMAttachmentGate::test_the_gate_offers_exactly_two_answers_and_names_both_of_them` |
| C130 | the user answers "I have attached it" and Nomad now reports attached | the claim is re-probed rather than believed, and the ESM sources are then written | U | `test_apt_esm_gate:TestTheESMAttachmentGate::test_attach_now_re_probes_and_continues_when_the_target_became_attached` |
| C131 | the user answers "I have attached it" ten times without attaching, then skips | every answer re-probes; no bound cuts the loop short | U | `test_apt_esm_gate:TestTheESMAttachmentGate::test_attach_now_can_be_answered_any_number_of_times` |
| C132 | the user chooses to skip | the whole apt job is skipped, no review is presented, and Nomad receives not one write | U V | `test_apt_esm_gate:TestTheESMAttachmentGate::test_choosing_skip_raises_job_skipped_and_writes_nothing`; integration:`TestTheESMAttachmentGateOnVMs::test_an_unattached_target_skips_apt_sync_and_leaves_etc_apt_untouched` |
| C133 | the same skip, with a pin Atlas has that Nomad lacks | the pin does NOT land — skipping withholds the whole job, not only the ESM sources | V | integration: same test (the load-bearing `test ! -e` on the synthetic pin) |
| C134 | the skip's effect on the rest of the run | every other job still runs and the exit code is unaffected | V | integration: same test (asserts `snap_sync` ran and the sync exited 0) |
| C135 | a run with no interactive terminal and pending ESM writes | takes the skip and says why, naming both files and the absence of a TTY | U V | `test_apt_esm_gate:TestTheESMAttachmentGate::test_a_non_interactive_run_skips_the_whole_job`; integration: same test |
| C136 | a dry run with pending ESM writes | asks nothing, warns exactly once that a real run would skip apt_sync entirely, and previews no ESM write | U | `test_apt_esm_gate:TestTheESMAttachmentGate::test_a_dry_run_never_prompts_about_attachment` |
| C137 | Nomad is attached | no question at all, one probe, and both ESM sources are written with no warning | U | `test_apt_esm_gate:TestTheESMAttachmentGate::test_esm_sources_are_written_to_an_attached_target` |
| C138 | Atlas has no ESM sources | the attachment is never probed and no question is asked | U | `test_apt_esm_gate:TestTheESMAttachmentGate::test_a_source_with_no_esm_sources_never_probes_at_all` |
| C139 | Nomad already holds an ESM file with the same bytes | nothing to write, so nothing to ask — no probe at all | U | `test_apt_esm_gate:TestTheESMAttachmentGate::test_an_esm_file_the_target_already_matches_is_not_gated` |
| C140 | `pro` is missing / exits non-zero / prints non-JSON / prints a JSON array / prints an object with no `attached` key | each is treated as unattached, so the question is asked and nothing is written | U | `test_apt_esm_gate:TestTheESMAttachmentGate::test_an_unreadable_pro_probe_is_treated_as_unattached` (five parametrised cases) |
| C141 | the `pro status` payload naming the subscriber's account id and email, attached or unattached | neither reaches the log nor any string put in front of the user | U | `test_apt_esm_gate:TestTheESMAttachmentGate::test_the_probe_payload_is_never_logged` |

## C-J. The atomic `/etc/apt` unit (article: PKG-FR-APT-CONFIG-ATOMIC)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C142 | a run approving a pin, an apt-config file and a key write | exactly one `sudo apt-get update`, after all of them | U | `test_apt_etc_apt:TestRepoGroupOrdering::test_apt_get_update_runs_exactly_once_for_three_repo_items` |
| C143 | a run whose `/etc/apt` work and package installs both happen | still exactly one refresh — the install path's own refresh is a no-op | P | ordering is asserted (`test_key_then_source_then_update_then_package_install`) but the refresh COUNT on such a run is not |
| C144 | the write order within the unit | keys, then pins and apt config, then the distribution's sources, then the derived vendor repositories, then approved deletions, then unused-key collection, then the refresh | U | `test_apt_etc_apt:TestRepoGroupOrdering::test_key_then_source_then_update_then_package_install`, `::test_pins_travel_without_a_review_line_and_land_before_the_sources`; `test_apt_keyrings:TestUnusedKeyringCollection::test_key_left_unreferenced_by_an_approved_removal_is_deleted` |
| C145 | every file the unit will touch | backed up first, into one run-scoped backup directory | U | `test_apt_etc_apt:TestRepoGroupTransaction::test_failed_update_restores_changed_deletes_created_records_group_failures` |
| C146 | a backup that fails | the unit aborts before ANY write, every group item is failed, every derived write is recorded failed, and no `KeyError` escapes | U | `test_apt_etc_apt:TestRepoGroupBackupFailure::test_backup_failure_fails_every_group_item_without_crashing` |
| C147 | the refresh succeeds | no restore command is issued and the backup directory is discarded | U | `test_apt_etc_apt:TestRepoGroupTransaction::test_successful_update_issues_no_restore_command` |
| C148 | the refresh fails | every file that existed before is restored, every file this run created is deleted, and the backup is discarded after a clean rollback | U | `test_apt_etc_apt:TestRepoGroupTransaction::test_failed_update_restores_changed_deletes_created_records_group_failures` |
| C149 | the refresh fails | every approved group item is reported failed, including ones whose own write had succeeded | U | same test |
| C150 | the refresh fails | every derived write is charged as failed too, so a package depending on one cannot install against the pre-run `/etc/apt` | U | same test (asserts `/etc/apt/preferences.d/curl-pin` in `derived.failed`) |
| C151 | after a rollback | apt is re-probed and the run says whether Nomad recovered | P | the second `apt-get update` is asserted; the "recovered / still broken" phrasing in the failure text is not |
| C152 | a rollback step that itself fails | the failure names the file, the message says ROLLBACK INCOMPLETE, and the backup directory is kept with its path named | U | `test_apt_etc_apt:TestRepoGroupTransaction::test_failed_rollback_step_warns_and_keeps_the_backup` |
| C153 | a rollback where one file cannot be restored | the remaining files are still attempted rather than left in their post-run state | P | no test issues two failing restores; the loop's `continue`-on-failure shape is untested |
| C154 | packages approved on a run whose repository unit rolled back but whose packages did not depend on it | still attempted, and not reported failed | U | `test_apt_etc_apt:TestRepoGroupTransaction::test_rollback_does_not_prevent_package_items_from_being_attempted` |
| C155 | installs running after a rollback whose re-probe succeeded | issue no further `apt-get update` — exactly two for the whole run | U | `test_apt_etc_apt:TestRepoGroupTransaction::test_post_rollback_install_issues_no_further_apt_get_update` |
| C156 | a run whose only `/etc/apt` work is a rotated key (identical repository files) | the unit still runs, and the refresh happens | U | `test_apt_etc_apt:TestRepoGroupRemovalAndKeyChange::test_rotated_keyring_is_refreshed_although_its_source_file_is_identical` |
| C157 | a repository and its now-unused key both removed | each gets its own `rm`, and the single refresh follows both | U | `test_apt_etc_apt:TestRepoGroupRemovalAndKeyChange::test_source_and_its_key_both_removed_with_one_update_after_both` |
| C158 | the metadata-refresh marker is present but the unit finds nothing to do | the marker succeeds with "no repository changes to refresh for" and no `apt-get update` is issued | — | — (see Gaps) |

## C-K. A derived write's failure and its visibility (articles: PKG-FR-DERIVED-FAILURE, PKG-FR-DERIVED-VISIBLE)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| C159 | a derived repository file whose own `sudo install` fails | no item fails for the file; the approved package that needed it fails, naming the path and apt's error | U | `test_apt_etc_apt:TestRepoGroupOrdering::test_a_repository_whose_own_promotion_fails_also_fails_its_package` |
| C160 | a derived write that fails and was needed by two approved packages | BOTH fail, including one that would otherwise have installed | — | — (see Gaps) |
| C161 | one approved package's own install failing | the repository stays, and the other packages sharing it are untouched | P | `test_apt_job:TestContinueOnFailure::test_second_of_three_fails_all_attempted_one_failure_raised` proves per-item isolation but with no shared derived file |
| C162 | a derived `/etc/apt` file landing on a real run | logged as it lands, naming the destination and Atlas | — | — (see Gaps) |
| C163 | a signing key landing on a real run | logged as it lands, naming the destination and Atlas | U | `test_apt_keyrings:TestKeyWritesAreVisible::test_a_provisioned_key_is_logged_as_it_lands` |
| C164 | a signing key collected on a real run | logged as it goes | U | `test_apt_keyrings:TestKeyWritesAreVisible::test_a_collected_key_is_logged_as_it_goes` |
| C165 | a dry run whose only `/etc/apt` work is a derived pin | previews "Would write /etc/apt/preferences.d/… from <Atlas>" and issues no `sudo install` and no `send_file` | U V | `test_apt_probe:TestWhatAptItselfReads::test_a_dry_run_previews_the_derived_writes_and_issues_none`; integration:`TestAptSyncEndToEnd::test_apt_repository_state_dry_run_previews_derived_writes_and_reviews_no_repository` |
| C166 | a dry run with a key to write | previews "Would write signing key …" and writes none | U | `test_apt_keyrings:TestKeyWritesAreVisible::test_a_dry_run_previews_the_key_it_would_write` |
| C167 | a dry run with an approved repository deletion that would orphan a key | previews "Would delete signing key …, which no repository would reference" and deletes none | U | `test_apt_keyrings:TestKeyWritesAreVisible::test_a_dry_run_previews_the_key_it_would_collect` |
| C168 | a dry run's account of the refresh | reports the metadata-refresh marker by its own label | V | integration:`TestAptSyncEndToEnd::test_apt_repository_state_dry_run_previews_derived_writes_and_reviews_no_repository` |
| C169 | a dry run for a repository that feeds no approved package | previews neither the repository nor its key | V | integration: same test |
| C170 | a repository withheld from the review because Nomad still installs from it | named in the log with what keeps it | — | — (see Gaps) |

## Gaps

**C7 — `.bak`/`.dpkg-dist`/`.save` in `preferences.d` and `apt.conf.d` (‼, PKG-FR-APT-IGNORES).** `capture_dir_digests` passes `extensions` only for `sources.list.d`; the other two directories are captured unfiltered, with the recorded rationale "apt reads extensionless files in both" (`probe.py:capture_dir_digests`, `.planning/…/02-SPEC-package-review-model.md`). That rationale justifies dropping the `*.list`/`*.sources` narrowing but not the absence of apt's own ignore rules, so a `99-vendor.bak` on Nomad's `preferences.d` becomes an offered pin deletion and one on Atlas's becomes an always-synced write. **Not verified this session** whether apt applies `Dir::Ignore-Files-Silently` to `preferences.d`/`apt.conf.d` — check that first; if it does, the fix is an ignore-suffix filter shared by all three directories, and a unit test with a mocked digest listing carrying `99pin` and `99pin.bak` asserting only `99pin` reaches a diff or a derived write. If it does not, the row is wrong and PKG-FR-APT-IGNORES is met.

**C10 — `/etc/apt/sources.list` on Nomad only.** Unit-testable with mocks: source `sha256sum /etc/apt/sources.list` exits 1, target's succeeds; assert no diff and no write name `sources.list`.

**C23 — a repository not written for an install declined at the review.** The strongest form of PKG-FR-REPO-DERIVED and untested. Unit test: the `foo_source_responses()` fixture with `install_reviewer(job, {})` (nothing approved); assert no `sudo install` naming `sources.list.d/foo.sources`, no key write, and no `apt-get update`.

**C25 — one write for two packages needing the same repository.** Unit test on `_two_late_installs_context()`-shaped fixtures: approve both, assert exactly one promotion of `foo.sources`.

**C33 — the conflict answer is never recorded.** `apt:conflict:` ids reach no `ItemDiff`, so `_record_permanent_skips` cannot see them and `REPO_CONFLICT_REVIEW_ACTION` is absent from `_PROMOTABLE_ACTIONS` — but nothing asserts it, and this is the overwrite half of PKG-FR-NO-MARK-ON-ORIGIN. Unit test in the shape of `test_block_state_decisions.py:TestAptRepoItemDecisions::test_no_repository_or_pin_id_can_reach_a_decision_file`: run `differing_repo_context(recorded=decision_file("apt:package:curl"))`, force `SKIP_ALWAYS` onto the conflict entry through a hand-built `ReviewOutcome`, assert neither decision file gains an `apt:conflict:` entry.

**C36 — a differing repository feeding a marked package that no proposed install would write.** The `_files_an_approval_would_write` gate is the whole of PKG-FR-REPO-CONFLICT's last sentence and is unasserted. Unit test: `differing_repo_context` with Atlas's `vendor-tool` present on BOTH machines (so no install is proposed); assert no `REPO_CONFLICT_REVIEW_ACTION` group, no `sudo install` naming `vendor.list`, and no `cat` of either machine's copy.

**C68/C69 — what `stranded()` may never name.** Both hold by construction (`_failed` and `_repo_writes` filters). Cheap unit tests off `_late_collateral_context()`: (68) make the `foo.sources` promotion fail and assert no "stays on" line; (69) add a derived pin and assert it is never named.

**C99 — a `/usr/share/keyrings` orphan is never collected.** Unit test alongside `test_a_global_trust_key_is_never_collected`: same fixture, orphan in `/usr/share/keyrings` instead of `trusted.gpg.d`.

**C102/C103 — collection failure paths.** Both are mockable: (102) make `sudo cp --archive` fail for the key alone and assert no `rm` for it plus a warning naming it; (103) make `sudo rm --force <key>` fail and assert a warning and that the run continues to the refresh.

**C108 — a pin naming an origin Nomad lacks.** Weak as written (nothing in the code conditions the pin bucket on origins). If wanted, assert it explicitly by giving Nomad no repository at all and checking the pin still lands — near-duplicate of C105; low value.

**C119 — a pin returns every run (PKG-NG-PIN-LOCAL).** Needs a two-run unit test: decline the pin deletion, feed the (unchanged, empty) decision file back into a second `plan()`, assert `apt:pin:vendor-pin` is offered again.

**C143 — one refresh on a run that has both `/etc/apt` writes and package installs.** Add `sum(1 for c in commands if c == "sudo apt-get update") == 1` to `test_key_then_source_then_update_then_package_install`, or its own test.

**C151 — the rollback's recovery verdict.** `_rollback` returns "<Nomad> apt recovered after rollback" / "apt still broken after rollback", and that phrase reaches every group item's failure text. Unit test: two `apt-get update` results — fail, then fail again — and assert "still broken" appears in the failures; the existing test covers the succeed-on-reprobe path implicitly but asserts no phrase.

**C153 — a rollback that fails on more than one file.** Unit test: make both the restore and the create-file delete fail, assert both are named in the ROLLBACK INCOMPLETE message and that both commands were issued (nothing short-circuits).

**C158 — the marker with an empty unit.** Reachable because `Keyrings.pending_work()` is a deliberate superset. Unit test: a rotated key whose reference does not survive (its only source file is approved for removal on the other side), assert the marker item succeeds and no `apt-get update` is issued. Fiddly to set up; low value, but it is the only branch of `ensure_converged`'s early return.

**C160 — a failed derived write fails EVERY package that needed it.** PKG-FR-DERIVED-FAILURE's "including ones that would otherwise have installed" is unasserted. Unit test: two approved installs from one derived `foo.sources`, make its promotion fail, assert both item ids are in `PackageItemFailures` and neither reached `apt-get install`.

**C161 — a package's own failure is not charged back.** Extend the C160 fixture: both packages approved, `foo.sources` lands, `pkg-a`'s install command fails, `pkg-b`'s succeeds; assert only `pkg-a` failed and that no derived destination is in `derived.failed`.

**C162 — the real-run log line for a derived `/etc/apt` file.** `EtcApt._write_derived` emits `wrote {dest} from {source}` at `LogLevel.FULL`, and nothing asserts it — the key half of the same obligation is tested (C163), the file half is not. Unit test: mirror `test_a_provisioned_key_is_logged_as_it_lands` with a derived pin, asserting `"wrote /etc/apt/preferences.d/mozilla from source-host" in caplog.text`.

**C170 — the withheld-repository log line.** `_plan_repo_diffs` logs `keeping repository {filename}: {Nomad} still installs …` at FULL; untested. Add a `caplog` assertion to `test_a_repository_an_ordinary_target_package_uses_is_withheld_too`.

All gaps above are unit-testable with the existing mocked-executor fixtures. None needs a VM: the only branches with real-`apt` semantics in this area (a real repository plus key removal, and a real unattached-target skip) already have VM coverage (C63, C104, C132–C135).

## Notes for the assembler

- **Overlaps with area A (packages/origins).** C21/C22/C24/C86/C87 are origin-classification rows I kept because the observable outcome is *which repository file travels*. If area A emits the same scenarios keyed on the package's diff class, merge and keep one row per pair rather than two.
- **Overlaps with area D (collateral).** C64–C67 (stranded repositories) exist only on the late-collateral path, because `DerivedWrites.build` reads the accepted decisions, so an install declined at the review never derives its repository at all. Area D owns the question that withdraws the install; I own what the run then says about the file. Do not let the stranding rows be folded into the collateral question's rows — the article is separate and its "MUST NOT be reported as a failure or a warning" clause has its own test.
- **Overlaps with area H (review UI).** C29–C32, C37, C40–C42, C57, C111, C112, C116 assert generic screen mechanics through apt's own groups. They are here because PKG-FR-REPO-CONFLICT and PKG-FR-PIN-DELETE dictate the shape (how many answers, what must be shown, whether it is recordable). If area H covers the same `tests/unit/jobs/test_package_review.py` symbols generically, keep mine as the apt-specific instances and cross-reference rather than duplicating.
- **Rows I split.** PKG-FR-REPO-DELETE's "counted after this run's approved removals and counting packages the target marked machine-specific" became four rows (C47–C50) because each population is a distinguishable situation with the same outcome and different evidence. PKG-FR-KEY-CLEANUP's "nothing on the target references any more" became six (C92–C98) for the same reason.
- **Ambiguity: PKG-FR-APT-IGNORES' scope.** The article says "MUST NOT be treated as *repository* configuration", which on a strict reading exempts `preferences.d` and `apt.conf.d`. On a loose reading (the article sits in the *Repositories, keys and pins* section, which also carries pins and apt config) it binds them. C7 is written against the loose reading and flagged ‼; if the strict reading is intended, downgrade C7 to a note.
- **Ambiguity: PKG-FR-REPO-DELETE and `/etc/apt/sources.list`.** The article's "a repository present on the target and not on the source" is silent about `sources.list`, which apt reads and which this tool captures but never removes. The code makes it structurally unremovable (it is not a `sources.list.d` entry). C10 records that as a branch; the article does not require it explicitly.
- **`PKG-FR-REPO-CONFLICT` gate wording.** The code gates the question on the *plan-time superset* of installs (`_files_an_approval_would_write` — every install the review proposes), not on the approved set, which cannot be known before the question is asked. That is the only coherent reading of "a repository this run writes because an approved package comes from it" given the ordering, but it means a file can be asked about and then not written because the package was declined. No article covers that outcome; nothing is written, so nothing is wrong — worth one sentence in the final document rather than a row.
