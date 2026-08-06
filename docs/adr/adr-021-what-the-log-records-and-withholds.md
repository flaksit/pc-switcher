# ADR-021: What the log records, and what it withholds

Status: Draft

Date: 2026-07-30

## TL;DR

The log names every item a review presented, every self-directed change a tool made, and each command's output verbatim at DEBUG. One class of content is withheld everywhere: a credential embedded in a URL — the URL is the secret. Snippets are stored and replayed as written; only their rendering is redacted.

## Implementation Rules

**Required**
- Log every reviewed item with its decision, including skipped items.
- Log every self-directed change (apt resolving its own dependencies).
- Record each command's output verbatim at DEBUG.
- Redact URL `userinfo` (RFC 3986) at every exit where a URL leaves the process: the log filter, the executor confirmation, `ReviewEntry`, `ItemDiff` label writes, and any question a job puts directly to `Confirmer`.
- A snippet body is redacted only when displayed. It is stored and replayed as its author wrote it (`PKG-FR-SNIPPET-VERBATIM`).

**Forbidden**
- Aggregates standing in for the record ("3 of 5 applied").
- Content-based withholding beyond stated rules (this ADR + ADR-020 Ubuntu Pro).
- Redaction of the item, package, file or command a failure concerns.

## Context

ADR-010 settled the logging infrastructure but not its content, which was adequate while log lines narrated work in progress.

Package sync changed that. Its reviews make decisions the user will reconstruct months later, and a skipped item leaves no trace unless the log records it deliberately. That same widening exposes credentials that private PPAs and commercial repositories embed in URLs.

## Decision

### The log is the record, not a progress narration

A reader answers three questions from the log alone: what was proposed, what the user decided, what happened.

### Self-directed changes are logged, not reviewed

The user has no basis to decide a package manager's own dependency resolution. Not asking is not the same as hiding — the log names it.

### Verbatim output at DEBUG

A summary is a guess about what will matter later, made before knowing. The cost is size; the DEBUG floor is configurable.

### Credentials in URLs are withheld everywhere

The URL is the secret, so every place a URL appears is a place the secret appears. Redaction sits at each of the five exits, not at each call site that builds a string.

The snippet exit redacts the rendering only. A snippet is opaque to the tool and replayed exactly as written — rewriting the file or the replayed command would break the install it exists to reproduce.

## Consequences

**Positive**
- A run is reconstructable from its log alone.
- The exposure already present in the command trace closes with the change that widens the record.
- New jobs and commands inherit the rule instead of re-implementing it.

**Negative**
- Log volume grows.
- Each redaction point sits on the path of everything of its kind; a mistake there affects every job.
- Redacted output is no longer byte-identical to what the command printed.
- The rule protects credentials in URLs only. A secret that reaches a command another way is not covered.

## Alternatives Considered

- **Log decisions, summarise the output** — rejected: the summary is chosen before knowing what a post-mortem needs.
- **Verbatim output without redaction** — rejected: writes credentials into a world-readable file.
- **Redact in each job** — rejected: leaves every new call site free to forget, and leaves the executor's command trace uncovered.
- **Declare the debug log sensitive instead of redacting** — rejected: moves a tool problem onto the user; the user also reads review lines and conflict displays.
- **Withhold whole URLs** — rejected: origin divergence is reported by URL because names lie.

## References

- ADR-010: logging infrastructure — the mechanism this ADR states content rules for
- ADR-020: package convergence — the requirements that forced these rules, and the Ubuntu Pro withholding precedent
- ADR-022: read-failure attribution
- `docs/system/package-sync.md`: `PKG-FR-LOG-DECISIONS`, `PKG-FR-LOG-VERBATIM`, `PKG-FR-CREDENTIAL-PRIVACY`, `PKG-FR-COLLATERAL-AUTO`, `PKG-FR-ESM-PRIVACY`, `PKG-FR-SNIPPET-VERBATIM`
