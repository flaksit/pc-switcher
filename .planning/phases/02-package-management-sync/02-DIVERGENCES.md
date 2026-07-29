# Package sync divergences

Places where the code on `gsd/phase-02-package-management-sync` and the decision documents disagree, or where the code does something no document states. Found while building `02-BEHAVIOUR-LEDGER.md`. Nothing here is resolved — each is a ruling for the user to make. The narrative describes the code and marks the disputed points rather than picking a side.

Two seeds from the planning stage were checked and did **not** hold up: dry run does show and answer the review, which is what `PKG-FR-DRY-RUN` asks for; and `--confirm-each-command` does cover the decision records, the snippet registry and the refresh pause, as `PKG-FR-CONFIRM-EACH` requires. Neither is a divergence.

## DIV-01 — A failed probe ends the whole run, contradicting the stated failure isolation

`PKG-FR-OUTCOME-FAILED` requires that "one failed job MUST NOT stop the others". The orchestrator honours that for `PackageItemFailures` only: that arm records FAILED and continues (`orchestrator.py:1292-1314`). `ProbeFailed` is a plain `RuntimeError` (`probes.py:37`), so it falls through to `except Exception`, which records FAILED and then re-raises (`orchestrator.py:1315-1335`), ending the run.

Whether this is wrong depends on which reading is intended. `probes.py:42-48` argues a probe failure means the machine or the tool is broken, which is not a finding about any item and arguably *should* stop everything. `PKG-FR-OUTCOME-FAILED` does not distinguish the two. **Ruling needed:** does a dead package-manager read fail its own job and let the others run, or stop the sync?

## DIV-02 — The automation escape hatch writes permanent state and is documented nowhere

`PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` takes a JSON `item_id → decision` map and bypasses every prompt, returning `was_interactive=True` (`review.py:580-582`). Because the permanence guards test exactly that flag (`sync_core.py:454`, `manual_installs_sync.py:583`), this path **writes machine-specific marks and install snippets** — the two things `PKG-FR-SKIP-ONCE` and `PKG-FR-MACHINE-SPECIFIC` describe as coming from an explicit human choice.

It is deliberately absent from `--help`, the config schema and every doc (`review.py:35-39`), and is intended for integration tests that have no TTY. But an environment variable is not a test-only mechanism: anything that can set it on a real run gets silent, unreviewed, permanent decisions.

**Ruling needed:** leave as is and document it as an accepted cost; make the automation path report `was_interactive=False` so nothing permanent is written; or gate it on a test-only signal. Note the second option would change what the integration tests can assert.

## DIV-03 — Passwordless-sudo preconditions differ per manager and are stated in no requirement

The four jobs have materially different prerequisites, all enforced in `validate()` and none written down outside the code:

| | source | target |
| - | - | - |
| apt | required | required |
| snap | required | required |
| flatpak | none | only when a system-scope item exists on either machine |
| manual | none | none |

`apt_sync.py:3876-3891` records why source-side sudo is a hard failure rather than a degradation: without it the `/etc/apt` capture silently returns empty digests and the sync reports success having replicated no repository configuration. `snap_sync.py:701-706` records that the source needs it because the refresh pause writes there, and `snap_sync.py:743-745` that snapd admin-gates even *reading* snap config.

This is a user-visible precondition — it decides whether the job can run at all — so it belongs in the requirements. **No conflict, just an omission.** The narrative states it; the articles need an entry.

## DIV-04 — `PKG-NG-SIDELOAD` claims an impossibility that issue #221 contradicts

`PKG-NG-SIDELOAD` lists as a knowingly accepted cost: "Sideloaded snaps cannot be reproduced. Nothing carries the file between machines."

Issue #221 says otherwise, and gives the mechanism: a snippet running `snap install --dangerous <file>` reproduces one, and the existing snippet machinery already covers it. What actually exists today is an unbuilt handoff — `snap_sync` detects sideloads, warns, drops them, and withholds the target's matching entry (`snap_sync.py:518-533`), and `manual_installs_sync` has no detector for them. So they are replicated by nobody.

#221 also flags the flatpak equivalent (a ref from a local bundle, or from a remote that no longer exists) as unverified.

**Ruling needed:** this is a deferred gap, not a law of nature, and the non-goal should be rewritten to say so. The narrative states it as a current gap pointing at #221.

## DIV-05 — Cross-manager review is asserted in one comment and denied everywhere else

`orchestrator.py:1293-1295` justifies the `PackageItemFailures` arm with "The user approved changes across several package managers in ONE batched review (D-24)". There is no such review. `sync_core.py:8-10` and `review.py:713-715` both state the opposite — batching is per manager, each job reviews its own groups inside its own `execute()`, and there is no coordinator.

The behaviour the comment guards is still right (one manager's failures should not cancel another's approved work); only the stated reason is wrong. Code comment fix, no behaviour change.

## DIV-06 — The generated decision-file header contains a leaked identifier

`state.py:93` writes, into every user's `~/.config/pc-switcher/<manager>.decisions.yaml`:

> This file is machine-local and is never synced to any peerfilter_inert. Remove…

`peerfilter_inert` is a function name that ran into the sentence. Cosmetic, but it ships to users' machines in a file the requirements describe as the record of their explicit decisions.

## DIV-07 — apt and flatpak apply the conflict screen to different candidate sets

`PKG-FR-REPO-CONFLICT` and `PKG-FR-FLATPAK-REPOINT` read as one rule in two ecosystems. They are not quite the same rule.

apt raises the conflict screen for **every** differing repository file that feeds a machine-specific target package, and approving it forces the write (`apt_sync.py:2267-2285`).

flatpak additionally gates on the remote being in the set `_derive_remotes` would provision if the review approved everything — so a remote no approved ref needs is never a question, and answering "overwrite" cannot by itself make a remote travel (`flatpak_sync.py:1420-1429`). The code names this as a deliberate divergence from apt.

The reasoning is sound — flatpak has no always-sync bucket to make a remote travel independently — but the articles state the two as symmetric. **Ruling needed:** is the asymmetry intended, and should the articles say so?

## DIV-08 — `manual_installs_sync` is not covered by the job-ordering check

`PKG-FR-JOB-ORDER` says "the three package-manager jobs" must precede `folder_sync`, and `orchestrator.py:1073` validates exactly those three. Code and article agree.

The question is whether the article is right. `manual_installs_sync` also installs software — by replaying a snippet — and that software writes its own stock defaults exactly as an apt package does, which is the whole reason for the ordering rule. The shipped config lists it before `folder_sync` (`default-config.yaml:55`), so the default is correct; nothing catches a user who reorders it.

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

`PKG-FR-APT-HOLD-VERSION` now requires the source's exact version for that case, with failure naming both versions where the target cannot supply it. Recorded in the criteria's gap register.

**Still unruled:** the same package held on *both* machines at different versions. `PKG-FR-APT-HELD-TARGET` suppresses any package-level item for a held target package, and a hold present on both sides produces no hold item either, so that divergence is currently invisible in every run. Converging it would mean unhold → install the source's version → re-hold, which is a larger change than the install case.

## Ambiguity flagged for the Stage 5 article audit

`PKG-FR-MACHINE-SPECIFIC` says the item is "never to be offered again on that machine. The mark MUST be local to that machine" with no antecedent for either "that machine". The rule the code implements is the holder rule (`sync_core.py:213-223`): install and change diffs record on the source, removal diffs on the target, written through that machine's own executor. The article is unstatable without a name for the holding machine, which the narrative's vocabulary section introduces. Full audit of all 124 articles against that vocabulary follows once the narrative is approved.
