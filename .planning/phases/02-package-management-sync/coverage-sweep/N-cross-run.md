# Sweep — N: cross-run and two-machine compositions

Enumeration drafted from the requirements; coverage column filled after reading the integration suite.

These are the behaviours that do not exist inside one run. A branch belongs here only when a second run, or a swap of the two machines' roles, is what makes the outcome observable. Everything else is a single-run row in A–K.

Machines: `Atlas` and `Vega`. "Run 1 from Atlas" means Atlas is the source.

## N. Across runs and across the two roles

| # | Narrative | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| N1 | Atlas has `wireshark`, Vega lacks it. Run 1 from Atlas, the user answers "never install on Vega" | the mark lands on Atlas, the holding machine. Run 2 from Atlas offers nothing | | |
| N2 | Same mark, then run 3 from Vega — roles reversed, so `wireshark` is now target-only | still no item: the mark protects it from removal by a sync from the other machine, not only from being installed | | |
| N3 | Vega has `steam`, Atlas does not. Run 1 from Atlas, the user answers "keep on Vega for good" | the mark lands on Vega. Neither Atlas→Vega nor Vega→Atlas raises `steam` again | | |
| N4 | An apt hold marked machine-specific in run 1 | run 2 raises no hold item, and the package's own item is unaffected | | |
| N5 | A snap refresh hold marked machine-specific in run 1 | run 2 raises no hold item; the snap's own presence item stays live | | |
| N6 | A flatpak mask marked machine-specific in run 1 | run 2 raises no mask item | | |
| N7 | Any item declined for this run only in run 1 | run 2 offers it again, unchanged, and run 1 recorded nothing | | |
| N8 | A run converged everything it was approved to converge | the next run between the same two machines presents nothing and issues no change | | |
| N9 | Run 1 Atlas→Vega installs `P`. The user then removes `P` on Vega by hand. Run 2 Vega→Atlas | `P` is offered for removal from Atlas, unticked; leaving it undecided changes nothing; approving it removes it | | |
| N10 | Atlas gains a repository, its key and a package from it. Run 1 Atlas→Vega | one question — the package. Key, repository and pins land in apt's required order before the install, and the run asks nothing about any of them | | |
| N11 | Run 1 writes a repository for an install a late collateral answer then withdrew | the repository stays. The run names it by URL and filename and says nothing on Vega installs from it, as neither a failure nor a warning. Run 2 does not offer it for deletion — Atlas still has the file | | |
| N12 | Vega has repository `S` and package `R` from it; Atlas has neither. Run 1 from Atlas, `R`'s removal declined | `S` is not raised at all — something on Vega still uses it | | |
| N13 | Same, but `R`'s removal approved | `S` becomes deletable in that same run: asked once, naming the URLs it declares, and its now-unreferenced key goes with it | | |
| N14 | Same, but Vega marked `R` machine-specific | `S` is never raised, in that run or any later one, even though `R` appears in no review | | |
| N15 | A package manual on Atlas arrives on Vega as an automatic dependency of something else. A later run's approved change would remove it | not protected: Vega's own manual set is what protects, and Vega's apt installed this one itself | | |
| N16 | A dependency-only package `Q` that Vega's apt keeps or drops across an install-then-remove round trip of `P` | pc-switcher never proposes, installs or removes `Q` in either run | | |
| N17 | A snippet authored on Atlas in run 1; run 2 launched from Vega, whose registry holds an entry Atlas lacks | the transfer stops and asks, naming the entries at risk; declining ends the run and sends nothing | | |
| N18 | Vega reports no Ubuntu Pro attachment and Atlas carries ESM repositories. Run 1 | apt is skipped whole, `/etc/apt` on Vega is exactly as it was, and the other jobs run. After Vega is attached, run 2 converges apt normally | | |
| N19 | The last flatpak application from a remote is removed on approval in run 1 | the remote and its signing key go with it; run 2 has neither to raise | | |
| N20 | A flatpak application marked machine-specific on Vega is the only user of a remote Atlas lacks | the remote is never deleted, in any run | | |
| N21 | Snap revisions converged in run 1, `folder_sync` running after | Vega's `~/snap/<app>/<revision>` matches Atlas's, so the data lands where the target's snapd will read it | | |
| N22 | Two machines, all four package jobs enabled, a full run | every job reviews before its own first change, one job's failure leaves the others' work intact, and the exit code reflects what failed | | |

## Notes for the assembler

Rows here must not restate a single-run branch. Where a narrative's only new content is "and it still works the second time", it belongs in N only if a requirement makes the second run's outcome different from the first's.
