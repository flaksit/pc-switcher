# Work units

From the sweep of all 127 articles and the whole doc surface against `0abe7670`. One commit per unit: fix, tests, every doc it affects, and its entries struck from the criteria's gap register and from `02-DIVERGENCES.md`. `02-DOC-DEBT.md` names the documentation each unit owes.

## U0 — Rulings owed before the rest can be planned

Ask as one set. Nothing here is a code change until it is answered.

- `PKG-FR-BATCHED` against the five one-item-at-a-time questions (repository deletion, pin deletion, repository or remote conflict, collateral, unreproducible item). Each was ruled that way because its own content has to be read immediately before the question. Either the article gains that exception or the code re-gathers them.
- `PKG-FR-HARMLESS-DEFAULT` for a snap whose revision or channel would change: is converging it an overwrite that must start at skip? The `/etc/apt/apt.conf.d` case needs no ruling — the article's own Why names it.
- `PKG-FR-FLATPAK-ORIGIN-DIFF` where an application names a remote its machine no longer configures: keep the code's name comparison as a documented fallback, or report nothing.
- DIV-02: `PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` reports the review as interactive, so it writes machine-specific marks and snippets. Accept and document, report it as non-interactive, or gate it on a test-only signal.
- DIV-03: the per-job passwordless-sudo preconditions are enforced in `validate()` and stated in no article. Which article covers them.

## U1 — Collateral

`PKG-FR-COLLATERAL-MANUAL`, `PKG-FR-COLLATERAL-MARKED`, `PKG-FR-COLLATERAL-AUTO`. Closes DIV-12.

The shipped bug first: `Collateral.plan_time` exempts every package under review before any answer exists. Only an approved removal may exempt one. `Collateral.classify` gains the upgrade case and `Collateral.protected` gains the target's machine-specific marks, with the question saying which package is marked. Auto collateral gets its log line.

## U2 — What the log records and what it withholds

`PKG-FR-LOG-DECISIONS`, `PKG-FR-LOG-VERBATIM`, `PKG-FR-CREDENTIAL-PRIVACY`. Closes DIV-13, DIV-14. Leaves ADR-021 ready to lose Draft.

One unit because verbatim output without the withholding point puts repository credentials in a world-readable file. The redaction lands in `executor.py`, where every command and its output already pass.

## U3 — Orchestration

`PKG-FR-READ-FAILS-JOB`, `PKG-FR-JOB-ORDER`. Closes DIV-01, DIV-05, DIV-08.

`ProbeFailed` fails its own job and lets the others run; the ordering check covers `manual_installs_sync` too. DIV-05 is a comment in the same handler asserting a cross-manager review that does not exist.

## U4 — apt repositories, keys and holds

`PKG-FR-REPO-DELETE`, `PKG-FR-REPO-CONFLICT`, `PKG-FR-APT-HOLD-VERSION`, `PKG-FR-APT-HOLD-INERT`, `PKG-FR-DERIVED-VISIBLE`. Closes DIV-07, DIV-10.

A repository still in use is withheld rather than disclosed; the conflict question narrows to a file this run writes for an approved package; a held package the target lacks is installed at the source's version or fails naming both. `PKG-FR-APT-HOLD-INERT` needs `apt-mark hold` measured against a package the machine does not have before it can be called met or unmet. Signing-key provisioning and collection get the log line and the dry-run preview every other derived write already has.

## U5 — flatpak remotes and the filter

`PKG-FR-FLATPAK-REMOTE-DELETE`, `PKG-FR-FLATPAK-FILTER`, `PKG-FR-FLATPAK-REMOTE-TRUST`. Closes DIV-11.

The largest unit: reading the filter widens the remote capture to four columns, which reshapes every remote fixture in the suite. A remote stops being a review item in every case and is deleted once nothing on the target uses it. An unverified remote is reported to an ordinary run, not only under `--confirm-each-command`.

## U6 — snap sideloads

`PKG-FR-SNAP-SIDELOAD`. A sideloaded snap only the target has is ignored and named, not offered for removal.

## U7 — The answers and their defaults

`PKG-FR-HARMLESS-DEFAULT`, `PKG-FR-EFFECT-NOT-MECHANISM`, `PKG-FR-NAME-THE-MACHINES`. Follows U0's ruling on the snap case.

An overwrite starts at skip; the permanent answer says the user will not be asked again; every failure, warning and `mutates=` phrase names the machines by hostname instead of by their role in the run.

## U8 — The leaked identifier

DIV-06: `state.py`'s decision-file header reads "never synced to any peerfilter_inert". One line, shipped to every user's `~/.config/pc-switcher/<manager>.decisions.yaml`.
