# Sweep — J: outcomes, failure isolation, the dry run, the no-terminal run, the log and what it withholds

Articles: `PKG-FR-OUTCOME-SUCCESS`, `PKG-FR-OUTCOME-SKIPPED`, `PKG-FR-OUTCOME-FAILED`, `PKG-FR-NO-TERMINAL`, `PKG-FR-DRY-RUN`, `PKG-FR-READ-FAILS-JOB`, `PKG-FR-LOG-DECISIONS`, `PKG-FR-LOG-VERBATIM`, `PKG-FR-CREDENTIAL-PRIVACY`, `PKG-FR-FAIL-NAMED`, `PKG-FR-SOURCE-INTENT`, `PKG-FR-MANAGER-CONVERGES`, `PKG-FR-CONFIRM-EACH`.

Test paths: unit modules are under `tests/unit/`; `test_package_sync_core`, `test_package_review`, `test_package_state`, `test_manual_installs_sync`, `test_snap_sync`, `test_flatpak_sync` in `tests/unit/jobs/`; `test_apt_*` in `tests/unit/jobs/apt/`; `test_step_gate`, `test_redaction`, `test_mutates_audit`, `test_machine_naming` in `tests/unit/`; orchestrator modules in `tests/unit/orchestrator/`; `test_package_sync` is `tests/integration/jobs/test_package_sync.py`.

## J-A Success (article: PKG-FR-OUTCOME-SUCCESS)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| J1 | Atlas has one package Nomad lacks; the user approves it and the install succeeds | The job reports success and the package is on Nomad | U V | `test_apt_job:TestContinueOnFailure` (converse); `test_package_sync:TestAptSyncEndToEnd::test_apt_sync_installs_missing_package` |
| J2 | Atlas and Nomad hold identical item sets, so the job presents nothing | The job reports success; the review is still consulted once, with no groups; no command carrying `mutates=` is issued | U V | `test_package_sync_core:TestIdempotency::test_identical_source_and_target_produce_no_diff_no_group_and_no_mutation`; `test_package_sync:TestPackageSyncIdempotency::test_second_consecutive_sync_has_nothing_to_do` |
| J3 | Every item presented is answered "skip this run" | The job reports success; nothing converges | U | `test_package_sync_core:TestDecisionsReachTheLog::test_a_skipped_item_leaves_a_line_where_nothing_else_would` |
| J4 | Every item presented is answered "always skip" | The job reports success; each mark is recorded on its holding machine; nothing converges | U | `test_package_state:TestPipelineWiring::test_skip_always_on_change_writes_to_source_not_target` |
| J5 | An approved install is withdrawn by a question this run's own first change made answerable (late collateral) | It is counted neither applied nor failed; the job does not fail on its account; the withdrawal and its reason are named | U | `test_apt_collateral:TestCollateralForARepositoryThisRunWrites::test_the_decision_is_named_in_the_log` |
| J6 | The plan holds only report-only findings and a terminal is present | Nothing converges; the job reports success | U | `test_package_review:TestInteractive::test_a_report_only_group_is_printed_and_asks_nothing` |
| J7 | A run whose only items were unreproducible and all answered "skip this run" | Skip-once counts as a resolution; the job reports success | U | `test_manual_installs_sync:TestSkipOnceResolution::test_run_whose_only_items_were_skipped_once_passes` |
| J8 | A job presented nothing and the run has no terminal | Success, not skipped — the target already matches | U | `test_package_sync_core:TestExecuteSelfContained::test_an_empty_plan_is_still_a_success_and_transfers_nothing` |

## J-B Skipped (article: PKG-FR-OUTCOME-SKIPPED; the two named causes)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| J9 | The run has no terminal and a job's review is non-empty | That job reports skipped, naming why; it does not report success | U V | `test_package_sync_core:TestExecuteSelfContained::test_a_non_interactive_package_review_skips_the_job_instead_of_applying_nothing`; `test_package_sync:TestPackageSyncWholeRunContracts::test_non_interactive_skip_all` |
| J10 | Nomad reports no Ubuntu Pro attachment and the user chooses to skip | `apt_sync` reports skipped naming the ESM files and the reason; `/etc/apt` on Nomad is exactly as found | U V | `test_apt_esm_gate:TestTheESMAttachmentGate::test_choosing_skip_raises_job_skipped_and_writes_nothing`; `test_package_sync:TestTheESMAttachmentGateOnVMs::test_an_unattached_target_skips_apt_sync_and_leaves_etc_apt_untouched` |
| J11 | Nomad reports no Ubuntu Pro attachment and there is no terminal to ask at | `apt_sync` reports skipped, saying no TTY was available to choose between attaching and skipping | U | `test_apt_esm_gate:TestTheESMAttachmentGate::test_a_non_interactive_run_skips_the_whole_job` |
| J12 | A skipped job's items included a "always skip" answer that could not be given | No decision file is created or changed on either machine | U V | `test_package_state:TestPipelineWiring::test_no_record_call_when_outcome_was_not_interactive`; `test_package_sync:TestPackageSyncWholeRunContracts::test_non_interactive_skip_all` |
| J13 | A skipped `manual_installs_sync` run | No snippet registry is transferred to Nomad | U | `test_manual_installs_sync:TestSnippetPush::test_a_run_with_no_terminal_pushes_nothing_even_with_nothing_to_review` |
| J14 | A skipped job | The target is untouched — the skip is raised before any mutating command | U V | `test_package_sync_core:TestExecuteSelfContained::test_a_non_interactive_package_review_skips_the_job_instead_of_applying_nothing`; `test_package_sync:TestPackageSyncWholeRunContracts::test_non_interactive_skip_all` |
| J15 | A job reports skipped, three other jobs follow | The run continues and each following job executes | U | `test_skipped_jobs:TestSkippedJobArm::test_the_orchestrator_records_a_skipped_job_and_runs_the_next_one` |
| J16 | A run in which one job was skipped and no job failed | The session is COMPLETED and the exit code is 0 | U | `test_skipped_jobs:TestSkippedJobArm::test_the_orchestrator_records_a_skipped_job_and_runs_the_next_one`; `test_session_status_from_job_results:TestSessionStatusReflectsJobResults::test_skipped_job_result_is_not_a_failure` |
| J17 | A skipped job's reason | Reaches the run's own report, not only the log | U | `test_skipped_jobs:TestSkippedJobArm::test_the_orchestrator_records_a_skipped_job_and_runs_the_next_one` |
| J18 | Skipped is distinguished from "answered no": a job the user answered entirely with declines | Reports success (J3), while a job nobody could answer reports skipped (J9) | U | J3 + J9 read together |

## J-C Failure and its isolation (articles: PKG-FR-OUTCOME-FAILED, PKG-FR-FAIL-NAMED)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| J19 | One approved item's converge command exits non-zero | The job reports failure | U | `test_apt_job:TestContinueOnFailure::test_second_of_three_fails_all_attempted_one_failure_raised` |
| J20 | Three approved items, the middle one fails | All three are attempted; the third still converges | U V | `test_apt_job:TestContinueOnFailure::test_second_of_three_fails_all_attempted_one_failure_raised`; `test_package_sync:TestPackageSyncWholeRunContracts::test_continue_on_item_failure` |
| J21 | Several items fail in one job | The failures are collected and raised once, after the loop, naming each item | U | `test_snap_sync:TestHoldAndRevisionFailuresArePerItem`; `test_apt_etc_apt:TestRepoGroupTransaction::test_failed_update_restores_changed_deletes_created_records_group_failures`; `test_apt_etc_apt:TestRepoGroupBackupFailure::test_backup_failure_fails_every_group_item_without_crashing` |
| J22 | A converge step refuses an item without attempting the command (a transaction guard) | That item alone fails, naming what it concerns; the loop continues | U | `test_apt_etc_apt:TestRepoGroupOrdering::test_a_failed_derived_repository_write_fails_the_package_that_needed_it`; `test_apt_etc_apt:TestKeyringsDirectoryEnsured::test_directory_preparation_failure_fails_the_item_not_the_run` |
| J23 | Forty items fail in one job | The end-of-run message adds one line for that job, not forty | U | `test_session_status_from_job_results:TestTheOutcomeMessageNamesWhatFailed::test_a_job_with_many_failed_items_stays_on_one_line` |
| J24 | Two jobs fail | The end-of-run message names both jobs and both reasons | U | `test_session_status_from_job_results:TestTheOutcomeMessageNamesWhatFailed::test_each_failed_jobs_reason_reaches_the_message` |
| J25 | A failed job with no recorded reason | The message still names the job | U | `test_session_status_from_job_results:TestTheOutcomeMessageNamesWhatFailed::test_a_failure_without_a_recorded_reason_still_names_its_job` |
| J26 | `apt_sync` fails its items; `snap_sync` follows | `snap_sync` still runs — one failed job does not stop the others | U V | `test_package_sync_core:TestOrchestratorPackageItemFailuresContinuation::test_failing_package_job_does_not_cancel_remaining_jobs`; `test_package_sync:TestPackageSyncWholeRunContracts::test_continue_on_item_failure` |
| J27 | A package job raises an ordinary exception that is neither a converge failure nor a dead read (a registry transfer error, a parser defect) | That job alone fails; the following jobs run — "any exception out of a package job stays in that job" | U | `test_job_failure_isolation:TestAnyFailureOfAPackageJobStaysInThatJob::test_a_generic_exception_does_not_stop_the_following_job` |
| J28 | That job's failure reason | Reaches its `JobResult` verbatim | U | `test_job_failure_isolation:TestAnyFailureOfAPackageJobStaysInThatJob::test_the_failed_result_carries_what_went_wrong` |
| J29 | A dead read (`ProbeFailed`) out of any job, package or not | Fails only that job; the following job runs | U | `test_job_failure_isolation:TestProbeFailedFailsOnlyItsOwnJob::test_the_orchestrator_records_it_failed_and_runs_the_next_job` |
| J30 | A lock conflict surfaces inside a package job | The whole run ends; no later job runs | U | `test_job_failure_isolation:TestAnyFailureOfAPackageJobStaysInThatJob::test_a_lock_conflict_still_ends_the_run` |
| J31 | A job outside package sync fails | The run still aborts (the #220 boundary this article does not cover) | U | `test_package_sync_core:TestOrchestratorPackageItemFailuresContinuation::test_other_exception_types_still_abort_the_run` |
| J32 | A run that continued past a failed job | The session is FAILED and the CLI exits non-zero | U V | `test_job_failure_isolation:TestProbeFailedFailsOnlyItsOwnJob::test_the_session_is_still_reported_failed`; `test_session_status_from_job_results:TestCliExitCodeFromSessionStatus::test_failed_session_exits_non_zero`; `test_package_sync:TestPackageSyncWholeRunContracts::test_continue_on_item_failure` |
| J33 | Any failure message a job emits | Names the item, package or file it concerns, and never a role ("the target") | U | `test_machine_naming:TestOutcomeMessages::test_no_outcome_message_names_a_role`; `test_job_failure_isolation:TestProbeFailedFailsOnlyItsOwnJob::test_the_failed_result_carries_the_command_that_did_not_answer` |
| J34 | A converge command's stderr | Is carried into the failure line and the run's summary | U V | `test_package_sync_core` (`_converge_one` path, asserted per manager); `test_package_sync:TestPackageSyncWholeRunContracts::test_continue_on_item_failure` |

## J-D The run with no terminal (article: PKG-FR-NO-TERMINAL)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| J35 | No TTY, groups to present | No prompt is constructed at all | U | `test_package_review:TestNonInteractive::test_no_prompt_constructed_and_everything_skipped_once` |
| J36 | No TTY | Every reviewable item comes back declined for this run | U | `test_package_review:TestNonInteractive::test_no_prompt_constructed_and_everything_skipped_once` |
| J37 | No TTY | Each item is NAMED as unasked and declined, never counted | U V | `test_package_review:TestNonInteractive::test_warns_naming_every_item_and_reports_groups`; `test_package_sync:TestPackageSyncWholeRunContracts::test_non_interactive_skip_all` |
| J38 | No TTY, unreproducible items | No snippet capture is offered; every such item is unresolved and none is a written snippet | U | `test_package_review:TestUnreproducibleGroupResolution::test_non_interactive_offers_no_capture_and_marks_every_item_unresolved` |
| J39 | No TTY, unreproducible items unresolved | The job is skipped for having a non-empty review, not failed for the unresolved items | U | `test_package_review:TestUnresolvedNeverFailsTheJob::test_non_interactive_unresolved_does_not_raise_on_that_basis_alone` |
| J40 | No TTY, apt collateral entries in the plan | They come back declined for this run and are not unresolved | U | `test_package_review:TestCollateralGroupResolution::test_non_interactive_collateral_entries_skip_once_and_are_not_unresolved` |
| J41 | No TTY, a repository-conflict entry in the plan | It comes back declined for this run; the overwrite does not happen | U | `test_package_review:TestRepoConflictGroupResolution::test_non_interactive_conflict_entries_skip_once_and_are_not_unresolved` |
| J42 | No TTY, a collateral question raised after the run's first change | The item is withheld rather than pushed through or failed | U | `test_apt_collateral:TestCollateralForARepositoryThisRunWrites::test_a_run_with_no_terminal_declines_it` |
| J43 | No TTY, a gate question that is not about an item (Ubuntu Pro) | Answers "nobody there" without constructing a prompt; the caller decides the fallback | U | `test_package_review:TestAskGate::test_no_tty_answers_none_without_constructing_a_prompt` |
| J44 | No TTY — what must NOT be written: a decision record | No decision file is written on either machine | U V | `test_package_state:TestPipelineWiring::test_no_record_call_when_outcome_was_not_interactive`; `test_package_sync:TestPackageSyncWholeRunContracts::test_non_interactive_skip_all` |
| J45 | No TTY — what must NOT be written: a snippet | No snippet is added to the source registry | U | `test_package_sync_core:TestFinalizeUnreproducible::test_no_finalize_writes_when_outcome_not_interactive` |
| J46 | No TTY — what must NOT be written: the registry transfer | No registry is pushed to the target, even when this run's review was empty | U | `test_manual_installs_sync:TestSnippetPush::test_a_run_with_no_terminal_pushes_nothing_even_with_nothing_to_review` |
| J47 | No TTY, empty review | The job reports success and `after_review()` is still skipped | U | `test_package_sync_core:TestExecuteSelfContained::test_an_empty_plan_is_still_a_success_and_transfers_nothing` |
| J48 | The undocumented automation environment variable is set | Its answers count as the user's own (`was_interactive` is true), so permanent answers are honoured; it appears in no help text | U | `test_package_review:TestAutomationEnv::test_automation_env_returns_mapped_decisions_without_prompting`, `::test_env_var_not_mentioned_in_cli_help` |
| J49 | Whole non-interactive run against two real machines, one item diverged in each direction | Nothing applied, no decision file created, each item named, `apt_sync` reported skipped, exit code 0 | V | `test_package_sync:TestPackageSyncWholeRunContracts::test_non_interactive_skip_all` |

## J-E The dry run (article: PKG-FR-DRY-RUN)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| J50 | `--dry-run` on a terminal | The same plan is built and the same review is put to the user as in a real run | P | `test_apt_job:TestDryRun::test_dry_run_issues_no_mutating_command` and `test_manual_installs_sync:TestClassificationAuthority::test_dry_run_previews_on_the_fly_install_without_replay_or_write` drive `execute()` through a reviewer, so the review IS reached; no test compares a dry run's plan/groups against the same fixture's real run |
| J51 | `--dry-run` with install, change, remove and report-only diffs all approved | No converge command is issued for any of them | U | `test_package_sync_core:TestConvergeDispatchByAction::test_dry_run_zero_mutating_commands_across_all_four_action_types` |
| J52 | `--dry-run` preview of an item whose meaning lives in its detail | The preview line carries the item's detail, not just its name | U | `test_package_sync_core:TestConvergeDispatchByAction::test_dry_run_preview_carries_each_items_detail` |
| J53 | `--dry-run` with derived apt writes (a pin, a repository, a signing key, the metadata refresh) | Each derived change that a real run would make appears in the preview; one that no approved package needs does not | U V | `test_apt_probe:TestWhatAptItselfReads::test_a_dry_run_previews_the_derived_writes_and_issues_none`; `test_package_sync:TestAptSyncEndToEnd::test_apt_repository_state_dry_run_previews_derived_writes_and_reviews_no_repository` |
| J54 | `--dry-run` with a derived flatpak remote / a remote deletion / a filter clear | Each is previewed and none is issued | U | `test_flatpak_sync:TestRemotesAreDerivedFromApprovedRefs::test_a_dry_run_previews_the_derived_writes_and_issues_none`; `test_flatpak_sync:TestUnusedRemoteIsDeleted::test_a_dry_run_previews_the_deletion_and_issues_none`; `test_flatpak_sync:TestRemoteFilterReplicates::test_a_dry_run_previews_the_clear_and_issues_none` |
| J55 | `--dry-run` with a "always skip" answer | No decision file is written | U | `test_package_state:TestPipelineWiring::test_no_record_call_when_dry_run`; `test_package_sync_core:TestFinalizeUnreproducible::test_no_finalize_writes_during_dry_run` |
| J56 | `--dry-run` with a snippet authored during the review | The item is previewed as an install; no replay reaches the target and no registry write happens | U | `test_manual_installs_sync:TestClassificationAuthority::test_dry_run_previews_on_the_fly_install_without_replay_or_write` |
| J57 | `--dry-run` pushes the snippet registry | It does not | U | `test_manual_installs_sync:TestSnippetPush::test_dry_run_pushes_nothing` |
| J58 | `--dry-run` against a real machine | The target's `apt-mark showmanual` is byte-identical before and after | V | `test_package_sync:TestAptSyncEndToEnd::test_apt_sync_dry_run_changes_nothing` |
| J59 | `--dry-run` on a terminal, plan non-empty | The job reports success | V | `test_package_sync:TestAptSyncEndToEnd::test_apt_sync_dry_run_changes_nothing` (asserts the run's exit code is 0) |
| J60 | `--dry-run` with no terminal and a non-empty plan | The job reports skipped, for the same reason a real run does | — | see Gaps |
| J61 | `--dry-run` against an unattached Ubuntu Pro target | Nothing is asked; a warning says a real run would skip the whole apt job | U | `test_apt_esm_gate:TestTheESMAttachmentGate::test_a_dry_run_never_prompts_about_attachment` |
| J62 | `--dry-run` with unresolved unreproducible items | The run does not fail on that basis | U | `test_package_review:TestUnresolvedNeverFailsTheJob::test_dry_run_unresolved_does_not_raise_on_that_basis_alone` |

## J-F A read that cannot be answered (article: PKG-FR-READ-FAILS-JOB)

Per machine, per job, for a package-manager query and for a job's own scan.

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| J63 | `apt-mark showmanual` on Atlas exits 100 | `apt_sync` fails naming the command, its exit code and apt's own stderr; nothing is proposed | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_source_manual_set_read_that_did_not_answer_fails_the_job` |
| J64 | The target's manifest read does not answer | `apt_sync` fails naming the command and the machine | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_target_manifest_read_that_did_not_answer_fails_the_job` |
| J65 | The read apt's collateral protection rests on does not answer | `apt_sync` fails rather than classifying every casualty as automatic | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_collateral_protection_read_that_did_not_answer_fails_the_job` |
| J66 | The installed-version read does not answer | `apt_sync` fails | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_version_read_that_did_not_answer_fails_the_job` |
| J67 | The hold-set read does not answer | `apt_sync` fails | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_hold_read_that_did_not_answer_fails_the_job` |
| J68 | The target's installed-set read does not answer | `apt_sync` fails | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_an_unanswered_installed_set_read_fails_the_job` |
| J69 | The target's `apt-cache policy` does not answer | `apt_sync` fails | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_target_policy_read_that_did_not_answer_fails_the_job` |
| J70 | A `/etc/apt` directory digest listing does not answer | `apt_sync` fails naming the directory | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_directory_digest_read_that_did_not_answer_fails_the_job` |
| J71 | The source-file reference scan does not answer | `apt_sync` fails | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_source_file_scan_that_did_not_answer_fails_the_job` |
| J72 | A file a conflict question would print whole cannot be read | `apt_sync` fails naming the path and the machine, rather than showing an empty pane | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_conflict_content_read_that_did_not_answer_fails_the_job` |
| J73 | A file offered for removal cannot be read | `apt_sync` fails naming the path and the machine | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_removal_content_read_that_did_not_answer_fails_the_job` |
| J74 | The read that decides whether a repository still feeds anything does not answer | `apt_sync` fails rather than answering "it strands nothing" | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_removal_impact_read_that_did_not_answer_fails_the_job` |
| J75 | The source's `apt-cache policy` runs but prints no block at all | `apt_sync` fails — the guard where at least one answer is owed | U | `test_apt_probe:TestBareDebPackagesAreNotAptSyncsBusiness::test_a_source_policy_that_printed_nothing_at_all_fails_the_run` |
| J76 | `snap list --all` on Atlas exits 1 (snapd unreachable) | `snap_sync` fails naming the command, exit code and stderr | U | `test_snap_sync:TestAProbeThatDidNotAnswer::test_a_source_list_that_did_not_answer_fails_the_job` |
| J77 | `snap list --all` on Nomad exits 1 | `snap_sync` fails | U | `test_snap_sync:TestAProbeThatDidNotAnswer::test_a_target_list_that_did_not_answer_fails_the_job` |
| J78 | `flatpak list` cannot open the installation | `flatpak_sync` fails naming the command and flatpak's error | U | `test_flatpak_sync:TestAProbeThatDidNotAnswer::test_a_source_list_that_did_not_answer_fails_the_job` |
| J79 | `flatpak remotes` cannot parse its config | `flatpak_sync` fails naming the scope's command | U | `test_flatpak_sync:TestAProbeThatDidNotAnswer::test_a_remotes_read_that_did_not_answer_fails_the_job` |
| J80 | `flatpak mask` does not answer | `flatpak_sync` fails | U | `test_flatpak_sync:TestAProbeThatDidNotAnswer::test_a_mask_read_that_did_not_answer_fails_the_job` |
| J81 | `manual_installs_sync`'s own `apt-cache policy` detection read does not answer | The job fails naming the command | U | `test_manual_installs_sync:TestNoCandidateDetection::test_a_policy_read_that_did_not_answer_fails_the_job` |
| J82 | The same read answers with no block at all | The job fails — the two jobs asking the identical command carry the identical guard | U | `test_manual_installs_sync:TestNoCandidateDetection::test_a_policy_read_that_printed_no_block_at_all_fails_the_job` |
| J83 | `apt-mark showmanual` for the detection scan does not answer | The job fails | U | `test_manual_installs_sync:TestNoCandidateDetection::test_a_manual_set_read_that_did_not_answer_fails_the_job` |
| J84 | A job's OWN scan: the `/usr/local` + `/opt` walk cannot run | `manual_installs_sync` fails naming the failure, rather than reporting nothing found | U | `test_manual_installs_sync:TestUnownedScan::test_a_find_that_could_not_run_fails_the_job_rather_than_reporting_nothing` |
| J85 | A job's OWN scan: `dpkg --search` is dead, so every scanned path looks unowned | The job fails, because the witness path dpkg owns on every machine is absent from the reply | U | `test_manual_installs_sync:TestUnownedScan::test_a_dpkg_that_did_not_answer_does_not_make_every_path_unowned` |
| J86 | Silence must not be read as an empty installed set: `apt-mark showmanual` exits 0 with no output | Ordinary data — the target's packages become removal proposals | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_an_empty_source_manual_set_at_exit_zero_is_still_data` |
| J87 | Silence must not be read as broken: `snap list --all` exits 0 with no snaps | Ordinary data | U | `test_snap_sync:TestAProbeThatDidNotAnswer::test_a_source_with_no_snaps_installed_is_data_not_a_failure` |
| J88 | An empty hold set | Ordinary data | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_an_empty_hold_set_is_data_not_a_failure` |
| J89 | A `/etc/apt` directory that does not exist | Answers "nothing" at exit 0 and is planned through — the reshaped command, tested with the same privilege as the query it wraps | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_an_absent_directory_answers_nothing_rather_than_failing` |
| J90 | A scan root that is not there | Skipped by the loop, so it never reaches the exit code | U | `test_manual_installs_sync:TestUnownedScan::test_a_scan_root_that_is_not_there_is_skipped_not_an_error` |
| J91 | `sha256sum` over a glob matching nothing (a scope with no remote keyring) exits 1 | Not a failure — this read is deliberately unguarded | U | `test_flatpak_sync:TestAProbeThatDidNotAnswer::test_a_keyring_digest_read_exiting_non_zero_is_not_a_failure` |
| J92 | `dpkg --search` exits 1 because every queried path is genuinely unowned | Ordinary data — those paths become items | U | `test_manual_installs_sync:TestUnownedScan::test_a_batch_where_every_path_is_unowned_is_an_ordinary_answer` |
| J93 | The target's policy knows none of the source's names | Ordinary data; no answer was owed | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_target_policy_that_knows_none_of_the_source_names_is_data` |
| J94 | A machine holding nothing | Is never asked what it has installed | U | `test_apt_probe:TestAReadThatDidNotAnswer::test_a_machine_holding_nothing_is_never_asked_what_it_has_installed` |
| J95 | A dead read fails ONCE for the command, not once per item that depended on it | One failure line naming the command; no per-item reports | U | `test_machine_naming:TestProbeFailure::test_the_message_names_the_machine` (message shape); `test_job_failure_isolation:TestProbeFailedFailsOnlyItsOwnJob::test_the_failed_result_carries_the_command_that_did_not_answer` |
| J96 | The dead-read message | Names the machine by hostname, the command verbatim, the failing condition and the tool's own stderr — never a role | U | `test_machine_naming:TestProbeFailure::test_the_message_names_the_machine`; J63/J76/J78 assert command, condition and stderr |
| J97 | A dead read in one manager | Fails only that job; the other jobs still run | U | `test_job_failure_isolation:TestProbeFailedFailsOnlyItsOwnJob::test_the_orchestrator_records_it_failed_and_runs_the_next_job` |
| J98 | A request that is wrong rather than a tool that did not answer (a package the target's apt has never heard of) | Stays a per-item outcome and is never promoted to a job failure | U | `test_apt_probe:TestBareDebPackagesAreNotAptSyncsBusiness::test_a_name_an_answered_policy_printed_no_block_for_is_not_excluded`; `test_package_sync:TestAptSyncEndToEnd::test_a_package_the_targets_apt_cannot_locate_still_reaches_the_review` |

## J-G What the log records (article: PKG-FR-LOG-DECISIONS)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| J99 | A job presents three items answered apply / skip once / always skip | The log names all three with the decision each received | U | `test_package_sync_core:TestDecisionsReachTheLog::test_every_presented_item_is_named_with_its_decision` |
| J100 | An item the user skipped, which converges nothing and enters no report | It still produces a log line — the only record it was offered | U | `test_package_sync_core:TestDecisionsReachTheLog::test_a_skipped_item_leaves_a_line_where_nothing_else_would` |
| J101 | The words the log uses for a decision | The answer's own words ("skipped this run", "marked as this machine's own"), not the internal names | U | `test_package_sync_core:TestDecisionsReachTheLog::test_every_presented_item_is_named_with_its_decision` |
| J102 | A question asked outside the plan (a collateral question raised after the run's first change) | Its item and decision are named in the log too | U | `test_apt_collateral:TestCollateralForARepositoryThisRunWrites::test_the_decision_is_named_in_the_log` |
| J103 | A run with no terminal | Each unasked item is named in a warning; no count stands in for the record | U V | `test_package_review:TestNonInteractive::test_warns_naming_every_item_and_reports_groups`; `test_package_sync:TestPackageSyncWholeRunContracts::test_non_interactive_skip_all` |
| J104 | An approved install makes apt remove a package apt installed automatically | The removal is named in the log although nobody was asked | U | `test_apt_collateral:TestAutoCollateralIsLogged::test_auto_collateral_removal_is_named_in_the_log` |
| J105 | An approved install makes apt change an automatically-installed package's version | The log names both versions, without a second command to compare them | U | `test_apt_collateral:TestAutoCollateralIsLogged::test_an_auto_version_change_is_logged_without_a_version_comparison` |
| J106 | The same automatic collateral at apply time, from the transaction that actually happens | Logged there as well as at plan time | P | `Collateral.unapproved` calls `_log_auto`; the apply-time call is exercised by the apt converge tests but no test asserts the apply-time log line specifically |
| J107 | A manager makes a self-directed change other than apt's dependency resolution | Logged the same way | — | apt's collateral is the only case that exists today (ADR-021); nothing to test |

## J-H Verbatim manager output (article: PKG-FR-LOG-VERBATIM)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| J108 | Any command a job issues, read or write | The literal string handed to the shell is in the debug log before it runs | U | `test_step_gate:TestExecutorDebugTrace::test_read_and_write_are_both_traced` |
| J109 | A write | Its trace line carries the declared intent alongside the verbatim command | U | `test_step_gate:TestExecutorDebugTrace::test_read_and_write_are_both_traced` |
| J110 | A package manager prints to stdout and stderr | Both are recorded verbatim at debug, as separate records | U | `test_step_gate:TestExecutorDebugTrace::test_what_the_command_said_is_traced_too` |
| J111 | A command that printed nothing | Adds no output lines | U | `test_step_gate:TestExecutorDebugTrace::test_a_silent_command_adds_no_output_lines` |
| J112 | The user aborts at the confirmation prompt | The command was already traced — "what was I about to be asked" survives | U | `test_step_gate:TestExecutorDebugTrace::test_trace_is_written_before_the_gate_can_abort` |
| J113 | The trace's attribution | Each record carries the job and the machine it concerns | U | `test_step_gate:TestExecutorDebugTrace::test_trace_carries_job_and_host` |
| J114 | A login-shell-wrapped command | What is traced and prompted is byte-for-byte what the remote shell receives | U | `test_step_gate:TestExecutorGate::test_gate_sees_the_login_shell_wrapped_command` |
| J115 | The Ubuntu Pro attachment check's output, which names the subscriber | Only whether the target is attached may be logged | ‼ | `esm_gate` never logs the payload (`test_apt_esm_gate:TestTheESMAttachmentGate::test_the_probe_payload_is_never_logged`), but the executor traces every command's stdout verbatim (`executor._trace_output`, reached from `RemoteExecutor.run_command`), and `AptProbe.target_pro_attached` runs `pro status --format json` through it. See Gaps. |
| J116 | Verbatim output against a real package manager | The run's own log carries the manager's words | V | `test_package_sync:TestAptSyncEndToEnd::test_apt_repository_state_dry_run_previews_derived_writes_and_reviews_no_repository` (relies on the DEBUG trace of `sha256sum` output) |

## J-I The credential a URL carries (article: PKG-FR-CREDENTIAL-PRIVACY)

Five exits, then the `userinfo` grammar boundary.

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| J117 | A log line whose message text contains a credentialed URL | The userinfo is withheld | U | `test_redaction:TestCredentialRedactionFilter::test_the_message_is_redacted` |
| J118 | A package manager's own output reaching the log through a record argument | The userinfo is withheld | U | `test_redaction:TestCredentialRedactionFilter::test_a_credential_arriving_through_args_is_redacted` |
| J119 | A credentialed URL in a record's structured context (a command's stderr) | The userinfo is withheld | U | `test_redaction:TestCredentialRedactionFilter::test_structured_context_is_redacted` |
| J120 | The filter is actually on every route into the log | A line written through the configured logging stack reaches the file redacted | — | see Gaps: the filter is installed on both queue handlers in `logger.setup_logging`, but only the filter class is tested, never its installation |
| J121 | The per-command confirmation for a command carrying a credentialed URL | Neither the command nor the phrase shows the userinfo | U | `test_step_gate:TestExecutorDebugTrace::test_the_confirmation_prompt_withholds_a_url_credential` |
| J122 | A review line whose label or detail contains a credentialed URL | Shown redacted | P | `test_redaction:TestItemDiffText::test_every_string_the_user_reads_while_deciding_is_redacted` covers `ItemDiff`; `ReviewEntry` redacts label/detail/answer_hints identically but no test drives a credentialed label through a review screen |
| J123 | A repository file printed in full for a conflict decision, on both machines' panes | Neither pane shows the userinfo | U | `test_package_review:TestCredentialsInPrintedFileBodies::test_neither_version_of_a_conflicting_repository_shows_the_credential` |
| J124 | A pin file printed in full for a deletion decision | Shown redacted | U | `test_package_review:TestCredentialsInPrintedFileBodies::test_a_pin_file_offered_for_deletion_shows_no_credential` |
| J125 | The two snippet bodies the registry-overwrite question displays | Both shown redacted | U | `test_manual_installs_sync:TestSnippetRegistryOverwriteGuard::test_a_credential_in_a_snippet_body_is_withheld_from_the_question` |
| J126 | The snippet the tool stores and replays | Keeps its author's bytes exactly | U | `test_manual_installs_sync:TestSnippetRegistryOverwriteGuard::test_a_credential_in_a_snippet_body_is_withheld_from_the_question` (asserts the source file still holds the token) |
| J127 | The label a permanent decision keeps on disk | Written redacted | P | `test_redaction:TestItemDiffText::test_every_string_the_user_reads_while_deciding_is_redacted` proves `ItemDiff.label` is redacted; no test follows that label into the decision file |
| J128 | The item id a decision is keyed on | Left alone — rewriting it would make the decision unfindable | U | `test_redaction:TestItemDiffText::test_the_item_id_is_left_alone` |
| J129 | A failure naming the item, package or file it concerns | Not redacted away | U | J33 read with J128 |
| J130 | `https://bearer:TOKEN@host/...` — a token where a username belongs | The WHOLE userinfo goes, not just the part after the colon | U | `test_redaction:TestRedactCredentials::test_the_whole_userinfo_goes_not_only_the_password` |
| J131 | `https://token-only@host/...` — userinfo with no colon | Withheld | U | `test_redaction:TestRedactCredentials::test_the_whole_userinfo_goes_not_only_the_password` |
| J132 | A password containing each RFC 3986 sub-delimiter `! $ & ' ( ) * + , ; =` | No permitted character ends the withholding early — asserted per character | U | `test_redaction:TestRedactCredentials::test_every_character_rfc_3986_allows_in_a_userinfo_is_matched` |
| J133 | A password containing each unreserved punctuation character `- . _ ~` | Same | U | same test (the loop covers `-._~`) |
| J134 | A userinfo containing `:` | Same | U | same test |
| J135 | Unreserved alphanumerics in userinfo | Matched | U | `test_redaction:TestRedactCredentials::test_the_whole_userinfo_goes_not_only_the_password` |
| J136 | A percent-encoded userinfo (`us%40er:p%3Ass`) | Matched | U | `test_redaction:TestRedactCredentials::test_a_percent_encoded_userinfo_is_matched` |
| J137 | A userinfo of nothing but legal punctuation | Matched | U | `test_redaction:TestRedactCredentials::test_a_userinfo_of_nothing_but_legal_punctuation_is_matched` |
| J138 | Boundary: `/` is illegal in userinfo, so a shell command carrying a URL and an unrelated address later | Redacts nothing | U | `test_redaction:TestRedactCredentials::test_a_quoted_url_does_not_swallow_a_later_address` |
| J139 | Boundary: `?` ends the authority, so an `@` in a query string | Left alone | U | `test_redaction:TestRedactCredentials::test_an_at_sign_in_a_query_string_is_left_alone` |
| J140 | Boundary: an scp-style `user@host:path` (no `://`) | Left alone | U | `test_redaction:TestRedactCredentials::test_an_scp_style_target_is_untouched` |
| J141 | Boundary: `#` and whitespace inside a longer line | Only the URL's userinfo is rewritten; the rest of the line survives | P | `test_redaction:TestRedactCredentials::test_a_credential_inside_a_longer_line_redacts_only_the_url` covers whitespace/newlines; `#` has no test of its own |
| J142 | A URL with no credential | Untouched | U | `test_redaction:TestRedactCredentials::test_a_url_without_a_credential_is_untouched` |
| J143 | A string that passes two redaction exits | Not double-redacted | U | `test_redaction:TestRedactCredentials::test_it_is_idempotent` |

## J-J What a sync writes on the source, and what it never copies (articles: PKG-FR-SOURCE-INTENT, PKG-FR-MANAGER-CONVERGES)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| J144 | Planning | The target answers read-only questions only; no mutating command reaches it before the review | U V | `test_snap_sync:TestPlanReadOnly::test_plan_issues_no_mutating_snap_command`; `test_flatpak_sync:TestPlanReadOnly`; `test_package_sync:TestPackageSyncWholeRunContracts::test_each_manager_reviews_before_its_own_mutation` |
| J145 | A whole sync | What software Atlas has, and where it gets it from, is unchanged | P | `test_package_sync_core:TestIdempotency::test_identical_source_and_target_produce_no_diff_no_group_and_no_mutation` proves it for an identical pair only; no test asserts Atlas's own package state is unchanged after a converging run |
| J146 | Source write 1: a "always skip" answer about an item Atlas holds | A mark is recorded on Atlas and nowhere else | U | `test_package_state:TestPipelineWiring::test_skip_always_on_change_writes_to_source_not_target` |
| J147 | Source write 2: a snippet authored during the review | Written to Atlas's registry, not Nomad's | U | `test_package_sync_core:TestFinalizeUnreproducible::test_authored_snippet_is_written_to_the_source_registry_not_target` |
| J148 | Source write 3: the snap refresh pause | Applied on Atlas as well as Nomad, and restored afterwards | U | `test_snap_autorefresh_hold:TestConfirmEachCommandGate::test_apply_and_restore_declare_mutations_on_both_hosts` |
| J149 | No fourth source write exists | Every ungated executor call site in the codebase is accounted for as a read or a tracked gap; a new write fails the audit | P | `test_mutates_audit:TestMutatesCoverage::test_no_ungated_call_site_is_unaccounted_for` binds every call site, but classifies by read/write, never by machine — nothing pins the source-write count at three |
| J150 | No package manager's database, store or unpacked files travel between the machines | Only decisions plus the configuration a manager needs travel; the only file transfers a package job makes are repository/pin/config files, signing keys, remote filters and the snippet registry | — | no test states the prohibition; the nearest evidence is the enumerated `send_file` sites in `apt_sync/files.py`, `flatpak_sync.py` and `manual_installs_sync.py` |

## J-K The per-command confirmation (article: PKG-FR-CONFIRM-EACH)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| J151 | A converge command under `--confirm-each-command` | The user sees the verbatim command and must proceed or abort | U | `test_step_gate:TestExecutorGate::test_write_is_gated_with_the_verbatim_command` |
| J152 | The decision record, on the machine that holds the item — either machine | Gated, naming the item and the file | U | `test_step_gate:TestStateWritesReachTheGate::test_recording_a_decision_is_gated` (parametrised over both hosts) |
| J153 | Adding a snippet to the registry, on either machine | Gated, naming the item and the file | U | `test_step_gate:TestStateWritesReachTheGate::test_adding_a_snippet_is_gated` |
| J154 | Pushing the snippet registry to the target | Gated as a modification of the target, naming both paths | U | `test_step_gate:TestStateWritesReachTheGate::test_pushing_the_registry_to_the_target_is_gated`; `test_step_gate:TestExecutorGate::test_send_file_shows_both_paths_and_aborts_before_transfer` |
| J155 | Replaying a snippet on the target | Gated, naming the item | U | `test_package_state:TestSnippetRegistry` (asserts the replay call passes `mutates="replay install snippet for x"`) |
| J156 | The snap refresh pause and its restore, on both machines | Both gated; the read that captures the prior policy is not | U | `test_snap_autorefresh_hold:TestConfirmEachCommandGate::test_apply_and_restore_declare_mutations_on_both_hosts` |
| J157 | The restore's prompt | Names the prior value it is writing back, so skipping it is a visible loss | U | `test_snap_autorefresh_hold:TestConfirmEachCommandGate::test_restore_names_the_prior_value_it_is_writing_back` |
| J158 | Aborting at the restore | Is honoured rather than absorbed by the best-effort teardown, and still releases the lock and the connection | U | `test_snap_autorefresh_hold:TestConfirmEachCommandGate::test_abort_at_restore_is_not_swallowed_by_the_best_effort_handler`, `::test_cleanup_honours_the_abort_but_still_releases_resources` |
| J159 | A read | Never prompts — including reads of the decision file and the snippet registry | U | `test_step_gate:TestExecutorGate::test_read_is_never_gated`; `test_step_gate:TestStateWritesReachTheGate::test_reading_either_store_never_prompts` |
| J160 | The user answers "abort" | The command is not issued and the file is untouched | U | `test_step_gate:TestExecutorGate::test_abort_prevents_the_command`; `test_step_gate:TestStateWritesReachTheGate::test_aborting_leaves_the_file_untouched` |
| J161 | An in-process write that is neither a command nor a transfer | Gated through the same funnel | U | `test_step_gate:TestExecutorGate::test_declare_modification_gates_an_in_process_write` |
| J162 | No write a package job makes may bypass the gate | Every ungated executor call site is enumerated as a read or a tracked defect; the only tracked ungated write is `folder_sync`'s rsync pass (#209), which is not a package job | U | `test_mutates_audit:TestMutatesCoverage::test_no_ungated_call_site_is_unaccounted_for`, `::test_every_ungated_write_is_tracked`, `::test_the_audit_sees_the_executor_call_sites` |
| J163 | The prompt names the machine | By hostname, in the heading; never by role | U | `test_step_gate:TestTerminalUIStepGate::test_the_panel_names_the_machine_by_hostname`, `::test_the_abort_message_names_the_machine_by_hostname` |
| J164 | The prompt has no default | An accidental Enter re-prompts rather than choosing | P | `test_step_gate:TestTerminalUIStepGate` asserts proceed and abort; no test asserts `Prompt.ask` is called without a `default=` |
| J165 | The prompt cannot be answered (EOF / Ctrl-C) | Aborts the sync; never silently proceeds; the display is handed back | U | `test_step_gate:TestTerminalUIStepGate::test_unanswerable_prompt_aborts_never_proceeds` |
| J166 | `--confirm-each-command` on a run with no terminal | Refused before config is loaded or anything is connected, naming the flag | U | `test_commands:TestConfirmEachCommandFlag::test_refused_without_a_tty`, `::test_accepted_and_forwarded_on_a_tty`, `::test_a_non_interactive_run_without_the_flag_is_not_refused` |
| J167 | A command containing Rich markup characters (a snippet body, a bracketed filename) | Renders literally rather than raising mid-prompt | U | `test_step_gate:TestTerminalUIStepGate::test_command_with_markup_characters_does_not_raise` |
| J168 | The prompt's label | Names the job issuing the command | U | `test_step_gate:TestExecutorGate::test_active_job_labels_the_prompt` |

## Gaps

**J50 (P) — a dry run produces the same plan and the same review as a real run.** Two tests drive `execute()` under `dry_run=True` through a reviewer, so the review is provably reached; nothing asserts equality of plan/groups between a dry and a real run over the same fixture. Unit-testable: build one fixture, call `plan()` twice on two contexts differing only in `dry_run`, assert `plan.diffs` and `plan.groups` are equal, and assert the reviewer saw the same groups both times. Put it in `test_package_sync_core` on `FakeSyncJob` so it holds for every manager.

**J60 (—) — a dry run with no terminal reports skipped.** `sync_core.execute()` raises `JobSkipped` on `plan.groups and not outcome.was_interactive` with no reference to `dry_run`, so the behaviour is inferred, not asserted. Unit-testable: the existing `test_a_non_interactive_package_review_skips_the_job_instead_of_applying_nothing` with `make_context(dry_run=True, ...)`; assert `JobSkipped` and that the converge list is empty.

**J106 (P) — apply-time auto-collateral logging.** `Collateral.unapproved` calls `_log_auto` for the transaction that actually happens, and only the plan-time call has an assertion. Unit-testable in `test_apt_collateral`: approve an install whose apply-time simulation reports an auto-installed removal, and assert the same "installed automatically … not asked" line appears from the apply path (distinguish it from the plan-time line by the run phase, e.g. by clearing caplog after `plan()`).

**J115 (‼) — the Ubuntu Pro payload reaches the debug log.** `EsmGate` is careful: only the parsed boolean leaves it, and `test_the_probe_payload_is_never_logged` passes because the test's executor is a `MagicMock`. In a real run `AptProbe.target_pro_attached` issues `pro status --format json` through `RemoteExecutor.run_command`, which calls `_trace_output` and writes `stdout: {…}` at DEBUG — the subscriber's identity included. `PKG-FR-ESM-PRIVACY` says nothing else the attachment check learns may leave it, and `PKG-FR-LOG-VERBATIM` is explicitly subject to it. Needs a decision (a per-command opt-out from `_trace_output`, or parsing the boolean out of a command that prints nothing else) and then a test. Testable as a unit test against a real `RemoteExecutor` over a mocked asyncssh connection returning the payload, asserting the account name reaches no record.

**J120 (—) — the redaction filter's installation is untested.** `CredentialRedactionFilter` has three unit tests; nothing asserts it is attached to both `QueueHandler`s in `logger.setup_logging`, which is the claim that makes "every route into the log" true. Unit-testable: call `setup_logging` into a `tmp_path` log directory, emit a record carrying a credentialed URL through both `pcswitcher.*` and a third-party logger, flush the queue listener, and assert the file holds `***@` and not the token.

**J122 (P) — `ReviewEntry.label`/`detail`.** The printed-body tests cover `content` and `versions`; `ItemDiff` covers the decision-side strings. No test drives a credentialed label or detail through a review screen even though `ReviewEntry.__post_init__` redacts them. Unit-testable in `test_package_review`: an entry whose label and detail carry the URL, rendered through the non-interactive panel path, asserting the token is absent.

**J127 (P) — the label a recorded decision keeps on disk.** `ItemDiff` redaction is asserted on the dataclass; nothing follows it into `DecisionFile.record`'s written YAML. Unit-testable in `test_package_state`: a `SKIP_ALWAYS` diff whose label carries a credentialed URL, then assert the `mv --force` command's payload holds `***@`.

**J141 (P) — the `#` boundary.** `#` is illegal in userinfo and the regex class excludes it, but only `/`, whitespace and `?` have tests. One-line addition to `test_redaction:TestRedactCredentials`.

**J145 (P) — the source's own software is unchanged by a sync.** Only the identical-pair idempotency test speaks to it. A VM test is the honest form: capture `apt-mark showmanual`, `snap list --all` and `flatpak list` on the source before and after a converging run and assert equality. A unit test can only assert the absence of installing/removing commands on the source executor, which is worth having in `test_package_sync_core` but is weaker.

**J149 (P) — "exactly three" source writes.** The `mutates=` audit enumerates every call site but does not classify by machine, so a fourth source-side write would pass it. Unit-testable as an extension of `test_mutates_audit`: enumerate calls on a `self.source`/`source_run` receiver that pass `mutates=`, and assert the set of enclosing functions is exactly the three the article names. Fragile if a receiver is renamed — but the audit already accepts that trade for the read/write tables.

**J150 (—) — no package manager database, store or unpacked file is copied.** Nothing states it. Testable as a static audit beside `test_mutates_audit`: enumerate `send_file`/`get_file` call sites in `jobs/` and assert each remote path is under `/etc/apt`, a flatpak keyring/filter path, or the pc-switcher config directory — i.e. that no call names `/var/lib/dpkg`, `/var/lib/snapd` or `/var/lib/flatpak`.

**J164 (P) — the confirmation prompt has no default.** `TerminalUIStepGate` passes no `default=` and the comment says why; no test pins it. Unit-testable: patch `rich.prompt.Prompt.ask` and assert `"default"` is absent from its call kwargs.

**J107 (—) — a self-directed change by a manager other than apt.** No such case exists today; the row is here so a future one is not missed. Nothing to write.

## Notes for the assembler

- **Overlaps I did not claim.** The ESM gate's own mechanics (two answers, re-probe, privacy of the payload) belong to the apt area; I claim only its two consequences that are outcome-shaped — the skipped job (J10, J11) and the dry-run warning (J61) — plus the `PKG-FR-LOG-VERBATIM` conflict at J115. The `PKG-FR-COLLATERAL-AUTO` log lines (J104–J106) are apt's article; I carry them because `PKG-FR-LOG-DECISIONS`'s second clause is the general obligation they satisfy. Deduplicate against the apt sweep by keeping the row wherever the manager-specific detail lives and cross-referencing from the other.
- **`PKG-FR-DERIVED-VISIBLE` overlaps J53/J54.** The dry run's preview of derived writes is required by both that article and `PKG-FR-DRY-RUN`. I state it as the dry run's obligation; whoever holds `PKG-FR-DERIVED-VISIBLE` states the "logged as it lands" half.
- **Rows I merged.** The five credential exits in ADR-021 map onto J117–J127; I split the log exit into three rows (message, args, structured context) because they are three distinct failure modes of one filter, and added J120 for the wiring, which ADR-021 asserts and nothing tests.
- **Rows I split.** `PKG-FR-CREDENTIAL-PRIVACY`'s "no character that grammar permits may end the withholding early" is normative, so the sub-delimiter set, the unreserved punctuation, `:`, alphanumerics and percent-encoding are separate rows (J132–J137) even though one parametrised test covers several of them. The illegal-character boundaries (J138–J141) are the other side of the same rule and are separate because each is a different way the match could run away.
- **A behavioural reading worth confirming.** `execute()` skips a job whose review is non-empty and non-interactive, and a plan holding ONLY report-only findings counts as non-empty. So a headless run against two machines that differ only in package versions reports the job skipped rather than successful. That follows the letter of `PKG-FR-NO-TERMINAL` ("every package job with a non-empty review"), but it is worth a ruling: nothing was declined, because nothing was decidable. Not covered by any test either way.
- **`ConvergeItemDeclined`** (J5) exists as its own outcome — neither applied nor failed. It is only reachable through apt's late-collateral path today and is asserted only there. If another manager ever raises it, the base-pipeline behaviour (excluded from `PackageItemFailures`, named in its own summary line) has no test of its own.
- **Ambiguity.** `PKG-FR-OUTCOME-SUCCESS` says "the job did what those answers said". A run in which every approved item was withdrawn mid-apply by a later question therefore reports success while having applied nothing. That is consistent with `PKG-FR-COLLATERAL-MANUAL`'s "leaving the changes that cause the loss unapplied rather than failing later", but a reviewer running the tool by hand will see "success" for a job that changed nothing, and the report gives no other word for it.
