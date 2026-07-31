# ADR-022: A tool that did not answer fails fast; a tool that answered is data we handle

Status: Draft

Date: 2026-07-28

## TL;DR

Every command pc-switcher runs falls into exactly one of two classes — the tool or its environment failed to answer, or the tool answered and the answer is inconvenient — and they get opposite treatments: the first fails the job immediately, naming the command, and the second is data the code is responsible for handling, including when the answer is "nothing".

## Implementation Rules

**Required:**
- Every READ whose result feeds a decision MUST be classified into one of the two categories at the call site, and the classification MUST be justified with the measured behaviour of that specific command.
- A read that did not answer MUST raise `ProbeFailed` (`src/pcswitcher/jobs/packages/probes.py`). It MUST NOT be a `ConvergeItemFailed`, MUST NOT be reported against an item, and MUST NOT be caught by a per-item loop.
- A `ProbeFailed` message MUST name the host, the command verbatim, the failing condition, and the tool's own stderr when there is one. It fails ONCE for the command, never once per item that depended on it.
- The discriminator MUST be the command's own measured behaviour. The default discriminator is a non-zero exit code; a command for which that is wrong MUST say so in a comment at the call site and use something else or nothing.
- Where the exit code is ambiguous because a legitimate state and a real failure share it, the COMMAND MUST be reshaped so the exit code becomes unambiguous, in preference to guessing from output or from stderr text. Reshaping includes narrowing the command's ARGUMENTS to the cases the run has already established the tool can answer, and widening them with one case the tool must answer, whose absence from the reply proves it did not — both available where its syntax offers nothing.
- An empty result MUST be treated as data unless emptiness is provably not a state the machine can be in. The `answers=` guard exists for those cases and MUST be passed only where at least one answer is genuinely owed.
- A request that is wrong — a package that cannot be installed, an origin that cannot be replicated, a snippet that exits non-zero — stays a per-item failure under ADR-020 D-27 and MUST NOT be promoted to a job failure.

**Forbidden:**
- No blanket "non-zero exit fails the job" rule applied without checking the command. Several reads in this codebase exit non-zero in their normal case.
- No blanket "empty output means broken" rule. Most of these commands have a legitimate empty answer, and turning it into a failure breaks ordinary machines.
- No reading a failed read's empty result as a manifest. That is the specific defect this ADR exists to close.
- No swallowing a read failure into a warning and continuing with a degraded picture.

## Context

The four package jobs diff two manifests and propose the difference. Until this ADR, a read that failed returned an empty `CommandResult` and the code parsed it like any other, so a failed capture read as "that machine has nothing" and inverted the whole diff. A `snap list --all` that failed on the source emptied the source manifest, which turned every snap on the target into an `EXTRA_ON_TARGET` removal proposal; the only thing between that and a wiped target was that removal groups arrive unticked. The same shape sat in a dozen other reads, including the one that carries apt's collateral protection.

`477f191e` fixed two apt origin probes and introduced the pattern. This generalises it and states the boundary, because the boundary is the part that is easy to get wrong in both directions.

## Decision

### D-01: The two categories

A command call has two failure modes, and they are not the same kind of event.

**The tool or its environment did not answer.** A transient network failure, a package-manager lock held by `unattended-upgrades`, an interrupted dpkg, an unreadable `/var/lib/dpkg/status`, an unparsable `apt.conf.d`, a snapd that is not running, a flatpak installation that cannot be opened, sudo that is not available. Nothing about pc-switcher's data or logic explains any of these, nothing in the run can repair them, and every conclusion drawn from the result is unfounded. This fails fast.

**The tool answered, and the answer is not what the run wanted.** A package the target's apt has never heard of. A repository the source does not declare. An install that exits non-zero because its repository was never added. A snap that is sideloaded and cannot be reproduced. A snippet the user's own script fails on. Each of these is a fact about one item, produced by a tool that was working; deciding what to do with it is the whole job. These are handled, reported per item, and the run continues (ADR-020 D-27).

The line is not "did the command succeed". It is **did the tool answer the question it was asked**.

One command sits on the line: `apt-get --dry-run`. Measured in a stock `ubuntu:24.04`, a name apt cannot locate exits **100** with `E: Unable to locate package`, which is the same exit code a held dpkg lock produces, and no rewrite of the command's syntax separates them, because apt offers no second code and no second mode.

At **apply time** it stays on the per-item side. The command simulates one approved install or removal, apt's refusal is a fact about that request (ADR-020 D-27), and a lock met there cannot be undone by failing fast — the items already converged stay converged.

At **plan time** the ambiguity is removed instead of classified, by D-03's remedy applied to the command's ARGUMENTS rather than its syntax. The rehearsal names only the packages the target's `apt-cache policy` gave a candidate for one command earlier in the same run, so `E: Unable to locate package` cannot be the cause of a failure there (`apt_sync.origins.OriginClassifier.target_resolvable`). An ADR-020 D-34 class-3 install — the repository that supplies it is written during converge, so the target's apt has never heard the name — is excluded from the rehearsal rather than tolerated inside it, because apt refuses the whole batch on one such name and would take every other package's collateral protection down with it.

What can still fail there is a lock, a broken apt, or a candidate set apt cannot resolve — unmet dependencies, or a conflict between two approved packages. The last is data about the request, so `ProbeFailed` remains the wrong type; and plan time has no per-item loop to report any of them against. The `ConvergeItemFailed` therefore aborts the plan, which is the right outcome: a rehearsal that did not happen must not be reported as a clean one.

### D-02: Fail fast means `ProbeFailed`, at job level, once

`ProbeFailed` (`packages/probes.py`) is a `RuntimeError` deliberately outside the `ConvergeItemFailed` hierarchy, so it escapes the per-item loops in `PackageSyncJob.apply()` and propagates out of `execute()`. One type across all four jobs, because the code that must react to it is the orchestrator, which has no reason to care which manager's read went dark.

What the user sees is one line naming the machine by hostname (`PKG-FR-NAME-THE-MACHINES`), the command as it was run, why it did not answer, and the tool's own stderr:

> probe on Atlas did not answer — `apt-mark showmanual` exited 100: E: Problem opening /var/lib/dpkg/status

Naming the command verbatim is the point. The failure the old behaviour produced was a screenful of package removals with the real cause appearing nowhere.

### D-03: Where the boundary is drawn when a command is ambiguous

The exit code is the default discriminator and it is right for most of these commands, but it is a property of the command, not a law. Three shapes occur, and they were measured rather than assumed.

**The exit code separates cleanly.** `apt-mark showmanual`/`showhold` and `apt-cache policy` exit 100 when apt cannot read the status file or parse `apt.conf.d`, and 0 with the real answer otherwise — including exit 0 for a package name apt has never heard of, which is why a non-zero exit from apt is never a statement about a package. `snap list --all` exits 1 when snapd is unreachable and 0 when snapd reports zero snaps. `flatpak list`, `flatpak remotes` and `flatpak mask` exit 1 when the installation cannot be opened or its config cannot be parsed, and 0 when the machine simply has none of what was asked for. `dpkg-query --show` exits 1 when its admin directory is unreadable. `sudo cat <path>` exits 1 on an absent or unreadable path and nothing else makes it exit non-zero, and every path `apt_sync` `cat`s was named by a digest capture root ran moments earlier — so the file provably exists and root can read it, and a non-zero exit is only ever a real failure. These reads are guarded on the exit code alone.

**The exit code is meaningless because non-zero is the normal answer.** `sha256sum <glob>` over a glob that matches nothing exits 1, which is what a flatpak scope with no remote keyring looks like. `sha256sum <path>` over a single path exits 1 when the file is absent, which is `apt_sync`'s way of learning that a machine has no `/etc/apt/sources.list` — its absence IS the answer `apt_sync.probe.capture_file_digest` is asked for, and it returns `None` rather than guarding. `dpkg --search <paths>` exits 1 as soon as one queried path is unowned, which is precisely the finding `manual_installs_sync`'s unowned scan is looking for. Guarding any of these on the exit code would fail every run on an ordinary machine. The two `sha256sum` reads are therefore unguarded and say so at the call site; `dpkg --search` is guarded on its answer instead — `manual_installs_sync._DPKG_OWNERSHIP_WITNESS` puts `/usr/bin/dpkg`, which dpkg owns on every machine, into the same batch, and a reply that does not claim it did not come from a dpkg that answered. Without that, a dead `dpkg --search` prints nothing, every candidate looks unowned, and the user is asked to write an install snippet for every entry under `/opt` and `/usr/local`.

**The exit code is ambiguous, so the command is reshaped.** `find <dir>` exits 1 both when the directory is absent — a legitimate machine with no `/etc/apt/preferences.d` — and when it cannot be read. Neither reading of that exit code is acceptable: failing breaks ordinary machines, accepting it reads an unreadable directory as "that machine has no repositories" and offers every file on the other machine for removal. So the ambiguity is removed at the source: `_capture_dir_digests` wraps its `find` in `if sudo test -d <dir>; then ... fi`, `_scan_source_file_references` walks the single always-present `/etc/apt` with `-path` selectors instead of naming a possibly-absent `/etc/apt/sources.list` as a start point, and `manual_installs_sync._scan_unowned_installs` drives one `find` per scan root from a loop that skips a root that is not there, so an absent `/opt` never reaches the exit code. In both cases absence now answers "nothing" at exit 0, and a non-zero exit means only a real failure. Reshaping the command is preferred to parsing stderr text, which is locale-dependent and would make the classification a string match.

A reshaped command's test must hold the same privilege as the query it wraps, or the reshape introduces the ambiguity it removed: measured, an unprivileged `test -d` on a directory inside an unsearchable parent exits 1, collapsing the whole `if` to exit 0 with no output — "this machine has no pins or keys" for a directory root would have listed.

The plan-time `apt-get --dry-run install` is the third instance, and the one whose syntax offers no handle at all, so its ARGUMENTS carry the reshape instead (D-01): the batch names only packages the target's own `apt-cache policy` gave a candidate for, which is what removes `E: Unable to locate package` from its failure modes.

### D-04: Emptiness is data unless emptiness is impossible

Most of these reads have a perfectly ordinary empty answer: a machine with no snaps, no flatpaks, no held packages, no pins, no third-party keyrings. Promoting emptiness to a failure would break those machines, so it is not the rule. `require_answer`'s optional `answers=` parameter is the exception mechanism, and it is passed only where at least one answer is genuinely owed — today only by `apt-cache policy` reads over names apt must know: names installed on the machine being asked (`apt_sync.probe.AptProbe.source_policy` and `manual_installs_sync._scan_no_candidate_apt_packages`, which run the byte-identical command over the same set on the same host and must therefore carry the identical guard), and names this run has already established the machine has a candidate for (`apt_sync.origins.OriginClassifier._verify`). apt prints exactly one block per name it knows. An `apt-cache policy` over names the machine may legitimately never have heard of — the source's set asked of the target — passes nothing, because no block is owed for any of them.

That exception is a judgement and is recorded as one: **a probe that answered "I know none of these" and a probe that died both print nothing, and the output alone cannot separate them.** `477f191e` resolved it toward failing fast, on the grounds that misattributing an environment failure to every package's provenance is the worse reading and that a set apt knows nothing about is a set from which nothing could have been installed anyway. The cost is that a genuinely empty answer to those two specific probes fails the run. It is documented rather than eliminated because the information needed to eliminate it does not exist in the output.

The measured evidence behind `answers=`: `apt-cache policy <unknown-name>` exits 0 and prints nothing, while a broken sources configuration exits 100 with `E:` on stderr and an empty stdout; corrupt lists, an unreadable cache, an unprivileged user and a bad `apt.conf` all still exit 0 and answer. And `apt-cache policy $(apt-mark showmanual)` on the development machine produced 152 blocks for 152 names at exit 0 — every installed name yields a block.

### D-05: The one accepted residual

`apt-mark showmanual` with `/var/lib/dpkg/status` absent exits **0** and prints **nothing** (measured in a stock `ubuntu:24.04`). That is a broken machine whose answer is byte-identical to a machine with no manually-installed packages, and no `answers=` guard is applied to it, because an empty manual set is a legitimate — if strange — state and failing on it would be a false failure. It is accepted knowingly: every other way apt fails to read its status exits 100 and is caught, and a machine whose dpkg status file has been deleted has larger problems than a sync.

### D-06: Job-level failure is the ceiling

A `ProbeFailed` escaping a job fails that job and no more: the orchestrator records it FAILED on the same non-aborting arm as `PackageItemFailures` and runs the rest, so a transient apt lock no longer stops `folder_sync`. The four package jobs are independent by D-15/D-16, which is what makes one manager's dead read no evidence about another's already-approved work.

This is the package half of **GitHub issue #220** (job failure independence). The issue stays open for the rest: every other exception out of a job still aborts the run, so a `folder_sync` or `vscode_state_sync` failure remains terminal, and deciding which core jobs must stay terminal belongs there.

### D-07: One shared mechanism, no shared base class

The guard lives in `src/pcswitcher/jobs/packages/probes.py` as free functions, alongside `apt_policy.py`. ADR-020 D-15/D-16 keeps the four package jobs independent and forbids a shared base class that only some of them supply inputs for. A guard every job needs is exactly the kind of thing that would otherwise be smuggled into a base class. `apt_sync` keeps a thin `commands.require_apt_answer` wrapper holding apt's own evidence and the `answers=` judgement; the other three call `require_answer` directly.

## Consequences

**Positive:**
- A failed manifest capture can no longer read as "that machine has nothing". The inverted-diff class of defect — every snap, every apt package, every flatpak on the target proposed for removal because one read failed — is closed at every site that produced it.
- The user is told what actually broke. One line naming the command and the tool's own stderr, instead of a review screen full of consequences.
- apt's collateral protection can no longer switch itself off silently: an unanswered `apt-mark showmanual` on the target used to classify every collateral package as automatic, which is ADR-020 D-30 disabled with nothing said.
- The keyring garbage collector can no longer delete keys that are still in use because the source-file scan came back empty.
- The repository-conflict review can no longer show two empty panes for a file whose content it could not read, which is an overwrite approved off a diff nobody saw (ADR-020 D-37).
- The classification is written down per call site, so the next read added to these jobs has to state which category it is in rather than defaulting to the dangerous one.

**Negative:**
- One broken read costs its whole job, including the items that had nothing to do with it. This is a real loss of availability, taken deliberately in exchange for not shipping wrong changes.
- Two commands are now shaped for their exit code rather than for the shortest expression of the query, and a future edit that "simplifies" the `test -d` wrapper or the `-path` selectors back to the obvious form silently reintroduces the ambiguity. The comments at both sites say so.
- The `answers=` judgement (D-04) can fail a run that had nothing wrong with it, on the three `apt-cache policy` probes that carry it. Known, and preferred to the alternative.
- The classification is per command and cannot be derived mechanically, so it is only as good as the measurement behind each call site. A tool that changes its exit-code behaviour in a future release breaks the classification silently.
- Failing fast on a read means some runs that previously produced a partial, wrong-but-plausible result now produce nothing. That is the intent, and it will occasionally be inconvenient.

## Alternatives Considered

- **A blanket "non-zero exit fails the job" rule** — rejected on measurement: `sha256sum` over an empty glob and `dpkg --search` over deliberately-unowned paths both exit non-zero in their normal case, so the blanket rule fails every run on an ordinary machine.
- **A blanket "empty output means the read failed" rule** — rejected for the mirror reason: a machine with no snaps, no flatpaks, no pins or no held packages is an ordinary machine, and most of these reads answer "nothing" legitimately.
- **Parsing stderr to tell "directory absent" from "directory unreadable"** — rejected: locale-dependent string matching, and reshaping the command removes the ambiguity outright.
- **Reporting a failed read as a warning and continuing with the degraded picture** — rejected: that is the current defect with a log line added. The degraded picture is a removal proposal for everything on the other machine.
- **Making the guard a `PackageSyncJob` method** — rejected per ADR-020 D-15/D-16: free functions in `packages/` keep the four jobs independent.
- **Retrying a failed read before failing** — not adopted here. It would help the transient cases and mask the persistent ones, and the run is interactive with the user present; deciding it belongs with #220's failure model rather than ahead of it.

## References

- ADR-020 (D-15/D-16 job independence, D-27 continue-and-report per item, D-30/D-40 collateral protection, D-34 origin classification, D-35 origin enforcement); ADR-014 (unified dry-run contract).
- `477f191e`: the first two sites and the pattern this ADR generalises, including the ambiguity it resolved and the cost it recorded.
- GitHub issue #220: job failure independence — the accepted follow-up that decides how far a job-level failure propagates.
- `src/pcswitcher/jobs/packages/probes.py`: the mechanism and the per-command measurements.
- `docs/system/package-sync.md`: the resulting per-job behaviour.
