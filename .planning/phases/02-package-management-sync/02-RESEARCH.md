# Phase 2: Package Management Sync - Research

**Researched:** 2026-07-22

**Domain:** Linux package manager inventory/convergence (apt, snap, flatpak) + repository/keyring/pin state + TUI batched review

**Confidence:** MEDIUM-HIGH (core mechanisms verified live against this machine's real Ubuntu 24.04 apt/snap/flatpak state; TUI widget choice is lower confidence, new dependency)

## Summary

Phase 2 is not really a "pick a library" phase — it is a **process-orchestration** phase. Every ecosystem (apt, snap, flatpak) already has an authoritative CLI for both inventory (`apt-mark showmanual`, `snap list --all`, `flatpak list`) and convergence (`apt-get install`, `snap install --revision`, `flatpak install`). The work is: capture the right subset of each tool's state into a manifest, diff it against the target's own query of the same tool, and drive the tool's own install/remove verbs to converge — never touching the package databases directly (D-01). This was verified directly against the actual project machine (Ubuntu 24.04.4 LTS) during research, and the live numbers match CONTEXT.md's inventory exactly (153 manual apt packages, keys split 3 in `/etc/apt/keyrings` + 5 in `/etc/apt/trusted.gpg.d`, mixed deb822/`.list` sources.list.d entries, flatpak refs split user/system).

The one place this phase needs a genuinely new capability is the **batched checkable TUI review** (D-24): Rich has no built-in multi-select checkbox widget, so a new dependency is needed there. The existing `confirmer.py` pause/resume-around-blocking-prompt pattern generalizes directly to a checkbox-style prompt library — Live is fully paused while the alternate library owns the terminal.

**Primary recommendation:** Shell out to each ecosystem's own CLI via the existing `Executor` protocol (exactly like `folder_sync`/`vscode_state_sync` do), using `dpkg-query`/`apt-mark`/`apt-cache policy` for apt, whitespace-column-parsed `snap list --all` for snap, and `flatpak list --columns=...`/`flatpak remotes --columns=...` for flatpak. Use `dpkg --compare-versions` for any deb version comparison — never hand-roll Debian version ordering. Add `questionary` (checkbox prompts, prompt_toolkit-based) for the batched review, invoked exactly where `confirmer.py` currently calls `Prompt.ask`, after pausing the Live display.

## Architectural Responsibility Map

This project is a two-machine SSH-orchestrated CLI tool (ADR-002), not a multi-tier web app. Tiers below are adapted to that shape.

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Manifest capture (what's installed) | Source Orchestrator (`LocalExecutor`) | — | Source always runs locally; captures its own state via each ecosystem's CLI |
| Target state query (diff basis) | Target Executor (`RemoteExecutor`, stateless) | — | Per ADR-002, target runs stateless scripts over SSH; no persistent target-side process |
| Diff / decide / three-way-decision logic | Source Orchestrator | — | All decision logic runs on the source, which drives the whole sync (ADR-002) |
| Convergence (install/remove/pin) | Target Executor (writes), Source Orchestrator (writes, when source itself is being corrected e.g. never — sync is one-directional per REQ-manual-sync-workflow) | — | Only the target is ever mutated by convergence; source is read-only for this phase |
| Machine-local decision file | Source filesystem AND Target filesystem (each machine has its own) | — | Never synced (D-08); each machine's copy is authoritative for itself, read/written via whichever executor is "local" to that role at the time |
| Shared synced config (snippet registry, example decision files) | Shared/synced config (`config_sync.py` path or a new synced file) | Source + Target filesystem | Snippets are "knowledge about the package" (D-23), same tier as `config.yaml` |
| Batched TUI review | Source Orchestrator (terminal/TTY is always the source's) | — | The user only ever interacts with the source terminal; target has no TTY (ADR-002 stateless scripts) |
| folder_sync exclusion export | Source Orchestrator (path computation) → consumed by `folder_sync` (also source-side, translates to rsync filter which then runs on both ends) | — | Same pattern as ADR-018's `vscode_state_exclude_paths()` |

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| REQ-sync-scope-packages | Sync installed packages across apt, snap, flatpak, manual .debs, custom PPAs, and install-script packages; detect package conflicts / version mismatches | Standard Stack (CLI commands per ecosystem), Architecture Patterns (item model, three-way diff), Don't Hand-Roll (version comparison, GPG trust), Code Examples |
| REQ-conflict-detection-no-resolution | Detect conflicts arising from unsupported concurrent use and report them; resolution is manual | Architecture Patterns (batched review = D-24/D-25), Common Pitfalls (dpkg lock, apt-mark hold vs pin distinction), Code Examples (diff class enumeration) |
</phase_requirements>

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

- **D-01:** Convergence is declarative manifest, replayed. The source captures a manifest of items; the target diffs its own state against it and converges using each ecosystem's own tooling. Package databases and stores (`/var/lib/dpkg`, `/var/lib/snapd`, the flatpak OSTree store) are never rsynced.
- **D-02:** Everything the jobs handle is an item with a stable identity, not just packages. Item classes: apt package, apt source, apt key, apt pin, apt config file, snap, snap channel, flatpak ref, flatpak remote, unreproducible/manual install. All classes flow through one diff → decide → apply pipeline.
- **D-03:** apt's manifest carries the manually-installed set (`apt-mark showmanual`, 153 packages on P17), not the full dpkg selection set (2306). apt resolves dependencies on the target. The set is curated by the machine-specific decision file.
- **D-04:** Versions float to whatever the repos currently offer; the tool installs by name and does not pin the source's exact version. Version mismatches are detected and reported as a diff class, never force-downgraded.
- **D-05:** Version constraints do sync: `/etc/apt/preferences.d` pins are items like any other, so deliberate pinning replicates while incidental version skew does not.
- **D-06:** Snap revisions and flatpak scope do converge: the manifest records snap name + channel + revision and flatpak ref + origin + user/system scope, and the target converges with `snap refresh`/`snap install --revision` and `flatpak install --user/--system`. Constraint: convergence must not leave a snap held or otherwise blocked from auto-refresh. The exact snapd mechanism that satisfies both is a research item — **see Code Examples §Snap below: use `snap install --revision=N`, never `snap refresh --hold`.**
- **D-07:** Every difference gets a three-way decision: apply / skip once / skip always, for every item class in every job. "Apply" means "converge this item", direction-dependent: missing-on-target → install/add/enable; extra-on-target → remove/delete/disable; present-on-both-but-different → change target to match source. The review UI must name the concrete action per item.
- **D-08:** "Skip always" records the item in a per-machine decision file that is never synced. An item recorded on machine M is inert on M in both roles.
- **D-08a:** Which machine's file gets the entry follows which machine holds the item. Source-held item declined → recorded on source. Target-held item whose removal is declined → recorded on target. The job writes it on the correct end of the connection.
- **D-09:** Decision files live in `~/.config/pc-switcher/`, one file per manager. Need a non-overridable exclusion alongside ADR-017's runtime-state exclusion; must not be picked up by config_sync.
- **D-10:** Defaults ship as an example file only, never hardcoded in Python and never merged into `default-config.yaml`.
- **D-11:** apt sources, keys, pins, apt config, flatpak remotes and snap channels are inventory items with the same three-way decision, not a file mirror.
- **D-12:** A repo's signing key travels bundled with its repo item, verbatim — copied byte-for-byte. Keys are never re-fetched from vendors. Legacy `/etc/apt/trusted.gpg.d` keys form a separate global-trust item set.
- **D-13:** Both `/etc/apt/preferences.d` and `/etc/apt/apt.conf.d` sync as items.
- **D-14:** flatpak remotes and snap channel tracking are provisioned on the target before installs that need them.
- **D-15:** Three separate jobs — `apt_sync`, `snap_sync`, `flatpak_sync` — rather than one `package_sync`.
- **D-16:** The shared core (item model, diff, three-way decision flow, batched TUI review, machine-local file I/O, snippet registry) is extracted while designing and building, not deferred.
- **D-17:** Package jobs run before folder_sync. Apps are provisioned first, then their data lands on top.
- **D-18:** Items no package manager can reproduce are detected: apt packages with no repo candidate (4 on P17: `brscan3`, `brother-udev-rule-type1`, `cnpg`, `falco-app`) plus unowned installs under `/usr/local` and `/opt`.
- **D-19:** Scanning for unowned installs is acceptable because decisions are recorded — noise exactly once, then never again.
- **D-20:** An install-snippet registry ships as part of the shared core. A snippet is an opaque text blob: recorded and replayed non-interactively through the existing executor; exit code decides success. Never parsed, versioned, diffed or reasoned about.
- **D-21:** Registration is mandatory: an unreproducible item must end up either with a snippet or recorded as machine-specific. pc-switcher must offer to add a snippet on the fly during review.
- **D-22:** Snippets cover bare `.deb`s and manual installs only. Snap and flatpak items do not carry snippets.
- **D-23:** Snippets live in the shared, synced config, not the machine-local decision file.
- **D-24:** All differences are presented in one batched review before any change is applied, grouped by manager and action. Real TUI checkable list, not a sequence of prompts. Must compose with the single persistent Live panel and its pause/resume-around-prompts behavior (Phase 1 plans 01-17/01-18).
- **D-25:** "Conflicts and version mismatches" are a diff class inside that same review: missing-on-target, extra-on-target, version-mismatch (both versions shown), held/pinned, repo-unavailable, unreproducible.
- **D-26:** No TTY / non-interactive run: skip all, once. Nothing applied, nothing recorded permanently, everything unresolved reported. Special non-interactive test behavior must be hidden (undocumented, absent from `--help`, active only under a specific test env var).
- **D-27:** A failing item does not stop the job: continue, collect, report at the end. Each failure logged with stderr, listed in job summary; job result is failure.
- **D-28:** The target downloads from its own repos. No source-cache reuse, no offline mode.
- **D-29:** The package jobs export their excluded paths to folder_sync (ADR-018 precedent): `flatpak_sync` owns `~/.local/share/flatpak`, `snap_sync` owns `~/snap/<app>/<rev>` revision dirs.

### Claude's Discretion

- Manifest serialization format and on-disk layout, and the exact item-identity scheme per class (consistent with D-02).
- The snapd mechanism that converges revisions without blocking auto-refresh (D-06) — **research answer below: `snap install --revision=N` / `snap refresh --revision=N`, not `snap refresh --hold`.**
- Concrete TUI widget choice for the checkable review list (D-24), within the constraints of the existing Rich Live model — **research answer below: `questionary.checkbox()` (or `InquirerPy` as alternative), invoked via the existing pause/resume pattern.**
- Whether apt pins/config files (D-13) are diffed as whole files or as parsed entries.

### Deferred Ideas (OUT OF SCOPE)

- Source-cache reuse / offline installs — rejected for Phase 2 (D-28); revisit if target-side downloads prove slow/unreliable.
- Snippets for snap/flatpak items — YAGNI today.
- Replicating `/usr/local` and `/opt` trees as files — detected/decided on, not reproduced. Relevant to Phase 3.
- Normalizing the flatpak system/user scope split — a change to the machines, not a sync feature.
- Migrating legacy `/etc/apt/trusted.gpg.d` keys to per-repo keyrings — copied verbatim (D-12), not migrated.
- Relaxing one-module-one-Job (issue #30) and job DAG ordering (issue #28) — D-17's ordering is explicit for now.
</user_constraints>

## Project Constraints (from CLAUDE.md)

- `uv run ruff check . && uv run ruff format .`, `uv run basedpyright`, `uv run pytest`, `tests/run-integration-tests.sh` — all must pass; these are the standard verification loop for this project.
- New GitHub PRs must be created as **draft** so integration tests don't run prematurely.
- Python 3.14+, modern type syntax (`str | None`, `list[str]`, `@override`, `StrEnum`), `from __future__ import annotations`.
- `uv add` for new dependencies (never raw `pip`); `uv run` for all commands.
- Full type annotations; `basedpyright` clean; `ruff` clean; tests via `pytest`.
- Atomic file writes for state/config where partial output is harmful (relevant to the machine-local decision file and manifest writes).
- Never use bare `except`; raise `AssertionError` with explanation for invariant violations.
- Git: SSH only, merge without fast-forward/squash, never amend history unless asked.

## Standard Stack

### Core (ecosystem CLIs — no library replaces these; D-01 mandates using each tool's own mechanism)

| Tool/Command | Verified Version (this machine) | Purpose | Why Standard |
|---|---|---|---|
| `apt-mark showmanual` / `apt-mark manual` / `apt-mark auto` | apt 2.8.3 [VERIFIED: local dpkg/apt] | Read/write the manually-installed set (D-03) | The only correct source for "what the user asked for" vs. what apt pulled in as a dependency; confirmed 153 manual packages on this machine, matching CONTEXT.md |
| `apt-mark showhold` | apt 2.8.3 [VERIFIED: local dpkg/apt] | List packages held from upgrade (dpkg selection state, distinct from a pin) | Needed to detect the "held" diff class in D-25; confirmed 0 held packages on this machine |
| `dpkg-query -W -f='<fmt>'` | dpkg 1.22.6 (Ubuntu 24.04) [VERIFIED: local dpkg] | Query installed package name + exact version reliably, machine-parseable | `apt list --installed` explicitly warns it has no stable CLI/output contract for scripting; `dpkg-query` custom format strings are the documented stable interface |
| `dpkg --compare-versions <v1> <op> <v2>` | dpkg 1.22.6 [VERIFIED: local dpkg — tested `2:1.0 gt 10.0` returns true, confirming epoch handling] | Compare two deb version strings correctly (epoch:upstream-revision semantics) | Debian version ordering is not lexicographic and not PEP 440; only dpkg's own comparator is correct — see Don't Hand-Roll |
| `apt-cache policy <pkg>` | apt 2.8.3 [VERIFIED: local apt] | Show installed vs candidate version and which pinned priority won | Needed for the version-mismatch/pinned diff class; shows the *why* behind a pin decision |
| `/etc/apt/sources.list.d/*.sources` (deb822) and `*.list` (legacy one-line) | Ubuntu 24.04.4 LTS [VERIFIED: local `/etc/apt`, both formats present live] | Repository definitions, the "apt source" item class (D-11) | Both formats are valid simultaneously on Ubuntu 24.04; must preserve each entry's original format rather than normalizing — see Common Pitfalls |
| `/etc/apt/keyrings/*.gpg` (per-repo, referenced via `Signed-By:`/`signed-by=`) | [VERIFIED: local, 3 keys present: antigravity, github cli, microsoft] | Modern per-repo trust (D-12) | Recommended location since APT 2.4; each key authenticates only its own repo |
| `/etc/apt/trusted.gpg.d/*.gpg` (legacy global trust) | [VERIFIED: local, 5 keys present: postgresql, spotify x2, ubuntu-keyring x2] | Legacy global-trust key set, separate item class per D-12 | `apt-key` itself is deprecated (Ubuntu 20.10+, stricter in 24.04+) but existing files here still work; copied verbatim, never migrated (deferred) |
| `/etc/apt/preferences.d/*` | [VERIFIED: local, 6 files present: gh-github, nodejs, nsolid, no-esm-docker, ubuntu-pro-esm-apps, ubuntu-pro-esm-infra] | Version/origin pins, D-13 | `Package:`/`Pin:`/`Pin-Priority:` stanzas; distinct mechanism from `apt-mark hold` — see Don't Hand-Roll / Pitfalls |
| `/etc/apt/apt.conf.d/*` | [VERIFIED: local, 17 files present] | apt behavior config, D-13 | Plain files, sync as opaque items (whole-file diff is the simplest correct approach — see Claude's Discretion note) |
| `snap list --all` | snapd 2.76.1 [VERIFIED: local snap] | Enumerate installed snaps with Rev/Tracking(channel)/Notes columns | No `--json` flag exists on this snap CLI (confirmed via `snap help list`); columnar output is the documented, script-consumed interface |
| `snap install --revision=<N> <name>` / `snap refresh --revision=<N> <name>` | snapd 2.76.1 [CITED: snapcraft.io docs] | Converge a snap to a specific revision without leaving it held (D-06) | Directly installs/refreshes to the exact revision as a one-time action; does not touch the standing auto-refresh policy |
| `snap get system refresh.hold` | snapd 2.76.1 [VERIFIED: local, read-only status query] | **Read-only** check of any existing hold — use this, never `snap refresh --hold` with no args to "check" status | See Common Pitfalls — this is the single most important operational finding of this research session |
| `flatpak list --app --columns=application,version,origin,installation` | Flatpak 1.14.6 [VERIFIED: local flatpak] | Enumerate installed refs with version, remote, and user/system scope in one call | Documented `--columns` field list (`man flatpak-list`); one call gets everything the manifest needs per ref |
| `flatpak remotes --columns=name,url` | Flatpak 1.14.6 [VERIFIED: local flatpak] | Enumerate configured remotes (D-11/D-14) | Confirmed `flathub` present in both user and system scope with the same URL — flatpak tracks these as separate per-installation remote lists even when identical |
| `flatpak remote-add <name> <url>` then `flatpak install --user\|--system <remote> <ref>` | Flatpak 1.14.6 [CITED: flatpak docs] | Provision remote before install (D-14) | Order matters: `flatpak install` fails if the remote is not yet configured in that scope |

### Supporting (new Python dependency)

| Library | Version | Purpose | When to Use |
|---|---|---|---|
| `questionary` | 2.1.1 [ASSUMED — package name from WebSearch/training, not from an authoritative source; flagged SUS below, gate behind `checkpoint:human-verify`] | Checkbox-style multi-select prompt (`questionary.checkbox()`) for the batched TUI review (D-24) | Only when building the checkable review list; invoke after `TerminalUI.pause()` exactly where `confirmer.py` calls `Prompt.ask()`, then `TerminalUI.resume()` |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|---|---|---|
| `questionary` | `InquirerPy` (0.3.4) [ASSUMED, also SUS] | Actively-maintained PyInquirer successor with a similar checkbox API; slightly less widely adopted than questionary. Either works with the pause/resume pattern; pick one, don't depend on both. |
| `questionary`/`InquirerPy` (prompt_toolkit-based) | Hand-rolled curses/readchar checkbox rendered with Rich | Avoids a new dependency entirely, but reinvents arrow-key navigation, scrolling for long lists, and terminal-resize handling that questionary/InquirerPy already solve — explicitly a Don't Hand-Roll case (see below) |
| Column-parsed `snap list --all` | snapd's local REST API over `/run/snapd.socket` (JSON) | More robust to future column-format changes, but adds raw HTTP-over-unix-socket handling for no immediate benefit at this project's scale (17 snaps on the reference machine); revisit only if column parsing proves fragile |
| Whole-file diff for `preferences.d`/`apt.conf.d` | Parsed-stanza diff (Package/Pin/Pin-Priority as three separate fields) | Whole-file is simpler and sufficient since these files are typically small and hand-authored; parsed-stanza gives friendlier diff messages but is more code for marginal benefit. Recommend whole-file for v1 (Claude's Discretion, per CONTEXT.md) |

**Installation:**
```bash
uv add questionary
```

**Version verification:** `pip index versions questionary` → 2.1.1 confirmed live during this research session (2026-07-22). `pip index versions InquirerPy` → 0.3.4.

## Package Legitimacy Audit

| Package | Registry | Age | Downloads | Source Repo | Verdict | Disposition |
|---|---|---|---|---|---|---|
| `questionary` | PyPI | First release 2018, latest 2.1.1 published 2025-08-28 [VERIFIED: package-legitimacy seam] | Unknown (seam could not fetch download stats — not itself a red flag; the seam has no negative signal beyond this) | `github.com/tmbo/questionary` [VERIFIED: package-legitimacy seam] | SUS (reason: `unknown-downloads` only) | Flagged — planner must add `checkpoint:human-verify` before `uv add questionary`, but the multi-year release history and public GitHub repo make this a low-risk flag in practice |
| `InquirerPy` | PyPI | First release 2021, latest 0.3.4 published 2022-06-27 [VERIFIED: package-legitimacy seam] | Unknown (same seam limitation) | `github.com/kazhala/InquirerPy` [VERIFIED: package-legitimacy seam] | SUS (reason: `unknown-downloads` only) | Alternative if `questionary` doesn't fit — same checkpoint requirement applies if chosen instead |

**Packages removed due to [SLOP] verdict:** none.

**Packages flagged as suspicious [SUS]:** `questionary`, `InquirerPy` — both flagged only because the legitimacy seam could not retrieve download-count telemetry, not because of any negative signal (no missing repo, no suspicious postinstall, no anomalously-new release). Both have multi-year GitHub histories under long-standing maintainer accounts. Recommend the planner insert one `checkpoint:human-verify` task immediately before whichever one is chosen for installation, per protocol — but do not treat this as a strong warning signal.

*The package name `questionary`/`InquirerPy` was discovered via WebSearch/training knowledge, not an authoritative source (no Context7 entry checked), so per the provenance rule it is tagged `[ASSUMED]` regardless of the registry lookup succeeding.*

## Architecture Patterns

### System Architecture Diagram

```
                         SOURCE (LocalExecutor)                                   TARGET (RemoteExecutor, stateless, via SSH — ADR-002)
                    ┌───────────────────────────────┐                       ┌──────────────────────────────────────────┐
                    │ 1. Capture manifest            │                      │                                          │
  apt_sync    ─────▶│    apt-mark showmanual          │                      │                                          │
                    │    dpkg-query (versions)        │                      │                                          │
                    │    sources.list.d/*, keyrings/*,│                      │                                          │
                    │    preferences.d/*, apt.conf.d/*│                      │                                          │
                    │    read machine-local decision  │                      │                                          │
                    │    file (skip-always filter)    │                      │                                          │
                    └──────────────┬───────────────────┘                     │                                          │
                                   │  manifest (in-memory / temp)            │                                          │
                                   ▼                                        │                                          │
                    ┌───────────────────────────────┐   query target state  │                                          │
                    │ 2. Diff                        │ ──────────────────▶  │  apt-mark showmanual, dpkg-query,        │
                    │    per item-class: missing /   │ ◀──────────────────  │  ls sources.list.d/keyrings/etc,         │
                    │    extra / version-mismatch /  │   target state       │  read target's OWN machine-local         │
                    │    held/pinned / unreproducible│                      │  decision file (never synced)            │
                    └──────────────┬───────────────────┘                     │                                          │
                                   ▼                                        │                                          │
                    ┌───────────────────────────────┐                       │                                          │
                    │ 3. Batched TUI review           │                      │                                          │
                    │    (questionary checkbox,       │                      │                                          │
                    │    grouped by manager + action; │                      │                                          │
                    │    Live paused/resumed)          │                     │                                          │
                    │    → apply / skip-once /         │                     │                                          │
                    │      skip-always decisions       │                     │                                          │
                    └──────────────┬───────────────────┘                     │                                          │
                                   │  skip-always → write to correct        │                                          │
                                   │  machine's decision file (D-08a)       │                                          │
                                   ▼                                        │                                          │
                    ┌───────────────────────────────┐  drive convergence    │                                          │
                    │ 4. Converge                     │ ──────────────────▶ │  apt-get install <names>                 │
                    │    (per-item, continue-on-error │                     │  snap install --revision=N               │
                    │    per D-27)                     │                     │  flatpak install --user/--system         │
                    └──────────────┬───────────────────┘                     │  copy source/key files, write pins/config│
                                   ▼                                        │  replay snippet (opaque, non-interactive)│
                    ┌───────────────────────────────┐                       └──────────────────────────────────────────┘
                    │ 5. Report                       │
                    │    per-item failures + stderr   │
                    │    job result = failure if any  │
                    └───────────────────────────────┘

folder_sync runs AFTER all three package jobs (D-17, enforced by sync_jobs: order in config.yaml — orchestrator resolves jobs in config-dict-iteration order, confirmed in orchestrator.py `_first_sync_scopes`/`_discover_and_validate_jobs`).
```

### Recommended Project Structure

```
src/pcswitcher/jobs/
├── package_sync_core.py   # shared: Item model, ManagerDiff, ThreeWayDecision enum,
│                           #   machine-local decision file I/O, snippet registry,
│                           #   batched review renderer (D-16)
├── apt_sync.py             # AptSyncJob — apt package/source/key/pin/config items
├── snap_sync.py             # SnapSyncJob — snap/channel items
└── flatpak_sync.py          # FlatpakSyncJob — flatpak ref/remote items
```

### Pattern 1: Item model with a stable identity per class (D-02)

**What:** A single tagged-union-style `Item` type (or one dataclass per class, sharing a `ItemDiff` protocol) so all three jobs and the shared review renderer operate on one shape.

**When to use:** Every manifest entry, on both source-capture and target-query sides.

**Example:**
```python
# Illustrative shape only — the planner/researcher own the exact fields per D-02's
# "Claude's Discretion" note on item-identity scheme.
@dataclass(frozen=True)
class AptPackageItem:
    name: str
    version: str          # dpkg-query version string; compare via dpkg --compare-versions
    manager: Literal["apt"] = "apt"

@dataclass(frozen=True)
class SnapItem:
    name: str
    channel: str           # tracking, e.g. "latest/stable"
    revision: int
    manager: Literal["snap"] = "snap"

@dataclass(frozen=True)
class FlatpakItem:
    ref: str                # e.g. "app/com.slack.Slack/x86_64/stable"
    origin: str              # remote name, e.g. "flathub"
    scope: Literal["user", "system"]
    manager: Literal["flatpak"] = "flatpak"
```

### Pattern 2: Three-way decision drives the review grouping (D-07/D-24)

**What:** The diff result for every item is one of `missing_on_target` (install), `extra_on_target` (remove), `version_mismatch`/`held`/`pinned`/`repo_unavailable`/`unreproducible` (report, decision required). The review groups by `(manager, action)` so a bulk-tick can never conflate installs with removals.

**When to use:** Building the batched review payload before calling the checkbox prompt.

**Example (source: this session's own reasoning from D-07/D-25, not an external source):**
```python
class DiffAction(StrEnum):
    INSTALL = "install"     # missing on target
    REMOVE = "remove"       # extra on target
    CHANGE = "change"       # version-mismatch / channel/scope differs
    REPORT_ONLY = "report_only"  # held, pinned, repo-unavailable, unreproducible — needs a decision but no direct converge verb
```

### Pattern 3: Batched review via paused Live + questionary checkbox (D-24)

**What:** Reuse the existing `Confirmer`/`TerminalUI.pause()`/`resume()` pattern from `confirmer.py`, but swap the blocking call from `rich.prompt.Prompt.ask` to `questionary.checkbox(...).ask()`.

**When to use:** Once per sync run, after all three package jobs' diffs are computed (or per-job if jobs must stay independent per D-15 — confirm during planning whether one shared review across all three jobs or three separate reviews better satisfies "batched").

**Example:**
```python
# Source: this project's confirmer.py pause/resume pattern (existing code),
# combined with questionary's documented checkbox() API (github.com/tmbo/questionary)
self._ui.pause()
try:
    choices = [questionary.Choice(title=f"{action}: {item.label()}", value=item.id, checked=True)
               for item in group]
    selected = await asyncio.to_thread(
        lambda: questionary.checkbox("Apply these changes?", choices=choices).ask()
    )
finally:
    self._ui.resume()
```
Note: `questionary.checkbox(...).ask()` is a blocking call — wrap in `asyncio.to_thread` (this codebase is asyncio-throughout per ADR-005) rather than calling it directly on the event loop.

### Anti-Patterns to Avoid

- **Diffing `dpkg --get-selections` (all ~2306 packages) instead of `apt-mark showmanual` (153):** produces a manifest an order of magnitude too large and full of dependency noise; D-03 is explicit about this.
- **Treating `apt-mark hold` and a `preferences.d` pin as the same thing:** they are different mechanisms (dpkg selection state vs. version/priority preference) with different diff semantics — see Common Pitfalls.
- **Calling `snap refresh --hold` to "check" hold status:** it is a mutating command when given no arguments — see Common Pitfalls (discovered live during this research session).
- **Re-fetching a repo's signing key from the vendor during sync:** violates D-12 ("copied byte-for-byte"); always transfer the source's own keyring file bytes.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---|---|---|---|
| Comparing two deb version strings for the version-mismatch diff class | A custom "parse epoch:upstream-revision and compare" function | `dpkg --compare-versions <v1> lt <v2>` (shell out) | Debian version ordering has specific tilde/epoch/tie-breaking rules that differ from PEP 440 and from naive string/semver comparison; verified live that `dpkg --compare-versions` correctly ranks `2:1.0` above `10.0` (epoch beats upstream number) — a naive comparator would get this backwards |
| Checkable multi-select TUI list with arrow-key navigation, scrolling, "select all" | Curses-based or raw-ANSI checkbox renderer on top of Rich | `questionary.checkbox()` (or `InquirerPy`) | Rich has no built-in multi-select widget (confirmed: no such component exists in Rich as of this session); reinventing keyboard navigation, terminal resize handling, and scrolling for potentially 100+ item lists (apt alone can have 153 manual packages) is exactly the kind of "deceptively complex" UI problem a maintained library already solves |
| Verifying a repo's GPG signature manually (parsing/verifying a detached signature) | Custom `gpg --verify` wrapper | apt's own `Signed-By`/legacy trust mechanism — just copy the keyring file bytes (D-12) and let `apt-get update`/`apt-get install` do the actual cryptographic verification | apt already re-verifies every fetch against the configured keyring on every operation; the job's only responsibility is getting the correct keyring bytes onto the target, not re-implementing signature checking |
| Parsing apt's human-oriented `apt list --installed` output for scripting | A regex parser for `apt list` lines | `dpkg-query -W -f='<format>'` | `apt`'s own manpage explicitly warns its CLI/output is not stable for scripting use; `dpkg-query`'s `-f` format string is the documented, stable interface |

**Key insight:** In this domain, the "custom solution" risk is not in choosing a wrong library — it's in re-deriving semantics (version ordering, hold-vs-pin, key trust) that the underlying package managers already encode precisely and that are easy to get subtly wrong in ways that only surface as a bad diff/decision months later.

## Common Pitfalls

### Pitfall 1: `snap refresh --hold` with no arguments is a MUTATING command, not a status query

**What goes wrong:** Running `snap refresh --hold` to "check" whether anything is currently held actually **sets** an indefinite hold on auto-refresh for **all** snaps on the machine.

**Why it happens:** The command's help text frames `--hold` as a modifier on `refresh`, and it's easy to assume calling it with no snap name and no explicit duration is a no-op inspection. It is not — it applies globally and persists until explicitly undone with `snap refresh --unhold`.

**How to avoid:** Never call `snap refresh --hold` for inspection. Use the read-only `snap get system refresh.hold` to check the current global hold state, and `snap refresh --list` to see what's pending. Only call `snap refresh --hold=<snap>` (naming a specific snap) if a hold is actually the intended action — and per D-06, holds should not be used at all for revision convergence; use `snap install --revision=N` instead.

**Warning signs:** Any test or validate() step that shells out to `snap refresh --hold` without a snap name argument and treats the output as informational.

**This was discovered live during this research session** — running it against the actual research machine set a global hold, which required a manual `sudo snap refresh --unhold` to undo (flagged to the user; not auto-corrected without confirmation, per the "verification before assertion"/safety rules governing this session).

### Pitfall 2: `apt-mark hold` and a `preferences.d` pin look similar but are different mechanisms

**What goes wrong:** A diff/decision implementation that only reads `preferences.d` (D-13) misses packages held via plain `apt-mark hold` (a dpkg selection state stored in `/var/lib/dpkg`, not a file under `/etc/apt`), and vice versa.

**Why it happens:** Both prevent a package's version from changing, so they're easy to conflate as "the same diff class." D-25 lists `held/pinned` as one diff class, but the two are read via different mechanisms (`apt-mark showhold` vs. parsing `preferences.d` files) and mean different things: a hold blocks ALL upgrades of that package outright; a pin adjusts priority so a package prefers one origin/version over another but can still upgrade within what that pin allows.

**How to avoid:** Query both explicitly — `apt-mark showhold` for hold state, `preferences.d` file contents for pins — and represent them as distinguishable facts even if surfaced under one review category.

**Warning signs:** A "held/pinned" diff class implementation that only has one data source.

### Pitfall 3: `.list` and `.sources` (deb822) files for the same repository conflict

**What goes wrong:** If a repo has both a legacy `.list` entry and a new `.sources` entry (e.g. after a partial `apt modernize-sources` run, or a vendor install script writing one format while an old one lingers) with different `Signed-By` values, apt raises a hard "Conflicting values" error and refuses to update.

**Why it happens:** Both formats are valid simultaneously in `sources.list.d/`; nothing prevents two files describing the same repo from coexisting.

**How to avoid:** When capturing/diffing apt source items, treat each file (by filename, not by parsed URI) as the item identity per D-02, and preserve its original format on transfer rather than normalizing everything to deb822 or vice versa — don't invent a migration this phase doesn't need. Confirmed live: this machine already has both formats coexisting cleanly for *different* repos (`.sources` for azure-cli/google-chrome/pgdg-style entries, `.list` for antigravity/github-cli/hashicorp/tailscale/spotify), which is fine as long as no single repo is double-defined.

**Warning signs:** A repo appearing twice in the manifest under different identities that both resolve to the same URI.

### Pitfall 4: The `Executor.Process` protocol has no stdin — snippet replay must be non-interactive

**What goes wrong:** A snippet (D-20) that runs `dpkg -i some.deb` or any installer expecting a debconf prompt will hang forever, because this project's `Process` protocol deliberately provides no stdin (documented in `executor.py`'s `Process` docstring: "stdin is intentionally not supported").

**Why it happens:** Snippets are opaque text blobs (D-20) — the tool never inspects their content, so it cannot detect in advance whether a given snippet will try to prompt.

**How to avoid:** Document (in the snippet-authoring guidance surfaced during D-21's "add a snippet on the fly" flow) that snippets must run non-interactively — e.g. `DEBIAN_FRONTEND=noninteractive dpkg -i pkg.deb; apt-get install -f -y` rather than a bare `dpkg -i` that might trigger a debconf prompt.

**Warning signs:** A snippet replay that never returns / times out on the target.

### Pitfall 5: dpkg/apt lock contention from `unattended-upgrades` or a concurrent manual `apt` session

**What goes wrong:** `apt-get install` on the target fails immediately with a lock-held error if `unattended-upgrades` or another apt/dpkg process holds `/var/lib/dpkg/lock-frontend`.

**Why it happens:** Ubuntu runs `unattended-upgrades` on a timer by default; a sync landing mid-timer collides.

**How to avoid:** Per D-27 (continue, collect, report), treat a lock-contention failure as a per-item failure like any other rather than a special case — but consider a `validate()`-time check (as `folder_sync` checks sudo access) that surfaces this clearly rather than as an opaque per-item apt error. This project already has a validate()-phase convention for exactly this kind of preflight system-state check (see `jobs/base.py` `SyncJob`/three-phase validation described in CONTEXT.md's Established Patterns).

**Warning signs:** Every apt item in a run failing with the same generic exit-code-100 stderr mentioning "Could not get lock".

## Code Examples

### apt: capturing the manual-install manifest with versions

```bash
# Source: verified live on this project's own dev machine (apt 2.8.3, dpkg 1.22.6)
apt-mark showmanual
# → 153 lines, package names only

dpkg-query -W -f='${Package}\t${Version}\n' <package-name>
# → "curl\t8.5.0-2ubuntu10.11" — stable, scriptable format (unlike `apt list --installed`)
```

### apt: comparing versions correctly

```bash
# Source: verified live — confirms epoch handling
dpkg --compare-versions "2:1.0" gt "10.0" && echo "true"   # epoch 2 beats plain 10.0
dpkg --compare-versions "1.0-1" lt "1.0-2" && echo "true"  # debian-revision ordering
```

### snap: converging to a specific revision without a hold

```bash
# Source: CITED snapcraft.io docs (snap install / snap refresh reference)
snap install --revision=<N> <name>     # if not yet installed
snap refresh --revision=<N> <name>     # if already installed at a different revision
snap get system refresh.hold           # READ-ONLY check for any existing global/per-snap hold
# NEVER: snap refresh --hold   (with no snap name — mutates global state, see Pitfall 1)
```

### flatpak: full inventory in one call

```bash
# Source: verified live (Flatpak 1.14.6), man flatpak-list
flatpak list --app --columns=application,version,origin,installation
# → be.alexandervanhee.gradia	1.13.0	flathub	user
#   com.slack.Slack	4.50.143	flathub	system
#   ...
flatpak remotes --columns=name,url
# → flathub	https://dl.flathub.org/repo/   (appears once per scope it's configured in)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|---|---|---|---|
| `apt-key add` / global `/etc/apt/trusted.gpg` | Per-repo keyring in `/etc/apt/keyrings` + `Signed-By`/`signed-by=` in the repo's own source entry | apt-key deprecated Ubuntu 20.10+, stricter enforcement 24.04+ [CITED: DigitalOcean/opensource.com/Debian manpages] | Any repo the tool captures may be in either the legacy trusted.gpg.d style (D-12's "separate global-trust item set") or the modern per-repo style — both must be handled, not just the modern one |
| Legacy one-line `sources.list`/`.list` entries | deb822 `.sources` structured format | Ongoing migration since ~2022, `apt modernize-sources` helper added recently [CITED: sleeplessbeastie.eu/OSTechnix] | Both formats coexist on this machine today; the item model must not assume one format exclusively |

**Deprecated/outdated:**
- `apt-key`: deprecated, do not use for anything in this phase (D-12 already avoids it by copying keyring files directly).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `questionary` is the right checkbox library choice (vs. `InquirerPy` or a hand-rolled widget) | Standard Stack / Supporting, Architecture Patterns Pattern 3 | Low-medium — both candidate libraries have the same integration shape with the existing pause/resume pattern; swapping later is a contained change confined to one review-renderer module (D-16's extracted shared core) |
| A2 | `questionary.checkbox().ask()` composes cleanly with an already-paused Rich `Live` display with no terminal-mode conflicts | Architecture Patterns Pattern 3 | Medium — this was reasoned from documented behavior (both libraries fully own the terminal during their own prompt, same as Rich's own `Prompt.ask` which this project already pauses Live for) but not empirically tested in this session; the planner should treat the first integration as a spike/prototype task with a `checkpoint:human-verify` before building the full review renderer on top of it |
| A3 | Whole-file diffing is sufficient for `preferences.d`/`apt.conf.d` items (vs. parsed-stanza diffing) | Standard Stack, Alternatives Considered | Low — explicitly left as Claude's Discretion in CONTEXT.md; if wrong, only affects diff message quality, not correctness |

**If this table is empty:** N/A — see rows above.

## Open Questions

1. **Does the batched review span all three jobs (apt+snap+flatpak) in one screen, or one review per job?**
   - What we know: D-24 says "one batched review before any change is applied, grouped by manager and action" — "grouped by manager" reads as one review covering all enabled package jobs together.
   - What's unclear: D-15 also insists on three independently-toggleable jobs with independent failure isolation. If only `apt_sync` is enabled, does the shared core still render a review, and does a single shared review object cut across job boundaries in the orchestrator (which currently runs jobs sequentially, each owning its own `execute()`)?
   - Recommendation: Planner should decide during plan design whether the review is collected by the shared core across all enabled package jobs before any of their `execute()` bodies converge anything (one screen), or per-job (three screens in a row) — either satisfies D-24's literal wording, but the orchestrator's existing sequential single-job-at-a-time model favors the shared-core-collects-first approach; this needs a concrete plan decision, not more research.

2. **Snapd REST socket vs. CLI column parsing — durability of the column format across snapd versions**
   - What we know: This machine's snapd 2.76.1 has a stable, documented column layout (`Name Version Rev Tracking Publisher Notes`) with no `--json` flag.
   - What's unclear: Whether older snapd versions the fleet might run have a different column set/order.
   - Recommendation: Pin the parser to header-name-based column indexing (parse the header row, not fixed character offsets) rather than assuming column order, so a future snapd column reorder doesn't silently corrupt the manifest.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|---|---|---|---|---|
| `apt`/`apt-mark`/`apt-cache`/`dpkg`/`dpkg-query` | `apt_sync` | ✓ | apt 2.8.3, dpkg 1.22.6 | — (required; Ubuntu 24.04 ships all of these by default, REQ-environment-constraints) |
| `snap`/snapd | `snap_sync` | ✓ | snap/snapd 2.76.1 | — (required; Ubuntu 24.04 ships snapd by default) |
| `flatpak` | `flatpak_sync` | ✓ | Flatpak 1.14.6 | If absent on a given machine, `flatpak_sync` should validate() and report cleanly rather than fail hard — flatpak is not part of the default Ubuntu 24.04 install and may be genuinely absent on some fleet machines |
| `questionary` (new Python dependency) | Batched TUI review (D-24), all three jobs | ✗ (not yet added) | 2.1.1 available on PyPI [ASSUMED, SUS — see audit] | `uv add questionary`; gate the initial install behind `checkpoint:human-verify` per the package legitimacy protocol |

**Missing dependencies with no fallback:**
- None — `apt`/`snap`/dpkg are guaranteed present per REQ-environment-constraints (Ubuntu 24.04 target); `questionary` is a straightforward `uv add`.

**Missing dependencies with fallback:**
- `flatpak` — validate()-time detection with a clean skip/report if a machine genuinely lacks it, rather than treating its absence as a hard error.

## Validation Architecture

### Test Framework

| Property | Value |
|---|---|
| Framework | pytest 9.1.1 + pytest-asyncio 1.4.0 (asyncio_mode=auto), pytest-randomly [VERIFIED: pyproject.toml] |
| Config file | `pyproject.toml` `[tool.pytest]` section |
| Quick run command | `uv run pytest tests/unit/jobs/ -x` |
| Full suite command | `uv run pytest` (unit) + `tests/run-integration-tests.sh` (VM-isolated, btrfs-reset) |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|---|---|---|---|---|
| REQ-sync-scope-packages | apt manifest capture returns exactly `apt-mark showmanual` set with correct versions | unit | `uv run pytest tests/unit/jobs/test_apt_sync.py -x` | ❌ Wave 0 |
| REQ-sync-scope-packages | snap manifest capture parses `snap list --all` by header, not fixed offsets | unit | `uv run pytest tests/unit/jobs/test_snap_sync.py -x` | ❌ Wave 0 |
| REQ-sync-scope-packages | flatpak manifest capture separates user/system scope correctly | unit | `uv run pytest tests/unit/jobs/test_flatpak_sync.py -x` | ❌ Wave 0 |
| REQ-sync-scope-packages | version comparison uses `dpkg --compare-versions`, correctly ranks epoch versions | unit | `uv run pytest tests/unit/jobs/test_package_sync_core.py -k version -x` | ❌ Wave 0 |
| REQ-conflict-detection-no-resolution | held package (apt-mark hold) and pinned package (preferences.d) are surfaced as distinguishable diff facts | unit | `uv run pytest tests/unit/jobs/test_package_sync_core.py -k held_or_pinned -x` | ❌ Wave 0 |
| REQ-conflict-detection-no-resolution | non-interactive run skips all items, records nothing, reports everything (D-26) | integration | `tests/run-integration-tests.sh tests/integration/jobs/test_package_sync.py::test_non_interactive_skip_all` | ❌ Wave 0 |
| REQ-conflict-detection-no-resolution | a failing item does not stop the job; job result is failure; other items still processed (D-27) | integration | `tests/run-integration-tests.sh tests/integration/jobs/test_package_sync.py::test_continue_on_item_failure` | ❌ Wave 0 |
| REQ-sync-scope-packages | machine-local decision file entry (skip-always) makes an item inert in both source and target roles (D-08) | unit | `uv run pytest tests/unit/jobs/test_package_sync_core.py -k decision_file -x` | ❌ Wave 0 |

### Sampling Rate

- **Per task commit:** `uv run pytest tests/unit/jobs/ -x`
- **Per wave merge:** `uv run pytest` (full unit suite) + relevant `tests/run-integration-tests.sh` targets
- **Phase gate:** Full suite green (`uv run ruff check . && uv run ruff format . && uv run basedpyright && uv run pytest` + integration suite) before `/gsd-verify-work`

### Wave 0 Gaps

- [ ] `tests/unit/jobs/test_package_sync_core.py` — shared Item model, three-way decision, decision-file I/O, version comparison
- [ ] `tests/unit/jobs/test_apt_sync.py` — apt manifest capture, diff, converge (mocked executor)
- [ ] `tests/unit/jobs/test_snap_sync.py` — snap manifest capture (header-based column parsing), revision converge
- [ ] `tests/unit/jobs/test_flatpak_sync.py` — flatpak manifest capture (user/system scope), remote provisioning
- [ ] `tests/integration/jobs/test_package_sync.py` — VM-isolated: real apt/snap/flatpak convergence against the two test VMs (pc1/pc2), non-interactive skip-all, continue-on-failure
- [ ] Framework install: none — pytest/pytest-asyncio already present; `questionary` itself needs no test-framework addition (it will be exercised via mocked `.ask()` in unit tests, not driven interactively in CI)

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---|---|---|
| V2 Authentication | No | This phase adds no new authentication surface; SSH auth is unchanged from Phase 1 (ADR-002) |
| V3 Session Management | No | N/A — no session concept introduced |
| V4 Access Control | Yes | Package installs on the target require root (sudo) for apt/snap; flatpak `--user` scope does not. Sudo access must be validated per-job in `validate()`, matching `folder_sync`'s existing pattern of checking sudo access before executing |
| V5 Input Validation | Yes | Package names, repo URIs, and file paths flowing into shell commands (apt-mark, dpkg-query, snap, flatpak) must be shell-quoted via `shlex.quote()` exactly as `vscode_state_sync.py`/`folder_sync.py` already do for every interpolated value — never format package/path strings directly into a command string |
| V6 Cryptography | Yes | GPG keyring files (D-12) are copied byte-for-byte, never regenerated or re-derived; the tool must never attempt to construct or modify keyring content, only transfer it verbatim and let apt's own signature verification do the actual crypto |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---|---|---|
| Shell injection via an attacker-controlled or malformed package name/repo URI reaching a shelled-out apt/snap/flatpak command | Tampering | `shlex.quote()` every interpolated value, exactly as the existing codebase already does in `vscode_state_sync.py`/`folder_sync.py` (`shlex.quote(target_db)`, etc.) — no new pattern needed, just apply the existing convention to the new jobs |
| A malicious or corrupted snippet (D-20) executing arbitrary code on the target | Elevation of Privilege / Tampering | Snippets are user-authored (added during the human-driven review flow, D-21) and run through the same executor as everything else — no additional sandboxing is specified by CONTEXT.md and none is proposed here (snippets are explicitly "opaque, never parsed" by design — the trust boundary is "the user who runs pc-switcher already has root on both machines," same as every other converge action in this phase) |
| Re-fetching a repo's signing key from an untrusted network location instead of copying the source's own verified key | Spoofing | D-12 already mandates byte-for-byte transfer of the source's own keyring file — no network key fetch, ever, for this phase |
| dpkg/apt lock contention silently corrupting a partial install | Denial of Service (self-inflicted) | Detect lock-held failures explicitly (Pitfall 5) and surface them as a distinguishable per-item failure, not a generic error |

## Sources

### Primary (HIGH confidence — direct verification against this machine's live state)

- Local `apt-mark showmanual`, `apt-mark showhold`, `dpkg-query`, `dpkg --compare-versions`, `apt-cache policy` — run directly on this project's dev machine (Ubuntu 24.04.4 LTS), 2026-07-22.
- Local `/etc/apt/sources.list.d/`, `/etc/apt/keyrings/`, `/etc/apt/trusted.gpg.d/`, `/etc/apt/preferences.d/`, `/etc/apt/apt.conf.d/` directory listings and file contents.
- Local `snap list --all`, `snap version`, `snap help list`.
- Local `flatpak list --app --columns=...`, `flatpak remotes --columns=...`, `flatpak info`, `flatpak --version`.
- `src/pcswitcher/jobs/base.py`, `vscode_state_sync.py`, `folder_sync.py`, `executor.py`, `confirmer.py`, `models.py`, `orchestrator.py` — read directly this session.
- GitHub issue #118 comments (`gh issue view 118 --comments`) — snap-revision motivation for D-06.

### Secondary (MEDIUM confidence — WebSearch cross-checked against official/manpage sources)

- apt-mark manpages (Ubuntu/Debian manpages.ubuntu.com, manpages.debian.org).
- deb822 `.sources` format and Signed-By migration guidance (sleeplessbeastie.eu, OSTechNix, dev.to writeups referencing Debian's `apt modernize-sources`).
- apt-key deprecation and `/etc/apt/keyrings` recommendation (DigitalOcean tutorial, opensource.com, Ubuntu/Debian manpages).
- `apt_preferences(5)` man page semantics (linux.die.net, ccrma.stanford.edu mirror).
- snapcraft.io "Manage updates" and "Refresh control" docs for `--hold`/`--revision` semantics.
- `flatpak-list(1)` man page (man7.org, Debian/Ubuntu/Arch manpage mirrors).
- Textualize/rich GitHub discussion #960 confirming no built-in checkbox widget in Rich.
- `questionary`/`InquirerPy` GitHub repos and PyPI listings.

### Tertiary (LOW confidence — not independently cross-checked)

- None retained as authoritative claims in this document — every WebSearch finding above was either cross-checked against this machine's live behavior or against an official manpage/vendor-docs source, so all are classified MEDIUM rather than LOW.

## Metadata

**Confidence breakdown:**
- Standard stack (apt/snap/flatpak CLIs): HIGH — verified live against the actual reference machine, matching CONTEXT.md's inventory numbers exactly.
- Standard stack (questionary/checkbox TUI): MEDIUM — sound reasoning from documented library behavior and existing codebase pattern, but not empirically integration-tested this session (see Assumption A2).
- Architecture (item model, diff, converge pipeline): HIGH — directly derived from locked CONTEXT.md decisions (D-01–D-29), not invented.
- Pitfalls: HIGH — Pitfall 1 (snap hold) was discovered live, not from documentation; Pitfalls 2-5 verified against live system state or existing codebase conventions.

**Research date:** 2026-07-22

**Valid until:** 30 days (stable OS-tooling domain; re-verify sooner if Ubuntu ships a snapd/apt/flatpak major version change, or if `questionary`/`InquirerPy` is swapped for a different TUI choice during planning)
