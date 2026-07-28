# UAT 02-01 runbook: real-TTY interactive batched review

Drives `.planning/phases/02-package-management-sync/02-UAT.md` test 1 by hand. Nothing in this file has been executed against a machine; every command below is for the user to run.

The batched review is the whole interaction surface of phase 02 and no automated test drives it. Unit tests inject a `FakeReviewer`; the VM suite pre-answers every prompt through `PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` (`packages/review.py`). Decisions are proven, prompts are not.

Each package job runs its OWN review, inside its own `execute()`, before its own first mutating command (`PackageSyncJob.execute` in `packages/sync_core.py`). There is one review per manager and none spanning managers, and a job reviews exactly once — nothing a run writes re-opens a decision it already took.

## 1. Prompt inventory

Everything the review can put on screen, and the section that provokes it.

| Prompt | Shape | Provoked by |
| --- | --- | --- |
| Install / change group | checkbox, pre-ticked | 4a, 4h, 4i |
| Removal group | checkbox, unticked | 4b, 4h |
| Never offer again on this machine | second checkbox over what the apply list left unticked | leave anything unticked in 4a |
| Report-only group | checkbox, and NO permanence question after it | 4c |
| Repository deletion / pin deletion | checkbox, unticked, two answers only | 4d, 4e |
| flatpak remote deletion | checkbox, unticked, two answers only | 4f |
| Repository conflict | per-entry select, two answers, both file bodies shown | 4g |
| flatpak remote-repoint conflict | per-entry select, two answers, both configurations shown | 4j |
| Manual-collateral removal | per-entry select, install-anyway / skip / abort | 4b |
| Unreproducible resolution | per-entry select, three answers, then the multi-line snippet editor | 4k |
| Ubuntu Pro attachment gate | select outside the item review, two answers | 6 |
| Snippet-registry overwrite | the shared `Confirmer` panel, not a review prompt | 4l |

## 2. Where to run it

On the Hetzner test VMs `pc1` (source) and `pc2` (target), from a real terminal on your workstation, over `ssh -t` into pc1. Not on this dev machine: the run installs and removes apt packages, writes `/etc/apt` on the target, converges snaps and flatpaks, and takes btrfs snapshots under `/.snapshots/pc-switcher` (`btrfs_snapshots.py`).

The reviewer is constructed unconditionally by the orchestrator (`Orchestrator._reviewer`, a `TerminalUIReviewer`), and `--yes` feeds only the `Confirmer`, so a plain `pc-switcher sync` on a TTY is exactly the code path under test.

### Lock — your decision

Any use of pc1/pc2 needs the Hetzner label lock. Acquire it yourself and release it yourself. Never run `lock.sh clear`, even against a stale CI holder.

```bash
cd /home/janfr/dev/pc-switcher
export HCLOUD_TOKEN="$(pass show dev/pc-switcher/testing/hcloud_token_rw)"
tests/integration/scripts/internal/lock.sh status
tests/integration/scripts/internal/lock.sh acquire "janfr-uat-02-01"
# ... run the UAT ...
tests/integration/scripts/internal/lock.sh release "janfr-uat-02-01"
```

If `status` reports another holder, stop and decide by hand.

### The VMs

```bash
export PC1="$(hcloud server ip pc1)"
export PC2="$(hcloud server ip pc2)"
```

Both VMs must run this branch's build:

```bash
for h in "$PC1" "$PC2"; do
  ssh testuser@"$h" 'curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash -s -- --ref gsd/phase-02-package-management-sync'
  ssh testuser@"$h" 'pc-switcher --version'
done
```

Both must also carry the snap/flatpak fixtures — currently `FIXTURES_VERSION=4` in `tests/integration/scripts/internal/vm-test-fixtures.sh`; `--with-app` on the source only:

```bash
ssh testuser@"$PC1" 'bash -s -- --with-app' < tests/integration/scripts/internal/vm-test-fixtures.sh
ssh testuser@"$PC2" 'bash -s' < tests/integration/scripts/internal/vm-test-fixtures.sh
```

The fixtures leave pc1 with the flatpak app `io.github.fragglet.sdl_sopwith` (user scope) and the `flathub-beta` remote, and both machines with the `flathub` remote, the `org.freedesktop.Platform/x86_64/25.08` runtime and the snaps `hello` and `hello-world`. Setup below builds on that.

`pc1` and `pc2` resolve each other by name via `/etc/hosts` with bidirectional SSH trust (`tests/integration/scripts/internal/configure-hosts.sh`), so `pc-switcher sync pc2` from pc1 works as-is.

## 3. Rehearsal without VMs

`review_items` and `ask_gate` can be driven directly against the real `TerminalUI` and the real `questionary` widgets from the repo venv. No system state changes; it only renders prompts and prints the resulting `ReviewOutcome`.

The harness lives at `/tmp/claude-1000/-home-janfr-dev-pc-switcher/6f351f45-55a4-4eda-a695-eb874d952f76/scratchpad/review-harness.py` and has been run (its non-interactive path, which prints every group panel and returns all-`skip_once`). It is NOT committed: this runbook's own commit touches one file, and a rehearsal script has no home in `src/` or `tests/`. If it is wanted permanently, `tests/manual/review_harness.py` is the place, in its own commit. Otherwise recreate it from the listing below and run `uv run python <path>` from the repo root.

```python
"""Drive the REAL batched review — real TerminalUI, real questionary widgets — with no
system state touched and no machine contacted.
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
from pcswitcher.ui import TerminalUI

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
        [ReviewEntry("apt:package:tree", "tree (2.1.1-2ubuntu3)", "report", "source has 2.1.1-2ubuntu3.24.04.2")],
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
                "ubuntu.sources differs on the two machines and feeds machine-specific packages on the target: cowsay",
                versions=("# pcsw-uat marker\nTypes: deb\nURIs: http://example/ubuntu\n", "Types: deb\nURIs: http://example/ubuntu\n"),
            )
        ],
    ),
    ReviewGroup(
        "apt",
        REPO_REMOVAL_REVIEW_ACTION,
        "Delete repositorys the source no longer has (apt)",
        [ReviewEntry("apt:source:99-pcsw-uat.list", "99-pcsw-uat.list (list)", "delete repository")],
    ),
    ReviewGroup(
        "apt",
        REPO_REMOVAL_REVIEW_ACTION,
        "Delete pin files the source no longer has (apt)",
        [ReviewEntry("apt:pin:99-pcsw-uat.pref", "99-pcsw-uat.pref", "delete pin file")],
    ),
    ReviewGroup(
        "apt",
        COLLATERAL_REVIEW_ACTION,
        "Resolve apt manual-collateral removals",
        [
            ReviewEntry(
                "apt:collateral:fortunes",
                "fortunes",
                "resolve",
                "manually-installed package that apt's own simulation says would be removed by removing the selected packages",
            )
        ],
    ),
    ReviewGroup(
        "manual",
        UNREPRODUCIBLE_REVIEW_ACTION,
        "Resolve manual items with no reproducible install",
        [ReviewEntry("unreproducible:unowned-path:/opt/pcsw-uat-app", "/opt/pcsw-uat-app", "resolve")],
    ),
]

GATE_MESSAGE = (
    "The source carries ubuntu-esm-apps.sources, which this sync would write to the target, "
    "but the target reports no Ubuntu Pro attachment.\n\n"
    "Skipping runs no part of apt_sync this sync and leaves the target's /etc/apt untouched."
)


async def main() -> None:
    console = Console()
    ui = TerminalUI(console=console, total_steps=1)
    ui.start()
    try:
        gate = await ask_gate(
            title="Ubuntu Pro attachment required on the target",
            message=GATE_MESSAGE,
            proceed_label="I have attached the target — re-check and continue",
            stop_label="Skip apt_sync this run (other jobs continue)",
            console=console,
            ui=ui,
        )
        outcome = await review_items(GROUPS, console=console, ui=ui)
    finally:
        ui.stop()

    # emoji=False, markup=False: an item id like `apt:package:sl` is a Rich emoji
    # shortcode, and would print as `apt<box>sl` under the default settings.
    console.rule("gate")
    console.print(f"  {gate}")
    console.rule("decisions")
    for item_id, decision in sorted(outcome.decisions.items()):
        console.print(f"  {item_id}: {decision}", emoji=False, markup=False)
    console.rule("snippets")
    for item_id, snippet in sorted(outcome.snippets.items()):
        console.print(f"  {item_id}:\n{snippet}", emoji=False, markup=False)
    console.rule("unresolved")
    console.print(f"  {outcome.unresolved}", emoji=False, markup=False)
    console.print(f"\nwas_interactive={outcome.was_interactive}")


asyncio.run(main())
```

What it exercises: questionary rendering, the pause/erase/rebuild of the Rich `Live` region, default tick state per direction, the never-offer-again screen, the two-answer screens, both conflict screens, the collateral and unreproducible flows, the multi-line editor, the gate, and Ctrl-C at every one of them.

What it does not: decision-file writes and their source-vs-target routing, the snippet registry push and replay, `/etc/apt` and flatpak remote convergence, the gate's re-probe loop, and every real package-manager effect. It is a rehearsal, not the UAT result.

## 4. Setup: diverge the two machines

All commands run as `testuser`, which has passwordless sudo on both VMs. Global undo is `tests/integration/scripts/reset-vm.sh pc1` / `... pc2` (it acquires the lock itself, or inherits `PCSWITCHER_LOCK_HOLDER`). Per-step undos are given anyway, and each step says which prompt it buys.

Run every step; the review then contains every screen in one pass.

### 4a. apt installs, and the never-offer-again screen

On pc1:

```bash
apt-cache policy sl cmatrix       # each must show a real Candidate
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y sl cmatrix
```

Two entries in "Install apt packages", both pre-ticked. Leaving one unticked at the review is what makes the never-offer-again screen appear.

Undo: `sudo apt-get purge -y sl cmatrix` on pc1, and on pc2 for whichever the run installed.

### 4b. apt removal, and the manual-collateral prompt

`fortunes` declares `Depends: fortunes-min` (verified against the noble archive), so one construction buys both screens.

On BOTH machines:

```bash
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends fortunes
```

`fortunes` is then manual on both (no diff) and inside pc2's `apt-mark showmanual` set, which is what makes it protected collateral. `fortunes-min` arrives as an automatic dependency on both.

On pc2 only:

```bash
sudo apt-mark manual fortunes-min
apt-get -s remove -y fortunes-min | grep '^Remv'      # must list BOTH fortunes-min and fortunes
```

`fortunes-min` is now manual on pc2 and not on pc1, so it is an `EXTRA_ON_TARGET`/`REMOVE` item in "Remove apt packages", unticked. The plan-time batched `apt-get --dry-run remove` then reports that removing it also removes `fortunes`, which is manual on the target and not itself reviewed — that is the manual-collateral entry (`AptSyncJob._collect_plan_time_collateral`).

The `grep` is the authoritative check. If it lists only `fortunes-min`, the collateral screen will not appear and there is no point going further with this pair.

Add a SECOND removal candidate, so one can be ticked and the other marked never-offer-again — that second entry is what proves D-08a's routing, since a skip-always on a REMOVE item must land on pc2's file and not pc1's. Promoting an existing automatic package to manual changes selection state only, never the disk:

```bash
ssh pc1 'apt-mark showmanual' | sort > /tmp/pc1-manual
apt-mark showmanual | sort > /tmp/pc2-manual
dpkg-query -W -f='${Package}\t${Status}\n' \
  | awk -F'\t' '$2=="install ok installed"{print $1}' | sort > /tmp/pc2-installed
comm -23 <(comm -23 /tmp/pc2-installed /tmp/pc2-manual) /tmp/pc1-manual | head -20
```

Pick one name from that list — call it `X` — whose removal drags nothing else, then promote it:

```bash
apt-get -s remove -y X | grep '^Remv'    # must list X and nothing else
sudo apt-mark manual X
```

Undo: `sudo apt-mark auto fortunes-min X` on pc2; `sudo apt-get install -y --no-install-recommends fortunes` on pc2 if the run removed it; `sudo apt-get purge -y fortunes fortunes-min` on both when finished.

Choosing "Install anyway" really removes both `fortunes-min` and `fortunes` from pc2. Choosing "Skip" removes nothing, but it writes `SKIP_ONCE` over EVERY package in the batched removal candidate set, not only the one that produced the collateral — `_classify_collateral` records the whole candidate set as the trigger — so it also cancels a never-offer-again tick made on `X` in the same review. Answer "Install anyway" in the run where you want `X`'s permanent skip recorded, and check "Skip" in a separate run; the erasure is worth recording either way.

### 4c. A report-only group, with no permanence question after it

On BOTH machines:

```bash
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y tree
```

On pc2, read the version table and downgrade to the older entry:

```bash
apt-cache policy tree                                   # note the two versions
sudo apt-get install -y --allow-downgrades tree=<older version from that table>
```

Same package, same vendor, different versions gives `VERSION_MISMATCH`/`REPORT_ONLY` (`_diff_apt_packages`), which reaches the review as "Report apt packages". That group must NOT be followed by a never-offer-again screen — a report-only item has no machine that holds it, so `SKIP_ALWAYS` is unreachable for it (`_PROMOTABLE_ACTIONS` in `packages/review.py`).

Undo: `sudo apt-get install -y --only-upgrade tree` on pc2.

### 4d. Repository deletion (two answers)

On pc2:

```bash
printf '# pcsw-uat placeholder, no repository declared\n' | sudo tee /etc/apt/sources.list.d/99-pcsw-uat.list
```

Comment-only on purpose: the file must be a real `sources.list.d` member so it is captured and diffed, without declaring an origin `apt-get update` would try to fetch.

pc1 has no such file, so it is an `apt:source:` REMOVE and lands in its own two-answer screen. Expect the title verbatim: `Delete repositorys the source no longer has (apt)` — the pluralisation is a real cosmetic defect (`_REPO_REMOVAL_VERBS` supplies "delete repository" and `_build_review_groups` appends "s"); record it, do not treat it as a failure of this test.

Undo: `sudo rm -f /etc/apt/sources.list.d/99-pcsw-uat.list` on pc2 (the run deletes it if you tick it).

### 4e. Pin deletion (two answers)

On pc2:

```bash
sudo tee /etc/apt/preferences.d/99-pcsw-uat.pref >/dev/null <<'EOF'
Package: tree
Pin: release o=Ubuntu
Pin-Priority: 500
EOF
```

The pin is only ever a FILE item now. It suppresses no package and produces no per-package echo — there is no `HELD_OR_PINNED` diff class any more (`DiffClass` in `packages/items.py`), so `tree` keeps its own report-only line from 4c.

Screen title: `Delete pin files the source no longer has (apt)`, unticked, two answers.

Undo: `sudo rm -f /etc/apt/preferences.d/99-pcsw-uat.pref` on pc2.

### 4f. flatpak remote deletion (two answers)

On pc2:

```bash
flatpak remote-add --user --if-not-exists pcsw-uat https://dl.flathub.org/beta-repo/flathub-beta.flatpakrepo
flatpak remotes --user --columns=name,url
```

pc1 has no `pcsw-uat`, so it is the only direction a remote is still a review line: `Delete flatpak remotes the source no longer has`, unticked, two answers. Nothing is installed from it, so its `detail` names no orphaned refs.

Undo: `flatpak remote-delete --user pcsw-uat` on pc2.

### 4g. Repository conflict (two answers, both file bodies)

Three conditions have to hold together (`AptSyncJob._capture_repo_conflicts`): a `sources.list.d` file present on BOTH machines with different bytes, at least one package recorded skip-always in pc2's apt decision file, and that package's installed origin declared by that file.

On pc2:

```bash
sudo cp /etc/apt/sources.list.d/ubuntu.sources ~/ubuntu.sources.bak
sudo sed -i '1i # pcsw-uat marker' /etc/apt/sources.list.d/ubuntu.sources
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y cowsay
apt-cache policy cowsay          # its installed origin must be the archive ubuntu.sources declares
```

A leading `#` line is a legal deb822 comment, so apt still parses the file; only the digest changes.

Then record `cowsay` machine-specific on pc2, by hand. Back up any existing file first — this is a whole-file write:

```bash
mkdir -p ~/.config/pc-switcher
cp ~/.config/pc-switcher/apt.decisions.yaml{,.bak} 2>/dev/null || true
cat > ~/.config/pc-switcher/apt.decisions.yaml <<'EOF'
machine_specific:
  "apt:package:cowsay":
    item_class: apt_package
    label: cowsay
    reason: null
    recorded_at: "2026-07-28T00:00:00+00:00"
EOF
```

That entry makes `cowsay` structurally invisible: `filter_inert` drops it from pc2's manifest, so it produces no diff of its own, and pc1 does not have it either. It exists purely to make `ubuntu.sources` a file whose overwrite would move software the user told the tool to leave alone — which is the one repository CHANGE that is still a question.

Expect: `Resolve apt repository conflicts`, one entry labelled `ubuntu.sources`, whose detail names `cowsay`, with the target's body and then the source's body in two panels — never a unified diff. Two answers.

"Overwrite" writes pc1's `ubuntu.sources` onto pc2, which removes the marker line; confirm beforehand that the two files are otherwise identical so this is a restore and nothing else:

```bash
diff <(ssh pc1 sudo cat /etc/apt/sources.list.d/ubuntu.sources) <(sudo sed 1d /etc/apt/sources.list.d/ubuntu.sources)
```

"Skip for now" keeps the target's version and seeds it as a failed derived write, so any approved install whose origin depended on it fails naming your decision (`_build_derived_writes`). With `cowsay` as the only machine-specific package there is no such install, so nothing should fail.

Undo: `sudo cp ~/ubuntu.sources.bak /etc/apt/sources.list.d/ubuntu.sources`; `sudo apt-get purge -y cowsay`; restore or delete `~/.config/pc-switcher/apt.decisions.yaml`.

### 4h. snap divergence

On pc2:

```bash
snap list hello hello-world                  # note the current channels and revisions first
sudo snap remove hello-world                 # -> "Install snap packages"
sudo snap refresh hello --channel=beta       # -> "Change snap packages" (revision and/or channel)
```

Undo: `sudo snap install hello-world`; `sudo snap refresh hello --channel=<noted channel> --revision=<noted revision>`.

### 4i. flatpak ref divergence

Already in the baseline: `io.github.fragglet.sdl_sopwith` is installed on pc1 only. Confirm:

```bash
ssh pc1 'flatpak list --user --app --columns=ref'
flatpak list --user --app --columns=ref
```

If pc2 has it: `flatpak uninstall --user -y io.github.fragglet.sdl_sopwith`.

### 4j. flatpak remote-repoint conflict (two answers, both configurations)

The trigger is narrower than apt's (`FlatpakSyncJob._capture_remote_conflicts`): the remote must be one this run would provision for an approved ref install, it must differ in URL or GPG verification, and a ref the TARGET recorded skip-always must take it as its origin in the same scope.

One ref satisfies both halves. On pc2:

```bash
flatpak remotes --user --columns=name,url                  # record flathub's URL first
flatpak install --user --assumeyes flathub io.github.fragglet.sdl_sopwith
cp ~/.config/pc-switcher/flatpak.decisions.yaml{,.bak} 2>/dev/null || true
cat > ~/.config/pc-switcher/flatpak.decisions.yaml <<'EOF'
machine_specific:
  "flatpak:ref:user:io.github.fragglet.sdl_sopwith/x86_64/stable":
    item_class: flatpak_ref
    label: io.github.fragglet.sdl_sopwith/x86_64/stable
    reason: null
    recorded_at: "2026-07-28T00:00:00+00:00"
EOF
flatpak remote-modify --user --url=https://dl.flathub.org/beta-repo/ flathub
flatpak remotes --user --columns=name,url                  # flathub must now differ from pc1's
```

The runtime is already on pc2 from the fixtures, so the app install is a small download. Recording it skip-always removes it from pc2's manifest, so pc1's copy is still proposed as an INSTALL — which is what makes `flathub` a derived candidate — while the installed copy still counts as a machine-specific ref depending on that remote.

Expect: `Resolve flatpak remote conflicts`, one entry labelled `flathub remote (user)`, detail naming the ref, and two panels listing only the facets that differ (here `url`).

Answering is where the two paths part. Untick the ref install on the earlier checkbox screen and pc2's remote is left exactly as you set it, whatever you answer here — a conflict answer only takes effect through a ref that needs the remote (`accept_review`). Tick it and answer "Overwrite" and the remote is repointed back to pc1's URL before the install runs; the install of a ref pc2 already has is unverified territory, so treat a per-item failure there as expected rather than as a finding. Tick it and answer "Skip for now" and the install must fail naming your decision.

Undo: `flatpak remote-modify --user --url=<the URL you recorded> flathub`; `flatpak uninstall --user -y io.github.fragglet.sdl_sopwith`; restore or delete `~/.config/pc-switcher/flatpak.decisions.yaml`.

### 4k. Unreproducible item, three-way resolution and the snippet editor

On pc1:

```bash
sudo mkdir -p /opt/pcsw-uat-app
echo hi | sudo tee /opt/pcsw-uat-app/README >/dev/null
grep -c 'unowned-path:/opt/pcsw-uat-app' ~/.config/pc-switcher/package-snippets.yaml 2>/dev/null || true
```

The scan covers the immediate children of `/usr/local`, `/opt`, `/usr/local/bin` and `/usr/local/lib` (`_UNOWNED_SCAN_ROOTS`), so this becomes `unreproducible:unowned-path:/opt/pcsw-uat-app` with no registry entry. `plan()` classifies it `REPORT_ONLY` and carves it into `Resolve manual items with no reproducible install`. The grep must print `0` or fail; a pre-existing snippet would make it an ordinary install instead.

A stock VM should yield only this one entry. If the group lists other unowned paths, answer them "Skip for now" — an interactive review must resolve every entry, so there is no way past them.

Author this snippet when you choose "Add an install snippet":

```
sudo mkdir -p /opt/pcsw-uat-app
```

It is replayed on pc2 in the SAME run (`after_review` finalizes, pushes the registry, then promotes the item to an approved INSTALL), so the directory really appears there.

Undo: `sudo rm -rf /opt/pcsw-uat-app` on pc1 and on pc2; remove the entry from `~/.config/pc-switcher/package-snippets.yaml` on both.

### 4l. Optional: the snippet-registry overwrite confirmation

Only if you want the non-additive push gate as well. On pc2, put an entry in the registry that pc1 does not have:

```bash
cp ~/.config/pc-switcher/package-snippets.yaml{,.bak} 2>/dev/null || true
cat > ~/.config/pc-switcher/package-snippets.yaml <<'EOF'
snippets:
  "unreproducible:unowned-path:/opt/pcsw-target-only":
    label: /opt/pcsw-target-only
    body: "true\n"
    authored_at: "2026-07-28T00:00:00+00:00"
    authored_on: pc2
EOF
```

`_guard_registry_overwrite` then shows a `Confirmer` panel naming the entry the wholesale push would LOSE. Declining aborts the whole sync with `SyncAbortedByUser`; accepting overwrites pc2's registry.

Undo: restore or delete `~/.config/pc-switcher/package-snippets.yaml` on pc2.

### 4m. Optional: an apt hold

On pc1: `sudo apt-mark hold tree`. Gives a separate `Hold apt packages` group with its own verb, distinct from the package's own item. Undo: `sudo apt-mark unhold tree`.

## 5. The two runs

Write the config on pc1. The orchestrator resolves `sync_jobs` in key order, so the reviews arrive apt, snap, flatpak, manual_installs. `folder_sync` and `vscode_state_sync` are left out to keep the run short.

```bash
mkdir -p ~/.config/pc-switcher
cat > ~/.config/pc-switcher/config.yaml <<'EOF'
logging:
  file: DEBUG
  tui: INFO
  external: WARNING

sync_jobs:
  apt_sync: true
  snap_sync: true
  flatpak_sync: true
  manual_installs_sync: true

disk_space_monitor:
  preflight_minimum: "5%"
  runtime_minimum: "3%"
  warning_threshold: "10%"
  check_interval: 5

btrfs_snapshots:
  subvolumes:
    - "@"
    - "@home"
  keep_recent: 2
EOF
```

The automation escape hatch must NOT be set, or nothing prompts at all:

```bash
env | grep -i PCSWITCHER_PACKAGE_REVIEW_AUTOMATION || echo "not set — good"
```

Both ends of the pipe must be a real TTY (`is_interactive` in `terminal.py` requires both):

```bash
python3 -c 'import sys; print(sys.stdin.isatty(), sys.stdout.isatty())'   # must print True True
```

### Pass 1 — every prompt, nothing converged

```bash
ssh -t testuser@"$PC1"
# on pc1:
pc-switcher sync pc2 --dry-run --yes --allow-first-sync
```

A dry run plans and reviews exactly as a real run does: every group is built, every prompt is shown, and the collateral and conflict screens are computed from read-only `apt-get --dry-run` and `apt-cache policy` calls. What it does not do is converge anything, write a decision file, persist a snippet, push the registry or write `/etc/apt` (`_record_permanent_skips` and `_finalize_unreproducible` return early under `dry_run`; `apply()` logs `[dry-run] Would ...` lines instead of issuing commands).

Walk the whole review here first and check section 7 against it. A snippet authored in this pass is discarded, so you will author it again in pass 2.

### Pass 2 — the real run

```bash
pc-switcher sync pc2 --yes --allow-first-sync
```

`--yes` auto-accepts the config-sync confirmation only; `--allow-first-sync` skips the first-sync overwrite prompt. Neither touches the package review. Add `--allow-out-of-order` on repeat runs if the topology check prompts.

Answer with a deliberate mix so every outcome is provable: tick `sl` and leave `cmatrix` unticked, then tick `cmatrix` on the never-offer-again screen; tick the `fortunes-min` removal, leave `X` unticked and tick `X` on that group's never-offer-again screen; answer the collateral prompt "Install anyway" (see 4b for why "Skip" would erase `X`'s permanent mark); answer the repository conflict "Overwrite"; tick the pin deletion and leave the repository deletion unticked; author the snippet.

## 6. The Ubuntu Pro gate — its own run

The gate is `apt_sync`'s only question that is not about an item, it fires inside `plan()` before any review group is built, and it is skipped under `--dry-run` (which warns instead). Provoke it separately, then undo it, because its "skip" answer costs the whole apt job.

Both VMs are unattached and carry no ESM sources, so make pc1 carry one:

```bash
sudo tee /etc/apt/sources.list.d/ubuntu-esm-apps.sources >/dev/null <<'EOF'
Enabled: no
Types: deb
URIs: https://esm.ubuntu.com/apps/ubuntu
Suites: noble-apps-security noble-apps-updates
Components: main
Signed-By: /usr/share/keyrings/ubuntu-pro-esm-apps.gpg
EOF
ssh pc2 'pro status --format json' | head -c 120   # "attached": false
```

`Enabled: no` keeps pc1's own apt from ever fetching from it; the gate keys on the filename and the digest, not on the content (`_ESM_SOURCE_FILENAMES`, `_pending_esm_writes`). Do not run `apt-get update` on pc1 while it is there.

Then run the real sync again and answer both ways:

- "I have attached the target — re-check and continue" without having attached anything: the gate RE-PROBES, logs `The target still reports no Ubuntu Pro attachment.` at WARNING, and asks again. The loop is unbounded by design.
- "Skip apt_sync this run (other jobs continue)": `JobSkipped`, so `apt_sync` reports `SKIPPED` (not FAILED, not SUCCESS), `/etc/apt` on pc2 is untouched, and snap, flatpak and manual_installs still run.
- Ctrl-C: `Sync aborted: sync aborted at a gate question (Ctrl-C): Ubuntu Pro attachment required on the target`.

Undo: `sudo rm -f /etc/apt/sources.list.d/ubuntu-esm-apps.sources` on pc1.

Do not answer "continue" after actually attaching pc2 unless you intend the ESM sources to land there.

## 7. What to check on screen

- The Rich `Live` region is erased before the first widget of a job's review and rebuilt after the last one — `review_items` pauses ONCE per job and resumes in a `finally`, not per group. The ESM gate pauses and resumes on its own, separately. No duplicated panel, no stale frame, no overwritten prompt line.
- Each group is preceded by a bordered panel listing its entries, with every untrusted string wrapped in `Text` (`_render_group_panel`). A package name containing brackets must not crash the run.
- Installs, changes and removals are separate screens, one group per `(action, item class)` (`_build_review_groups`), each titled with the concrete verb: "Install apt packages", "Remove apt packages", "Change snap packages", "Hold apt packages".
- Install-direction and change-direction groups arrive pre-ticked; removal-direction groups arrive unticked (`_REMOVAL_ACTIONS`).
- After a promotable group with anything left unticked, a second checkbox `"<title> — never offer again on this machine?"` appears, preceded by the dim "Enter leaves them for next run" hint. A fully-ticked group must NOT show it.
- The report-only group, both repository/pin deletion groups and the flatpak remote deletion group must NOT be followed by a never-offer-again screen. That is the whole of "two answers".
- Both conflict screens resolve one entry at a time, print the TARGET's version first and the SOURCE's second in separate panels, and offer exactly two choices.
- The collateral screen names the package and offers install-anyway / skip / abort, and comes last in apt's review — it is the only screen that can abort.
- The unreproducible group is a per-entry three-way select, never a checkbox, and prints the non-interactive-replay warning before opening the editor.
- "Add an install snippet" opens a multi-line editor; questionary's own instruction reads `Finish with 'Alt+Enter' or 'Esc then Enter'` and the label says "Esc then Enter to finish". Submitting an EMPTY body must print the yellow "cannot be empty" line and re-prompt the three-way choice, not fall through.
- Apt's review order is: installs, holds, changes, removals, report-only, repository conflicts, repository deletions, pin deletions, collateral. Flatpak's is: ref installs, ref removals, then remote conflicts, then remote deletions (mask groups, if any, sit with the refs under their own action).
- Each manager renders its own review inside its own job step, and nothing prompts again later in that job.

## 8. Abort paths

Ctrl-C is the only abort key the review handles: `questionary` turns it into `KeyboardInterrupt`, `Question.ask()` catches that and returns `None`, and the review turns `None` into `SyncAbortedByUser`. Nothing catches `EOFError`, so Ctrl-D has no defined behaviour here — do not use it as an abort and do not record its outcome as a result.

Test each separately, resetting pc2 between destructive attempts. The expected message after `Sync aborted:` is quoted verbatim.

1. Apply checkbox → `package review aborted at a checkbox screen (Ctrl-C)`.
2. Never-offer-again checkbox → `package review aborted at a never-offer-again screen (Ctrl-C)`.
3. Repository-deletion or pin-deletion checkbox → same message as 1 (they are checkbox screens).
4. Repository conflict select → `package review aborted while resolving the conflict on 'ubuntu.sources' (Ctrl-C)`. The flatpak remote conflict gives the same sentence with `'flathub remote (user)'` — the entry's own label.
5. Unreproducible three-way select → `package review aborted while resolving unreproducible item '/opt/pcsw-uat-app' (Ctrl-C)`.
6. ESM gate select → `sync aborted at a gate question (Ctrl-C): Ubuntu Pro attachment required on the target`.
7. Collateral select → deliberately different: Ctrl-C is NOT an abort there, it records `SKIP_ONCE` and the review continues (`_review_collateral_group`'s `else` branch). Only the explicit "Abort the sync" choice raises, with `collateral removal of manually-installed fortunes declined (abort chosen in review)`. Check both.

In every abort case: exit code 1, the CLI prints one calm `Sync aborted: <msg>` and not the red "Sync failed" path, `ui.resume()` still runs so the terminal is usable without `reset`, and `pc-switcher logs --last` contains the abort once at WARNING.

## 9. Verification after the real run

On pc1:

```bash
cat ~/.config/pc-switcher/apt.decisions.yaml
cat ~/.config/pc-switcher/snap.decisions.yaml
cat ~/.config/pc-switcher/flatpak.decisions.yaml
cat ~/.config/pc-switcher/manual.decisions.yaml
cat ~/.config/pc-switcher/package-snippets.yaml
pc-switcher logs --last
```

Note the manual-installs file is `manual.decisions.yaml` — the manager id is `manual`, not the job name.

On pc2:

```bash
cat ~/.config/pc-switcher/apt.decisions.yaml
cat ~/.config/pc-switcher/package-snippets.yaml
dpkg-query -W -f='${Package}\t${Status}\n' sl cmatrix fortunes fortunes-min tree
apt-mark showmanual | grep -E '^(fortunes|fortunes-min)$'
ls /etc/apt/preferences.d/ /etc/apt/sources.list.d/
snap list
flatpak list --user --app --columns=ref
flatpak remotes --user --columns=name,url
ls -l /opt/pcsw-uat-app
```

Expected:

- pc1's `apt.decisions.yaml` holds only INSTALL/CHANGE-direction skip-always entries (`apt:package:cmatrix`); pc2's holds only REMOVE-direction ones (`apt:package:X`), plus the `cowsay` entry you wrote by hand. `cmatrix` must NOT appear on pc2 and `X` must NOT appear on pc1 — getting that end wrong is the D-08a failure this test exists to catch.
- No `apt:source:`, `apt:pin:` or `flatpak:remote:` id appears in ANY decision file, in either direction — those classes are filtered out of the recording pass entirely (`AptSyncJob._record_permanent_skips`, `FlatpakSyncJob._record_permanent_skips`).
- `diff <(ssh pc1 cat ~/.config/pc-switcher/package-snippets.yaml) ~/.config/pc-switcher/package-snippets.yaml` is empty: the push is a whole-file copy.
- `/opt/pcsw-uat-app` exists on pc2 — the snippet replayed the same run it was authored.
- `99-pcsw-uat.pref` is gone from pc2 if you ticked it; `99-pcsw-uat.list` is still there if you left it unticked.
- `ubuntu.sources` on pc2 no longer carries the marker line if you answered "Overwrite".

Log checks in `pc-switcher logs --last` on pc1: every command appears verbatim at DEBUG with its job and host; each derived `/etc/apt` write appears as `wrote <path> from the source` at FULL, since a derived write has no review line; each provisioned flatpak remote appears as `provision <scope> flatpak remote <name>`; a skipped job carries `Job apt_sync skipped: ...` at WARNING.

Btrfs snapshots, to inspect or roll back:

```bash
sudo find /.snapshots/pc-switcher -mindepth 2 -maxdepth 2   # on both hosts
```

Pre/post pairs per host per session. Nothing else runs during the sync, so they are quiescent.

## 10. Cleanup

Undo 4a-4m and section 6, or reset both VMs, then release the lock.

```bash
tests/integration/scripts/reset-vm.sh pc1
tests/integration/scripts/reset-vm.sh pc2
tests/integration/scripts/internal/lock.sh release "janfr-uat-02-01"
```

## 11. Known unknowns

- Whether the VMs currently exist, are running, hold the baseline snapshot and carry fixtures version 4 is unverified — no `hcloud` command was run.
- `flatpak install` of a ref the target already has (4j's "Overwrite" path) has an unverified exit code. A per-item failure there is not a finding.
- `flatpak remote-add NAME <.flatpakrepo URL>` (4f) is the exact form the fixtures script uses, but it needs network from pc2 to Flathub. If that is unavailable, this screen has no other hand trigger that does not involve a second real remote.
- The apt-package `ORIGIN_MISMATCH` and `REPO_UNAVAILABLE` report classes have no procedure here. Both need two machines drawing one package name from two different vendors, or a source repository whose signing key is missing, which cannot be built on these VMs without adding a real third-party repository and then leaving it behind. Record them as not exercised rather than improvising.
- The fail-fast probe rule (ADR-022) has no safe hand trigger either: it is provoked by breaking a package manager mid-plan, and its visible outcome is the run stopping with `probe on the <host> did not answer — \`<command>\` ...` and that job FAILED. Do not manufacture it during this walkthrough.
- The exact rendering of a questionary widget inside a paused Rich `Live` region — the corruption this test looks for — cannot be predicted from the code, and is the point of running it.
