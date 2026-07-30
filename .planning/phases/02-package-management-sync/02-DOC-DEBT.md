# Doc debt

Documentation that is accurate about the code at `0abe7670` and stops being accurate when a Step 3 unit lands, plus documentation already written for the finished behaviour. Spent at Step 4; this file is deleted there.

## Owed when a unit lands

| Where | What it says now | Unit |
| - | - | - |
| `docs/jobs/package-sync.md` § Job ordering is enforced; `docs/system/package-sync.md` intro | three package-manager jobs must precede `folder_sync` | U3 (`PKG-FR-JOB-ORDER`) |
| `docs/jobs/package-sync.md` § When a package manager cannot be read, last paragraph; `docs/system/package-sync.md` shared-core bullet ("Until GitHub issue #220 lands") | a failed read ends the whole sync | U3 (`PKG-FR-READ-FAILS-JOB`) |
| `docs/jobs/package-sync.md` § apt collateral | collateral is "remove or downgrade"; nothing said about which removals exempt a package | U1 (`PKG-FR-COLLATERAL-MANUAL`, `PKG-FR-COLLATERAL-MARKED`, `PKG-FR-COLLATERAL-AUTO`) |
| `docs/jobs/package-sync.md` §§ Batched review, Deletions; `docs/system/package-sync.md` apt item-classes bullet; `docs/system/data-model.md` item table | an apt repository the source no longer has is offered for deletion whatever still uses it | U4 (`PKG-FR-REPO-DELETE`) |
| `docs/jobs/package-sync.md` §§ Batched review, Deletions, Flatpak remotes; `docs/system/package-sync.md` flatpak item-classes bullet; `docs/system/data-model.md` item table | a flatpak remote is a two-answer review item in the removal case | U5 (`PKG-FR-FLATPAK-REMOTE-DELETE`) |
| `docs/jobs/package-sync.md` § Flatpak remotes, last paragraph; `docs/system/package-sync.md` flatpak converges-by bullet | a remote's filter does not replicate and the run warns instead | U5 (`PKG-FR-FLATPAK-FILTER`) |
| `docs/jobs/package-sync.md` §§ Flatpak refs, Flatpak remotes; `docs/system/package-sync.md` flatpak covers bullet | remote names decide when a machine no longer configures the remote | U5 (`PKG-FR-FLATPAK-ORIGIN-DIFF`) |

## Already written for the finished behaviour

Correct once the unit lands, wrong today. Left as written rather than edited twice; recheck at Step 4.

| Where | Unit |
| - | - |
| `docs/jobs/package-sync.md` § Signing keys, "Every derived write is logged as it lands and previewed under `--dry-run`", and the same sentence in `apt_sync`'s module docstring | U4 (`PKG-FR-DERIVED-VISIBLE`) |
| `docs/jobs/package-sync.md` § apt collateral, "that becomes its own review item" — true only once a skipped removal keeps its protection | U1 (`PKG-FR-COLLATERAL-MANUAL`) |
| `docs/jobs/package-sync.md` § Batched review, the `<x>` legend line "never be asked again" | U7 (`PKG-FR-EFFECT-NOT-MECHANISM`) |
