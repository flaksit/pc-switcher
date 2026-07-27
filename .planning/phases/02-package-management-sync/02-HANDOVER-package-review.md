# Handover — package sync, open work

State at handover: **6 commits local on `gsd/phase-02-package-management-sync`, none pushed.** Remote is at `fba128a4`. Working tree clean. Gates green: ruff, codespell, basedpyright 0 errors, 1240 unit+contract tests, 80 integration tests collecting.

Do not push without the user saying so.

```
87104aac  docs(adr-020): keyring locations, overwrite rule, batching preference
089ea985  fix(apt-sync): re-diff and re-review packages after /etc/apt converges
f873e5fa  fix(apt-sync): resolve Signed-By against every place a keyring lives
6a0922cb  fix(apt-sync): parse a pin stanza the same way wherever it is read
d27337da  feat(apt-sync): manage signing keys transparently instead of reviewing them
43a52fa3  feat(apt-sync): name what a repo or key removal would strand on the target
```

## 1. The review has never been driven by a human — highest priority

The batched review is the phase's whole interaction surface and **no test drives the real one**. Unit tests inject a fake; the VM suite sets an env var that bypasses prompting entirely. Nobody has watched it render.

Where things are:

| What | Where |
| --- | --- |
| Real reviewer | `src/pcswitcher/jobs/packages/review.py:545` `TerminalUIReviewer`, wrapping `review_items` in the same file |
| Constructed | `src/pcswitcher/orchestrator.py:394` |
| Fake used by unit tests | `tests/unit/jobs/test_package_sync_core.py:72` and `tests/unit/jobs/test_manual_installs_sync.py:132` (two separate `FakeReviewer` classes) |
| CI bypass | `PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` (`review.py:94`) — trusted JSON of item_id → decision, no prompting |

So the automated coverage proves the *decisions* are honoured, never that the *prompts* work. Untested end to end: questionary checkbox rendering, pause/resume of the Rich Live panel around each prompt, the second "never offer again on this machine?" checkbox, the unreproducible three-way resolution with its multi-line snippet editor, the collateral install-anyway/skip/abort prompt, and Ctrl-C/EOF aborting the whole sync from each of those.

Worst gap: the **second review** added in `089ea985` fires *mid-`execute()`*, after `/etc/apt` has converged (`apt_sync.py:1410-1441`, `_rereview_repo_invalidated_packages`). A prompt during apply pauses the Live display at a point nothing has ever exercised.

`.planning/phases/02-package-management-sync/02-UAT.md` test 1 covers the real-TTY walkthrough and is still `pending`.

## 2. Derived repos — designed and approved, not started

User decision: apt sources stop being independently reviewable items, exactly as keys did in `d27337da`.

- Adding a repo is **derived** from the packages approved from it — no separate tickable line. This makes "package ticked, its source unticked" unrepresentable, which is where a family of inconsistencies came from.
- The repo is **named in the package's review detail** ("install gh (adds repo cli.github.com)") so the system change is visible without being a separate decision.
- Removal stays reviewed — it is destructive and affects packages that remain.
- **`/etc/apt/sources.list` and `ubuntu.sources` are never collected.** Reference counting protects them in practice (`ubuntu.sources` maps to 126 manual packages on the user's machine) but that must not be load-bearing on a delete.

Rationale is settled: the user only ever adds a repo because they want a package from it, so repo lifecycle is derivable from package lifecycle. Precedent to follow: `d27337da` (keys) for the shape, `43a52fa3` for naming consequences in `detail`.

Expect this to invalidate several C-section rows of `02-SCENARIO-COVERAGE.md` that assert source items in the review with three-way decisions.

## 3. The no-candidate detector has never fired

`_packages_with_no_candidate` (copies in `src/pcswitcher/jobs/manual_installs_sync.py` and `src/pcswitcher/jobs/apt_sync.py`) tests for the literal `Candidate: (none)`. **apt does not print that for a package installed from a hand-downloaded `.deb`** — dpkg's own status entry supplies the candidate.

Measured on the user's machine: `code`, `brscan3`, `cnpg`, `falco-app`, `brother-udev-rule-type1` all report `Candidate: <installed version>`. The detector matches none of them, so `manual_installs_sync`'s primary detection finds nothing, no snippet is ever offered, and `apt_sync` proposes those packages as ordinary installs that fail on the target with "Unable to locate package".

D-18's calibration ("4 apt packages have no repo candidate on P17") came from a rule that does not match reality.

Correct test, already implemented for C26 in `43a52fa3`: a package whose installed version's **only origin is `/var/lib/dpkg/status`** came from no repo. `gh` shows `500 https://cli.github.com/packages` alongside it; `code` shows only the dpkg line. Reuse `_installed_origins_by_package`.

Note: with the second review now in place, a package this fix reclassifies gets re-diffed after `apt-get update`, so a `(none)` → available transition reaches the user the same run.

## 4. Smaller open items

- **A pin on the target makes a package un-removable and un-markable.** The pinned branch in `sync_core.py` `_diff_apt_packages` requires only `target_item is not None`, so a package present on the target, absent from the source and named by any pin never produces a `REMOVE` item — and `REPORT_ONLY` items cannot be skip-always'd, so the noise cannot be silenced either. No decision covers suppressing the *removal* direction. Needs a user ruling.
- **Same package surfaced by two managers** (`apt_sync` `REPO_UNAVAILABLE` + `manual_installs_sync` unreproducible) — matrix A11, no test, never decided.
- **Dry-run shows the pre-repository classification**, since it converges nothing and so cannot re-review. Documented in `apt_sync`'s module docstring and `docs/jobs/package-sync.md`.
- **flatpak twin of the contradictory pair** (matrix F9): `_remote_ready_on_target` answers from plan-time state, so an approved remote *removal* plus an approved ref install from it slips the guard.

## 5. Verified this session — do not re-litigate

- #208 D9 **holds**: a system-wide `refresh.hold` does not mask per-snap `held` notes. Proven on a VM once the privileged-read fix landed.
- The #215 flatpak GPG trust fix **survives real Flathub** — options parse, keyring digest read, `--gpg-import` reproducing a byte-identical keyring, and the replicated remote actually installing.
- Keyring resolution: **11 of 20 source files dangling → 0**, re-verified independently against the real `/etc/apt`.
- `snap get system refresh.hold` is admin-gated; the unprivileged read returned `None` unconditionally, which made every sync clear a user's own hold. Fixed in `5e6a9a4c` (pushed).

## 6. Process notes worth carrying

- **Three vacuous tests shipped this phase** before being caught: the seven discover-or-skip VM tests, `test_snap_revision_converges_without_hold`, and `test_key_then_source_then_update_then_package_install`. All three passed while proving nothing. Mutation-check anything load-bearing — break the implementation, confirm the test fails.
- **Tests must create the environment they need.** There is no discover-or-skip convention any more; `tests/integration/scripts/internal/vm-test-fixtures.sh` provisions snaps and real Flathub into the VM baseline. No `pytest.skip` belongs in `tests/integration/jobs/test_package_sync.py`.
- **Agents experiment in docker or on pc1/pc2, never the dev machine.** One removed user flatpak state here.
- The VM lock (`tests/integration/scripts/internal/lock.sh`) leaks on a CI timeout and needs `clear` by hand. The user accepts this; timeouts are the thing to prevent.
- Integration CI only triggers on PRs targeting `main`.
