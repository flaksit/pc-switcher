# Sweep — K: opting in, job independence and order, validation preconditions, the `folder_sync` boundary

Test evidence is `path/to/module:TestClass::test_name`, unit tests rooted at `tests/`, integration at `tests/integration/`.

## K.1 Opting in and the shipped default (articles: PKG-FR-OPT-IN)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| K1 | Run `pc-switcher init` on Atlas and read the config it created | `apt_sync` is present and off; no sync installs or removes an apt package until the user turns it on | — | `unit/orchestrator/test_config_system:TestShippedDefaultConfig::test_package_jobs_ship_disabled` asserts the other three only |
| K2 | Same, for `snap_sync` | present and off | U | `unit/orchestrator/test_config_system:TestShippedDefaultConfig::test_package_jobs_ship_disabled` |
| K3 | Same, for `flatpak_sync` | present and off | U | same test |
| K4 | Same, for `manual_installs_sync` | present and off | U | same test |
| K5 | The shipped `default-config.yaml` is loaded | It validates against the shipped schema and yields `folder_sync`/`vscode_state_sync` on | U | `unit/orchestrator/test_config_system:TestShippedDefaultConfig::test_shipped_default_config_loads` |
| K6 | Read the shipped config for a per-job settings section | No top-level `apt_sync`/`snap_sync`/`flatpak_sync`/`manual_installs_sync` section ships; each job's resolved config is empty | U | `…:TestShippedDefaultConfig::test_shipped_config_omits_empty_package_sections` |
| K7 | A hand-written config enables all four package jobs and writes no section for any of them | Loads without error; each job's config is empty | U | `…:TestShippedDefaultConfig::test_config_omitting_package_sections_validates` |
| K8 | A config with no `sync_jobs` block at all | No package job is instantiated; nothing is installed or removed | U | `unit/orchestrator/test_config_system:TestEmptyConfig::test_core_empty_config_file` (asserts the empty config loads; job discovery over the empty map is the inferred consequence) |
| K9 | Atlas's config enables `apt_sync` and nothing else; Nomad diverges by one apt package | The package converges on Nomad; no snap, flatpak or snippet work happens | V | `integration/jobs/test_package_sync:TestAptSyncEndToEnd::test_apt_sync_installs_missing_package` |
| K10 | Config enables `snap_sync` alone | The snap revision converges; nothing else runs | V | `integration/jobs/test_package_sync:TestPackageSyncWholeRunContracts::test_snap_revision_converges_without_hold` |
| K11 | Config enables `flatpak_sync` alone | The ref and its derived remote land; nothing else runs | V | `…:TestPackageSyncWholeRunContracts::test_flatpak_derives_the_remote_its_ref_needs_and_carries_its_key` |
| K12 | Config enables `manual_installs_sync` alone | The registry is pushed and the snippet replayed; nothing else runs | V | `integration/jobs/test_package_sync:TestManualInstallsSyncEndToEnd::test_manual_installs_sync_pushes_registry_and_replays_snippet` |
| K13 | A config names a job key that is not one of the shipped names | Load fails naming the unknown key | U | `unit/orchestrator/test_config_system:TestJobEnableDisable::test_core_edge_unknown_job_in_config` |
| K14 | A config turns one package job on and leaves the other three absent | Only the named job is discovered and run; the absent ones are never instantiated | P | K9–K12 each configure exactly one job, but none asserts that the other three produced no result and issued no command |

## K.2 Job independence (articles: PKG-FR-JOB-INDEPENDENCE)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| K15 | `apt_sync` on, `manual_installs_sync` off; Atlas holds a package installed from a hand-downloaded `.deb` | `apt_sync` produces no item, no review line and no install for it — the same as when `manual_installs_sync` is on | P | `unit/jobs/apt/test_apt_probe:TestBareDebPackagesAreNotAptSyncsBusiness::test_bare_deb_package_produces_no_diff_and_no_review_entry`, `::test_bare_deb_package_reaches_no_apt_get_install`; neither varies the sibling's enable flag (no package job reads it — verified by reading `src/pcswitcher/jobs/`) |
| K16 | `manual_installs_sync` on, every other package job off | It transfers the snippet registry itself, before any replay, without any other job running | U V | `unit/jobs/test_manual_installs_sync:TestSnippetPush::test_push_sends_source_registry_under_the_user_home_never_etc`; `integration/…:TestManualInstallsSyncEndToEnd::test_manual_installs_sync_pushes_registry_and_replays_snippet` |
| K17 | Two package jobs enabled; the first raises an unexpected exception mid-run | It is recorded failed with its reason; the second job still runs and reports its own outcome | U | `unit/orchestrator/test_job_failure_isolation:TestAnyFailureOfAPackageJobStaysInThatJob::test_a_generic_exception_does_not_stop_the_following_job`, `::test_the_failed_result_carries_what_went_wrong` |
| K18 | The first job's package manager cannot be queried at all | That job fails naming the command; the next job runs | U | `unit/orchestrator/test_job_failure_isolation:TestProbeFailedFailsOnlyItsOwnJob::test_the_orchestrator_records_it_failed_and_runs_the_next_job`, `::test_the_failed_result_carries_the_command_that_did_not_answer` |
| K19 | A package job surfaces a lock conflict | The run ends; no later job runs (the one failure that is not isolated) | U | `…:TestAnyFailureOfAPackageJobStaysInThatJob::test_a_lock_conflict_still_ends_the_run` |
| K20 | Two package jobs enabled, both machines diverged for both | Each manager settles its own review before that same manager changes Nomad; neither waits on the other's review | V | `integration/jobs/test_package_sync:TestPackageSyncWholeRunContracts::test_each_manager_reviews_before_its_own_mutation` |
| K21 | `apt_sync` enabled alone, with a `snap.decisions.yaml` present on the machine | The snap decision file is neither read nor rewritten | — | nothing asserts the per-manager isolation of the decision store from the enable flags |
| K22 | `snap_sync` disabled, other package jobs enabled | No snapd refresh pause is written on either machine | U | `unit/orchestrator/test_snap_autorefresh_hold:TestHoldEngaged::test_hold_not_set_when_snap_sync_disabled` (and `::test_hold_set_on_both_hosts_when_snap_sync_enabled` for the converse) |
| K23 | `folder_sync` enabled; `snap_sync`/`flatpak_sync` toggled | `folder_sync`'s own transfer changes: with them off it mirrors `~/snap/<app>/<rev>` and `~/.local/share/flatpak`, with them on it does not | ‼ | The behaviour is asserted (`unit/jobs/test_folder_sync:TestPackageJobExcludeFiltersGating::*`), but it is a job whose behaviour depends on whether another job is enabled — read literally, the article forbids it. Deliberate (D-29); see Notes |

## K.3 The four package jobs run before `folder_sync` (articles: PKG-FR-JOB-ORDER)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| K24 | Read the shipped config's `sync_jobs` key order | All four package jobs are listed before `folder_sync` | U | `unit/orchestrator/test_config_system:TestShippedDefaultConfig::test_package_jobs_precede_folder_sync` |
| K25 | A hand-edited config enables `flatpak_sync` on a line after `folder_sync: true` | One configuration error naming `flatpak_sync` and `folder_sync` | U | `…:TestPackageJobsBeforeFolderSyncStructuralCheck::test_package_job_after_folder_sync_yields_config_error` |
| K26 | Same, for `manual_installs_sync` | One error naming `manual_installs_sync` | U | `…::test_manual_installs_after_folder_sync_yields_a_config_error` |
| K27 | Same, for `apt_sync` | One error naming `apt_sync` | P | only reached inside `…::test_all_four_package_jobs_after_folder_sync_yield_four_errors`, which asserts the set of job names, not `apt_sync` alone |
| K28 | Same, for `snap_sync` | One error naming `snap_sync` | P | same as K27 |
| K29 | All four listed after `folder_sync` | Four errors, one per job | U | `…::test_all_four_package_jobs_after_folder_sync_yield_four_errors` |
| K30 | All four listed before `folder_sync` | No error; the run proceeds | U | `…::test_package_jobs_before_folder_sync_yields_no_error` |
| K31 | A package job listed after `folder_sync` but disabled | No error — only enabled jobs can race | U | `…::test_disabled_package_job_after_folder_sync_yields_no_error` |
| K32 | `folder_sync` disabled (or absent) with a package job listed after it | No error | U | `…::test_folder_sync_disabled_yields_no_error_regardless_of_order` |
| K33 | A misordered config is used for a real sync | The run refuses to start: it stops at job discovery, no job executes, nothing is changed on either machine | P | the errors are asserted; nothing asserts that discovery raises and that no job then executes |
| K34 | A correctly ordered config with several jobs enabled | Jobs execute one after another in the config's key order, package jobs first | P | `unit/orchestrator/test_skipped_jobs`/`test_job_failure_isolation` prove sequential execution of a given list; no test binds the executed order to the configured order |

## K.4 Job discovery (articles: PKG-FR-OPT-IN, PKG-FR-JOB-INDEPENDENCE)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| K35 | `apt_sync` enabled | Resolves to the apt job class | U | `unit/jobs/apt/test_apt_job:TestJobDiscovery::test_orchestrator_resolves_apt_sync_to_apt_sync_job` |
| K36 | `snap_sync` enabled | Resolves to the snap job class | U | `unit/jobs/test_snap_sync:TestJobDiscovery::test_orchestrator_resolves_snap_sync_to_snap_sync_job` |
| K37 | `flatpak_sync` enabled | Resolves to the flatpak job class | U | `unit/jobs/test_flatpak_sync:TestJobDiscovery::test_orchestrator_resolves_flatpak_sync_to_flatpak_sync_job` |
| K38 | `manual_installs_sync` enabled | Resolves to the manual-installs job class | U | `unit/jobs/test_manual_installs_sync:TestJobDiscovery::test_orchestrator_resolves_manual_installs_sync_to_its_job` |
| K39 | An enabled name resolves to no class | It is reported skipped with a reason; the run continues and the exit code is unaffected | U | `unit/orchestrator/test_skipped_jobs:TestUnresolvableEnabledJob::test_an_unresolvable_enabled_job_is_recorded_skipped` |
| K40 | A resolvable name | Leaves no skipped result behind | U | `…:TestUnresolvableEnabledJob::test_a_resolvable_job_leaves_no_skipped_result` |

## K.5 Validation preconditions (articles: PKG-FR-SUDO-PRECONDITION)

The table's four jobs × two machines. `apt_sync` and `snap_sync`: required on both. `flatpak_sync`: none on the source, target only where a system-scope item exists on either machine. `manual_installs_sync`: none on either.

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| K41 | `apt_sync` enabled; Atlas has no passwordless sudo | Validation fails saying so for Atlas; the run does not start and nothing is captured or written | U | `unit/jobs/apt/test_apt_job:TestValidate::test_source_without_passwordless_sudo_yields_validation_error` |
| K42 | `apt_sync` enabled; Nomad has no passwordless sudo | Validation fails saying so for Nomad, listing every binary the grant must permit | U | `…:TestValidate::test_target_without_passwordless_sudo_yields_validation_error_naming_the_binaries` |
| K43 | `apt_sync` enabled; `apt-mark` missing on Atlas | Validation fails naming Atlas | P | `…:TestValidate::test_apt_mark_unavailable_yields_validation_error` exercises the target only |
| K44 | `apt_sync` enabled; `apt-mark` missing on Nomad | Validation fails naming Nomad | U | `…:TestValidate::test_apt_mark_unavailable_yields_validation_error` |
| K45 | `apt_sync` enabled; both machines healthy, lock free | No validation error | U | `…:TestValidate::test_all_checks_pass_returns_no_errors` |
| K46 | `snap_sync` enabled; Atlas has no passwordless sudo | Validation fails naming Atlas (the refresh pause writes there too) | U | `unit/jobs/test_snap_sync:TestValidate::test_source_without_passwordless_sudo_yields_validation_error` |
| K47 | `snap_sync` enabled; Nomad has no passwordless sudo | Validation fails naming Nomad | U | `…:TestValidate::test_target_without_passwordless_sudo_yields_validation_error` |
| K48 | `snap_sync` enabled; `snap` missing on Atlas | Validation fails naming Atlas | U | `…:TestValidate::test_snap_unavailable_on_source_yields_validation_error` |
| K49 | `snap_sync` enabled; `snap` missing on Nomad | Validation fails naming Nomad | U | `…:TestValidate::test_snap_unavailable_on_target_yields_validation_error` |
| K50 | `snap_sync` enabled; both machines healthy | No validation error | U | `…:TestValidate::test_valid_environment_yields_no_errors` |
| K51 | `snap_sync` enabled; a machine already carries a standing refresh hold | Validation records it and does not fail | P | the read is logged, never appended to errors (`SnapSyncJob.validate`); `::test_valid_environment_yields_no_errors` passes with the default hold response but asserts nothing about the log line |
| K52 | `flatpak_sync` enabled; every item, remote and mask on both machines is user scope | Validation asks Nomad for no sudo at all and returns no error | U | `unit/jobs/test_flatpak_sync:TestValidate::test_user_scope_only_never_checks_sudo`; `…:TestMaskSystemScopeGate::test_user_scope_only_mask_never_checks_sudo` |
| K53 | `flatpak_sync` enabled; a system-scope application exists on Atlas; Nomad has no passwordless sudo | Validation fails naming Nomad and the system-scope operations that need it | U | `…:TestValidate::test_system_scope_item_present_without_sudo_yields_validation_error` |
| K54 | Same, but the system-scope application exists only on Nomad | Validation fails naming Nomad | — | no test drives the target-side branch of the scope gate |
| K55 | Same, but what exists is a system-scope remote (either machine) | Validation fails naming Nomad | — | no test drives the remote branches of the scope gate |
| K56 | Same, but what exists is a system-scope mask on Atlas | Validation fails naming Nomad | U | `…:TestMaskSystemScopeGate::test_system_scope_mask_requires_target_sudo` |
| K57 | Same, but the system-scope mask exists only on Nomad | Validation fails naming Nomad | — | target-side mask branch undriven |
| K58 | `flatpak_sync` enabled; `flatpak` missing on Atlas | Validation fails naming Atlas, without raising | U | `…:TestValidate::test_flatpak_unavailable_on_source_yields_validation_error` |
| K59 | `flatpak_sync` enabled; `flatpak` missing on Nomad | Validation fails naming Nomad and saying what to install there | U | `…:TestValidate::test_flatpak_unavailable_on_target_yields_validation_error_and_does_not_raise` |
| K60 | `flatpak_sync` enabled; `flatpak` missing on a machine | The scope gate is not evaluated and no sudo probe is issued | — | the code short-circuits on both version checks; nothing asserts it |
| K61 | `flatpak_sync` enabled; both machines healthy, no system-scope anything | No validation error | U | `…:TestValidate::test_valid_environment_with_no_system_scope_items_yields_no_errors` |
| K62 | `manual_installs_sync` enabled; neither machine grants passwordless sudo | Validation passes — this job requires none on either machine | P | `unit/jobs/test_manual_installs_sync:TestValidate::test_valid_environment_yields_no_errors` returns no error but does not assert that no sudo probe was issued to either machine |
| K63 | `manual_installs_sync` enabled; `apt-cache` missing on Atlas | Validation fails naming Atlas | U | `…:TestValidate::test_apt_cache_unavailable_on_source_yields_validation_error` |
| K64 | `manual_installs_sync` enabled; `dpkg` missing on Atlas | Validation fails naming Atlas | U | `…:TestValidate::test_dpkg_unavailable_on_source_yields_validation_error` |
| K65 | Any of the above failures | The message carries copy-paste remediation: the drop-in path, the `visudo --file` command, the grant line with the connecting account substituted, and a verification command | U | `unit/test_sudoers:TestPasswordlessSudoHint::test_names_every_required_binary`, `::test_uses_the_drop_in_not_etc_sudoers`, `::test_directs_the_user_through_visudo`, `::test_includes_a_verification_command`, `::test_substitutes_a_known_user_into_the_grant_line`, `::test_flags_the_placeholder_when_the_user_is_unknown`, `::test_says_a_broader_grant_is_acceptable` |
| K66 | Any of the above failures | The failing machine is identified in the reported error | U | every `TestValidate` case above asserts `e.host` |
| K67 | A precondition is missing | It is discovered in the validation step, never mid-execute: no job re-probes sudo or the dpkg lock while applying, and none degrades to a reduced capture | — | verified by reading `src/pcswitcher/` (the only `sudo --non-interactive true` and `fuser` call sites are the four `validate()` bodies); no test guards it |
| K68 | One enabled job fails validation while the others would pass | The whole run refuses to start before any job executes; nothing is changed on either machine | — | `unit/orchestrator/test_job_lifecycle:TestUS1AS5ValidationErrorsHaltSync::test_core_us_job_arch_as5_validation_errors_halt_sync` calls `validate()` directly and simulates the orchestrator; the raise in discovery is unasserted |

## K.6 The dpkg lock (articles: PKG-FR-APT-DPKG-LOCK)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| K69 | `apt_sync` enabled; unattended-upgrades holds Nomad's dpkg frontend lock | Validation fails saying the lock is held on Nomad and to retry once it finishes; the run does not start | U | `unit/jobs/apt/test_apt_job:TestValidate::test_dpkg_lock_held_yields_distinct_validation_error` |
| K70 | Same, but the lock is free | No lock error | U | `…:TestValidate::test_all_checks_pass_returns_no_errors` (the probe's non-zero exit is the "all clear") |
| K71 | The lock is held | The run neither waits nor retries: the refusal is immediate and states the reason | P | K69 asserts the message; nothing asserts the absence of a wait/retry (there is none in the code — single `fuser` probe, no loop) |
| K72 | A dry run while the lock is held | Still refuses to start — validation is not skipped for a dry run | — | `validate()` is called unconditionally by discovery; no test covers the dry-run branch |

## K.7 The boundary with `folder_sync` (articles: PKG-FR-DATA-BOUNDARY, PKG-FR-SNAP-DATA-BOUNDARY, PKG-FR-MACHINE-SPECIFIC)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| K73 | A sync of `/home` with every package job disabled | The machine-local decision files (`~/.config/pc-switcher/*.decisions.yaml`) are still excluded from the mirror — a machine's own "always skip" list never reaches the other machine | U | `unit/jobs/test_folder_sync:TestDecisionFileExcludeFilters::test_unconditional_regardless_of_which_folder_is_synced`, `::test_home_under_synced_folder_anchors_the_glob_under_user_subdir` |
| K74 | A sync of `/root` by a user whose home is `/home/alice` | Nothing is excluded on that folder's behalf; the exclusion follows the invoking user's home | U | `…:TestDecisionFileExcludeFilters::test_glob_outside_synced_folder_is_skipped`, `::test_root_invoker_excludes_under_root` |
| K75 | The decision-file exclusion versus the user's own filter file | It is emitted before the central filter is merged, so no user rule is consulted first | U | `…:TestDecisionFileExcludeFilters::test_decision_file_exclude_precedes_merge_filter` |
| K76 | The user writes a `+` rule for the decision file in their own filter | The file stays excluded — the rule cannot re-expose it | U | `…:TestDecisionFileExcludeFilters::test_user_plus_rule_for_decision_file_does_not_change_command_ordering` |
| K77 | `snap_sync` enabled; Atlas keeps an older retained `~/snap/<app>/<rev>` beside the current one | The retained older revision's data directory is not mirrored; the current revision's is | U | `unit/jobs/test_snap_sync:TestExcludePaths::test_excludes_old_revisions_keeps_current_common_and_current_symlink`; `unit/jobs/test_folder_sync:TestSnapSyncExcludeFilters::test_old_revision_excluded_current_kept`; `…:TestPackageJobExcludeFiltersGating::test_snap_sync_enabled_includes_revision_exclusion` |
| K78 | `~/snap/<app>/current` is missing or dangling | Every revision directory of that app is excluded | U | `unit/jobs/test_snap_sync:TestExcludePaths::test_dangling_current_falls_back_to_excluding_all_revisions`, `::test_missing_current_symlink_falls_back_to_excluding_all_revisions` |
| K79 | No `~/snap` directory at all | Nothing is excluded on snap's behalf | U | `unit/jobs/test_snap_sync:TestExcludePaths::test_no_snap_directory_returns_empty`; `unit/jobs/test_folder_sync:TestSnapSyncExcludeFilters::test_no_snap_directory_yields_no_filters` |
| K80 | `~/snap/<app>/common` and the `current` symlink | Always mirrored — never excluded | U | `unit/jobs/test_snap_sync:TestExcludePaths::test_excludes_old_revisions_keeps_current_common_and_current_symlink` |
| K81 | `snap_sync` disabled, `folder_sync` enabled; Atlas and Nomad are on different revisions of an app | Data directories for revisions Nomad's snapd never installed do not land on Nomad | ‼ | `unit/jobs/test_folder_sync:TestPackageJobExcludeFiltersGating::test_snap_sync_disabled_excludes_nothing` asserts the opposite behaviour: with `snap_sync` off, every revision directory is mirrored |
| K82 | `snap_sync` enabled but the user skips the revision change for one app (or it fails) | The current-revision data directory of an app whose revision did not converge does not land on Nomad | ‼ | the exclude set is computed from Atlas's filesystem alone, before and independently of the review's outcome (`snap_sync_exclude_paths`, called only from `folder_sync`); nothing narrows it after the review |
| K83 | `flatpak_sync` enabled | `~/.local/share/flatpak` is excluded from the mirror; `~/.var/app` is not | U | `unit/jobs/test_flatpak_sync:TestExcludePaths::test_returns_flatpak_data_dir_excludes_var_app`; `unit/jobs/test_folder_sync:TestFlatpakSyncExcludeFilters::test_flatpak_data_dir_included_var_app_never_mentioned`, `…:TestPackageJobExcludeFiltersGating::test_flatpak_sync_enabled_includes_data_dir_exclusion_not_var_app` |
| K84 | `flatpak_sync` disabled | `~/.local/share/flatpak` is mirrored like any other data | U | `…:TestPackageJobExcludeFiltersGating::test_flatpak_sync_disabled_excludes_nothing` |
| K85 | A `/root` sync while the flatpak store is under `/home/alice` | Nothing is excluded on flatpak's behalf | U | `unit/jobs/test_folder_sync:TestFlatpakSyncExcludeFilters::test_flatpak_data_dir_outside_synced_folder_is_skipped`; `…:TestSnapSyncExcludeFilters::test_revision_dir_outside_synced_folder_is_skipped` for the snap equivalent |
| K86 | The package exclusions versus the user's own filter file | Both are emitted before the central merge, so no user rule can re-expose them | U | `…:TestPackageJobExcludeFiltersGating::test_both_package_exclusions_precede_merge_filter` |
| K87 | No sibling-enablement information is available to `folder_sync` | Neither package exclusion is emitted and the run does not fail | U | `…:TestPackageJobExcludeFiltersGating::test_missing_enabled_sync_jobs_omits_both_exclusions_without_raising` |
| K88 | `manual_installs_sync` skipped (no terminal) or declined, `folder_sync` enabled | Nomad's snippet registry is not overwritten by this run | ‼ | `~/.config/pc-switcher/package-snippets.yaml` is matched by no exclusion (the decision glob is `*.decisions.yaml`, and neither shipped filter file mentions it), so `folder_sync` mirrors it regardless — bypassing `PKG-FR-REGISTRY-CONSENT` and the "no registry transferred" half of `PKG-FR-OUTCOME-SKIPPED`/`PKG-FR-NO-TERMINAL` |
| K89 | `apt_sync` enabled with `folder_sync` | No `folder_sync` exclusion is needed for apt: everything `apt_sync` writes lives outside the synced folders (`/etc/apt`, the dpkg database) | — | nothing asserts it; it is a claim about the absence of apt paths under `/home` and `/root` |
| K90 | A run of `apt_sync` | It transfers only repository files, pins and keyring bytes — no application data | — | verified by reading `apt_sync/files.py` (its only `send_file`); no test states the boundary |
| K91 | A run of `flatpak_sync` | It transfers only signing keys and remote filter files — never `~/.var/app` or the flatpak store | P | `unit/jobs/test_flatpak_sync:TestExcludePaths::test_returns_flatpak_data_dir_excludes_var_app` covers the exclusion side only |
| K92 | A run of `snap_sync` | It transfers no file between machines at all — convergence is `snap install/refresh/remove` on Nomad | — | no `send_file`/`get_file` exists in `snap_sync.py` (read); unasserted |
| K93 | A run of `manual_installs_sync` | The only file it transfers is the snippet registry | P | `unit/jobs/test_manual_installs_sync:TestSnippetPush::test_push_sends_source_registry_under_the_user_home_never_etc` asserts what it sends, not that it sends nothing else |

## Gaps

- **K1** — `apt_sync` shipping disabled is asserted nowhere. One line in `unit/orchestrator/test_config_system:TestShippedDefaultConfig::test_package_jobs_ship_disabled`. Unit, trivial.
- **K8** — no test drives job discovery over a config with no `sync_jobs` block. Unit: build an `Orchestrator` with `sync_jobs = {}` and assert `_discover_and_validate_jobs()` returns no jobs and no unresolved results.
- **K14** — none of K9–K12 asserts the three unconfigured jobs produced nothing. Cheapest as a unit test over discovery (one enabled name in, one job class out); the integration runs would need a per-manager state comparison they already have machinery for (`_MachinePackageState`).
- **K21** — enabling one manager must not touch another's decision file. Unit: run `apt_sync` with a populated `snap.decisions.yaml` on disk and assert the file is neither read nor written.
- **K23** — see Notes; a requirements decision, not a test gap.
- **K27, K28** — `apt_sync` and `snap_sync` each need their own single-job misordering case, mirroring the existing `flatpak_sync` and `manual_installs_sync` ones. Unit, trivial.
- **K33** — nothing asserts the refusal itself. Unit: a misordered config through `_discover_and_validate_jobs()` must raise, and no job's `execute()` may have been called.
- **K34** — bind executed order to configured order. Unit: two stub jobs, config order reversed, assert the run order follows the config.
- **K43** — the `apt-mark`-missing-on-the-source branch. Unit, one line off the existing target-side test.
- **K51** — a pre-existing refresh hold must be recorded and must not fail validation. Unit with `caplog`.
- **K54, K55, K57** — the scope gate has six inputs (item / remote / mask × source / target) and two are driven. Unit: four more `make_context` cases against `FlatpakSyncJob.validate()`, each asserting the target sudo probe fires.
- **K60** — with `flatpak` absent, no sudo probe may be issued. Unit: assert no `sudo --non-interactive true` in the target's calls.
- **K62** — `manual_installs_sync` must probe sudo on neither machine. Unit: same `not any("sudo --non-interactive true" in c …)` assertion the flatpak user-scope test already uses.
- **K67** — the project rule that every environment assumption is checked in `validate()`. Best as a static audit in the shape of `unit/test_mutates_audit.py`: no `sudo --non-interactive`/`fuser /var/lib/dpkg/lock-frontend` call site outside a `validate()` body.
- **K68** — a validation error must stop the run before any job executes. Unit against `_discover_and_validate_jobs()` with a stub job returning one `ValidationError`; assert it raises and no `execute()` ran.
- **K71** — no wait or retry on the dpkg lock. Unit: assert exactly one `fuser` call and no sleep in the validate path.
- **K72** — dry run + held lock. Unit: `dry_run=True` context, lock held, assert the same validation error.
- **K81** — with `snap_sync` off, `folder_sync` mirrors every revision directory. This is deliberate (module comment above `_RSYNC_SUDO_COMMANDS`: excluding them with no job managing them "would strand that data unmirrored"), and it contradicts `PKG-FR-SNAP-DATA-BOUNDARY` read literally. Needs a requirements ruling before a test; if the current behaviour stands, the article needs the condition "where `snap_sync` is enabled" written into it.
- **K82** — a declined or failed revision convergence still gets its current-revision data mirrored. Real hole in the boundary, not a wording problem: the exclude set is computed before the review and never narrowed by it. Closing it means `folder_sync` asking `snap_sync` which apps actually converged, which is a cross-job dependency the current design avoids. A VM test can show it (diverge a snap revision, answer skip, check whether `~/snap/<app>/<source-rev>` appeared on the target); a unit test can only show the exclude set ignores decisions.
- **K88** — `folder_sync` mirrors `~/.config/pc-switcher/package-snippets.yaml`. Either add it to the non-overridable exclusions (and let `manual_installs_sync` remain the only transporter, which is what D-23 and the consent gate assume), or state the overlap deliberately. Unit test once decided: assert the registry relpath appears in the built rsync command's exclusions. A VM test would show the live consequence: a non-interactive run that reports `manual_installs_sync` skipped still ends with the target's registry replaced.
- **K89, K90, K92** — the "no package job syncs application data" prohibition is asserted for flatpak's store only. A static audit is the reliable form: every `send_file`/`get_file` call site in `jobs/apt_sync/`, `jobs/snap_sync.py`, `jobs/flatpak_sync.py`, `jobs/manual_installs_sync.py` must target a path in an allowed set (`/etc/apt/**`, the keyring/filter staging dir, the snippet registry). Mirrors the existing `unit/test_mutates_audit.py` approach.
- **K91, K93** — upgrade the existing assertions from "sends this" to "sends only this" (assert the full `send_file` call list).

## Notes for the assembler

- **K23 is a genuine requirements conflict, not a bug to file.** `PKG-FR-JOB-INDEPENDENCE` says no job's behaviour may depend on whether another is enabled. `folder_sync`'s package-store exclusions are gated on `snap_sync`/`flatpak_sync` by design (D-29), and the gating is what `PKG-FR-SNAP-DATA-BOUNDARY` is trying to buy. Two readings: the article's "each job" means each package job (in which case `folder_sync` is out of scope and nothing is wrong), or it means every job (in which case the code is non-conformant and always will be). The article's own section is `What package sync is for`, which favours the narrower reading — but the sentence does not say so. It needs one clause either way. K81 is the same conflict seen from the other article.
- **Machine naming in validation failures.** `PKG-FR-SUDO-PRECONDITION` says "naming the machine that lacks it"; `PKG-FR-NAME-THE-MACHINES` explicitly exempts validation failures from the hostname rule. The shipped errors carry a `host` role field and read "on source"/"on target", and the orchestrator prints `apt_sync (source): …`. I read that as conformant. If the sweep's owner disagrees, K41–K49 and K63–K64 all become findings at once — flagging rather than deciding.
- **Rows that overlap other areas.** K88 (registry transfer) touches area F's `PKG-FR-REGISTRY-CONSENT` and area J's `PKG-FR-OUTCOME-SKIPPED`/`PKG-FR-NO-TERMINAL`; I kept it here because the mechanism is a `folder_sync` exclusion that does not exist. K77–K82 are the `folder_sync` half of `PKG-FR-SNAP-DATA-BOUNDARY`; snap's own half (revision convergence) is area E's. K17–K19 restate `PKG-FR-OUTCOME-FAILED`'s isolation clause from the independence side — area J may hold the same rows from the outcome side; merge or cross-reference.
- **`PKG-NG-AUTOMATION-ENV`'s "must stay out of the configuration schema"** is a fact about the shipped schema, which is my section K.1's subject matter, but the article is not in my list and I have not enumerated it. Whoever holds it should know the schema's `sync_jobs` block is `additionalProperties: false` with no automation key, and `config-schema.yaml`'s root is likewise closed.
- **Integration coverage of the boundary is zero.** No integration test enables a package job and `folder_sync` in the same run — `_package_sync_test_config` never writes a `folder_sync` entry, and `test_end_to_end_sync.py` never enables a package job. Every row in K.7 is unit-only, including the two ‼ rows, whose consequences are only observable on real machines.
