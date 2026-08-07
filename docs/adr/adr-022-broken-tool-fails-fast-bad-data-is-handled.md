# ADR-022: A tool that did not answer fails fast; a tool that answered is data we handle

Status: Draft

Date: 2026-07-28

## TL;DR

Every subprocess a subsystem invokes falls into one of two classes: the tool did not answer, or the tool answered and the answer is inconvenient.

Opposite treatments: the first fails the subsystem immediately, naming the command; the second is data the caller handles.

## Scope

Applies to any pc-switcher subsystem that shells out to a subprocess and reads its result.

This ADR does not enumerate every read a subsystem must do or how each is classified — that is each subsystem's own responsibility, per the rules here.

## Additional implementation rules

**Required**

- Every read whose result feeds a decision MUST be classified at the call site as tool-answered or tool-did-not-answer, justified with the command's measured behaviour.
- A read that did not answer MUST raise an exception the subsystem's per-item loop does not catch. The exception MUST name the host, the command verbatim, the failing condition, and the tool's own stderr where there is one. It fails ONCE for the command, never once per dependent item.
- The default discriminator is a non-zero exit code. A command for which that is wrong MUST say so in a comment at the call site and use something else or nothing.
- Where the exit code is ambiguous — a legitimate state and a real failure share it — the COMMAND MUST be reshaped so the exit code becomes unambiguous. In preference to parsing output or stderr text: narrow the arguments to cases the run has already established the tool can answer; widen with one case the tool must answer as a witness; or wrap in an outer test that answers absence unambiguously.
- A reshape's outer test MUST hold the same privilege as the query it wraps.
- An empty result MUST be treated as data unless emptiness is provably not a state the machine can be in.

**Forbidden**

- No blanket "non-zero exit fails the subsystem" rule applied without checking the command. Several reads exit non-zero in their normal case.
- No blanket "empty output means broken" rule. Most reads have a legitimate empty answer.
- No reading a failed read's empty result as inventory or manifest.
- No swallowing a read failure into a warning and continuing with a degraded picture.

## Context

Subsystems that diff two machines' state rest on reads asking what each machine has.

Before this ADR, a failed read returned an empty result and code parsed it like any other — an empty capture read as "that machine has nothing" and inverted the whole diff.

The pattern was first surfaced by package sync: `snap list --all` failing on the source emptied the source manifest and proposed removing every snap on the target. The shape sits in every read that returns a collection.

## Decision

### The two categories

**The tool did not answer.** A transient network failure, a package-manager lock, an interrupted subprocess, an unreadable status file, a daemon not running, an installation that cannot be opened, sudo unavailable. Nothing in pc-switcher explains any of these, nothing in the run repairs them, and every conclusion drawn from the result is unfounded. **Fail fast.**

**The tool answered, and the answer is inconvenient.** A package the target has never heard of. A file the source does not have. A subprocess that exits non-zero because a request was wrong. Each of these is a fact about one request, produced by a tool that was working; deciding what to do with it is the whole subsystem. **Per-item, continue.**

The line is not "did the command succeed". It is **did the tool answer the question it was asked**.

### Fail-fast at subsystem level, once

The exception a failed read raises sits outside the per-item exception hierarchy so it escapes per-item loops. It fails the subsystem once, naming the command and the tool's own stderr. Example:

> probe on Atlas did not answer — `apt-mark showmanual` exited 100: E: Problem opening /var/lib/dpkg/status

Naming the command verbatim is the point. The pre-ADR behaviour produced a screenful of removal proposals with the real cause appearing nowhere.

### Ambiguous exit codes → reshape the command

Three measured shapes occur, and each is decided against the command's own tested behaviour:

**The exit code separates cleanly** — most reads. Guarded on exit code alone.

**Non-zero is the normal answer** — some reads exit non-zero as part of the expected shape of the reply. These are either unguarded (say so at the call site) or guarded on the answer itself rather than the exit code — for example, adding to the batch one case the tool must answer, so an answer that does not claim it proves the tool did not answer (argument widening as a witness).

**The exit code is ambiguous** — the same non-zero exit means both a legitimate state and a real failure. Reshape the command: narrow to arguments already known to succeed, or wrap in an outer test that answers absence unambiguously. Reshaping is preferred to parsing stderr text, which is locale-dependent and turns classification into a string match. A reshape's outer test must hold the same privilege as the wrapped query — measured, an unprivileged `test -d` on a directory inside an unsearchable parent exits 1, collapsing the whole `if` to exit 0 with no output.

### Empty is data unless emptiness is impossible

Most reads have an ordinary empty answer — no items installed, no matching files. Promoting emptiness to a failure breaks those cases.

The exception is reads whose answer set is genuinely owed: those may treat "empty" as "tool did not answer".

That exception is a judgement per read and must be recorded as one: a read that answered "I know none of these" and a read that died can print the same output.

### Subsystem-level failure is the ceiling

A read failure fails its subsystem and no more; the orchestrator continues with the remaining subsystems. This assumes the subsystems are independent, which is a separate decision (currently GitHub issue #220).

A subsystem MAY escalate a specific read to a run-ending abort where nothing in the run can repair it, the USER can, and the fix has to land before the next sync reads it. Per-read judgement, per-subsystem justified.

## Consequences

**Positive**

- A failed capture can no longer read as "that machine has nothing".
- The user sees one line naming what actually broke, instead of a screen of consequences.
- Every downstream defect that turned on the inverted diff — silent switch-off of consent guards, garbage collection of in-use resources, review screens showing two empty panes — closes at the same site.
- The classification is written down per call site, so the next read added to a subsystem has to state which category it is in rather than defaulting to the dangerous one.

**Negative**

- One broken read costs its whole subsystem, including the items that had nothing to do with it. This is a loss of availability, taken deliberately in exchange for not shipping wrong changes.
- Reshaped commands are shaped for their exit code rather than for the shortest form. A future edit that "simplifies" a reshape back to the obvious form silently reintroduces the ambiguity. Call-site comments must warn.
- Classification is per command and cannot be derived mechanically. A tool that changes its exit-code behaviour in a future release breaks the classification silently.
- Failing fast means some runs that previously produced a partial, wrong-but-plausible result now produce nothing. That is the intent, and it will occasionally be inconvenient.

## Alternatives Considered

- **A blanket "non-zero exit fails the subsystem" rule** — rejected: several reads exit non-zero in their normal case, so the rule fails every ordinary run.
- **A blanket "empty output means the read failed" rule** — rejected: a machine with no snaps, no packages, no matching files is ordinary.
- **Parsing stderr to disambiguate** — rejected: locale-dependent string matching, and reshaping the command removes the ambiguity outright.
- **Warn-and-continue on a failed read** — rejected: that is the defect this ADR closes.
- **Retrying a failed read before failing** — deferred. It would help transient cases and mask persistent ones; the decision belongs with the wider failure-model work in GitHub #220.

## References

- ADR-014: unified dry-run contract — a dry run must still surface a failed read
- ADR-020: package convergence — the first subsystem this rule was written for
- ADR-021: what the log records — the failure line this ADR produces goes through those rules
- `docs/adr/considerations/adr-022-command-measurements.md`: per-command measured behaviour for the reads pc-switcher uses today (which discriminator, why, and where each classification is enforced)
- `docs/system/package-sync.md`: `PKG-FR-READ-FAILS-JOB` (the rule instantiated for package sync), `PKG-FR-REGISTRY-CONSENT` (a run-ending escalation this ADR permits)
- GitHub issue #220: subsystem-level failure independence
