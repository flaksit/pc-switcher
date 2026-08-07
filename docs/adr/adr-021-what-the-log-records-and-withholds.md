# ADR-021: What the log records, and what it withholds

Status: Draft

Date: 2026-07-30

## TL;DR

**No credential may appear in the log.** Among what must reach it: the decisions the user gave (skipped items included), the changes tools made on their own behalf, and each command and its output at DEBUG.

## Scope

This ADR adds content rules to ADR-010's logging infrastructure. It is not the complete definition of what pc-switcher must or must not log.

## Additional implementation rules

**Required**

- Log every reviewed item with its decision, including skipped items.
- Log every self-directed change (apt resolving its own dependencies).
- Log each command and its output at DEBUG.
- **No credential may appear in the log.** Code MUST NOT compose a log line that carries a secret. For content the code does not fully control — command output, snippet bodies replayed through the executor, file bodies quoted for a decision — the log filter MUST redact URL `userinfo` (RFC 3986); expanding detection to other credential shapes is a future concern that does not weaken the rule.

**Forbidden**

- Aggregates standing in for the record ("3 of 5 applied").
- Content-based withholding beyond stated rules (this ADR + ADR-020 Ubuntu Pro).
- Redaction of the item, package, file or command a failure concerns.

## Context

ADR-010 settled the logging infrastructure but not its content, which was adequate while log lines narrated work in progress.

The content rules below became necessary once reviews started making decisions the user will reconstruct months later, and once command output could carry credentials embedded in URLs.

## Decision

### The log is the record, not a progress narration

A reader answers three questions from the log alone: what was proposed, what the user decided, what happened. A skipped item leaves no trace unless it is logged deliberately, so it is.

### Self-directed changes are logged, not reviewed

The user has no basis to decide a package manager's own dependency resolution. Not asking is not the same as hiding — the log names it.

### Verbatim output at DEBUG, minus the credential redaction

A summary is a guess about what will matter later, made before knowing. The cost is size; the DEBUG floor is configurable.

What appears in the log is byte-identical to what the command printed, except where the credential-redaction rule below rewrites it.

### No credential in the log — rule, mechanism, accepted gap

**The rule**: no credential may reach the log.

**The mechanism today**: code that composes its own log lines does not put secrets in them by construction, and everything else passes through a log filter that redacts URL `userinfo`. Redaction sits at that filter, once, rather than at each call site that builds a string.

**The accepted gap**: a snippet body is user-authored shell that the tool does not parse. A credential the user writes into a snippet in a shape URL-userinfo redaction does not recognise may reach the log. Auto-detecting secrets in arbitrary shell is not in scope; expanding the redaction mechanism to catch more shapes may be, and would strengthen this ADR's guarantee without changing the rule.

Display-time redaction — what the user sees at a prompt, a review line, a file body shown for a decision — is a separate concern and lives in the specs (`PKG-FR-CREDENTIAL-PRIVACY`).

## Consequences

**Positive**

- A run is reconstructable from its log alone.
- The exposure that existed in the command trace closes with the change that widens the record.
- New jobs and commands inherit the rule instead of re-implementing it.

**Negative**

- Log volume grows.
- The log-filter redaction sits on the path of every log write; a mistake there affects every job.
- The mechanism protects URL credentials only. A secret that reaches a command another way — an environment variable echoed, a password on a command line, a token in a snippet — is not covered by today's redaction, though the rule still forbids it.

## Alternatives Considered

- **Log decisions, summarise the output** — rejected: the summary is chosen before knowing what a post-mortem needs.
- **Verbatim output without redaction** — rejected: writes credentials into a world-readable file.
- **Redact in each job** — rejected: leaves every new call site free to forget, and leaves the executor's command trace uncovered.
- **Declare the debug log sensitive instead of redacting** — rejected: moves a tool problem onto the user.
- **Withhold whole URLs** — rejected: origin divergence is reported by URL because names lie.
- **Detect credentials in snippet bodies before logging them** — deferred: auto-detecting secrets in arbitrary shell is a large mechanism against a user-authored surface. The rule stands regardless of whether the mechanism is expanded.

## References

- ADR-010: logging infrastructure — the mechanism this ADR adds content rules to
- ADR-020: package convergence — the Ubuntu Pro withholding precedent
- ADR-022: read-failure attribution
- `docs/system/package-sync.md`: `PKG-FR-LOG-DECISIONS`, `PKG-FR-LOG-VERBATIM`, `PKG-FR-CREDENTIAL-PRIVACY`, `PKG-FR-COLLATERAL-AUTO`, `PKG-FR-ESM-PRIVACY`, `PKG-FR-SNIPPET-VERBATIM`
