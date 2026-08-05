---
phase: 02
phase_name: "package-management-sync"
project: "PC-switcher"
generated: "2026-08-05"
counts:
  decisions: 13
  lessons: 7
  patterns: 9
  surprises: 7
missing_artifacts: []
---

# Phase 02 Learnings: package-management-sync

## Decisions

### Per-manager review, no cross-manager coordinator
Each package job runs plan → review → apply inside its own `execute()` via an injected reviewer. `PackagePhaseCoordinator` was built, then removed.

**Rationale:** A cross-manager review contradicts the job independence D-15 requires. The original reading of "one batched review grouped by manager" as cross-manager was never what the user asked for.
**Source:** 02-14-SUMMARY.md, 02-15-SUMMARY.md

### Four jobs, not three — `manual_installs_sync` owns unreproducible detection
Unreproducible-item detection and the snippet registry moved out of `apt_sync` into their own job with its own enable flag.

**Rationale:** Folded into `apt_sync`, disabling apt sync silently disabled manual-install detection; and half the work (unowned files under `/usr/local` and `/opt`) is not apt's business.
**Source:** 02-17-SUMMARY.md

### Version divergence is asymmetric: apt and flatpak float, snap converges
apt and flatpak report a version difference and change nothing; snap converges revision and channel.

**Rationale:** Snap stores per-user app data in revision-numbered directories `~/snap/<app>/<rev>/`. apt uses stable paths and flatpak uses id-named `~/.var/app/<id>`, so only snap's data path embeds the version — both machines must sit on the same revision for `folder_sync` to mirror the active data dir.
**Source:** 02-UAT-REVIEW-FIXES.md (issue #118)

### The snippet registry travels by its own job's `send_file`, never `config_sync`
`manual_installs_sync.after_review()` pushes `package-snippets.yaml` itself; `config_sync` carries `config.yaml` only.

**Rationale:** `config_sync` runs at SyncStep 9, before any review, so it cannot carry a snippet the user has not authored yet. `folder_sync` cannot be relied on either — user sync jobs are user-controllable, so no job's correctness may depend on another running.
**Source:** 02-18-SUMMARY.md

### Reproducibility is classified from the source registry, and snippets apply the same run
`plan()` reads `SnippetRegistry(self.source)`; `after_review()` pushes the registry, then promotes on-the-fly-authored snippets to INSTALL.

**Rationale:** The source is the machine being replicated. Classifying from the target meant a freshly authored snippet was only applied on the *next* run.
**Source:** 02-22-SUMMARY.md

### A version mismatch is structurally unconvergeable, not merely undecided
Mismatches plan as `DiffClass.VERSION_MISMATCH`/`DiffAction.REPORT_ONLY`, and `apply()` drops every REPORT_ONLY diff regardless of the recorded decision.

**Rationale:** A guard that depends on a decision can be defeated by a wrong decision. Excluding the action class outright means no answer can force-converge a mismatch.
**Source:** 02-VERIFICATION.md, `packages/sync_core.py:441`

### apt collateral is classified auto vs manual before anything is refused
Collateral that is auto-installed proceeds silently; manual collateral becomes a three-way install-anyway/skip/abort review item asked at plan time.

**Rationale:** Blanket refusal of any install whose `apt-get -s` simulation removes something blocks legitimate installs whose only collateral is a dependency nobody chose.
**Source:** 02-16-SUMMARY.md

### Shared package helpers live in `jobs/packages/`
`items.py`, `review.py`, `state.py`, `sync_core.py` moved into a subpackage with the `package_` prefix stripped; job modules and `base.py`/`context.py` stay in `jobs/` for discovery.

**Rationale:** `jobs/` should hold job modules plus the base and context only.
**Source:** 02-19-SUMMARY.md

### questionary over InquirerPy
questionary 2.1.1 is the phase's only new runtime dependency, and drives every package review prompt.

**Rationale:** The legitimacy gate was cleared by explicit user approval plus live PyPI/GitHub verification (2138 stars, 24 releases 2018–2025, creation date matching first PyPI release) — not training-data recall.
**Source:** 02-02-SUMMARY.md

### The decision file is machine-local and never synced; the snippet registry is shared
`*.decisions.yaml` is excluded from `folder_sync` by a global-first, non-overridable glob that `packages/state.py` owns.

**Rationale:** "This package is mine" is a statement about one machine. Syncing it would export one machine's exclusions to the other.
**Source:** 02-04-SUMMARY.md

### `HELD_OR_PINNED` outranks version-mismatch and removal
Any target item named by a hold or pin reports as held/pinned even when its versions also differ.

**Rationale:** The hold is the more informative thing to show — it explains why the other difference exists.
**Source:** 02-05-SUMMARY.md

### Skip-once is a valid resolution, not an unresolved state
Only a cancelled or abandoned review leaves an item unresolved and fails the job.

**Rationale:** The user may be declining something temporary; that answer should not make the run unclean.
**Source:** 02-CONTEXT.md (D-21 corrected), 02-15-SUMMARY.md

### snapd auto-refresh is paused via `snap set system refresh.hold`, not `snap refresh --hold`
Written on both hosts, with a timed value, restoring the machine's prior policy in cleanup.

**Rationale:** `snap refresh --hold` with no snap name sets an *indefinite* global hold and its `--unhold` clears unrelated per-snap holds. The system option is symmetric with the read-only `snap get`, and a timed value self-expires as a crash backstop. Consequence: passwordless sudo is now required on the source too, not just the target.
**Source:** 02-UAT-REVIEW-FIXES.md

## Lessons

### The shared base's `plan()` was apt-shaped, and both later managers had to override it
`PackageSyncJob.diff_items()` hardcodes `ItemClass.APT_PACKAGE` and reads `.version`, a field `SnapItem` and `FlatpakItem` do not carry. Both `SnapSyncJob` and `FlatpakSyncJob` override `plan()` entirely, against their plans' literal "inherit `plan()` unchanged" instruction, and reuse only `DecisionFile`/`filter_inert`/`_build_review_groups`.

**Context:** The abstraction was extracted from one manager, so it encoded that manager's item shape. Flatpak hit it for a second reason — the base has no notion of one item class that must converge before another (remotes before refs).
**Source:** 02-08-SUMMARY.md, 02-09-SUMMARY.md

### Faithful execution of a wrong decision still produces the wrong system
Plans 02-01 to 02-13 shipped, were code-reviewed and went green in CI, then 02-14 to 02-21 removed the coordinator, split out a fourth job and reverted `config_sync`. Nothing was defective; the decisions were.

**Context:** The rework was caught only by a human reading the executed phase. The correction was made as a delta replan in place rather than a new phase, because nothing had merged yet.
**Source:** 02-14-SUMMARY.md through 02-21-SUMMARY.md

### Only the real VM run found the same-run defect
`manual_installs_sync` applied source-authored snippets one run too late. Twenty-one plans of unit tests, review and type-checking did not surface it; the first CI integration run on PR #206 did.

**Context:** The unit tests seeded the registry wherever the code read it, so they agreed with the bug. Only an end-to-end run over two machines could disagree.
**Source:** 02-21-SUMMARY.md, 02-22-SUMMARY.md

### A grep acceptance criterion can contradict the prose of its own plan
Three times: `manager_name: ClassVar` contains the substring `name: ClassVar` the same plan required absent; a docstring explaining `grep -c 'review_items' == 0` was itself a match; a correctly negated sentence ("no coordinator sits between the jobs") failed `grep -ci 'coordinator' == 0`.

**Context:** A mechanically-checkable criterion is only as good as its distance from the text it scans. Each was caught by running the plan's own check before commit.
**Source:** 02-03-SUMMARY.md, 02-09-SUMMARY.md, 02-20-SUMMARY.md

### Assert on mutating command shapes, not on substrings
Tests asserting `"sudo" not in cmd` and `"apt-get -s" not in cmd` during `plan()` broke when legitimately read-only probes were added. They were rewritten to check for `sudo install`, `sudo rm`, `sudo apt-get`, `sudo cp`.

**Context:** `sudo <read-only command>` is a read. A blanket substring ban conflates the privilege with the mutation and blocks correct code.
**Source:** 02-05-SUMMARY.md, 02-06-SUMMARY.md

### A stubbed prompt proves nothing about the terminal
Unit tests stub `questionary.checkbox()`/`select()`/`text()` and CI answers reviews via `PCSWITCHER_PACKAGE_REVIEW_AUTOMATION`, so real prompt_toolkit rendering, keybindings and terminal-mode handoff from a paused Rich Live display were never exercised by any automated path. This is what kept verification at `human_needed` until the hand UAT ran.

**Context:** Declaring it up front as a human checkpoint (02-02 Task 3) is what made it a tracked obligation instead of an untested assumption.
**Source:** 02-02-SUMMARY.md, 02-VERIFICATION.md

### The orchestrator's D-17 ordering guard still names only three jobs
`_check_package_jobs_precede_folder_sync` hardcodes `("apt_sync", "snap_sync", "flatpak_sync")` and omits `manual_installs_sync`. The truth still holds via the shipped `default-config.yaml` key order, which is asserted by test; only the defence-in-depth guard is incomplete.

**Context:** `orchestrator.py` was in no delta plan's `files_modified`, and the delta phase guard forbade scope expansion. Left as a known one-line follow-up.
**Source:** 02-17-SUMMARY.md

## Patterns

### Pause-the-display prompt
`ui.pause()` → `try/finally: ui.resume()`, with the blocking `.ask()` dispatched via `asyncio.to_thread`.

**When to use:** Any blocking prompt_toolkit prompt raised while the persistent Rich Live display is running.
**Source:** 02-02-SUMMARY.md

### Base no-op hook, subclass override
`_finalize_unreproducible`, `_unresolved_as_failures` and `after_review` are no-ops on `PackageSyncJob` and implemented only by the manager that produces the diff class.

**When to use:** One manager needs a step in the shared pipeline that the others must not perform — keeps `execute()` the single source of the plan/review/apply order.
**Source:** 02-17-SUMMARY.md, 02-18-SUMMARY.md

### Reviewer injection seam
`JobContext.reviewer` is optional with a `None` default so lightweight test contexts omit it; a job that reviews asserts it is set and fails loudly at `execute()` rather than silently applying unreviewed diffs.

**When to use:** Injecting an interactive collaborator into a job that must never run unattended by accident.
**Source:** 02-15-SUMMARY.md

### Parse by whatever the tool declares as its own column contract
`snap list --all` is parsed header-driven (read the header row, build a name-to-index map), verified against a fixture with header *and* body columns swapped. `flatpak list --columns=...` is parsed by fixed tab position, because the `--columns` flag is itself the order contract.

**When to use:** Scraping any CLI's tabular output — pick the mechanism the tool guarantees, not the one that reads more naturally.
**Source:** 02-08-SUMMARY.md, 02-09-SUMMARY.md

### Vocabulary lookup with a degrade-to-bare-verb backstop
`_ACTION_VOCABULARY.get((item_class, action), action.value)` — a missing entry yields the bare `DiffAction` word rather than dropping the group.

**When to use:** Mapping an open enum to display text where a miss must never make a review entry disappear.
**Source:** 02-05-SUMMARY.md

### Call-count `side_effect` for drift tests
The same `apt-get -s` command returns a clean preview at plan time and a collateral preview at apply time.

**When to use:** Proving a last-line-of-defence guard actually fires — the world must change between the two reads.
**Source:** 02-16-SUMMARY.md

### An integration test synthesizes its own divergence
The apt-repository-state test writes a uuid-suffixed deb822 `.sources` + keyring pair the fresh target lacks, replacing a natural-pair search that perpetually `pytest.skip`ped.

**When to use:** Any VM test whose subject must exist on the fleet — a skip that never fires reads as coverage and is not.
**Source:** 02-23-SUMMARY.md

### Configuration reference names keys, job docs explain behaviour
`docs/configuration.md` lists config keys and links out; what a job does lives in `docs/jobs/<name>.md`.

**When to use:** Any doc split where "how do I set this" and "what will this do" are different questions.
**Source:** 02-20-SUMMARY.md

### Atomic remote write as one command
`mkdir -p ... && printf '%s' <quoted> > <tmp> && mv -f <tmp> <path>` — one round trip, verified round-trip safe for multi-line YAML containing quotes, backslashes and `%`.

**When to use:** Writing a small file over the executor where three separate calls would cost three round trips and leave a torn file on failure.
**Source:** 02-04-SUMMARY.md

## Surprises

### The defect that mattered most was invisible until real hardware ran it
The same-run snippet-application bug survived 21 plans, a cross-AI plan review, a deep code review and a full unit suite.

**Impact:** Two extra plans (02-22, 02-23) and a CONTEXT correction. It also retroactively justified the integration suite's cost.
**Source:** 02-21-SUMMARY.md

### A stable sort made a test's intended ordering structurally impossible
`AptSyncJob.plan()` gives `APT_PACKAGE` and `UNREPRODUCIBLE` diffs the same sort rank, so construction order always places apt packages first. Sandwiching a failing item between two apt installs cannot happen.

**Impact:** `test_continue_on_item_failure` uses three UNREPRODUCIBLE items whose snippet bodies run real `apt-get install`, proving the same thing by a different route.
**Source:** 02-11-SUMMARY.md

### `~/` immediately followed by a shlex-quoted word still tilde-expands
Verified by direct `bash -c` test rather than assumed.

**Impact:** `DecisionFile` resolves paths with a bare `~/` prefix and saves an executor round trip per `load()`/`record()`, while still meeting the `shlex.quote()` requirement on the relpath.
**Source:** 02-04-SUMMARY.md

### basedpyright strict rejects the hook overrides the design requires
`Sequence[SnapItem]` / `Sequence[FlatpakItem]` are not subtypes of the base's `Sequence[AptPackageItem]`, so `reportIncompatibleMethodOverride` fires on every capture/query hook.

**Impact:** One `# pyright: ignore` per hook, each carrying a comment stating why it is safe — these subclasses override `plan()` and never route through the base call that would expect an `AptPackageItem` back.
**Source:** 02-08-SUMMARY.md, 02-09-SUMMARY.md

### `home.filter` already had no snap or flatpak rules
The CONTEXT note said to retire them.

**Impact:** Nothing to retire — only an explanatory comment was added. Found by reading the shipped file instead of trusting the note.
**Source:** 02-10-SUMMARY.md

### GSD's plan counter reported "Plan 2 of 21" for a delta replan
Plans 01–13 had shipped and 14–21 were the delta, so the handler's computed sequence number was meaningless.

**Impact:** None to artifacts — completion, progress and session fields were correct. Left as the handler produced it rather than hand-edited.
**Source:** 02-14-SUMMARY.md

### A `git add` of an already-`git rm`-ed path aborts the whole stage
Task 2 of 02-15 intended one commit; the aborted stage captured only the deletion.

**Impact:** The reworked test modules landed as a companion follow-up commit rather than rewriting history.
**Source:** 02-15-SUMMARY.md
