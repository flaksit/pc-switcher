# Independent review — codex, gpt-5.6-sol, effort high

Run 2026-07-31 against `9a3938aa` (1598 unit+contract tests green), read-only, no files modified.
Verdict as given: **the phase is not conformant and neither ADR is ready to leave Draft.**

Every finding below is codex's own text. Nothing here is verified yet — each one is a claim to
check against the code before acting, and a claim that may be wrong.

## Verdict

The criteria file does contain 129 normative article definitions, and its traceability-table counts add to 129. The stronger claim — "no orphans on either side" — is false. At least one narrative obligation is missing, several articles add obligations the narrative never states, and `PKG-FR-SOURCE-INTENT` directly contradicts both the narrative and other articles.

The implementation has multiple unregistered failures, including two credential-disclosure paths, incomplete package-job isolation, unsafe repository deletion, broken flatpak filter ordering, incomplete flatpak trust replication, snap refresh-policy mutation contrary to the requirement, and an apt held-package case that can remain unconverged indefinitely.

## Critical findings

### 1. The URL-userinfo regex leaks valid credentials containing an apostrophe

What is wrong: `redaction._URL_USERINFO` stops matching at an apostrophe. A valid URL such as `https://user:pa'ss@example.test/repo` is therefore not redacted at all.

Evidence:

- `redaction.py`, `_URL_USERINFO`
- The expression is `(?<=://)[^/\s@'\"<>]+@`; the excluded `'` prevents the match from reaching the terminating `@`.
- RFC 3986 permits sub-delimiters, including apostrophe, in `userinfo`.
- `tests/unit/test_redaction.py` covers ordinary username/password URLs but not this valid character.

Violation: narrative credential rule; `PKG-FR-CREDENTIAL-PRIVACY`; ADR-021's central decision.

Severity: Critical — credential disclosure to logs, review output, and command confirmations.

### 2. The snippet-registry overwrite question bypasses every advertised redaction exit

What is wrong: manual snippet-registry conflicts display both full snippet bodies through `Confirmer.confirm`. They are Rich-escaped but never credential-redacted and never wrapped in `ReviewEntry`.

Evidence:

- `manual_installs_sync.py`, `ManualInstallsSyncJob._render_overwrite_diff`
- The method applies `rich.markup.escape` to `Snippet.body`, which protects markup parsing only.
- `ManualInstallsSyncJob._guard_registry_overwrite` passes that result directly to `context.confirmer.confirm`.
- This path goes through neither `ReviewEntry.__post_init__`, `ItemDiff.__post_init__`, nor `Executor._announce`.
- A permitted opaque snippet such as `curl https://user:token@host/...` is shown verbatim.

Violation: narrative credential rule; `PKG-FR-CREDENTIAL-PRIVACY`; ADR-021's claim that every decision-time display passes through `ReviewEntry`.

Severity: Critical — direct terminal disclosure of a stored credential.

## High findings

### 3. Package-job failure isolation only covers two exception classes

What is wrong: one package job continues to the next only when it raises `PackageItemFailures` or `ProbeFailed`. Any other exception aborts the complete job loop.

Evidence:

- `orchestrator.py`, `Orchestrator._run_jobs_in_task_group`
- The specific exception arm records failure and continues; the following `except Exception` records failure and re-raises.
- Real package-job paths can raise other exceptions: registry transfer failures, filesystem errors, executor/connection errors, assertions, and parser defects.
- Isolation tests exercise only the named exception types; they would continue to pass while generic package failures still aborted later jobs.

Violation: narrative "one failed job does not stop the others"; `PKG-FR-JOB-INDEPENDENCE`; `PKG-FR-OUTCOME-FAILED`.

Severity: High — approved work in unrelated package managers can be discarded.

### 4. The accepted apt repository-deletion gap can strand installed software, and its justification is false

What is wrong: repository usage is calculated only over target-manual packages plus machine-specific marks. A repository used exclusively by an automatically installed package can be offered for deletion.

Evidence:

- `jobs/apt_sync/job.py`, `AptSyncJob._capture_apt_config` calls `AptProbe.packages_by_source_file` with `target_manual_set | marked`, not the complete installed set.
- The gap register says apt will remove such an automatic package "once that something goes".
- `jobs/apt_sync/commands.py`, `remove_args`, uses `apt-get remove --assume-yes`; it does not use `autoremove`.
- An automatic package may also remain required by a kept manual package. Deleting its only repository can strand it from updates or reinstallation.

Violation: narrative "a repository is not offered for deletion while anything uses it"; `PKG-FR-REPO-DELETE`.

Severity: High — potentially leaves installed packages dependent on deleted package sources.

### 5. Collateral consent is missing for repository-derived installs

What is wrong: installs the target cannot resolve until this run writes their repository are deliberately excluded from plan-time collateral analysis. The apply-time guard only refuses the item; it does not offer the mandated accept/keep/stop decision.

Evidence:

- `jobs/apt_sync/origins.py`, `OriginClassifier.target_resolvable`
- `jobs/apt_sync/collateral.py`, `Collateral.plan_time`
- `jobs/apt_sync/packages.py`, apply-time transaction guards
- ADR-020 explicitly admits that for these packages "the user is told afterwards rather than offered go-ahead / keep-the-package / stop-the-sync beforehand."
- `PKG-FR-ASK-AGAIN` expressly permits a later question when facts become knowable only after earlier writes.

Violation: `PKG-FR-COLLATERAL-MANUAL`; `PKG-FR-ASK-AGAIN`; narrative's three-way collateral decision.

Severity: High — the implementation replaces required informed consent with a late failure.

### 6. Flatpak filter ordering is not actually satisfied

What is wrong: `_apply_remote_filters` runs after application installs, but any old target filter remains active during those installs. A target’s existing filter can therefore block the application before the source filter is copied.

Evidence:

- `src/pcswitcher/jobs/flatpak_sync.py`, `FlatpakRemoteItem.filter_path`, `_write_derived_remote`, `_apply_remote_filters`
- `filter_path` has `compare=False`.
- `_write_derived_remote` neither clears nor temporarily disables the target filter.
- `_apply_remote_filters` runs later, after the potentially blocked install.
- `TestRemoteFilterReplicates.test_the_filter_lands_after_the_app_it_could_exclude` asserts command order only. Its fake target does not model a pre-existing active filter.

A related convergence defect exists when the source is unfiltered and the target is filtered: `_apply_remote_filters` skips source remotes whose `filter_path is None`, so the target-only filter is never removed.

Violation:

- `PKG-FR-FLATPAK-FILTER`
- `PKG-FR-MANAGER-CONVERGES`
- Narrative’s reason for applying filters after applications.

Severity: High—approved applications can fail or remain permanently filtered contrary to source state.

### 7. Verified Flatpak remotes using a machine-level trust anchor do not carry their trust

What is wrong: a verified source remote without a per-remote keyring is replicated with verification enabled but no signing key. The code assumes the target already has the same machine-level anchor.

Evidence:

- `src/pcswitcher/jobs/flatpak_sync.py`, `_parse_flatpak_remotes`, `_stage_source_key`, `_remote_trust_flags`
- `_parse_flatpak_remotes` assigns `key_digest=None` when trust comes from a machine-level anchor.
- `_stage_source_key` then returns `None`.
- `_remote_trust_flags` emits no `--gpg-import`.
- The job never captures or transfers `/usr/share/ostree/trusted.gpg.d`.
- `TestRemoteTrustTravelsWithTheDerivedWrite.test_verified_remote_without_a_key_of_its_own_adds_plainly` explicitly asserts this defective behavior.

Violation:

- Narrative: a remote’s signing key is synced byte-for-byte from the source.
- `PKG-FR-FLATPAK-REMOTE-TRUST`

Severity: High—the target can receive an unusable verified remote.

### 8. Snap refresh policy is modified even when the prior policy cannot be read

What is wrong: the criterion says an unreadable machine must be left untouched. The orchestrator still writes a timed hold and merely declines to clear it during cleanup.

Evidence:

- `src/pcswitcher/orchestrator.py`, `_hold_snap_autorefresh`, `_apply_snap_hold`, `_restore_snap_hold`
- `_hold_snap_autorefresh` calls `_apply_snap_hold` regardless of the captured `readable` flag.
- `_apply_snap_hold` also treats a failed set or failed verification as a warning and continues unpaused.
- `test_an_unreadable_hold_is_left_alone_rather_than_cleared` checks only that cleanup does not clear it; the test accepts the earlier overwrite.
- Other tests explicitly assert that failed application or verification never fails the sync.

Violation:

- `PKG-FR-SNAP-REFRESH-PAUSE`
- Narrative’s unconditional statement that auto-refresh is paused for the run.

Severity: High—can overwrite unknown user policy or run with the required race guard absent.

### 9. A stale target apt hold can permanently suppress the required exact-version install

What is wrong: package diffing suppresses every package-level item whenever the package name appears in the target hold set, even when the package is not installed.

Evidence:

- `src/pcswitcher/jobs/apt_sync/diffing.py`, `diff_apt_packages`
- The unconditional `if name in target_hold_names: continue` precedes missing-target install generation.
- [packages.py](src/pcswitcher/jobs/apt_sync/packages.py), `PackageConverger.hold`, documents that `apt-mark` can record a hold for a package that is not installed.
- If source and target both hold the name while only the source has the package, no install and no hold diff are produced. The target remains missing forever.
- If only the target carries the stale hold, the first run merely proposes unhold and does not install.
- `test_a_held_package_outside_the_targets_manual_set_is_still_not_proposed` codifies the broad suppression rather than testing installed-versus-absent state.

Violation:

- Narrative exact-version held-package rule.
- `PKG-FR-APT-HOLD-VERSION`
- Source-intent convergence.

Severity: High—persistent failure to install a source-held package.

### 10. A non-interactive empty plan can still transfer the snippet registry

What is wrong: the shared core raises `JobSkipped` only for a non-empty review. An empty manual-install plan proceeds through `after_review`, which can overwrite the target registry.

Evidence:

- `src/pcswitcher/jobs/packages/sync_core.py`, `PackageSyncJob.execute`
- Condition: `if plan.groups and not outcome.was_interactive`.
- `src/pcswitcher/jobs/manual_installs_sync.py`, `ManualInstallsSyncJob.after_review` and `_push_snippet_registry`
- A source registry can contain stale or previously authored entries even when the current scan produces no review items.
- `test_an_empty_plan_is_still_a_success` requires `after_review` to run but does not test manual-registry mutation.

Violation:

- `PKG-FR-NO-TERMINAL`: “no registry transferred.”
- Narrative’s non-interactive behavior.

Severity: High—an unattended run can perform a state transfer expressly forbidden for that mode.

## Medium findings

### 11. `PKG-FR-SOURCE-INTENT` contradicts the authoritative narrative

The article says a sync “MUST NOT modify the source.” The narrative requires source writes:

- machine-specific decisions are written on the holding machine, which may be the source;
- newly authored snippets persist in the source registry;
- snap auto-refresh is paused on both machines;
- `PKG-FR-CONFIRM-EACH` itself names decision records, snippet registry, and snap refresh pause as writes.

Evidence:

- `docs/planning/package-sync-conformance-criteria.md`, `PKG-FR-SOURCE-INTENT`
- `docs/planning/package-sync-user-requirements.md`, “Decisions and their memory” and “What happens during a sync”

Severity: Medium as a code defect, High as a specification defect. The article—not the source-writing code—needs correction.

### 12. The “129 articles, no orphans” assertion is semantically false

The numeric part is correct: there are 129 normative definitions before the gap register, and the table sums to 129. The semantic coverage claim fails.

Narrative requirement with no article:

- Flatpak remote deletion “takes its signing key with it.”
- `PKG-FR-FLATPAK-REMOTE-DELETE` requires remote deletion but omits the key.
- The code appears to satisfy the narrative through `flatpak remote-delete`; the article does not express it.

Articles that add obligations absent from the narrative include:

- `PKG-FR-OPT-IN`: “ship disabled”
- `PKG-FR-JOB-ORDER`: mandatory refusal to start
- `PKG-FR-SNAP-SCOPE` and `PKG-FR-SNAP-CONFINEMENT`: confinement convergence
- `PKG-FR-SUDO-PRECONDITION`: the exact per-job sudo matrix
- `PKG-FR-SNAP-DATA-BOUNDARY`: never-installed revision-directory exclusion
- `PKG-FR-FLATPAK-MASK`: report edits/moves and prohibit normalization
- `PKG-FR-SNIPPET-VERBATIM`: reject empty snippets; the traceability prose itself admits this was not in the narrative
- `PKG-FR-DRY-RUN`: terminal dry run must report success

`PKG-FR-APT-ORIGIN-VERIFY` also says “each approved install,” while the narrative flow routes distribution installs around the post-repository origin check. ADR-020 and the code exempt distribution origins.

Severity: Medium—traceability is structurally complete but not faithful.

### 13. ADR-020 contradicts itself and the criteria

Evidence in `docs/adr/adr-020-declarative-package-convergence.md`:

- D-07 first says `REPORT_ONLY` takes no answer.
- The same decision later says report-only diffs “offer apply or skip only.”
- The code follows the first rule and the narrative: report-only findings are printed without a question.
- Its summary describes pins as derived from approved packages, while D-36 and the narrative say every source pin always syncs.
- It exempts distribution origins from verification while `PKG-FR-APT-ORIGIN-VERIFY` literally exempts none.
- It correctly characterizes batching as a SHOULD/strong preference, while `PKG-FR-BATCHED` is written as an unconditional MUST, softened only indirectly by `PKG-FR-ASK-AGAIN`.

Severity: Medium. ADR-020 should remain Draft.

### 14. ADR-021’s “four exits” architecture claim is false

Evidence in `docs/adr/adr-021-what-the-log-records-and-withholds.md`:

- It claims all decision-time content passes through `ReviewEntry`.
- `ManualInstallsSyncJob._render_overwrite_diff` is a fifth exit and bypasses it.
- Its broader “wherever pc-switcher writes” wording also conflicts with exact opaque snippet storage under `PKG-FR-SNIPPET-VERBATIM` if a snippet contains a credential URL. The narrative is narrower: user-visible or logged exposure.

Severity: Medium, elevated by the Critical leak above. ADR-021 should remain Draft.

### 15. Collateral approval is keyed only by package name, not by consequence

What is wrong: the same protected package can appear as collateral in more than one transaction or direction, but all effects share `apt:collateral:<package>`.

Evidence:

- `src/pcswitcher/jobs/apt_sync/collateral.py`, `_item`, `_trigger_ids`, `resolve`, `unapproved`
- `_item` overwrites `_trigger_ids[diff.item_id]`.
- `resolve` stores only the collateral package name in `_approved`.
- Approval of one effect therefore exempts later removal, downgrade, or upgrade effects for that package, and the last duplicate overwrites attribution for earlier entries.

Violation:

- `PKG-FR-COLLATERAL-MANUAL`: consent to the consequence specifically.
- `PKG-FR-COLLATERAL-ATTRIBUTION`

Severity: Medium—less common transaction shape, but the consent model is structurally incapable of representing it correctly.

### 16. Manual-install discovery can turn a failed probe into a large false finding set

What is wrong: `_scan_unowned_installs` guards neither `find` nor `dpkg --search`.

Evidence:

- `src/pcswitcher/jobs/manual_installs_sync.py`, `_scan_unowned_installs`
- A failed `find`, whose stderr is redirected away, is treated as an empty scan.
- A total `dpkg --search` failure with empty stdout makes every candidate look unowned.
- The legitimate exit-1 case for some unowned paths explains why exit code alone is insufficient, but does not justify ignoring exit status and stderr entirely.

Violation:

- `PKG-FR-READ-FAILS-JOB`
- Narrative: silence must not be treated as an empty installed set.

Severity: Medium—can create misleading unresolved-install prompts for every `/opt` or `/usr/local` entry.

### 17. Outcome articles are internally ambiguous, and the code selects “success” for an all-skip interactive review

Evidence:

- `PKG-FR-OUTCOME-SUCCESS`: success when the job did what its review approved.
- `PKG-FR-OUTCOME-SKIPPED`: a job that deliberately did nothing must report skipped.
- An interactive review where the user skips every item satisfies both descriptions.
- `PackageSyncJob.execute` raises `JobSkipped` only for non-interactive review.
- `ManualInstallsSyncJob` has a test explicitly named `test_run_whose_only_items_were_skipped_once_passes`.

Severity: Medium—criteria need a precise distinction before code can be judged unambiguously.

### 18. Final failure reporting loses the failed item names

What is wrong: item failures are logged during the job, but the final session result reports only job names.

Evidence:

- `src/pcswitcher/orchestrator.py`, `_summarize_job_outcomes`
- Output is `Jobs reported failures: apt_sync`, not the failed item labels or reasons.
- `JobResult.error_message` holds detail, but the aggregate terminal/session summary discards it.

Violation:

- `PKG-FR-OUTCOME-FAILED`: failures reported together naming each item.
- `PKG-FR-FAIL-NAMED`

Severity: Medium—users receive a materially less actionable final result, even though earlier log lines may contain the names.

## Gap-register audit

| Registered survivor | Verdict |
|---|---|
| `PKG-FR-COLLATERAL-MANUAL` removal batch | The stated symptom is real. The justification is overstated: it does not establish that every removal run requires one simulation per candidate. A post-review simulation of the approved batch could cheaply identify clean runs; per-candidate work is needed only when attribution is required. The entry also omits the repository-derived-install failure. |
| `PKG-FR-REPO-DELETE` | Honestly admits nonconformance, but its acceptance rationale is unsafe and factually inconsistent with `remove_args`, which does not invoke autoremove. |
| `PKG-FR-FLATPAK-FILTER` | Incorrectly says ordering is met “by construction.” Only the new `--filter` command is late; an existing target filter remains active during install. |

Unlisted unmet or partially met articles include:

- `PKG-FR-JOB-INDEPENDENCE`
- `PKG-FR-MANAGER-CONVERGES`
- `PKG-FR-NO-TERMINAL`
- `PKG-FR-CREDENTIAL-PRIVACY`
- `PKG-FR-APT-HOLD-VERSION`
- `PKG-FR-COLLATERAL-ATTRIBUTION`
- `PKG-FR-SNAP-REFRESH-PAUSE`
- `PKG-FR-FLATPAK-REMOTE-TRUST`
- `PKG-FR-OUTCOME-FAILED`
- `PKG-FR-READ-FAILS-JOB`
- `PKG-FR-FAIL-NAMED`

`PKG-FR-SNAP-CONFINEMENT` is also unmet literally, although I consider the article itself an overreach beyond the narrative.

## Article-by-article disposition

This compact matrix covers all 129 articles through their traceability sections. “All others pass” means no contrary code path was found in static inspection; it is not a claim of live package-manager validation.

| Narrative section | Exceptions | Remaining articles |
|---|---|---|
| What package sync is for | `JOB-INDEPENDENCE` fail; `SNAP-SCOPE` partial/overreaching; `OPT-IN`, `JOB-ORDER` overreach | Pass |
| The model | `SOURCE-INTENT` contradicts narrative; `MANAGER-CONVERGES` partial; `BATCHED` normative drift | Pass |
| What happens during a sync | `NO-TERMINAL`, `CREDENTIAL-PRIVACY` fail; `DRY-RUN`, `SUDO-PRECONDITION` overreach | Pass |
| Decisions and memory | None found | Pass |
| apt / Installing | `APT-ORIGIN-VERIFY` disagrees with ADR/code on distribution exemption | Others pass |
| apt / Removing | None found | Pass |
| apt / Reporting | Target-held suppression underreaches the narrative’s drift-reporting intent | Others pass |
| apt / Holds | `APT-HELD-TARGET` conflicts with narrative reporting; `APT-HOLD-VERSION` fails for stale target hold | Others pass |
| apt / Collateral | `COLLATERAL-MANUAL`, `COLLATERAL-ATTRIBUTION` partial | Others pass |
| apt / Repositories | `REPO-DELETE` partial | Others pass |
| apt / Ubuntu Pro | None found | Pass |
| apt / Applying changes | None found | Pass |
| snap | `SNAP-CONFINEMENT` unmet/overreaching; `SNAP-REFRESH-PAUSE` fail; `SNAP-DATA-BOUNDARY` overreach | Others pass |
| flatpak | `REMOTE-TRUST`, `FILTER` fail; remote-deletion key is missing from the article but implemented | Others pass |
| Manual installs | Registry transfer violates cross-cutting `NO-TERMINAL`; discovery violates `READ-FAILS-JOB` | Section articles otherwise pass |
| When something goes wrong | `OUTCOME-SKIPPED` ambiguous; `OUTCOME-FAILED`, `READ-FAILS-JOB`, `FAIL-NAMED` partial/fail | `OUTCOME-SUCCESS` passes under the code’s chosen interpretation |
| Non-goals | No implementation contradiction found | Pass |

## Cleared suspicions

- Normal source-held apt installation is correctly exact-versioned: `_held_versions` captures the source version; `PackageConverger._install` uses `name=version`; the hold is applied after installation; unavailable versions fail without fallback. The defect is specifically the stale target-hold suppression path.
- Flatpak remote deletion itself is careful: `_delete_unused_remotes` re-reads target applications and runtimes after actual removals, includes machine-specific and origin-divergent refs, and `flatpak remote-delete` removes the per-remote keyring. The unsafe deletion finding belongs to apt, not Flatpak.
- The standard redaction exits are implemented competently for ordinary URLs: `CredentialRedactionFilter`, `Executor._announce`, `ItemDiff.__post_init__`, and `ReviewEntry.__post_init__` cover their intended normal routes. The failures are regex coverage and a route outside those exits.
- `PackageItemFailures` and `ProbeFailed` do continue to later jobs. The failure-isolation defect is the narrower exception allow-list.
- Ubuntu Pro privacy appears sound: the raw `pro status` payload remains inside the ESM gate and only the parsed attachment boolean escapes.
- Dry-run guards in the four package jobs generally prevent manager writes, snippet persistence, registry transfer, and snap refresh-pause writes.
- Report-only findings are not asked about in the implementation, which matches the narrative and the first half of ADR-020 D-07.

## Not examined or not live-verified

- I did not execute pytest, because even cache and temporary-file behavior would conflict with the requested read-only audit. This report is static.
- I did not run real apt, snap, or Flatpak transactions. Assertions described as “measured” in comments were not independently reproduced.
- I did not exhaustively inspect every rendered TUI string or every configuration-validation wording outside the package-specific paths.
- I did not validate packaging defaults from an installed wheel; default enablement was assessed from repository configuration only.
- I did not test every RFC-valid URL-userinfo character—apostrophe alone is sufficient to demonstrate the regex defect.
