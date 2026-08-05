# Package sync rework — the rulings and the work they imply

Decisions taken with the user. Each one changes the requirements first; the specification, the job guide, the code and the tests follow from there. Where a ruling contradicts an article, the article is what moves.

## Rulings

### A snap's revision is not a standing preference

A revision or channel difference stays a reviewed item, worded as the effect it has — "will overwrite revision X on <target> with revision Y" — and offers **apply and skip-once only**. It MUST NOT be markable machine-specific: nobody holds a revision as a per-machine preference, and the mark left the two machines' manifests disagreeing about a snap neither would raise again.

### A block replicates like a pin, not like a decision

An apt hold and a flatpak mask are **derived**: they follow the software they apply to, replicate without review, and are never items. This is `PKG-FR-PIN-ALWAYS`'s treatment of pins, for the same reason — a block changes nothing about what software exists, only about what may move, and replicating one costs nothing.

Consequences: the merged-question machinery, the block-state decision store, and every article and row that makes a block separately decidable go. Sweep all of section B, E.8, F.13 and N4–N6, and every row citing `PKG-FR-BLOCKS-REPLICATE`, `PKG-FR-APT-HOLD-*`, `PKG-FR-SNAP-HOLD` or `PKG-FR-FLATPAK-MASK` — B14 among them, not only the rows named here.

### A hold whose package is absent is a bookkeeping failure

On either machine. The run aborts and tells the user to clean it up. This is not a principle about bookkeeping in general; it is a decision not to carry logic for a case that should not exist.

### A flatpak filter is in force before the applications install

Order: add or sync the remote, add or sync its filter, then install. Nothing is cleared first, so no window exists in which the target's remote offers more than either machine meant. A filter that denies an application the source itself has installed is a bookkeeping failure of the same kind — abort and say so. A filter that cannot be written or applied warns, naming the filter and the remote.

### A remote's trust travels wherever it is stored

`PKG-FR-FLATPAK-REMOTE-TRUST` has no exception. A key held through the ostree per-remote `gpgkeypath` option is read and carried like any other.

### Collateral the user was never asked about

D37: a removal candidate the user skipped, carried off by another approved removal's cascade, must be **asked** about, not refused at the guard. The cost — one `apt-get --dry-run` per candidate on a run with removals — is accepted.

### Software no manager can reproduce is found by diffing, not by scanning one machine

`manual_installs_sync` scans **both** machines and presents what the target lacks. A finding that the target already holds is not raised, which is what makes a secondary path — a symlink in `bin` pointing at an app in `/opt` — stop being asked about once the snippet has run. Resolution is still decided by the **source's** registry alone: it is the one in charge, and it reaches the target in the same run or the run aborts.

### Where hand-installed software lives

Scanned: `/opt`, and directly under `/usr/local` — plus inside `bin`, `sbin`, `lib`, `games`, `src`. Never scanned: `etc`, `include`, `man`, `share`. Anything installed there is assumed to arrive with an application the scan finds elsewhere.

A finding may be a file, a directory or a symlink. It is not a finding if dpkg owns it, if it is one of the directories `base-files.postinst` creates, or if it is a directory with no file anywhere beneath it.

Under `/opt`, an unowned entry is judged by its own shape:

| `/opt/<X>` holds | Meaning | Action |
| - | - | - |
| files | `/opt/<app>` | `<X>` is the finding |
| no files, one directory | `/opt/<vendor>/<app>` | that directory is the finding |
| no files, several directories | cannot be told apart | ask the user which it is |
| nothing | — | not a finding |

The FHS skeleton list is nine names from `base-files.postinst`, held as a constant with a VM test asserting the machine still declares exactly those.

## The work

1. Snap revision: apply/skip-once, no mark
2. Apt holds derived, like pins
3. Flatpak masks derived, like pins
4. Hold without package: abort, name it
5. Flatpak order: remote, filter, install
6. Filter failure warns, never clears first
7. Read remote key from `gpgkeypath`
8. D37: ask about the skipped candidate
9. New scan scope, six `/usr/local` dirs
10. `/opt` vendor-or-app question when ambiguous
11. Diff-based: scan both machines
12. Source registry alone decides resolution
13. Delete the invented 25-finding limit
14. Rename "ratchet" to plain wording
15. Sweep untagged tests, correct marks
16. D61, F121, H133 become U
17. Delete J107 row
18. Close K89, do K67 properly
19. Drop K82, K88 gap entries
20. Integration tests for collateral damage
21. Integration tests for apt origins
22. Re-audit V rows, drop unit-provable
23. Five checks, push, watch CI

Items 9–13 depend on 11's shape. Items 14–19 land after everything that moves a row.

## Two standing rules this settles

**A test tier follows what is under test.** A branch a unit test proves reliably needs no integration test. Integration is for what depends on real package-manager or machine behaviour, and nothing else.

**"Ratchet" is not this project's word.** The cross-reference between the scenario document and the suite is called what it is.
