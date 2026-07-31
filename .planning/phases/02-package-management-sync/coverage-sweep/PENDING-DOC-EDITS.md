# Matrix edits owed, from phase-2 agent reports

Applied centrally once every phase-2 agent has finished. Agents do not edit the matrix.

## From A/B

- A3, B7, B14, B18, B25, B29, B33, B35 — `—` becomes `U`, with the new test symbols in the agent's report.
- A53, A58, B24 — `P` becomes `U`; the existing tests were strengthened.
- A40 — applied already: now `‼`, with a findings entry. Remove its gap-register bullet.
- A36, A60 — belong to `test_apt_origins.py`; the C/D agent owns that module. Check its report.

## From E (snap)

- E4, E5, E9, E12, E14, E16, E19, E20, E29, E34, E41, E43, E45, E52, E99, E114 — `—` becomes `U`.
- **E6 cites the wrong symbol.** The row credits `TestSnapItem::test_reports_its_item_class` with asserting `item_id == "snap:firefox"`; that assertion is in `test_label_names_the_snap_channel_and_revision`. Repoint.
- **E12, E14** — their old `Test` entries (`TestSideloadedSnaps::test_store_snaps_in_the_same_listing_still_diff_and_converge`, `TestHoldAndRevisionFailuresArePerItem::test_unfetchable_revision_is_a_clean_per_item_failure_not_a_crash`) are obsolete; point them at the new `TestConvergeRevisionAndChannel` tests.
- **E114 has no gap-register bullet** though its `Cov` was `—`. Nothing to remove; note it was closed anyway.
- E68–E70 (`test_block_state_decisions.py`) and E109–E112 (`test_folder_sync.py`) still need their E ids added by those modules' owners.

## From K (preconditions)

- K1, K2, K3, K4, K8, K14, K27, K28, K33, K34, K67, K68 — `—` becomes `U`.
- **K67's wording is off by one.** It says the probes' only call sites are "the four `validate()` bodies". Measured by the audit that now guards it: three `validate()` bodies (`AptSyncJob`, `SnapSyncJob`, `FlatpakSyncJob`) plus `sudoers.passwordless_sudo_hint`, which is remediation text the user runs rather than a probe. `ManualInstallsSyncJob` issues neither, and `FolderSyncJob` probes with `sudo rsync --version`. Fix the count.
- **G92 was closed by the K agent**, in `test_first_sync_scope.py` — the only module it can live in. Move its gap-register bullet's ownership note accordingly.
- K21, K43, K51, K54, K55, K57, K60, K62, K71, K72, K91, K93 — each job's own `validate()` tests; left to that manager's agent. Check their reports before leaving these open.
- K89, K90, K92 (and J150, the same audit) — a static `send_file`/`get_file` path audit, deliberately not forked into a module the K agent did not own.

## From G (manual installs)

- G6, G35, G36, G45, G47, G52, G54, G55, G60, G78, G80, G83, G84, G90, G92, G93, G94 — `—` becomes `U`.
- **G55's citation moves** from `test_package_state:TestPipelineWiring::test_no_record_call_when_dry_run` to the job-specific `test_manual_installs_sync:TestPermanentMarkWrites::test_a_rehearsal_records_no_permanent_mark`.
- **G46 gains a citation**: the new `TestNoTerminalRun::test_a_run_with_no_terminal_and_findings_skips_before_touching_the_target` asserts the run's own outcome, which is what the row said no test did. Re-check the row's `P` once that is in.
- **G92 is closed twice** — once here and once by the K agent in `test_first_sync_scope.py`. Keep the job-specific one and cite both, or drop one; do not leave the row citing neither.
- G61 (`test_package_review`) and G74's orchestrator half (two stub jobs) are outside this agent's modules; check the H and J reports.
- G31–G33, G38–G42, G48 — tests in `test_package_review`; the H agent owns the tagging.

## From J (outcomes, log, privacy)

- J120, J141, J149, J150, J164 — `—` becomes `U`. J149 and J150 are two new static audits, each verified to fail when the behaviour it guards is removed.
- **J150 largely satisfies K89, K90 and K92**, and the "sends only this" half of K91 and K93 for flatpak and `manual_installs_sync`. Re-check those rows rather than leaving them open.
- J122 and J127 are tagged with their remaining gap stated in the test's own docstring; the tests that would close them belong to the H agent (`test_package_review`) and the G agent (`test_package_state`).
- J107 and J18 need no test: J107's case does not arise under ADR-021, and J18 is J3 and J9 read together. Say so in the register rather than leaving them as gaps.
- **A new question for the requirements**: `Orchestrator._update_sync_history` writes `~/.local/share/pc-switcher/sync-history.json` on Atlas every run. That is a fourth source-side write in the literal sense. The audit classifies it as outside `PKG-FR-SOURCE-INTENT`'s subject — the tool's own record, not software, and not made by a package job — and the article does not say whether that reading is right. Add it to *Questions the requirements do not settle*.

## From H (review and consent)

- H23, H69, H79, H93, H96, H101, H109, H110, H113, H151, H152, H157, H163, H171, H173 — `—` becomes `U`.
- **H165's citation was proving less than the row claimed.** The automation test mapped both item ids, so "an item absent from the map is declined" was indistinguishable from "a mapped decline is honoured". The map now omits one id and the row is genuinely covered.
- **H35's alternative in the register is hollow** — `review_items` is handed no executor, so it cannot issue a command between screens; the risk is only observable at job level. Move the row's home to `apt/test_apt_job:TestOneReviewPerRun`.
- **H80's exemption is genuinely exercised**: `apt_sync/job.py`'s validation messages use the role words, which is what `PKG-FR-NAME-THE-MACHINES` exempts. The row stands; its test belongs in the manager's own module.
- **H93 records a grammar mismatch, deliberately unfixed**: the repository-conflict screen's act hint is declarative with the machine as subject while every other screen's is imperative with it as object. The set rule holds and is now asserted; the one-grammar rule is not. Keep this in *Questions the requirements do not settle*.
- H133 is covered by construction; say so in the register rather than leaving it a gap.

## From F (flatpak)

- F60, F63, F66, F84, F97, F98, F111, F112, F116, F120, F136, F139, F143, F144, F145 — `—` becomes `U`. F82, F87, F110, F138 — `P` becomes `U`.
- **F98's Scenario is unreachable as worded.** `plan()` captures the source remotes once and the write reads only that snapshot, so there is no second call to answer differently. The branch is reached when an application's `origin` names a remote Atlas's own `flatpak remotes` does not list. Reword the Scenario; the Expected column is already right.
- F44, F118, F121 stay open pending rulings; F72 and F147's remaining half are VM.
- `FakeFlatpakTarget` needed no extension.

## Cross-section tagging still owed

An agent could only tag tests in the modules it owned, so rows whose test lives in another section's module carry no id yet. Known: E68–E70, F130, F131 and H-section rows in `test_block_state_decisions.py`; E109–E112, F4 and K-section rows in `test_folder_sync.py`; J54, J78, J79, J80, J91, J144, K37, K52, K83, K91, N19, N20 in `test_flatpak_sync.py`; G31–G33, G38–G42, G48, G61 in `test_package_review.py`. One pass over the suite closes them all — match every row's cited symbol against the docstring that symbol carries.

## Housekeeping

- `flatpak_tag_f.py` appeared in the repository root during the flatpak agent's run — a working script, not a deliverable. Delete it before committing.
- `test_apt_etc_apt.py` referenced an undefined `METADATA_REFRESH_ITEM_ID` mid-run. Confirm the C/D agent resolved it; the suite and `basedpyright` must be clean before the commit.
