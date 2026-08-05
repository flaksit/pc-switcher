# Work units

From the sweep of all 127 articles and the whole doc surface against `0abe7670`. One commit per unit: fix, tests, every doc it affects, and its entries struck from the criteria's gap register and from `02-DIVERGENCES.md`. `02-DOC-DEBT.md` names the documentation each unit owes.

## U0 — What the rulings changed in the documents

Documentation only, one commit. The rulings themselves are already folded into the register and into the units below.

- **Batched** means the questions come one after another with no pause and in whatever shape answers them best — not that one shape fits every item. The five one-item-at-a-time questions conform, and the register entry against them is gone. Say what the word means, in the narrative and in `PKG-FR-BATCHED`, so the next reader does not re-open it.
- The per-job passwordless-sudo preconditions (DIV-03) are enforced in each `validate()` and stated in no article. Add one under *Preconditions and defaults* carrying DIV-03's table — apt both machines, snap both machines, flatpak the target and only for a system-scope item, manual neither — and correct the traceability count.
- `PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` (DIV-02) stays as it is, recorded as an accepted cost: anything that can set it gets silent permanent decisions. It needs an entry among the non-goals and a line in the job guide.

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

`PKG-FR-FLATPAK-REMOTE-DELETE`, `PKG-FR-FLATPAK-FILTER`, `PKG-FR-FLATPAK-REMOTE-TRUST`, `PKG-FR-FLATPAK-ORIGIN-DIFF`. Closes DIV-11.

The largest unit: reading the filter widens the remote capture to four columns, which reshapes every remote fixture in the suite. A remote stops being a review item in every case and is deleted once nothing on the target uses it. An unverified remote is reported to an ordinary run, not only under `--confirm-each-command`. An application whose remote its machine no longer configures has no origin URL, which matches no other origin, so it reports a divergence and the text says the URL is missing.

## U6 — snap sideloads

`PKG-FR-SNAP-SIDELOAD`. A sideloaded snap only the target has is ignored and named, not offered for removal.

## U7 — The answers and their defaults

`PKG-FR-HARMLESS-DEFAULT`, `PKG-FR-EFFECT-NOT-MECHANISM`, `PKG-FR-NAME-THE-MACHINES`.

An `/etc/apt/apt.conf.d` overwrite starts at skip, a snap change keeps *apply*; the permanent answer says the user will not be asked again; every failure, warning and `mutates=` phrase names the machines by hostname instead of by their role in the run.

## U8 — The leaked identifier

DIV-06: `state.py`'s decision-file header reads "never synced to any peerfilter_inert". One line, shipped to every user's `~/.config/pc-switcher/<manager>.decisions.yaml`.
