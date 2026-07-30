# Package sync divergences


Two seeds from the planning stage were checked and did **not** hold up: dry run does show and answer the review, which is what `PKG-FR-DRY-RUN` asks for; and `--confirm-each-command` does cover the decision records, the snippet registry and the refresh pause, as `PKG-FR-CONFIRM-EACH` requires. Neither is a divergence.

## DIV-01 — A failed probe ends the whole run, contradicting the stated failure isolation

`PKG-FR-OUTCOME-FAILED` requires that "one failed job MUST NOT stop the others". The orchestrator honours that for `PackageItemFailures` only: that arm records FAILED and continues (`orchestrator.py:1292-1314`). `ProbeFailed` is a plain `RuntimeError` (`probes.py:37`), so it falls through to `except Exception`, which records FAILED and then re-raises (`orchestrator.py:1315-1335`), ending the run.

Whether this is wrong depends on which reading is intended. `probes.py:42-48` argues a probe failure means the machine or the tool is broken, which is not a finding about any item and arguably *should* stop everything. `PKG-FR-OUTCOME-FAILED` does not distinguish the two. **Ruling needed:** does a dead package-manager read fail its own job and let the others run, or stop the sync?

**Closed 2026-07-30 (U3).** Ruled: it fails its own job only. `ProbeFailed` shares the orchestrator's non-aborting arm with `PackageItemFailures`, so the run records that job FAILED and continues. ADR-022 D-06 carries the reason; issue #220 stays open for every other job-level exception, which still aborts.

## DIV-02 — The automation escape hatch writes permanent state and is documented nowhere

`PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` takes a JSON `item_id → decision` map and bypasses every prompt, returning `was_interactive=True` (`review.py:580-582`). Because the permanence guards test exactly that flag (`sync_core.py:454`, `manual_installs_sync.py:583`), this path **writes machine-specific marks and install snippets** — the two things `PKG-FR-SKIP-ONCE` and `PKG-FR-MACHINE-SPECIFIC` describe as coming from an explicit human choice.

It is intended for integration tests that have no TTY. But an environment variable is not a test-only mechanism: anything that can set it on a real run gets silent, unreviewed, permanent decisions.

**Closed 2026-07-30 (U0).** Ruled: leave the behaviour, record the cost. `PKG-NG-AUTOMATION-ENV` states it, the narrative's non-goals carry it, and the job guide names it. It stays out of `--help` and the config schema.

## DIV-03 — Passwordless-sudo preconditions differ per manager and are stated in no requirement

The four jobs have materially different prerequisites, all enforced in `validate()` and none written down outside the code:

| | source | target |
| - | - | - |
| apt | required | required |
| snap | required | required |
| flatpak | none | only when a system-scope item exists on either machine |
| manual | none | none |

`apt_sync.py:3876-3891` records why source-side sudo is a hard failure rather than a degradation: without it the `/etc/apt` capture silently returns empty digests and the sync reports success having replicated no repository configuration. `snap_sync.py:701-706` records that the source needs it because the refresh pause writes there, and `snap_sync.py:743-745` that snapd admin-gates even *reading* snap config.

This is a user-visible precondition — it decides whether the job can run at all — so it belongs in the requirements. **Closed 2026-07-30 (U0):** `PKG-FR-SUDO-PRECONDITION` carries the table. The code already conforms.

## DIV-04 — Sideloaded snaps: resolved by ruling them out of scope

Resolved 2026-07-29. `PKG-NG-SIDELOAD` claimed sideloaded snaps "cannot be reproduced" as an accepted cost, which #221 contradicts — a snippet running `snap install --dangerous` reproduces one. The article is deleted: it described deferred work as a deliberate non-goal.

The replacement ruling is that sideloaded snaps are out of scope entirely and ignored on both machines, rather than half-handled. `PKG-FR-SNAP-SIDELOAD` now says so.

**Closed 2026-07-30 (U6).** `SnapSyncJob.plan` partitions both machines' listings, warns per machine naming what it found, and withholds every name sideloaded on either machine from both — so a target-only sideloaded snap is no longer a removal candidate, and a store snap the target sideloaded under the same name is no longer an install.

## DIV-05 — Cross-manager review is asserted in one comment and denied everywhere else

`orchestrator.py:1293-1295` justifies the `PackageItemFailures` arm with "The user approved changes across several package managers in ONE batched review (D-24)". There is no such review. `sync_core.py:8-10` and `review.py:713-715` both state the opposite — batching is per manager, each job reviews its own groups inside its own `execute()`, and there is no coordinator.

The behaviour the comment guards is still right (one manager's failures should not cancel another's approved work); only the stated reason is wrong. Code comment fix, no behaviour change.

**Closed 2026-07-30 (U3).** The arm's comment now cites D-15/D-16 job independence: nothing coordinates the four jobs, so one manager's failure is no evidence about another's approved work.

## DIV-06 — The generated decision-file header contains a leaked identifier

`state.py:93` writes, into every user's `~/.config/pc-switcher/<manager>.decisions.yaml`:

> This file is machine-local and is never synced to any peerfilter_inert. Remove…

`peerfilter_inert` is a function name that ran into the sentence. Cosmetic, but it ships to users' machines in a file the requirements describe as the record of their explicit decisions.

**Closed 2026-07-30 (U8).** The sentence ends at "peer", and `TestDecisionFileRecord` asserts the written header line so a symbol cannot reach it again unnoticed.

## DIV-07 — apt and flatpak apply the conflict screen to different candidate sets

`PKG-FR-REPO-CONFLICT` and `PKG-FR-FLATPAK-REPOINT` read as one rule in two ecosystems. They are not quite the same rule.

apt raises the conflict screen for **every** differing repository file that feeds a machine-specific target package, and approving it forces the write (`apt_sync.py:2267-2285`).

flatpak additionally gates on the remote being in the set `_derive_remotes` would provision if the review approved everything — so a remote no approved ref needs is never a question, and answering "overwrite" cannot by itself make a remote travel (`flatpak_sync.py:1420-1429`). The code names this as a deliberate divergence from apt.

The reasoning is sound — flatpak has no always-sync bucket to make a remote travel independently — but the articles state the two as symmetric.

**Closed 2026-07-30 (U4).** Ruled: both ask only what the approved changes need, so the asymmetry was apt's alone. `AptSyncJob._files_an_approval_would_write` computes the repository files this run would derive if the review approved every install it proposes, and `_plan_repo_diffs` intersects the differing files with that set before the conflict question is raised.

## DIV-08 — `manual_installs_sync` is not covered by the job-ordering check

`PKG-FR-JOB-ORDER` says "the three package-manager jobs" must precede `folder_sync`, and `orchestrator.py:1073` validates exactly those three. Code and article agree.

The question is whether the article is right. `manual_installs_sync` also installs software — by replaying a snippet — and that software writes its own stock defaults exactly as an apt package does, which is the whole reason for the ordering rule. The shipped config lists it before `folder_sync` (`default-config.yaml:55`), so the default is correct; nothing catches a user who reorders it.

**Closed 2026-07-30 (U3).** Ruled: the rule covers all four. `Orchestrator._check_package_jobs_precede_folder_sync` validates `manual_installs_sync` alongside the three managers.

**Ruling needed:** extend the rule to all four, or state explicitly why snippet-installed software does not need it.

## DIV-09 — The UAT runbook describes two behaviours that have since been fixed

`02-UAT-01-RUNBOOK.md` §4b tells the tester that choosing "Skip" at a collateral prompt "writes `SKIP_ONCE` over EVERY package in the batched removal candidate set, not only the one that produced the collateral … so it also cancels a never-offer-again tick made on `X` in the same review", and instructs them to work around it by splitting the check across two runs.

That is no longer true. Attribution narrows the batch by re-rehearsing each candidate alone (`apt_sync.py:2528-2576`), and a skip overrides only `APPLY` decisions, explicitly leaving `SKIP_ALWAYS` intact (`apt_sync.py:2645-2666`). Fixed in `402f7067`.

§4d likewise tells the tester to expect the title `Delete repositorys the source no longer has (apt)` and to record the pluralisation as a known cosmetic defect. That was fixed in `ffa06900`; the title now reads `Delete repositories …` (`apt_sync.py:338-341`, `apt_sync.py:2055`).

§4a is now stale in a third way: it tells the tester that "leaving one unticked at the review is what makes the never-offer-again screen appear". That screen no longer exists — `9ba0d437` replaced the two-pass checkbox flow with one decision screen per group, where every row carries its own answer and there is no leftover set to re-offer. UAT test 1 in `02-UAT.md` describes the same superseded flow ("questionary checkbox composes with paused Rich Live, installs/removals grouped separately, removals start unticked").

A tester following the runbook as written would report three fixes as regressions and would look for a screen that has been deliberately removed. The runbook and `02-UAT.md` both need updating before UAT runs.

## DIV-10 — A held package is installed at the wrong version, then frozen there

Ruled on 2026-07-29, after the code was written, so this is a known gap rather than a contradiction.

An apt hold blocks install, upgrade and removal alike — measured on `ubuntu:24.04`: `apt-get remove` and `apt-get install` on a held package both refuse with `E: Held packages were changed`, and `autoremove` leaves it alone. It therefore carries the intent "do not move this off the version that works" as well as "do not lose this", and apt offers no way to distinguish them.

`apt_sync` installs every package by name (`_install_args` → `apt-get install --assume-yes --no-install-recommends <name>`) and applies the hold as a separate item afterwards. So where the source holds a package the target lacks, the target installs whatever version its repositories currently offer and then freezes on it — permanently, since nothing will move a held package again. The two machines end up held at different versions with nothing reporting it.

`PKG-FR-APT-HOLD-VERSION` now requires the source's exact version for that case, with failure naming both versions where the target cannot supply it.

**Closed 2026-07-30 (U4).** The install asks for `<name>=<version>` from `AptSyncJob._held_versions`, and `PackageConverger._held_version_refusal` turns apt's refusal into a failure naming the source's version and the target's candidate, with no fallback. `PKG-FR-APT-HOLD-INERT` closes with it, on a measurement that showed apt does not enforce it: on `ubuntu:24.04`, `apt-mark hold` exits 0 and records the hold for a package that is merely NOT INSTALLED, and exits 100 only for a name apt has never heard of. `PackageConverger._hold_refusal` is therefore this job's own guard — a hold whose package item was skipped, failed, or only reported the package missing fails alone before any command.

**Still open:** the same package held on *both* machines at different versions. `PKG-FR-APT-HELD-TARGET` suppresses any package-level item for a held target package, and a hold present on both sides produces no hold item either, so that divergence is still invisible in every run and this unit did not change it. Converging it would mean unhold → install the source's version → re-hold, which is a larger change than the install case.

## Ambiguity flagged for the Stage 5 article audit — done 2026-07-30

`PKG-FR-MACHINE-SPECIFIC` says the item is "never to be offered again on that machine. The mark MUST be local to that machine" with no antecedent for either "that machine". The rule the code implements is the holder rule (`sync_core.py:213-223`): install and change diffs record on the source, removal diffs on the target, written through that machine's own executor. The article was unstatable without a name for the holding machine, which the narrative's vocabulary section now introduces; the article uses the term and drops its inline gloss.

The full audit ran over all 123 articles on 2026-07-30. Ten findings, all in the criteria and all vocabulary rather than substance: `rehearsal` for the narrative's *dry run*; `provenance` for *origin*, which the criteria itself contradicted by saying *origin divergence* elsewhere; `prompts` and `a screen` for what the user is asked; `Skip-once` for the narrative's decision words; `blocks` as a collective noun the narrative never defines; `in either direction` on the banned list; two articles carrying two Why lines that said the same thing twice; and `PKG-FR-ASK-WHEN-NOT-DERIVABLE` claiming to enumerate every non-derivable question while omitting four of them. No article was found that could not be stated in the narrative's vocabulary once reworded.

## DIV-11 — A filtered remote is replicated unfiltered

Ruled on 2026-07-29, after the code was written, so this is a known gap rather than a contradiction.

**Measured** (flatpak 1.14.6, `02-SPEC-snap-flatpak-derivation.md` §2.8): `flatpak remote-modify --filter=/tmp/f.filter <remote>` records the *path* `/tmp/f.filter` in the remote's `filter` column and adds a `filtered` token to `options`. The filter's content is an ordinary file at that arbitrary local path, outside the ostree store.

The original reading was that this makes the filter unsyncable — "not repository-or-key material". It does not. The content is a file, and the run already carries files byte-for-byte: a repository's signing key is copied exactly this way. What the earlier decision actually gave up on was the *path*: it is arbitrary, so replicating the filter means the flatpak job writing to a location it does not own, possibly inside `folder_sync`'s territory.

`PKG-FR-FLATPAK-FILTER` now requires the filter to be replicated: copied byte-for-byte to the same absolute path on the target, re-applied to the replicated remote, derived rather than reviewed, and applied after the approved applications from that remote have landed. Failure to copy or re-apply fails every approved application from that remote.

`flatpak_sync` implements the opposite: `_FLATPAK_REMOTES_CMD_TEMPLATE` requests `name,url,options`, so the path is never read, and the shipped behaviour is one WARNING per filtered remote naming the `remote-modify --filter` command. Implementing the requirement widens the capture to four columns, which reshapes every remote fixture in the suite — with the `filter` column requested, an unfiltered remote prints four fields, not three.

`docs/jobs/package-sync.md:252` documents the shipped warning behaviour and stays accurate until the code changes.

**Open within this ruling:** the ordering rule is stated as a design requirement, not from measurement. Whether flatpak actually refuses to install a ref its own filter excludes has not been tested.

**Closed 2026-07-30 (U5).** The capture asks for `name,url,options,filter`; `_apply_remote_filters` copies the filter to the same absolute path and re-applies it after the converge loop, failing every approved ref whose own origin is that remote if it cannot land. `_delete_unused_remotes` replaces the removal review item, counting use against the target's own post-loop ref listing. `_warn_if_unverified` tells an ordinary run, and `_same_vendor` treats an absent URL as matching nothing. What survives, recorded in the criteria's gap register: the ordering rule's premise is still unmeasured.

## DIV-12 — A skipped removal loses its collateral protection

Found on 2026-07-30 while checking the narrative's collateral claim against the code. This is a shipped bug, not a requirement that moved.

`_collect_plan_time_collateral` builds `reviewed_names` from the install and removal candidate lists (`apt_sync.py:2586-2588`), before the review runs and therefore before any answer exists. `_classify_collateral` then skips every package in that set (`apt_sync.py:2670`). The comment justifies it as "a decision the user is taking anyway" — true for a removal the user approved, false for one they skipped.

So: a package offered for removal, kept by answering *skip*, is excluded from collateral protection for the rest of the run. Approving an unrelated install whose transaction removes it deletes it with no question and, per `PKG-FR-COLLATERAL-AUTO`'s classification, no review line either. The user asked to keep it and it goes.

`PKG-FR-COLLATERAL-MANUAL` now states that only an APPROVED removal exempts a package.

**Closed 2026-07-30 (U1).** The install batch exempts nothing, so a removal candidate an install would take is a collateral question. What survives, and is recorded as a survivor in the criteria's gap register: inside the removal batch a candidate is still exempt from its own transaction, so a skipped candidate carried off by another approved removal's cascade is refused by the apply-time guard rather than asked about.

## Rulings that close earlier divergences

- **DIV-08** (`manual_installs_sync` outside the job-ordering check) is ruled: the rule covers all four package jobs. Closed in U3; see the entry above.
- The ESM cost question behind `PKG-FR-ESM-GATE` is answered by measurement rather than ruling: on an attached Ubuntu 24.04 desktop, 60 of 2297 installed packages resolve their candidate to `esm.ubuntu.com`, including `ffmpeg`, `gimp` and `imagemagick`. The container's zero was an artefact of its package set.

## DIV-13 — A repository credential reaches the log

Ruled on 2026-07-30, after the code was written.

A private PPA or a commercial repository carries its credential in the URL itself (`https://user:token@host/...`), so the URL is the secret. `Executor._announce` traces every command verbatim at DEBUG (`executor.py:154`), the repository-conflict question shows both machines' copies of a source file in full, and `PKG-FR-LOG-VERBATIM` now adds the manager's own output. Nothing in the codebase redacts anything — `grep -rn redact src/` finds no match.

Logs are written to `~/.local/share/pc-switcher/logs` with mode `rw-rw-r--`, so the exposure is to every account on the machine that wrote the log. It does not spread: pc-switcher's own runtime files are excluded from `folder_sync` ahead of any user filter and cannot be re-included (`default-config.yaml:135-137`).

`PKG-FR-CREDENTIAL-PRIVACY` now requires the embedded credential to be withheld wherever a URL is written or shown.

**Closed 2026-07-30 (U2).** `redaction.redact_credentials` replaces every absolute URL's whole userinfo, applied at four exits: `logger.CredentialRedactionFilter` on both queue handlers, the confirmation prompt in `executor._announce`, `ReviewEntry.__post_init__` for everything a review shows including the files it prints whole, and `ItemDiff.__post_init__` for the label a recorded decision keeps.

Related, and already satisfied: `PKG-FR-ESM-PRIVACY` is honoured by construction rather than by filtering — `pro status --format json` is parsed and only the `attached` boolean escapes (`apt_sync.py:113-114`, `278-281`).

## DIV-14 — The log records counts, not decisions, and no manager output at all

Verified on 2026-07-30 against the current head, after the logging requirements were ruled on.

`PKG-FR-LOG-DECISIONS` asks for every item a job presented together with the decision it received. What the code writes is:

- one FULL line per item that was actually applied — `f"{diff.action.value} {diff.label}"` (`sync_core.py:493`) — or, in a dry run, one `Would …` line per item (`sync_core.py:397`);
- INFO counts around them: how many changes are being applied, how many succeeded, how many failed with a joined summary (`sync_core.py:385-416`);
- in a non-interactive run, a WARNING carrying the COUNT of unresolved items (`review.py:822`) and one line per group that was not asked (`review.py:914`).

So an item the user skipped produces no line at all, and no line anywhere pairs an item with its decision. Auto-collateral is likewise absent — `_classify_collateral` deliberately produces "nothing, not even a report line the user cannot act on" (`apt_sync.py:2655-2656`), which `PKG-FR-COLLATERAL-AUTO` now overrides.

`PKG-FR-LOG-VERBATIM` asks for the manager's own output verbatim in the debug log. `Executor._announce` traces the command TEXT at DEBUG before running it (`executor.py:154`); nothing traces what came back. The only output that reaches the log is `stderr` attached as structured context on error paths (`sync_core.py:489`, `apt_sync.py:3551-3557`). No stdout, at any level.

**Closed 2026-07-30 (U2), together with DIV-13** as that interaction required. `PackageSyncJob._log_decisions` writes one FULL line per presented item naming its answer; `review_items` names each item it could not ask about instead of counting them; `Collateral._log_auto` names each auto-collateral change; `executor._trace_output` records every command's stdout and stderr verbatim at DEBUG.

Log volume is a known consequence, not an objection: runs on the maintainer's desktop already produce 350-378 MB logs, and the ruling was "all of it, as written".
