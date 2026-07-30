# Doc debt

Documentation that is accurate about the code at `0abe7670` and stops being accurate when a Step 3 unit lands, plus documentation already written for the finished behaviour. Spent at Step 4; this file is deleted there.

## Owed when a unit lands

| Where | What it says now | Unit |
| - | - | - |
| `docs/jobs/package-sync.md` § apt collateral | collateral is "remove or downgrade"; nothing said about which removals exempt a package | U1 (`PKG-FR-COLLATERAL-MANUAL`, `PKG-FR-COLLATERAL-MARKED`, `PKG-FR-COLLATERAL-AUTO`) |

## Already written for the finished behaviour

Correct once the unit lands, wrong today. Left as written rather than edited twice; recheck at Step 4.

| Where | Unit |
| - | - |
| `docs/jobs/package-sync.md` § apt collateral, "that becomes its own review item" — true only once a skipped removal keeps its protection | U1 (`PKG-FR-COLLATERAL-MANUAL`) |
| `docs/jobs/package-sync.md` § Batched review, the `<x>` legend line "never be asked again" | U7 (`PKG-FR-EFFECT-NOT-MECHANISM`) |
