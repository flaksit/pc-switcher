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

from pcswitcher.jobs.apt_sync.messages import build_esm_gate_message
from pcswitcher.jobs.packages.items import Machines
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
SOURCE_HOST = "atlas"
TARGET_HOST = "nomad"

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
            ReviewEntry("apt:package:cmatrix", "cmatrix (2.0-6)", "install", "from download.example.com"),
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
    # Reported, not asked: this group is printed and the review moves straight on. Its
    # note is the whole point of showing it here — a version difference is the one reported
    # condition with a remedy, and the remedy is not a decision on any screen.
    ReviewGroup(
        "apt",
        "report_only",
        "Version differences (apt packages)",
        [
            ReviewEntry(
                "apt:package:tree",
                "tree (2.1.1-2ubuntu3)",
                "report",
                f"{SOURCE_HOST} has 2.1.1-2ubuntu3.24.04.2, {TARGET_HOST} has 2.1.1-2ubuntu3",
            ),
            ReviewEntry(
                "apt:package:curl",
                "curl (8.5.0-2ubuntu10.6)",
                "report",
                f"{SOURCE_HOST} has 8.5.0-2ubuntu10.6, {TARGET_HOST} has 8.5.0-2ubuntu10.4",
            ),
        ],
        note=f"These converge on their own: run `sudo apt update && sudo apt upgrade` on {TARGET_HOST}.",
    ),
    # TWO conflicting files, because one screen answers a whole batch and the shape of that
    # is the thing a rehearsal has to show: both files' versions are printed first, in pairs,
    # and the single screen underneath carries a row per file.
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
                f"package you marked as specific to {TARGET_HOST}, so a sync normally leaves it alone",
                versions=(
                    "# pcsw-uat marker\nTypes: deb\nURIs: http://example/ubuntu\n",
                    "Types: deb\nURIs: http://example/ubuntu\n",
                ),
            ),
            ReviewEntry(
                "apt:conflict:vendor.sources",
                "vendor.sources",
                "overwrite",
                f"vendor.sources is different on the two machines, and {TARGET_HOST} installs brscan3, "
                f"brscan-skey from it — packages you marked as specific to {TARGET_HOST}, so a sync normally "
                "leaves them alone",
                versions=(
                    "Types: deb\nURIs: https://vendor.example.com/apt\nSuites: stable\nComponents: main\n",
                    "Types: deb\nURIs: https://vendor.example.com/apt\nSuites: testing\nComponents: main\n"
                    "Signed-By: /usr/share/keyrings/vendor.gpg\n",
                ),
            ),
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
                "remove",
                f"{TARGET_HOST} would stop getting software from https://vendor.example.com/apt",
            ),
            ReviewEntry(
                "apt:source:98-old-ppa.sources",
                "98-old-ppa.sources (sources)",
                "remove",
                f"{TARGET_HOST} would stop getting software from https://ppa.launchpadcontent.net/example/ppa; "
                f"{TARGET_HOST} installs example-tool from 98-old-ppa.sources — package you marked as specific "
                f"to {TARGET_HOST}, so it would stay installed but never get another update",
            ),
        ],
    ),
    # THREE pin files, each printed whole above the one screen that answers all three. The
    # rehearsal exists to show what several file bodies in a row do to the screen: how far
    # the decision column ends up, and whether the pointer is still findable after them.
    ReviewGroup(
        "apt",
        REPO_REMOVAL_REVIEW_ACTION,
        f"Delete pin files {SOURCE_HOST} no longer has (apt)",
        [
            ReviewEntry(
                "apt:pin:99-pcsw-uat.pref",
                "99-pcsw-uat.pref",
                "remove",
                content="Package: *\nPin: origin vendor.example.com\nPin-Priority: 900\n",
            ),
            ReviewEntry(
                "apt:pin:70-no-recommends.pref",
                "70-no-recommends.pref",
                "remove",
                content="Package: firefox\nPin: release o=Ubuntu\nPin-Priority: -1\n",
            ),
            ReviewEntry(
                "apt:pin:60-backports.pref",
                "60-backports.pref",
                "remove",
                f"{TARGET_HOST} would stop preferring backports for postgresql-client",
                content=(
                    "Package: postgresql-*\n"
                    "Pin: release a=noble-backports\n"
                    "Pin-Priority: 500\n"
                    "\n"
                    "Package: *\n"
                    "Pin: release a=noble-backports\n"
                    "Pin-Priority: 100\n"
                ),
            ),
        ],
    ),
    # Two collateral packages with DIFFERENT causes and different effects — one removal,
    # one downgrade — which is why they are asked one screen at a time: no single legend
    # could phrase both.
    ReviewGroup(
        "apt",
        COLLATERAL_REVIEW_ACTION,
        f"Packages you installed yourself on {TARGET_HOST} that this sync would remove or downgrade (apt)",
        [
            ReviewEntry(
                "apt:collateral:fortunes",
                "fortunes",
                "remove",
                f"Removing fortunes-min on {TARGET_HOST} would remove fortunes",
                answer_hints=(
                    f"remove fortunes-min from {TARGET_HOST}, so fortunes is removed as well",
                    f"keep fortunes on {TARGET_HOST}; fortunes-min will not be removed; will be asked again next sync",
                ),
            ),
            ReviewEntry(
                "apt:collateral:libgimp2",
                "libgimp2",
                "downgrade",
                f"Installing gimp on {TARGET_HOST} would downgrade libgimp2 from 2.10.38 to 2.10.36",
                answer_hints=(
                    f"install gimp on {TARGET_HOST}, so libgimp2 is downgraded from 2.10.38 to 2.10.36 as well",
                    f"keep libgimp2 on {TARGET_HOST}; gimp will not be installed; will be asked again next sync",
                ),
            ),
        ],
    ),
    ReviewGroup(
        "manual",
        UNREPRODUCIBLE_REVIEW_ACTION,
        f"{SOURCE_HOST} has these and no package manager can install them on {TARGET_HOST} (manual)",
        [
            ReviewEntry("unreproducible:unowned-path:/opt/pcsw-uat-app", "/opt/pcsw-uat-app", "resolve"),
            ReviewEntry("unreproducible:unowned-path:/usr/local/bin/mytool", "/usr/local/bin/mytool", "resolve"),
        ],
    ),
]

# The real builder, not a paraphrase: the gate's whole job is telling the user how to attach
# the target, and a rehearsal that abbreviates that away cannot show whether the instructions
# read well on screen — which is the one thing this harness exists to check.
GATE_MESSAGE = build_esm_gate_message(
    ("ubuntu-esm-apps.sources", "ubuntu-esm-infra.sources"), Machines(SOURCE_HOST, TARGET_HOST), "apt_sync"
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
