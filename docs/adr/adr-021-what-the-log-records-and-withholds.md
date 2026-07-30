# ADR-021: What the log records, and what it withholds

Status: Draft

Date: 2026-07-30

## TL;DR

The log is the record of what pc-switcher did and why, so it names every item a review presented with the decision it received, every change a tool made on its own behalf that no review showed, and each command's own output verbatim at debug level. Against that, one class of content never reaches it: a credential embedded in a URL is withheld wherever pc-switcher writes or shows a URL, because the URL is the secret.

## Implementation Rules

**Required:**
- Every item a review presented MUST be logged with the decision it received, including items the user skipped — an item that produced no change MUST still produce a line.
- Every change a tool made on its own behalf that no review showed — a package manager resolving its own dependencies is the case that exists today — MUST be logged.
- A command's own output MUST be recorded verbatim at DEBUG, alongside the command text the executor already traces there.
- A credential embedded in a URL MUST be withheld wherever pc-switcher writes or shows that URL: the executor's command trace, a command's recorded output, anything the user reads while deciding, and a configuration file displayed in full for a decision.
- Redaction MUST sit at each point a path leaves the process, never at each call site that builds a string. There are three: every log record, the per-command confirmation prompt, and the text a review shows while the user decides.

**Forbidden:**
- No aggregate standing in for the record: "3 of 5 applied" does not say which two did not, and a count is not a decision.
- No content-based withholding beyond a stated rule. What may not be logged is enumerated — this ADR's credential rule and ADR-020's Ubuntu Pro attachment payload — and everything else is recorded.
- No redaction of the item, package, file or command a failure concerns. Failures name what they concern; that is the point of the record.

## Context

ADR-010 settled the logging *infrastructure* — stdlib logging, queue handler, the FULL level, three floor settings. It says nothing about content, which was adequate while log lines were narration of work in progress.

Package sync changed that. Its reviews make decisions the user will want to reconstruct months later, and today a decision leaves a trace only when it caused a change: an item the user skipped produces no line at all, and a dependency the package manager removed on its own initiative produces none either. The user asked for both, and for each command's output verbatim, so that a post-mortem reads the tool's own words rather than a paraphrase.

That same widening creates an exposure. A private PPA or a commercial repository carries its credential in the URL itself, so recording a package manager's output records the secret. The executor has traced every command verbatim at DEBUG since long before this, log files sit at mode `rw-rw-r--` in `~/.local/share/pc-switcher/logs`, and nothing in the codebase redacts anything. The exposure is therefore older than the requirement that surfaced it.

## Decision

### The log is the record, not a progress narration

A reader of the log can answer three questions without the tool's help: what was proposed, what the user decided about each of it, and what happened. That obliges a line per presented item rather than a line per change, because "the user was asked and said no" is a fact about the run.

### Self-directed changes are logged, not reviewed

A change the tool makes on its own behalf is not a review item — the user has no basis to decide it, which is why ADR-020 derives it rather than asking. But not asking is not the same as hiding. A package manager removing a dependency it installed itself is its own business and proceeds silently in the review; the log still names it, so a user who finds something gone can find out why.

### Verbatim output, at debug

Each command's own output is recorded as the command produced it. A summary is a guess about what will matter later, made before knowing. The cost is size — runs already produce logs of several hundred megabytes — and it is accepted: the debug floor is configurable, and a log nobody can diagnose from is worth less than a large one.

### A credential in a URL is withheld everywhere

Repository credentials live inside the URL, so every place a URL appears is a place the secret appears: the command trace, recorded output, a review line, a configuration file shown whole for a decision. Withholding it in the log alone would leave it on screen; withholding it on screen alone would leave it in a world-readable file.

There is no single point. `Executor` covers the command trace and each command's output, but a job's own log lines never pass through it, and the text a review shows — a repository file printed whole for a conflict — is built in-process and goes to the screen, not through a command. So the rule is applied at each of the three exits instead: a logging filter on the queue handlers, which is every route into the log; the confirmation prompt in `Executor`, the one thing there that never becomes a log record; and `ItemDiff`, which is the single shape every review line is built from. Three points, each of which every path of its kind passes through — not a rule repeated at each call site that happens to build a URL.

### The precedent this generalises

ADR-020's Ubuntu Pro rule already withholds content by construction: `pro status --format json` is parsed and only the `attached` boolean escapes, because the payload names the subscriber. That is the same shape — a named class of content that may not be logged, enforced where the content enters the process rather than where it leaves.

## Consequences

**Positive:**
- A run is reconstructable from its log alone: what was proposed, what was decided, what the tool did unasked, and what each command said.
- The exposure that already existed in the command trace closes with the same change that widens the record.
- Three redaction points and no fourth, so a new job, a new command or a new review line inherits the rule instead of re-implementing it.

**Negative (costly to reverse):**
- Log volume grows, and the current several-hundred-megabyte runs are the floor rather than the ceiling.
- Each redaction point sits on the path of everything of its kind in the program, so a mistake there affects every job rather than one. The logging filter additionally renders each record's message eagerly, giving up stdlib logging's deferred formatting.
- Redacted output is no longer byte-identical to what the command printed, so a reader cannot diff a log against a live command and expect equality.
- The rule protects credentials in URLs and nothing else. A secret that reaches a command another way is not covered, and pretending otherwise would be worse than stating the boundary.

## Alternatives Considered

- **Log the decisions, summarise the output** — rejected: the summary is chosen before knowing what a future post-mortem needs, which is the failure this ADR exists to fix.
- **Verbatim output without redaction** — rejected: it writes repository credentials into a file every account on the machine can read.
- **Redact in each job rather than at the exits** — rejected: it leaves every new call site free to forget, and the oldest path — the command trace the executor has always written — uncovered.
- **Declare the debug log sensitive instead of redacting** — rejected: it moves a tool problem onto the user, and the user reads review lines and conflict displays too, which no file permission covers.
- **Withhold whole URLs** — rejected: the URL is what makes a repository identifiable, and origin divergence is reported by URL precisely because names lie (ADR-020 D-35, D-41).

## References

- ADR-010: Standard library logging infrastructure — the mechanism this ADR states the content rules for. Not superseded: infrastructure and content evolve independently.
- ADR-020: Declarative package convergence — the requirements that forced these rules, and the Ubuntu Pro withholding precedent this generalises.
- ADR-022: A read that did not answer fails the job — the other half of "the log names what went wrong".
- `docs/planning/package-sync-conformance-criteria.md`: `PKG-FR-LOG-DECISIONS`, `PKG-FR-LOG-VERBATIM`, `PKG-FR-CREDENTIAL-PRIVACY`, `PKG-FR-COLLATERAL-AUTO` and `PKG-FR-ESM-PRIVACY` — the same rules as individually checkable articles.
- `.planning/phases/02-package-management-sync/02-DIVERGENCES.md`: DIV-13 and DIV-14 record how far the shipped code is from these rules.
