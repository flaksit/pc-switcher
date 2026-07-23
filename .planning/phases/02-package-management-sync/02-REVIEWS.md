---
phase: 2
reviewers: [codex]
reviewed_at: 2026-07-23T00:35:00Z
plans_reviewed: [02-01-PLAN.md, 02-02-PLAN.md, 02-03-PLAN.md, 02-04-PLAN.md, 02-05-PLAN.md, 02-06-PLAN.md, 02-07-PLAN.md, 02-08-PLAN.md, 02-09-PLAN.md, 02-10-PLAN.md, 02-11-PLAN.md, 02-12-PLAN.md]
---

# Cross-AI Plan Review — Phase 2

Reviewer: Codex CLI (gpt-5.5, reasoning effort high), run with read-only repo access so every claim could be checked against source.

## Codex Review

## Summary

The plan set is unusually thorough and mostly aligned with the project’s reliability-first architecture, but I would not approve it as-is. The biggest issue is that the implementation architecture still treats `apt_sync`, `snap_sync`, and `flatpak_sync` as independent sequential jobs, while the phase decision requires one batched review before any package-manager change. There are also several concrete integration risks in the current repo: `folder_sync` cannot currently see sibling job enablement, config sync only transfers `config.yaml`, and `RemoteExecutor.send_file()` cannot write directly into `/etc/apt` as the normal SSH user. Overall: strong design intent, but several plan corrections are needed before implementation.

## Strengths

- The plans correctly anchor Phase 2 in an ADR before implementation. `02-01-PLAN.md` makes ADR-020 the durable source for the manifest/replay model and moves `/etc/apt` scope deliberately rather than burying that boundary in code.

- The plan follows existing project mechanics for job discovery and strict config validation. The repo currently resolves jobs by `sync_jobs` iteration order and module/name matching in [orchestrator.py](/home/janfr/dev/pc-switcher/src/pcswitcher/orchestrator.py:882), [orchestrator.py](/home/janfr/dev/pc-switcher/src/pcswitcher/orchestrator.py:601), and [orchestrator.py](/home/janfr/dev/pc-switcher/src/pcswitcher/orchestrator.py:611), and the schema rejects unknown job keys via `additionalProperties: false` in [config-schema.yaml](/home/janfr/dev/pc-switcher/src/pcswitcher/schemas/config-schema.yaml:53) and [config-schema.yaml](/home/janfr/dev/pc-switcher/src/pcswitcher/schemas/config-schema.yaml:91).

- The TUI review plan reuses a real existing pattern. `TerminalUIConfirmer` already pauses/resumes around blocking prompts in [confirmer.py](/home/janfr/dev/pc-switcher/src/pcswitcher/confirmer.py:121), and interactivity is centralized in [terminal.py](/home/janfr/dev/pc-switcher/src/pcswitcher/terminal.py:10).

- The package-manager semantics are mostly conservative: use `apt-mark showmanual`, `dpkg-query`, `dpkg --compare-versions`, header-based snap parsing, flatpak scope as identity, byte-for-byte key transfer, and no package DB mirroring. Those are the right defaults for this project.

- The plans include focused safety tests for the most dangerous behaviors: removals separated and unchecked, non-interactive skip-all, no snap holds, skip-always inertness, and VM-level assertions against the package managers rather than only logs.

## Concerns

- **HIGH — 02-03 / 02-05 / 02-08 / 02-09 / 02-10: the “one batched review before any change” requirement is not actually achieved.**  
  The repo executes each job’s `execute()` sequentially in [orchestrator.py](/home/janfr/dev/pc-switcher/src/pcswitcher/orchestrator.py:1071) and [orchestrator.py](/home/janfr/dev/pc-switcher/src/pcswitcher/orchestrator.py:1080). But `02-03-PLAN.md` puts capture → diff → review → converge inside `PackageSyncJob.execute()` in [02-03-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-03-PLAN.md:119), and apt installs happen from that same job in [02-03-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-03-PLAN.md:128). That means `apt_sync` can mutate the target before `snap_sync` or `flatpak_sync` have even computed their diffs, contradicting D-24’s “one batched review before any change” across managers.

- **HIGH — 02-06: writing apt files directly into `/etc/apt` via `send_file()` will fail.**  
  `RemoteExecutor.send_file()` is plain SFTP as the SSH user, with no sudo path, in [executor.py](/home/janfr/dev/pc-switcher/src/pcswitcher/executor.py:362). The only current use writes into user-owned paths, e.g. VS Code state under home in [vscode_state_sync.py](/home/janfr/dev/pc-switcher/src/pcswitcher/jobs/vscode_state_sync.py:364) and [vscode_state_sync.py](/home/janfr/dev/pc-switcher/src/pcswitcher/jobs/vscode_state_sync.py:369). `02-06-PLAN.md` instead says to `send_file` to a temp path in the `/etc/apt` destination directory before `sudo mv` in [02-06-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-06-PLAN.md:142). Normal users cannot SFTP-write there. This needs a user-writable staging path plus `sudo install`/`sudo cp`/`sudo mv`.

- **HIGH — 02-07: the snippet registry will not sync unless `config_sync.py` is explicitly changed, but it is not listed as a modified file.**  
  Current config sync has fixed constants for only `config.yaml` in [config_sync.py](/home/janfr/dev/pc-switcher/src/pcswitcher/config_sync.py:22) and [config_sync.py](/home/janfr/dev/pc-switcher/src/pcswitcher/config_sync.py:23), reads only that file from target in [config_sync.py](/home/janfr/dev/pc-switcher/src/pcswitcher/config_sync.py:42), and copies only the provided `source_config_path` in [config_sync.py](/home/janfr/dev/pc-switcher/src/pcswitcher/config_sync.py:324). `02-07-PLAN.md` correctly says to extend config sync if it only transfers `config.yaml` in [02-07-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-07-PLAN.md:88), but `config_sync.py` is absent from the plan’s `files_modified` list in [02-07-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-07-PLAN.md:7). Without that edit, snippets authored on source will not reliably reach target.

- **HIGH — 02-03 / 02-05: the apt install/remove commands do not enforce the stated “only this package” safety.**  
  `02-03-PLAN.md` prohibits install-direction items from removing/downgrading/holding anything as a side effect in [02-03-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-03-PLAN.md:38), but then uses plain `apt-get install -y --no-install-recommends` in [02-03-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-03-PLAN.md:128). `02-05-PLAN.md` adds `apt-get remove -y` in [02-05-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-05-PLAN.md:150). Apt can remove or change additional packages to satisfy dependencies/conflicts. The plan needs an `apt-get -s` transaction preview parsed and included in the review before any real apt command.

- **HIGH — 02-06: failed `apt-get update` can leave the target’s apt configuration broken.**  
  The plan writes keys/sources first and models `apt-get update` as a later synthetic item in [02-06-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-06-PLAN.md:138) and [02-06-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-06-PLAN.md:140). If update fails after files were moved into `/etc/apt`, D-27’s continue/report model leaves those bad files in place. Pre-sync btrfs snapshots exist, but automatic rollback is Phase 7, so this can leave the target package manager unusable until manual repair. Repo/key/pin changes should be treated as a small transaction with rollback of changed files on update failure.

- **MEDIUM — 02-10: `folder_sync` cannot currently know whether `snap_sync` or `flatpak_sync` is enabled.**  
  `JobContext.config` is job-specific only in [context.py](/home/janfr/dev/pc-switcher/src/pcswitcher/jobs/context.py:12), and the orchestrator passes `job_config`, not the full config, when constructing each job in [orchestrator.py](/home/janfr/dev/pc-switcher/src/pcswitcher/orchestrator.py:897) and [orchestrator.py](/home/janfr/dev/pc-switcher/src/pcswitcher/orchestrator.py:902). `02-10-PLAN.md` says folder_sync should gate snap/flatpak exclusions on sibling job enablement from `self.context` in [02-10-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-10-PLAN.md:129). That field does not exist. Add `enabled_sync_jobs` or full read-only global config to `JobContext`.

- **MEDIUM — 02-07: “mandatory registration” is internally inconsistent.**  
  The must-have says an unreproducible item has no unresolved third state after sync in [02-07-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-07-PLAN.md:20). Later, the action says unresolved items are merely logged as a warning and the sync continues in [02-07-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-07-PLAN.md:146). That is not mandatory registration. If unresolved manual installs remain, the job should at least return failure so the sync is visibly not clean.

- **MEDIUM — 02-11: the continue-on-failure integration test setup cannot exercise the intended failure path.**  
  `02-11-PLAN.md` proposes using a package name that resolves to nothing as the middle failing install in [02-11-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-11-PLAN.md:74). But `02-05-PLAN.md` classifies no-candidate packages as `REPO_UNAVAILABLE` / `REPORT_ONLY`, not `INSTALL`, in [02-05-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-05-PLAN.md:80). That item should not reach converge, so the test will not prove D-27. Use a failing snippet, a deliberately broken repository item, or a controlled package-manager failure that still enters an apply path.

- **LOW — 02-02: the manual TUI spike depends on an undeclared artifact.**  
  The human checkpoint tells the user to run `/tmp/pcswitcher-review-spike.py` in [02-02-PLAN.md](/home/janfr/dev/pc-switcher/.planning/phases/02-package-management-sync/02-02-PLAN.md:139), but Task 2 does not list creating that driver as an action or acceptance criterion. Add it explicitly or replace the checkpoint with an inline command.

## Suggestions

- **02-03 / 02-05 / 02-08 / 02-09:** Add an explicit package-phase coordinator: capture/query/diff all enabled package managers first, render one combined review grouped by manager/action, then apply approved diffs in dependency order. This could be a shared `PackageReviewCoordinator` invoked by the orchestrator before package job convergence, or a single package-suite system step that keeps the three jobs’ config/failure isolation but splits them into `plan()` and `apply()` phases.

- **02-06:** Change apt file transfer to stage under a user-writable temp directory, then use `sudo install -o root -g root -m 0644` into `/etc/apt/...`. Add tests proving `send_file()` never targets `/etc/apt` directly.

- **02-06:** Treat apt repository/key/pin/config convergence as transactional: snapshot current target files to a temp backup, write changes, run `apt-get update`, and if update fails restore the previous files before reporting the item failure.

- **02-03 / 02-05:** Add apt transaction simulation before real `install` or `remove`. The review should show all apt-planned installs/removals/upgrades/downgrades, and destructive side effects should require explicit approval.

- **02-07:** Add `config_sync.py` and `tests/unit/cli/test_config_sync.py` to `files_modified`, and define the exact multi-file sync behavior for `config.yaml` plus `package-snippets.yaml`, including target-diff prompts and dry-run behavior.

- **02-10:** Add `enabled_sync_jobs: Mapping[str, bool]` to `JobContext`, populate it in `_create_job_context()`, and test folder_sync gating through that field.

- **02-07:** Resolve the mandatory-registration policy. Prefer: unresolved unreproducible items are allowed to remain for that run, but the package job result is failure until each is snippet-backed or machine-specific.

- **02-11:** Replace the repo-unavailable failure test with an apply-path failure. A snippet returning exit 42 is the cleanest controlled case and directly validates D-27 without destabilizing apt/snap/flatpak state.

## Risk Assessment

**Overall risk: HIGH.** The plan is directionally strong, but Phase 2 touches root package operations, repository trust, and package removals. The current plan has several high-impact integration gaps: no true all-manager pre-apply review, impossible `/etc/apt` SFTP writes, snippet registry sync not wired into the actual config-sync implementation, and apt commands whose real transaction may exceed the reviewed item. Fixing those issues should bring the plan down to medium risk; without them, implementation could violate the phase’s core promise that conflicts and destructive changes are reported before anything changes.
## Consensus Summary

Single reviewer, so there is no cross-reviewer consensus to compute. Instead, every finding was independently re-verified against the repository by the orchestrating agent before being accepted. All nine findings were confirmed; none were rejected or downgraded.

### Verified Findings

| Severity | Plan(s) | Finding | Verification |
| -------- | ------- | ------- | ------------ |
| HIGH | 02-03, 02-05, 02-08, 02-09, 02-10 | Per-job `execute()` means `apt_sync` mutates the target before `snap_sync`/`flatpak_sync` have diffed — D-24's single cross-manager batched review is not achieved | Confirmed: `orchestrator.py:1070-1080` awaits each `job.execute()` in turn; 02-03 places capture→diff→review→converge inside one job's `execute()` |
| HIGH | 02-06 | `send_file()` into `/etc/apt` is impossible — it is plain SFTP as the SSH user with no sudo path | Confirmed: `executor.py:362-370` is `sftp.put()`; the only existing caller writes under `$HOME` (`vscode_state_sync.py:364,369`). 02-06:142 stages the temp file inside the destination directory |
| HIGH | 02-07 | Snippet registry cannot reach the target — `config_sync.py` transfers only `config.yaml` and is absent from `files_modified` | Confirmed: `CONFIG_REMOTE_PATH` is hardcoded to `config.yaml` (`config_sync.py:23`), `_get_target_config` reads only that path; 02-07 `files_modified` lists no `config_sync.py` |
| HIGH | 02-03, 02-05 | `apt-get install -y` / `remove -y` can remove or downgrade packages beyond the reviewed item, violating 02-03's own prohibition | Confirmed: 02-03:38 prohibits side-effect removals; 02-03:128 issues a bare `apt-get install -y`, 02-05:150 a bare `apt-get remove -y`. No transaction preview exists |
| HIGH | 02-06 | A failed `apt-get update` leaves already-written repo/key files in place, and D-27 continues — the target's apt can be left broken with no automatic rollback until Phase 7 | Confirmed: 02-06:138-140 writes files first and models `update` as a later synthetic item |
| MEDIUM | 02-10 | `folder_sync` cannot read sibling job enablement — `JobContext.config` is job-scoped | Confirmed: `context.py:19` is job-specific config; `orchestrator.py:897-902` passes `job_config`. `folder_sync.py:234` reads `self.context.config["folders"]`, its own config. 02-10:129 assumes sibling enablement is readable |
| MEDIUM | 02-07 | "Mandatory registration" is self-contradictory — the must-have forbids an unresolved state, the action logs a WARNING and continues | Confirmed: 02-07:20 vs 02-07:146 |
| MEDIUM | 02-11 | `test_continue_on_item_failure` cannot exercise D-27 — its chosen failure is a repo-unavailable package, which is classified `REPORT_ONLY` and never reaches converge | Confirmed: 02-05:80 classifies no-candidate as `REPO_UNAVAILABLE`/`REPORT_ONLY`; 02-05:148 short-circuits `REPORT_ONLY` before touching the target; 02-11:74 relies on exactly that case |
| LOW | 02-02 | Human checkpoint tells the user to run `/tmp/pcswitcher-review-spike.py`, which no task declares creating | Confirmed: 02-02:139 references it; Task 2 has no action or acceptance criterion producing it |

### Agreed Concerns

The three that change the shape of the phase rather than one plan's detail:

1. **Cross-manager review coordination** — needs a plan/apply split so all enabled package managers diff before any of them applies.
2. **Privileged file writes** — `/etc/apt` writes need a user-writable staging path plus `sudo install`, and the repo/key/pin convergence needs to be transactional with restore-on-update-failure.
3. **Transaction fidelity** — the review must show what apt will actually do, not just the item the user ticked.

### Divergent Views

None — single reviewer.
