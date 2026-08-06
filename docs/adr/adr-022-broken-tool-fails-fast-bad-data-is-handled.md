# ADR-022: A tool that did not answer fails fast; a tool that answered is data we handle

Status: Draft

Date: 2026-07-28

## TL;DR

Every command falls into exactly one of two classes — the tool did not answer, or the tool answered and the answer is inconvenient. They get opposite treatments: the first fails the job immediately, naming the command; the second is per-item data the code handles.

## Implementation Rules

**Required**
- Every read feeding a decision MUST be classified at the call site, justified with the command's measured behaviour.
- A read that did not answer MUST raise `ProbeFailed` (`src/pcswitcher/jobs/packages/probes.py`). It MUST NOT be a `ConvergeItemFailed`, MUST NOT be reported against an item, MUST NOT be caught by a per-item loop.
- `ProbeFailed` names the host, the command verbatim, the failing condition, and the tool's own stderr. Once per command, never once per dependent item.
- The discriminator is the command's measured behaviour. Default: non-zero exit. Commands where that is wrong MUST say so at the call site.
- Where the exit code is ambiguous, the COMMAND MUST be reshaped so the code becomes unambiguous — via argument narrowing (naming only cases already established), argument widening (naming one case the tool must answer, as a witness), or an outer test — in preference to parsing output or stderr text.
- Empty result MUST be treated as data unless emptiness is provably impossible. The `answers=` guard is passed only where at least one answer is genuinely owed.
- A wrong request (package apt cannot locate, origin unreplicable, snippet that exits non-zero) stays per-item under ADR-020 D-27.

**Forbidden**
- Blanket "non-zero exit fails the job" — several reads exit non-zero in their normal case.
- Blanket "empty output means broken" — most reads have a legitimate empty answer.
- Reading a failed read's empty result as a manifest.
- Swallowing a read failure into a warning and continuing with a degraded picture.

## Context

The package jobs diff two manifests. Until this ADR, a failed read returned an empty `CommandResult` and code parsed it like any other: `snap list --all` failing on the source emptied the source manifest and turned every snap on the target into an `EXTRA_ON_TARGET` removal proposal. The same shape sat in a dozen other reads.

`477f191e` fixed two apt origin probes and introduced the pattern. This ADR generalises it.

## Decision

### D-01: The two categories

**The tool did not answer.** Transient network failure, held package-manager lock, interrupted dpkg, unreadable status file, snapd not running, flatpak installation unopenable, sudo unavailable. Nothing in pc-switcher explains these; nothing in the run repairs them. **Fail fast.**

**The tool answered, and the answer is inconvenient.** A package apt has never heard of. A repository the source does not declare. A snippet that exits non-zero. **Per-item, continue.**

The line: **did the tool answer the question it was asked**.

One command sits on the line: `apt-get --dry-run`. `E: Unable to locate package` and a held dpkg lock both exit 100, and apt offers no way to separate them. At apply time it stays per-item (per-item loop would report either). At plan time the ambiguity is removed by naming only packages a prior `apt-cache policy` gave a candidate for — so `E: Unable to locate package` cannot occur.

### D-02: `ProbeFailed`, at job level, once

`ProbeFailed` is a `RuntimeError` outside the `ConvergeItemFailed` hierarchy, so it escapes per-item loops. One line names the machine, the command, and the tool's stderr:

> probe on Atlas did not answer — `apt-mark showmanual` exited 100: E: Problem opening /var/lib/dpkg/status

### D-03: Ambiguous exit codes → reshape the command

Three measured shapes:

**Exit code separates cleanly** — `apt-mark showmanual/showhold`, `apt-cache policy`, `snap list --all`, `flatpak list/remotes/mask`, `dpkg-query --show`. Guarded on exit code alone.

**Non-zero is the normal answer** — `sha256sum <glob>` over an empty match, `sha256sum <path>` for an absent file (returns `None`), `dpkg --search` over unowned paths. These are unguarded (say so at the call site) or guarded on the answer instead — `manual_installs_sync._DPKG_OWNERSHIP_WITNESS` adds `/usr/bin/dpkg` to the batch as proof dpkg answered.

**Exit code is ambiguous — reshape** — `find <dir>` exits 1 both for absent and unreadable. `_capture_dir_digests` wraps in `if sudo test -d <dir>`; `_scan_source_file_references` uses `-path` selectors from an always-present root; `_scan_unowned_installs` skips absent roots in the driver. A reshape's test MUST hold the same privilege as the query it wraps.

The plan-time `apt-get --dry-run install` reshapes via ARGUMENTS: naming only packages `apt-cache policy` said the target can resolve.

### D-04: Empty is data unless impossible

Most reads have a legitimate empty answer. `require_answer`'s `answers=` parameter is the exception, passed only where at least one answer is owed — today only `apt-cache policy` over names apt must know: names installed on the machine (`AptProbe.source_policy`, `_scan_no_candidate_apt_packages`) and names this run established have a candidate (`OriginClassifier._verify`).

A recorded judgement: an `apt-cache policy` that answered "I know none" and one that died both print nothing; output cannot separate them. `477f191e` resolved it toward failing fast — misattributing environment failure to every package's provenance is the worse reading. Documented, not eliminated.

### D-05: One accepted residual

`apt-mark showmanual` with `/var/lib/dpkg/status` absent exits **0** and prints nothing — byte-identical to a machine with no manually-installed packages. No `answers=` guard: an empty manual set is legitimate, and a machine whose dpkg status is deleted has larger problems.

### D-06: Job-level failure is the ceiling

A `ProbeFailed` escaping a job fails that job and no more; the orchestrator runs the rest. Package half of **issue #220**; every other exception out of a job still aborts the run.

### D-06a: One read ends the run instead

An unparseable `package-snippets.yaml` raises `SyncAborted` from `packages.state`, not the `SyncAbortedByUser` subclass (nobody was asked). The USER can repair it, the fix has to land before the next sync reads it, and a passing sync with `manual_installs_sync` down reads as green rather than "go fix this file". Absent or empty registry stays ordinary data.

### D-07: Shared mechanism, no shared base class

Free functions in `packages/probes.py`, alongside `apt_policy.py`. ADR-020 D-15/D-16 forbids a base class that only some jobs supply inputs for. `apt_sync` keeps a thin `commands.require_apt_answer` wrapper; the others call `require_answer` directly.

## Consequences

**Positive**
- Inverted-diff defects (every snap proposed for removal because one read failed) closed at every site.
- User sees one line naming what broke, not a screen of consequences.
- apt collateral protection can no longer switch itself off silently.
- Keyring GC can no longer delete keys in use because the scan came back empty.
- Repository-conflict review can no longer show two empty panes.

**Negative**
- One broken read costs its whole job.
- Two commands are shaped for their exit code rather than the shortest form; a future "simplification" reintroduces the ambiguity. Call-site comments warn.
- `answers=` can fail a run that had nothing wrong on three `apt-cache policy` probes.
- Classification is per command; a tool that changes exit-code behaviour breaks the classification silently.

## Alternatives Considered

- **Blanket "non-zero fails the job"** — rejected on measurement.
- **Blanket "empty means broken"** — rejected: legitimate empty answers exist.
- **Parsing stderr to disambiguate** — rejected: locale-dependent, and reshape removes the ambiguity.
- **Warn-and-continue on failed reads** — rejected: that is the current defect.
- **Guard as a `PackageSyncJob` method** — rejected per ADR-020 D-15/D-16.
- **Retry before failing** — deferred to #220.

## References

- ADR-020, ADR-014
- `477f191e`: the first two sites
- GitHub issue #220
- `src/pcswitcher/jobs/packages/probes.py`
- `docs/system/package-sync.md`
