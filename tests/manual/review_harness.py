"""Drive the REAL batched review — real TerminalUI, real decision screens and questionary
widgets — with no system state touched and no machine contacted.

A rehearsal for UAT 02-01, not a UAT result. It renders every prompt shape the review can
produce and prints the resulting decisions. It does NOT exercise decision-file writes or
their source-vs-target routing, the snippet registry push and replay, /etc/apt or flatpak
remote convergence, or the ESM gate's re-probe loop — all of those need two real machines.

Run from the repo root:  uv run python <this file>
"""

import asyncio

from rich.console import Console

from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    REPO_CONFLICT_REVIEW_ACTION,
    REPO_REMOVAL_REVIEW_ACTION,
    UNREPRODUCIBLE_REVIEW_ACTION,
    ReviewEntry,
    ReviewGroup,
    ask_gate,
    review_items,
)
from pcswitcher.models import SyncAbortedByUser
from pcswitcher.ui import TerminalUI

# Stand-ins for the two machines, so the rehearsal reads the way a real run does — every
# screen names a machine rather than its role in the run.
SOURCE_HOST = "p17"
TARGET_HOST = "fleksi"

# One group per interaction shape the review supports. The action strings are load-bearing:
# "install" rows start applied and "remove" rows at skip-once (_REMOVAL_ACTIONS);
# "install"/"remove" are promotable so their screens offer the third answer, while
# "report_only" and REPO_REMOVAL_REVIEW_ACTION are two-answer screens (_PROMOTABLE_ACTIONS).
GROUPS = [
    ReviewGroup(
        "apt",
        "install",
        "Install apt packages",
        [
            ReviewEntry("apt:package:sl", "sl (5.02-1)", "install"),
            ReviewEntry("apt:package:cmatrix", "cmatrix (2.0-6)", "install", "from a vendor repository"),
            # Brackets in untrusted text: Rich would parse these as markup if the label
            # were not wrapped in Text. A crash here is the bug this entry exists to catch.
            ReviewEntry("apt:package:weird", "weird [bold red]name[/] 1.0", "install"),
        ],
    ),
    ReviewGroup(
        "apt",
        "remove",
        "Remove apt packages",
        [ReviewEntry("apt:package:fortunes-min", "fortunes-min (1:1.99.1-7.3build1)", "remove")],
    ),
    ReviewGroup(
        "apt",
        "report_only",
        "Report apt packages",
        [
            ReviewEntry(
                "apt:package:tree",
                "tree (2.1.1-2ubuntu3)",
                "report",
                f"{SOURCE_HOST} has 2.1.1-2ubuntu3.24.04.2, {TARGET_HOST} has 2.1.1-2ubuntu3",
            )
        ],
    ),
    ReviewGroup(
        "apt",
        REPO_CONFLICT_REVIEW_ACTION,
        "Resolve apt repository conflicts",
        [
            ReviewEntry(
                "apt:conflict:ubuntu.sources",
                "ubuntu.sources",
                "overwrite",
                f"ubuntu.sources is different on the two machines, and {TARGET_HOST} installs cowsay from it — "
                "packages you set to always skip, so a sync normally leaves them alone",
                versions=(
                    "# pcsw-uat marker\nTypes: deb\nURIs: http://example/ubuntu\n",
                    "Types: deb\nURIs: http://example/ubuntu\n",
                ),
            )
        ],
    ),
    ReviewGroup(
        "apt",
        REPO_REMOVAL_REVIEW_ACTION,
        f"Delete repositories {SOURCE_HOST} no longer has (apt)",
        [
            ReviewEntry(
                "apt:source:99-pcsw-uat.list",
                "99-pcsw-uat.list (list)",
                "delete repository",
                f"{TARGET_HOST} would stop getting software from https://vendor.example.com/apt",
            )
        ],
    ),
    ReviewGroup(
        "apt",
        REPO_REMOVAL_REVIEW_ACTION,
        f"Delete pin files {SOURCE_HOST} no longer has (apt)",
        [
            ReviewEntry(
                "apt:pin:99-pcsw-uat.pref",
                "99-pcsw-uat.pref",
                "delete pin file",
                content="Package: *\nPin: origin vendor.example.com\nPin-Priority: 900\n",
            )
        ],
    ),
    ReviewGroup(
        "apt",
        COLLATERAL_REVIEW_ACTION,
        f"Packages you installed yourself on {TARGET_HOST} that this sync would remove or downgrade (apt)",
        [
            ReviewEntry(
                "apt:collateral:fortunes",
                "fortunes",
                "resolve",
                f"Removing fortunes-min on {TARGET_HOST} would remove fortunes",
            )
        ],
    ),
    ReviewGroup(
        "manual",
        UNREPRODUCIBLE_REVIEW_ACTION,
        f"{SOURCE_HOST} has these and no package manager can install them on {TARGET_HOST} (manual)",
        [ReviewEntry("unreproducible:unowned-path:/opt/pcsw-uat-app", "/opt/pcsw-uat-app", "resolve")],
    ),
]

GATE_MESSAGE = (
    f"{SOURCE_HOST} carries ubuntu-esm-apps.sources, which this sync would copy to {TARGET_HOST} — but "
    f"{TARGET_HOST} is not attached to Ubuntu Pro.\n\n"
    f"Skipping means apt_sync does nothing at all this run and {TARGET_HOST}'s /etc/apt is left exactly as it is. "
    "Every other job still runs."
)

# `ask_gate` answers with a bare bool (or None), which says nothing on its own in a
# rehearsal transcript. These are what each answer MEANS to the run.
GATE_ANSWERS = {
    True: "continue — apt_sync runs and re-checks the attachment",
    False: "skip apt_sync this run — other jobs continue",
    None: "not asked — no TTY, the caller owns the fallback",
}


async def main() -> None:
    console = Console()
    ui = TerminalUI(console=console, total_steps=1)
    ui.start()
    try:
        gate = await ask_gate(
            title=f"{TARGET_HOST} needs an Ubuntu Pro attachment",
            message=GATE_MESSAGE,
            proceed_label=f"I have attached {TARGET_HOST} — check again and continue",
            stop_label="Skip apt_sync this run (every other job still runs)",
            console=console,
            ui=ui,
        )
        outcome = await review_items(
            GROUPS, console=console, ui=ui, source_hostname=SOURCE_HOST, target_hostname=TARGET_HOST
        )
    finally:
        ui.stop()

    # emoji=False, markup=False: an item id like `apt:package:sl` is a Rich emoji
    # shortcode, and would print as `apt<box>sl` under the default settings.
    console.rule("Ubuntu Pro attachment gate")
    console.print(f"  {GATE_ANSWERS[gate]}")
    console.rule("decisions")
    for item_id, decision in sorted(outcome.decisions.items()):
        console.print(f"  {item_id}: {decision}", emoji=False, markup=False)
    console.rule("snippets")
    for item_id, snippet in sorted(outcome.snippets.items()):
        console.print(f"  {item_id}:\n{snippet}", emoji=False, markup=False)
    console.rule("unresolved")
    console.print(f"  {outcome.unresolved}", emoji=False, markup=False)
    console.print(f"\nwas_interactive={outcome.was_interactive}")


try:
    asyncio.run(main())
except SyncAbortedByUser as e:
    # What `cli.py` does with the same exception: a Ctrl-C anywhere in the review is a
    # clean stop, and a traceback here would misrepresent the product as crashing.
    Console().print(f"[yellow]Sync aborted:[/yellow] {e}")
