# Handover — package sync requirements review

Where the review of `docs/planning/package-sync-user-requirements.md` stands. Start a new session with this file and the two requirement documents.

## What the documents are

`docs/planning/package-sync-user-requirements.md` — prose, authoritative for intent, ~3,700 words. `docs/planning/package-sync-conformance-criteria.md` — the same intent as 124 checkable articles, derived from it, with a traceability appendix mapping every article to a narrative section.

## How the review is running

The user reads the narrative top to bottom and comments. Each comment is applied to both documents, verified, and committed on its own. Verification after every change, all three must pass:

```
anchors resolve (GitHub slug: lowercase, drop non-word chars, EACH space -> one hyphen)
every article mapped in the traceability table, no phantom or dangling ids
no banned vocabulary reintroduced
```

Do not chain the check and the commit with `&&` — gate the commit on the check's exit code, or a failing check still commits.

## Standing instructions from the user

- **Be concise.** The reader's budget is ~15 minutes. Cut anything that is reasoning, evidence or tech analysis — ADR-020 holds it and is linked.
- **No jargon invented here.** Terms used must be defined, terms defined must be used. Already removed: "diff", "direction", "travel", "vendor", "never to be offered again", "screen"/"rows"/keystrokes.
- Say "synced", not "travel". Say "the user", never "you". Say "job", not "ecosystem".
- **No UI mechanics** — state the user's need, not the widget. A UI designer may find a better answer.
- **No restating core pc-switcher behaviour** (validation, dry run, `--confirm-each-command`) beyond what is specific to package sync.
- Never use the maintainer's real machine names. The documents use `Atlas`, `Nomad`, `Vega`.

## Review position

The user has read to the **flatpak** section. Everything above it is reviewed and settled. Not yet read: the rest of flatpak, *Software no manager can reproduce*, *When something goes wrong*, *What this deliberately does not do*, *Open questions*.

## Rulings made during review that the code does not implement

All three are in the criteria's gap register and in `02-DIVERGENCES.md`. They are requirements changes, not doc fixes.

1. **`PKG-FR-APT-HOLD-VERSION`** — a held package must be installed at the source's exact version, failing if the target cannot supply it. A hold names no version and blocks install, upgrade and removal alike (measured), so it carries "do not move this off the version that works". Code installs by name and holds afterwards. Still unruled: the same package held on *both* machines at different versions, which today produces no item at all.
2. **`PKG-FR-FLATPAK-REMOTE-DELETE`** — a remote is never a review item. One the source lacks is deleted once nothing on the target uses it, counted after this run's approved removals and including machine-specific and origin-diverged applications. Code still offers it as a two-answer item.
3. **`PKG-FR-SNAP-SIDELOAD`** — sideloaded snaps are out of scope (#221) and ignored on both machines; a run names them and does nothing else. Code still offers a target-only sideload for removal.

## Open items unrelated to the review

- `02-BEHAVIOUR-LEDGER.md` line references outside "The review" were taken at `ffa06900`; the branch has moved. Symbol names are reliable, line numbers are not. A re-read against the current head is outstanding.
- `02-UAT-01-RUNBOOK.md` still describes the superseded two-pass review flow (DIV-09).
- `~/go/bin/crit` was installed for the review TUI and is no longer used.
