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

The user has read the whole narrative and answered every open question. Both documents' *Open questions* sections now say so. What remains is not reading but consequence: the article audit below, and the implementation backlog.

## Rulings made during review that the code does not implement

All of these are in the criteria's gap register and in `02-DIVERGENCES.md`. They are requirements changes, not doc fixes — except DIV-12, which is a shipped bug.

1. **`PKG-FR-APT-HOLD-VERSION`** — a held package must be installed at the source's exact version, failing if the target cannot supply it. A hold names no version and blocks install, upgrade and removal alike (measured), so it carries "do not move this off the version that works". Code installs by name and holds afterwards. Still unruled: the same package held on *both* machines at different versions, which today produces no item at all.
2. **`PKG-FR-FLATPAK-REMOTE-DELETE`** — a remote is never a review item. One the source lacks is deleted once nothing on the target uses it, counted after this run's approved removals and including machine-specific and origin-diverged applications. Code still offers it as a two-answer item.
3. **`PKG-FR-SNAP-SIDELOAD`** — sideloaded snaps are out of scope (#221) and ignored on both machines; a run names them and does nothing else. Code still offers a target-only sideload for removal.
4. **`PKG-FR-FLATPAK-FILTER`** (DIV-11) — a remote's filter is replicated: the file copied byte-for-byte to the same path on the target, re-applied, derived, after that remote's applications land. flatpak records the filter's path rather than its content, which makes the content an ordinary file the run can carry like a signing key; the earlier "unsyncable" reading confused the content with the path. Code does the opposite and does not even request the filter column. Unmeasured within the ruling: whether flatpak refuses to install a ref its own filter excludes.

## Open items unrelated to the review

- `02-BEHAVIOUR-LEDGER.md` line references outside "The review" were taken at `ffa06900`; the branch has moved. Symbol names are reliable, line numbers are not. A re-read against the current head is outstanding.
- `02-UAT-01-RUNBOOK.md` still describes the superseded two-pass review flow (DIV-09).
- `~/go/bin/crit` was installed for the review TUI and is no longer used.

## Rulings from the open questions (2026-07-30)

5. **`PKG-FR-COLLATERAL-MANUAL`** (DIV-12) — being offered for removal is not consent to lose it. Only an APPROVED removal exempts a package from collateral protection; the code exempts every candidate it asked about, whatever the answer, so a skipped removal can be deleted silently by an approved install.
6. **`PKG-FR-COLLATERAL-MARKED`** — a machine-specific package is asked about for ANY change, upgrade included. The code classifies removals and downgrades only, and consults the target's manual set rather than the marks.
7. **`PKG-FR-READ-FAILS-JOB`** — a package manager that cannot be queried fails its own job only. `orchestrator.py:1324-1344` re-raises everything but `PackageItemFailures` and stops the run.
8. **`PKG-FR-JOB-ORDER`** — all four package jobs precede `folder_sync`. `orchestrator.py:1082` checks three.
9. **`PKG-FR-REPO-DELETE`** — a repository still used by anything on the target, machine-specific packages included, is never raised. The code offers it and discloses what it would strand.
10. **`PKG-FR-REPO-CONFLICT`** — narrowed to repositories this run writes for an approved package, which is what flatpak already does (closes DIV-07).
11. **`PKG-FR-LOG-DECISIONS`, `PKG-FR-LOG-VERBATIM`** — new. Every item presented with its decision, every change a manager made on its own behalf, and the manager's own output verbatim in the debug log. Not yet checked against the code, so not in the gap register.

## What the review changed structurally

- snap and flatpak open by naming the shape every job shares and split into subsections. apt's *Removing, and reporting without acting* is two sections; *Decisions and their memory* gained a subtitle. Traceability rows follow all of it.
- Six articles deleted as implementation detail or as claims that could not be substantiated: `PKG-FR-COLLATERAL-TIMING`, `PKG-FR-COLLATERAL-NEW-ORIGIN`, `PKG-FR-APT-HOLD-ORDER`, `PKG-NG-COLLATERAL-SOURCE-MANUAL`, `PKG-NG-COLLATERAL-MARKS`, `PKG-NG-DEB-ORPHANED`. `PKG-FR-APT-HELD-TARGET` was not deleted but re-mapped to *Holds*, since the narrative covers it there.
- 122 articles now, down from 124.

## Next

1. The Stage 5 article audit: all 122 articles re-read against the narrative's vocabulary. It was parked until the narrative settled, which it now has.
2. The implementation backlog — eleven rulings the code does not implement, one of them a bug (DIV-12). That is a planning input, not review work.
