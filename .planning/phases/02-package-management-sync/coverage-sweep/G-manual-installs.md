# Sweep — G: software no package manager can reproduce, and the snippet registry

Machines: `Atlas` is the source of the sync, `Nomad` the target.

## G.1 What the job detects (articles: PKG-FR-MANUAL-SCOPE, PKG-FR-DEB-OWNERSHIP)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| G1 | Atlas has `code` installed from a hand-downloaded `.deb`; its installed version's only version-table origin is dpkg's own status file | It is presented as an item Nomad cannot get from any package manager, identified as `unreproducible:apt-no-candidate:code` | U | `test_manual_installs_sync:TestNoCandidateDetection::test_package_whose_only_origin_is_dpkg_status_is_unreproducible` |
| G2 | Atlas has `gh` from its vendor repository (its policy block also lists `/var/lib/dpkg/status`, as every installed package's does) | Not presented — it is reproducible from a repository | U | `test_manual_installs_sync:TestNoCandidateDetection::test_repo_installed_package_is_not_unreproducible` |
| G3 | Atlas has `docker.io` fully repo-available but pinned below zero, so apt reports `Candidate: (none)` | Not presented — a pin does not make software unreproducible | U | `test_manual_installs_sync:TestNoCandidateDetection::test_negatively_pinned_package_is_not_unreproducible` |
| G4 | Atlas has a repo-installed package whose installed version comes from an ESM origin | Not presented | U | `test_manual_installs_sync:TestNoCandidateDetection::test_package_installed_from_a_repo_as_an_automatic_dependency_is_not_unreproducible` |
| G5 | Atlas has a hand-installed `.deb` that apt marks automatically-installed (absent from `apt-mark showmanual`) | Not presented — the scan covers the manual set only | — | none |
| G6 | Atlas has a package hand-installed at a version NEWER than any repository offers: its `***` row has no repository origin while older rows do | Presented — the repository cannot supply the version this machine has | — | none |
| G7 | Atlas's whole manual set (four packages, one hand-`.deb`) is examined | Exactly one examination of apt's policy for the whole set, never one per package, and only the hand-`.deb` is presented | U | `test_manual_installs_sync:TestNoCandidateDetection::test_one_batched_scan_separates_the_hand_deb_from_the_repo_installed` |
| G8 | apt answers about the manual set but says nothing about one queried name | That name is not presented; its silence indicts nothing | U | `test_manual_installs_sync:TestNoCandidateDetection::test_no_block_inside_an_answered_policy_read_indicts_nothing` |
| G9 | apt's policy read fails (package lists unreadable) | The job fails once naming the command and its error; nothing is proposed | U | `test_manual_installs_sync:TestNoCandidateDetection::test_a_policy_read_that_did_not_answer_fails_the_job` |
| G10 | apt's policy read exits 0 having said nothing about any package | The job fails once naming the command — silence is not "this machine has none" | U | `test_manual_installs_sync:TestNoCandidateDetection::test_a_policy_read_that_printed_no_block_at_all_fails_the_job` |
| G11 | Atlas's entire manual set was hand-installed from `.deb` files, so apt reports every one of them with no repository | An ordinary answer: every one is presented | U | `test_manual_installs_sync:TestNoCandidateDetection::test_a_policy_read_over_only_bare_deb_packages_still_answers` |
| G12 | Reading Atlas's manually-installed set fails | The job fails once naming that read; the run continues with the other jobs | U | `test_manual_installs_sync:TestNoCandidateDetection::test_a_manual_set_read_that_did_not_answer_fails_the_job` |
| G13 | Atlas has four entries under `/usr/local` and `/opt`, two of them owned by a package | Only the two unowned ones are presented, each named by its path | U | `test_manual_installs_sync:TestUnownedScan::test_scan_unowned_installs_yields_two_items_from_four_candidates` |
| G14 | Atlas has a deep tree of unowned files under `/opt/vendor/...` | The scan names the top-level finding and does not walk the tree: exactly `/usr/local`, `/opt`, `/usr/local/bin`, `/usr/local/lib`, one level deep each, in one command | U | `test_manual_installs_sync:TestUnownedScan::test_unowned_scan_queries_only_usr_local_and_opt` |
| G15 | Atlas has no `/opt` at all | That root is skipped; the scan is not an error | U | `test_manual_installs_sync:TestUnownedScan::test_a_scan_root_that_is_not_there_is_skipped_not_an_error` |
| G16 | A scan root exists but cannot be read (permission denied) | The job fails naming the error; it never reports "nothing installed by hand here" | U | `test_manual_installs_sync:TestUnownedScan::test_a_find_that_could_not_run_fails_the_job_rather_than_reporting_nothing` |
| G17 | Atlas has nothing under either root and nothing hand-installed | Nothing is presented, nothing is applied, the job is clean | U | `test_manual_installs_sync:TestEmptyDetection::test_empty_detection_produces_no_group_and_applies_nothing` |
| G18 | The ownership question itself cannot be answered (dpkg's file lists are unreadable) | The job fails naming the path dpkg owns on every machine; it does not declare every entry under `/opt` and `/usr/local` unowned | U | `test_manual_installs_sync:TestUnownedScan::test_a_dpkg_that_did_not_answer_does_not_make_every_path_unowned` |
| G19 | Every queried path really is unowned, so the ownership query reports a failure exit code | An ordinary answer: all of them are presented | U | `test_manual_installs_sync:TestUnownedScan::test_a_batch_where_every_path_is_unowned_is_an_ordinary_answer` |
| G20 | The scan asks about one path dpkg is certain to own, to prove it answered | That path is never presented as a finding | U | `test_manual_installs_sync:TestUnownedScan::test_the_witness_is_never_reported_as_a_finding` |
| G21 | Atlas has a package named `brscan3` and, separately, a path whose last component is `brscan3` | Two independent items, one per kind of finding | U | `test_manual_installs_sync:TestUnreproducibleItem::test_same_identifier_different_origin_yields_distinct_item_ids` |
| G22 | Nomad holds hand-installed software of its own | Nomad is never asked what it has; nothing of Nomad's is presented | U | `test_manual_installs_sync:TestInstallOnly::test_no_removal_diff_or_group_even_when_the_target_holds_items` |
| G23 | `apt-cache` is missing on Atlas | Validation fails before anything runs, naming Atlas and the missing tool | U | `test_manual_installs_sync:TestValidate::test_apt_cache_unavailable_on_source_yields_validation_error` |
| G24 | `dpkg` is missing on Atlas | Validation fails before anything runs, naming Atlas and the missing tool | U | `test_manual_installs_sync:TestValidate::test_dpkg_unavailable_on_source_yields_validation_error` |
| G25 | Both tools present on Atlas | No validation error, and no administrative-rights precondition is imposed on Nomad (a snippet's own needs are unknowable) | U | `test_manual_installs_sync:TestValidate::test_valid_environment_yields_no_errors` |
| G26 | Only this job is enabled — apt sync is not in the configuration at all | The hand-`.deb` finding is still detected and presented; the job asks apt and dpkg its own questions | U V | `test_manual_installs_sync:TestExecuteIndependentOfApt::test_plan_runs_with_apt_absent_from_config_and_manual_enabled`; `test_package_sync:TestManualInstallsSyncEndToEnd::test_manual_installs_sync_pushes_registry_and_replays_snippet` |
| G27 | A genuine hand-downloaded `.deb` installed on a real machine | Presented as an item needing a snippet | — | none (no VM test installs a bare `.deb`) |
| G28 | A stock Ubuntu 24.04 machine's own `/usr/local` and `/opt` are scanned | The findings are few enough to review by hand; the scan roots themselves are not reported as findings | — | none |

## G.2 The three end states (articles: PKG-FR-MANUAL-RESOLUTION, PKG-FR-MANUAL-SOURCE-DECIDES)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| G29 | A detected item for which Atlas holds no snippet | It appears in its own "no package manager can install these on Nomad" question, and in no other list | U | `test_manual_installs_sync:TestSnippetResolution::test_item_without_snippet_is_report_only_and_grouped_separately` |
| G30 | A detected item for which Atlas holds a snippet | It appears as an ordinary install for Nomad, alongside the rest | U | `test_manual_installs_sync:TestTracerEndToEnd::test_detect_plan_and_replay_end_to_end` |
| G31 | At the question the user chooses to write a command that installs it, and writes one | The item is resolved by that snippet; the text is taken exactly as typed, blank lines and indentation included | U | `test_package_review:TestUnreproducibleGroupResolution::test_add_snippet_choice_captures_body_verbatim_including_whitespace` |
| G32 | At the question the user chooses "never install it on Nomad" | The item is resolved permanently; no snippet is written | U | `test_package_review:TestUnreproducibleGroupResolution::test_skip_always_choice_yields_skip_always_decision_and_no_snippet` |
| G33 | At the question the user chooses "not for now" | The item is resolved for this run — this is a real answer, not an item left hanging | U | `test_package_review:TestUnreproducibleGroupResolution::test_explicit_skip_once_is_a_resolution_not_unresolved` |
| G34 | A run whose only findings were all answered "not for now" | The run ends clean; nothing failed | U | `test_manual_installs_sync:TestSkipOnceResolution::test_run_whose_only_items_were_skipped_once_passes` |
| G35 | An item answered "not for now" this run | Nothing is written anywhere, so the next sync asks about it again | — | none (no assertion that a skip-for-now writes no record for this job) |
| G36 | An item answered "never install it on Nomad" | The mark is written on Atlas, the machine that holds the software — not on Nomad | — | none (`_finalize_unreproducible`'s permanent-mark branch is unasserted) |
| G37 | Atlas holds a mark from an earlier run for a still-present finding | It is not presented again, in any list | U | `test_manual_installs_sync:TestInertFiltering::test_machine_specific_item_is_filtered_before_becoming_a_diff` |
| G38 | The user interrupts at the resolution question (Ctrl-C / EOF) | The whole sync aborts naming the item; the item is not silently left undecided | U | `test_package_review:TestUnreproducibleGroupResolution::test_cancelled_select_aborts_the_entire_sync` |
| G39 | The user chooses to write a snippet and submits nothing | The choice is put again; there is no fourth "undecided" outcome | U | `test_package_review:TestUnreproducibleGroupResolution::test_empty_snippet_body_reprompts_until_a_real_choice` |
| G40 | The user submits a body of spaces and newlines only | Refused, the choice is put again | U | `test_package_review:TestUnreproducibleGroupResolution::test_a_whitespace_only_snippet_is_not_a_resolution` |
| G41 | After an empty submission the user writes a real snippet | Captured; the item resolves by snippet | U | `test_package_review:TestUnreproducibleGroupResolution::test_empty_snippet_then_real_snippet_is_captured` |
| G42 | Each of two findings is put to the user | One question per finding, in the same form as every other question | U | `test_package_review:TestUnreproducibleGroupResolution::test_each_item_gets_a_decision_screen_of_its_own` |
| G43 | Nomad holds a snippet for the finding, Atlas holds none | Still unresolved: the user is asked to resolve it | U | `test_manual_installs_sync:TestClassificationAuthority::test_target_only_snippet_stays_report_only` |
| G44 | Atlas holds a snippet, Nomad holds none | Resolved: presented as an install | U | `test_manual_installs_sync:TestClassificationAuthority::test_source_snippet_classifies_install` |
| G45 | Nomad holds a permanent mark for the item; Atlas holds none | The item is still presented — only Atlas's marks silence an Atlas-held finding | — | none (inferred from `plan()` reading Atlas's decision file only) |
| G46 | A run with no terminal, and findings to resolve | Nothing is asked, no snippet is written, no mark recorded, and every finding is reported as unanswered | U | `test_package_review:TestUnreproducibleGroupResolution::test_non_interactive_offers_no_capture_and_marks_every_item_unresolved` |
| G47 | A run with no terminal, and findings to resolve | The job reports skipped before it touches Nomad | P | `test_package_sync_core:TestExecuteSelfContained::test_a_non_interactive_package_review_skips_the_job_instead_of_applying_nothing` — asserted on a stand-in job, not on this one |
| G48 | An answered run where an item somehow arrives unanswered | The job does not fail on that basis | U | `test_manual_installs_sync:TestSkipOnceResolution::test_interactive_unresolved_no_longer_fails_the_run`; `test_package_review:TestUnresolvedNeverFailsTheJob::test_interactive_unresolved_does_not_raise` |

## G.3 Written now, used now (article: PKG-FR-MANUAL-SAME-RUN)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| G49 | The user writes a snippet during the review | It is stored in Atlas's registry BEFORE that registry travels to Nomad, so the copy Nomad receives contains it | U | `test_manual_installs_sync:TestSnippetPush::test_snippet_authored_in_review_is_persisted_before_the_push` |
| G50 | A run that replays a snippet on Nomad | The registry reaches Nomad first, the replay second | U V | `test_manual_installs_sync:TestSnippetPush::test_push_runs_after_review_and_before_replay_in_execute`; `test_package_sync:TestManualInstallsSyncEndToEnd::test_manual_installs_sync_pushes_registry_and_replays_snippet` |
| G51 | A finding with no snippet at the start of the run, resolved by a snippet written during the review | It is installed on Nomad in that same run, not the next one | U | `test_manual_installs_sync:TestSameRunApplication::test_on_the_fly_snippet_is_replayed_the_same_run` |
| G52 | A run that writes one snippet and then continues to the install stage | The snippet's record is stamped once, and Atlas's and Nomad's copies of the registry are identical afterwards | — | none (the once-per-run guard is unasserted) |
| G53 | A rehearsal (`--dry-run`) where a snippet is written during the review | The item is previewed as an install on Nomad, no command runs on Nomad, and Atlas's registry file is not written | U | `test_manual_installs_sync:TestClassificationAuthority::test_dry_run_previews_on_the_fly_install_without_replay_or_write` |
| G54 | A rehearsal where Atlas already holds a snippet for a finding | Previewed as an install on Nomad, naming the item; nothing runs there | P | same as G53 — only the written-during-review variant is asserted |
| G55 | A rehearsal where a finding is answered "never install it on Nomad" | No mark is written on Atlas | P | `test_package_state:TestPipelineWiring::test_no_record_call_when_dry_run` covers the shared path on a stand-in job; this job's own permanent-mark branch is unasserted |

## G.4 The snippet itself (article: PKG-FR-SNIPPET-VERBATIM)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| G56 | A snippet written with leading indentation and blank lines between commands | Stored and read back byte for byte | U | `test_package_state:TestSnippetRegistry::test_add_then_get_round_trips_body_verbatim_including_whitespace` |
| G57 | A snippet is replayed on Nomad | Run exactly as written, as one unit, as the SSH user, with nothing added around it — no elevation, no login shell | U | `test_package_state:TestSnippetRegistry::test_replay_passes_body_as_one_quoted_argument_with_login_shell_false` |
| G58 | A snippet whose command asks a question (a debconf prompt) | It fails as its own item rather than hanging the sync — nothing is ever fed to its input | U | `test_manual_installs_sync:TestPromptingSnippetCannotHang::test_replay_supplies_no_stdin_and_a_prompting_snippet_is_a_plain_item_failure` |
| G59 | A snippet that exits non-zero while printing nothing recognisable | Its exit code alone decides: failed | U | `test_package_state:TestSnippetRegistry::test_replay_exit_code_alone_decides_success` |
| G60 | A snippet whose body contains shell metacharacters and square-bracketed text | Stored, displayed and replayed unchanged; nothing tries to interpret it | P | escaping for display is exercised only through the registry-overwrite question (G70); no test stores/replays a bracket- or metacharacter-heavy body |
| G61 | The user is about to write a snippet | Before the editor opens they are told that Nomad runs it with nobody watching, and shown a worked non-interactive shape | — | none |
| G62 | A second snippet is written for a different item | The first is preserved; the registry accumulates | U | `test_package_state:TestSnippetRegistry::test_add_preserves_an_unrelated_pre_existing_entry` |
| G63 | The registry file is being written when the machine dies | The file is never left half written (written aside, then moved into place) | U | `test_package_state:TestSnippetRegistry::test_write_is_atomic_temp_then_move` |
| G64 | The registry file is absent, empty, or corrupt | Read as "no snippets"; a corrupt one warns naming the file | U | `test_package_state:TestSnippetRegistry::test_absent_file_returns_empty_mapping`, `::test_empty_file_returns_empty_mapping`, `::test_malformed_registry_returns_empty_mapping_and_warns_naming_the_path` |

## G.5 The registry travels (articles: PKG-FR-REGISTRY-SYNCS, PKG-FR-REGISTRY-CONSENT)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| G65 | A run in which Atlas holds a registry and Nomad does not | Nomad ends the run holding Atlas's registry, under the SSH user's own home — never under a system directory | U V | `test_manual_installs_sync:TestSnippetPush::test_push_sends_source_registry_under_the_user_home_never_etc`; `test_package_sync:TestManualInstallsSyncEndToEnd::test_manual_installs_sync_pushes_registry_and_replays_snippet` |
| G66 | Atlas has never had a snippet written on it | Nothing is transferred and nothing fails | U | `test_manual_installs_sync:TestSnippetPush::test_absent_source_registry_makes_push_a_noop` |
| G67 | Only this job is enabled — no configuration sync, no folder sync | The registry still reaches Nomad | V | `test_package_sync:TestManualInstallsSyncEndToEnd::test_manual_installs_sync_pushes_registry_and_replays_snippet` (the run's configuration enables this job alone) |
| G68 | Nomad's registry is a subset of Atlas's, or holds nothing at all | The transfer happens with no question asked | U | `test_manual_installs_sync:TestSnippetRegistryOverwriteGuard::test_additive_overwrite_proceeds_without_confirming` |
| G69 | Nomad holds exactly the same entry, word for word | Still no question — nothing is lost or changed | U | `test_manual_installs_sync:TestSnippetRegistryOverwriteGuard::test_identical_target_entry_is_additive` |
| G70 | Nomad holds a snippet Atlas does not have | The user is asked, shown which entry would be lost and its text; approving completes the transfer | U | `test_manual_installs_sync:TestSnippetRegistryOverwriteGuard::test_lost_target_entry_prompts_and_proceeds_on_confirm` |
| G71 | Nomad holds the same item with a different body | The user is asked, shown both bodies, named as a change | U | `test_manual_installs_sync:TestSnippetRegistryOverwriteGuard::test_changed_body_is_non_additive_and_prompts` |
| G72 | Nomad holds the same item with the same body but a different label or authoring record | Treated as no change and overwritten without asking | ‼ | the comparison is on the body alone (`_guard_registry_overwrite`); no test |
| G73 | The user declines that question | The whole sync stops so the two registries can be reconciled by hand; nothing is sent | U | `test_manual_installs_sync:TestSnippetRegistryOverwriteGuard::test_lost_target_entry_aborts_on_decline` |
| G74 | The user declines, and other jobs were still to run | The run ends there rather than continuing with the remaining jobs | P | the unit test asserts the abort is raised; the run-level effect rests on the orchestrator's shared abort handling, untested for this call site |
| G75 | A run with no terminal (or `--yes`) where the transfer would lose a Nomad entry | It aborts — there is no flag that approves this | U | `test_manual_installs_sync:TestSnippetRegistryOverwriteGuard::test_non_interactive_non_additive_aborts` |
| G76 | A non-additive transfer with nothing wired up to ask the question | The run fails loudly and sends nothing | U | `test_manual_installs_sync:TestSnippetRegistryOverwriteGuard::test_non_additive_push_without_a_confirmer_fails_and_sends_nothing` (fails as a bare assertion — loud but not a user-readable message) |
| G77 | A snippet body that fetches a private package with a credential in its address, shown in that question | The credential is withheld from what the user reads, while the file that travels and the command that runs keep the author's exact bytes | U | `test_manual_installs_sync:TestSnippetRegistryOverwriteGuard::test_a_credential_in_a_snippet_body_is_withheld_from_the_question` |
| G78 | A snippet label or body containing square-bracketed text reaches that question | Shown as written; the display does not break | — | none |
| G79 | Nomad's registry file is corrupt | Read as holding nothing, so the transfer is treated as purely additive and overwrites it without asking | — | none; the degrade rule is asserted in isolation (G64) but not its consequence for consent |
| G80 | Atlas's registry file on disk is corrupt at transfer time | Every Nomad entry counts as lost, so the user is asked — and approving sends the corrupt file | — | none |
| G81 | A rehearsal (`--dry-run`) | No registry is transferred and no question is asked | U | `test_manual_installs_sync:TestSnippetPush::test_dry_run_pushes_nothing` |
| G82 | A run with no terminal whose scan found nothing to review | The job succeeds and still transfers no registry — the file holds entries from earlier runs that nobody approved sending | U | `test_manual_installs_sync:TestSnippetPush::test_a_run_with_no_terminal_pushes_nothing_even_with_nothing_to_review` |
| G83 | A run at a terminal whose scan found nothing to review | The registry still travels: an empty review means nothing new to decide, not nothing to carry | — | none |
| G84 | Creating the destination directory on Nomad fails, or Nomad's home cannot be resolved | The job fails naming Nomad; nothing is half-transferred | — | none |

## G.6 When a snippet does not work (article: PKG-FR-MANUAL-FAIL-ITEM)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| G85 | A snippet that existed when the run was planned is gone by the time it would run | That item fails on its own, naming it; the run continues and the job does not crash | U | `test_manual_installs_sync:TestSnippetResolution::test_missing_snippet_at_converge_is_a_failed_result_not_a_crash` |
| G86 | Three items, the middle one's snippet exits non-zero | The first and third are installed on Nomad, the middle is reported failed with its own output, and the sync's exit code is non-zero | U V | `test_manual_installs_sync:TestContinueOnFailure::test_failed_snippet_replay_is_a_per_item_failure_and_does_not_stop_the_job`; `test_package_sync:TestPackageSyncWholeRunContracts::test_continue_on_item_failure` |
| G87 | A snippet that needs administrative rights it does not have on Nomad | Fails as its own item and is reported like any other; the job did not pre-check for it | P | covered as an ordinary non-zero replay (G86); no test uses the missing-rights shape specifically |

## G.7 What is never done (articles: PKG-NG-MANUAL-REMOVE, PKG-FR-JOB-INDEPENDENCE for this job)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| G88 | Nomad holds hand-installed software Atlas does not have | No removal is ever offered, in any list | U | `test_manual_installs_sync:TestInstallOnly::test_no_removal_diff_or_group_even_when_the_target_holds_items` |
| G89 | Nomad is asked what unreproducible software it holds | It is never asked — there is no such question to put to it | U | `test_manual_installs_sync:TestInstallOnly::test_target_query_is_empty_by_design` |
| G90 | A run replays two snippets successfully on Nomad | Nothing on Nomad records what this job put there; a later run has no memory of it | — | none (no assertion that the only file this job writes on Nomad is the registry) |
| G91 | The job is named in the configuration | It resolves to its own job and runs on its own switch | U | `test_manual_installs_sync:TestJobDiscovery::test_orchestrator_resolves_manual_installs_sync_to_its_job` |
| G92 | A first sync where this job is enabled | The scope it announces names replaying install snippets as what it will do to Nomad | — | none (`describe_first_sync_scope` unasserted) |

## Gaps

**G5 — hand-`.deb` outside the manual set.** Unit test: `apt-mark showmanual` returns a set that omits the package, policy output includes its origin-less block; assert it is not presented. Mocks suffice.

**G6 — hand-installed at a newer version than the repository offers.** Unit test: craft policy output whose `***` row carries only `/var/lib/dpkg/status` while an older row carries a repository URI; assert the package is presented. Mocks suffice, and this is the shape the parser's docstring claims to handle with no test behind it.

**G27 — a real bare `.deb`.** Needs a VM: install a small `.deb` on pc1 with `dpkg --install`, run with only this job enabled and the automation map answering that item, assert the item reached the review. Real apt/dpkg semantics are the point — a mock cannot show that apt reports the installed version as its own candidate.

**G28 — the scan's real-world noise.** Needs a VM: run the scan on a stock pc1 and record what it names, in particular whether `/usr/local/bin` and `/usr/local/lib` are themselves reported alongside their children (they are queried both as roots and as entries of `/usr/local`). Not derivable from mocks — it depends on whether the distribution's own packages claim those directories.

**G35 — skip-for-now records nothing.** Unit test: drive `apply()` for this job with one unreproducible item decided skip-for-now and assert no write of `manual.decisions.yaml` reaches Atlas. Mocks suffice.

**G36 — the permanent mark is written on Atlas.** Unit test: same shape with skip-always, asserting the write goes through the source executor and never the target's, and that the entry carries the item's own id and label. Mocks suffice. This is the only branch of `_finalize_unreproducible` with no test at all — the read side (G37) is covered, the write side is not.

**G45 — a mark on Nomad does not silence an Atlas-held finding.** Unit test: seed Nomad's decision file with the item id, leave Atlas's empty, assert the item is still presented. Mocks suffice.

**G47 — this job's own no-terminal skip.** Unit test: `FakeReviewer(was_interactive=False)` with a non-empty scan, assert `JobSkipped` and that no `send_file` and no `bash -c` reached Nomad. Mocks suffice; the existing stand-in-job test does not exercise this job's `after_review`.

**G52 — the once-per-run guard.** Unit test: drive `execute()` with one written snippet and count the registry writes on Atlas (`mv --force` of `package-snippets.yaml`) — exactly one. Mocks suffice.

**G54 — rehearsal of a pre-existing snippet.** Unit test: dry run with Atlas holding the snippet and the review approving, assert the preview line names the item and no `bash -c` reached Nomad. Mocks suffice.

**G55 — rehearsal records no permanent mark.** Fold into G36's test with `dry_run=True`.

**G60 / G78 — a body the tool must not interpret.** Unit test: store and replay a body containing `[bold]`, `$(...)`, backticks and quotes, asserting exact round-trip and exact replay command; and drive the registry-overwrite question with such a label and body, asserting the question renders (it would raise on unescaped markup). Mocks suffice.

**G61 — the authoring warning.** Unit test in the review module: patch the editor, assert the console received text naming the target machine and warning that a command asking a question hangs the sync. Mocks suffice.

**G72 — label-only difference is silently overwritten (finding).** The comparison in `_guard_registry_overwrite` is `source.body != target.body`; label, authoring time and authoring machine are ignored. A Nomad entry whose label differs is replaced with no question. Whether this counts as "changing an entry the target holds" is a judgement the article does not settle — see Notes. If it is to be treated as a change, the fix and its test are both one line.

**G74 — declining ends the run, not just the job.** Unit or integration: the unit path can assert the exception escapes `execute()` unchanged; proving the remaining jobs do not run needs the orchestrator, so an orchestrator-level unit test with two stub jobs is the cheap version.

**G79 / G80 — a corrupt registry on either side.** Unit tests: (a) Nomad's registry unparseable, Atlas's holds one entry → assert whether a question is asked, and record the answer as the decision it is; (b) Atlas's on-disk registry unparseable while Nomad holds entries → assert the question is asked and, if approved, that a corrupt file is what gets sent. Mocks suffice. Both are ADR-022 "bad data is handled" cases with no coverage.

**G83 — an answered run with an empty review still transfers the registry.** Unit test: `FakeReviewer(was_interactive=True)`, empty scan, an on-disk registry on Atlas → assert `send_file` was called. Mocks suffice. This is the mirror of the covered no-terminal case (G82) and the one that shows the registry is carried for its own sake.

**G84 — transfer plumbing failures.** Unit test: make the directory creation on Nomad fail, then make the home lookup fail; assert each raises naming Nomad and that `send_file` never ran. Mocks suffice.

**G87 — a snippet lacking administrative rights.** Could be a VM test (a snippet doing `sudo` where the SSH user has no rights), but it adds nothing over G86 unless the message is asserted; low value.

**G90 — no record kept on Nomad.** Unit test: after a successful replay, assert no command issued to Nomad writes a decision file. Mocks suffice; cheap regression fence for the non-goal.

**G92 — first-sync scope wording.** Unit test asserting `describe_first_sync_scope` names this job and its mechanism. Trivial; may belong to whichever area owns first-sync scope.

## Notes for the assembler

- **Overlap with area A (apt).** `PKG-FR-DEB-OWNERSHIP` is split as instructed: G1–G12 and G26–G27 are the "this job owns it" half. The other half — apt sync producing no item, no review line and no install for the same package in any configuration — is area A's, and the two must be checked together: both jobs run the byte-identical policy command with the same strictness precisely so they cannot disagree about which packages are bare `.deb`s. If area A's rows and mine ever assert different predicates, that is a finding in itself.
- **Overlap with area H (review UI).** G31–G33 and G38–G42 are the unreproducible question's own three-way behaviour, which is specific to this job's items; the generic mechanics of the question (wording, machine naming, key layout, answers-as-a-set) belong to H, including `test_package_review:TestUnreproducibleGroupResolution::test_the_three_answers_read_as_they_do_on_every_other_screen`, which I have left to H.
- **Overlap with area J.** G46–G47 and G81–G82 touch the no-terminal and dry-run articles. I kept only the rows where the observable outcome is this job's own (a snippet written, a registry transferred); the general outcome rules are J's.
- **G77's credential rule** is `PKG-FR-CREDENTIAL-PRIVACY`, which is not in my list, but the registry-overwrite question is my article's question, so the row sits here. Whoever owns privacy should cross-reference rather than duplicate.
- **Genuinely ambiguous — G72.** "A registry transfer that would lose or change an entry the target holds" does not say whether an entry's label and authoring record are part of the entry or only its body is. The code says body only. Worth settling in the article rather than in the test.
- **Deliberately absent.** A sideloaded snap, and a flatpak from a local bundle or a dead remote, are named in the narrative as out of scope for now (issue #221). They produce no rows; they are not gaps.
- **Not reachable from the integration harness.** The review-automation environment variable answers with decisions only — it cannot supply a snippet body. Every "written during the review" row (G31, G39–G41, G49, G51, G53) is therefore unit-only by construction, and the VM test covers the neighbouring path instead (a snippet already on Atlas, transferred and replayed in one run). Anyone planning VM coverage for authoring needs a different hook, not a different test.
- **Rows I split.** The "vanished snippet" and "replay fails" halves of `PKG-FR-MANUAL-FAIL-ITEM` are G85 and G86: they reach the failure by different routes (a registry read that finds nothing versus a command that exits non-zero) and only one of them has VM coverage.
