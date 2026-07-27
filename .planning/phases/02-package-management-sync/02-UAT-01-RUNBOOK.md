# UAT 02-01 runbook: real-TTY interactive batched review

Drives `.planning/phases/02-package-management-sync/02-UAT.md` test 1 by hand. Nothing in this file has been executed; every command below is for the user to run.

The batched review is the entire interaction surface of phase 02 and no automated test has ever driven it. Unit tests inject a `FakeReviewer` (`tests/unit/jobs/test_package_sync_core.py:72`, `tests/unit/jobs/test_manual_installs_sync.py:132`); the VM suite pre-answers the review through `PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` (`src/pcswitcher/jobs/packages/review.py:461-463`). Decisions are proven, prompts are not.

## 1. Where to run it

Run it on the Hetzner test VMs `pc1` (source) and `pc2` (target), from a real terminal on your workstation, over `ssh -t` into pc1. Not on this dev machine: the run installs and removes apt packages, rewrites `/etc/apt` on the target, converges snaps and flatpaks, and takes btrfs snapshots under `/.snapshots/pc-switcher` (`src/pcswitcher/btrfs_snapshots.py:118`). Agents and experiments never touch the dev machine.

The reviewer is constructed unconditionally by the orchestrator (`src/pcswitcher/orchestrator.py:394`), and `--yes` only feeds the `Confirmer` (`src/pcswitcher/cli.py:278`), so a normal `pc-switcher sync` on a TTY is exactly the code path under test.

### Lock — your decision

Any use of pc1/pc2 must hold the Hetzner label lock. Acquire it yourself, and release it yourself when done. Never run `lock.sh clear`, even against a stale CI holder.

```bash
cd /home/janfr/dev/pc-switcher
export HCLOUD_TOKEN="$(pass show dev/pc-switcher/testing/hcloud_token_rw)"
tests/integration/scripts/internal/lock.sh status
tests/integration/scripts/internal/lock.sh acquire "janfr-uat-02-01"
# ... run the UAT ...
tests/integration/scripts/internal/lock.sh release "janfr-uat-02-01"
```

If `status` reports another holder, stop and decide manually — do not clear it.

### Option A (recommended): the VMs

```bash
export PC1="$(hcloud server ip pc1)"
export PC2="$(hcloud server ip pc2)"
```

Both VMs must run this branch's build. `install.sh --ref` takes a branch, and `gsd/phase-02-package-management-sync` is pushed at `702aa993`, matching local HEAD.

```bash
for h in "$PC1" "$PC2"; do
  ssh testuser@"$h" 'curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash -s -- --ref gsd/phase-02-package-management-sync'
  ssh testuser@"$h" 'pc-switcher --version'
done
```

Both VMs must also carry the snap/flatpak fixtures (`tests/integration/scripts/internal/vm-test-fixtures.sh`; `--with-app` on the source only):

```bash
ssh testuser@"$PC1" 'bash -s -- --with-app' < tests/integration/scripts/internal/vm-test-fixtures.sh
ssh testuser@"$PC2" 'bash -s' < tests/integration/scripts/internal/vm-test-fixtures.sh
```

`pc1` and `pc2` resolve each other by name via `/etc/hosts` with bidirectional SSH trust already configured (`tests/integration/scripts/internal/configure-hosts.sh:88-93,185-188`), so `pc-switcher sync pc2` from pc1 works as-is.

### Option B: local harness, no VMs, no system change

There is no offline harness for a real sync, but `review_items` can be driven directly against the real `TerminalUI` and the real `questionary` widgets from the repo venv. This changes no system state — it only renders prompts and prints the resulting `ReviewOutcome`.

Write this to a scratch file (e.g. `/tmp/claude-1000/-home-janfr-dev-pc-switcher/review-harness.py`) and run `uv run python <path>` from the repo root:

```python
import asyncio

from rich.console import Console

from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    UNREPRODUCIBLE_REVIEW_ACTION,
    ReviewEntry,
    ReviewGroup,
    review_items,
)
from pcswitcher.ui import TerminalUI

GROUPS = [
    ReviewGroup("apt", "install", "Install apt packages", [
        ReviewEntry("apt:package:sl", "sl 5.02-2", "install"),
        ReviewEntry("apt:package:cowsay", "cowsay 3.03+dfsg2-8", "install"),
    ]),
    ReviewGroup("apt", "remove", "Remove apt packages", [
        ReviewEntry("apt:package:libfoo1", "libfoo1 1.2-3", "remove"),
    ]),
    ReviewGroup("apt", COLLATERAL_REVIEW_ACTION, "Manually-installed collateral", [
        ReviewEntry("apt:collateral:file", "file", "review",
                    "would be removed by removing the selected packages"),
    ]),
    ReviewGroup("manual_installs", UNREPRODUCIBLE_REVIEW_ACTION,
                "Resolve manual_installs items with no reproducible install", [
        ReviewEntry("unreproducible:unowned-path:/opt/pcsw-uat-app", "/opt/pcsw-uat-app", "resolve"),
    ]),
]


async def main() -> None:
    console = Console()
    ui = TerminalUI(console=console, total_steps=1)
    ui.start()
    try:
        outcome = await review_items(GROUPS, console=console, ui=ui)
    finally:
        ui.stop()
    print(outcome.decisions)
    print(outcome.snippets)


asyncio.run(main())
```

What Option B does exercise: questionary rendering, the pause/erase/rebuild of the Rich `Live` region around each prompt (`src/pcswitcher/ui.py:179,195`), install-vs-removal grouping and default tick state, the never-offer-again second checkbox, the multi-line snippet editor, the collateral three-way prompt, and Ctrl-C / Ctrl-D behaviour at every prompt type.

What it does NOT exercise: the decision-file writes and their source-vs-target routing, the snippet registry push to the target, `/etc/apt` convergence, the second mid-`execute()` review, and any real package-manager state. Use it as a cheap rehearsal, not as the UAT result.

## 2. Setup: diverge the two machines in both directions

All commands run as `testuser`, which has passwordless sudo on both VMs. Cheapest global undo is `tests/integration/scripts/reset-vm.sh pc1` / `... pc2` (it acquires the lock itself, or inherits `PCSWITCHER_LOCK_HOLDER`); per-step undos are given anyway.

### 2a. apt installs (source has, target lacks)

On pc1:

```bash
apt-cache policy sl cowsay tree            # each must show a real Candidate
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y sl cowsay tree
```

Each becomes manual on pc1 and absent on pc2, producing `MISSING_ON_TARGET` / `INSTALL` diffs (`src/pcswitcher/jobs/apt_sync.py:363-372`).

Undo: `sudo apt-get purge -y sl cowsay tree` on pc1, and on pc2 too if the run installed them.

### 2b. apt removals (target has, source lacks)

Mirrors what the integration suite does (`tests/integration/jobs/test_package_sync.py:263-297`): promote a pc2-only auto-installed package to manual, which changes only selection state, not the disk.

On pc2:

```bash
ssh pc1 'apt-mark showmanual' | sort > /tmp/pc1-manual
apt-mark showmanual | sort > /tmp/pc2-manual
dpkg-query -W -f='${Package}\t${Status}\n' \
  | awk -F'\t' '$2=="install ok installed"{print $1}' | sort > /tmp/pc2-installed
comm -23 <(comm -23 /tmp/pc2-installed /tmp/pc2-manual) /tmp/pc1-manual | head -20
```

Pick two names from that list — call them `X` and `Y`:

```bash
sudo apt-mark manual X Y
```

`X` gives the ordinary removal group (`EXTRA_ON_TARGET` / `REMOVE`, `src/pcswitcher/jobs/apt_sync.py:385-395`). `Y` is reserved for the pin in 2c and will NOT appear in the first-pass removal group.

Undo: `sudo apt-mark auto X Y` on pc2. Note that ticking `X`'s removal in the review really removes it from pc2.

### 2c. `/etc/apt` divergence that fires the SECOND review

This is the deterministic construction and the one to rely on. A pin file on the target suppresses `Y` into `HELD_OR_PINNED` / `REPORT_ONLY` at plan time (`src/pcswitcher/jobs/apt_sync.py:347-356`); the source has no such file, so the file itself is an `EXTRA_ON_TARGET` / `REMOVE` repository-group item (`src/pcswitcher/jobs/apt_sync.py:1630-1634`). Approving that removal inserts the metadata-refresh marker (`src/pcswitcher/jobs/apt_sync.py:1834-1848`), `apply()` converges `/etc/apt` first (`src/pcswitcher/jobs/apt_sync.py:1885`), the re-diff finds `Y` now `REMOVE`, and that revealed item goes to a second review (`src/pcswitcher/jobs/apt_sync.py:1965-1967`).

On pc2, with `Y` substituted:

```bash
sudo tee /etc/apt/preferences.d/99-pcsw-uat.pref >/dev/null <<'EOF'
Package: Y
Pin: release o=Ubuntu
Pin-Priority: 900
EOF
sudo find /etc/apt/preferences.d -maxdepth 1 -type f -exec awk '/^Package:/{print FILENAME "\t" $0}' {} +
```

The second command is the same probe the job runs (`src/pcswitcher/jobs/apt_sync.py:1197`); it must print your stanza.

Undo: `sudo rm -f /etc/apt/preferences.d/99-pcsw-uat.pref` on pc2 (the run itself deletes it if you tick the removal).

### 2d. Optional: a package whose install adds a repository

The faithful version of the second-review trigger's other half — a repository installed this run supplying a package apt had no candidate for at plan time. Add a third-party repo on pc1 and install a package from it (e.g. GitHub CLI from `https://cli.github.com/packages`, or Tailscale), then verify on pc2 BEFORE the run:

```bash
apt-cache policy <pkg>     # on pc2: must show "Candidate: (none)" or no block at all
```

If pc2 already has a candidate, this construction reveals nothing and you should rely on 2c instead. I could not verify from this repository whether any specific vendor package is absent from Ubuntu 24.04's archive on these VMs — treat the choice of package as unverified until that `apt-cache policy` check passes.

Undo: remove the `.sources`/`.list` file and its keyring from pc1's `/etc/apt`, and purge the package.

### 2e. Collateral (D-30), if you want it

Cheapest reliable construction is the removal direction: `_collect_plan_time_collateral` simulates `apt-get remove -y <approved removals>` on the target (`src/pcswitcher/jobs/apt_sync.py:1695-1699`), and any would-be-removed package in the target's or source's manual set becomes a collateral review item (`src/pcswitcher/jobs/apt_sync.py:1723-1731`).

So pick `X` in 2b such that removing it would take a manually-installed package with it. Authoritative check, on pc2, before the run:

```bash
apt-get -s remove -y X | sed -n '/REMOVED/,/^$/p'
comm -12 <(apt-get -s remove -y X | grep -oP '^Remv \K\S+' | sort) /tmp/pc2-manual
```

If the second command prints anything, `X` yields a collateral prompt. If nothing on pc2 qualifies, say so in the UAT result rather than inventing a conflict pair — I could not confirm from this repo which packages on these VMs have manually-installed reverse dependencies.

Choosing "Install anyway" at the collateral prompt really removes the collateral package from pc2. Prefer "Skip" or "Abort" unless you intend the removal (and reset the VM afterwards either way).

### 2f. snap divergence

On pc2:

```bash
sudo snap remove hello-world        # -> MISSING_ON_TARGET / INSTALL (snap_sync.py:253-254)
sudo snap refresh hello --channel=beta   # optional: revision/channel CHANGE (snap_sync.py:285)
```

Undo: `sudo snap install hello-world`; `sudo snap refresh hello --channel=stable`.

### 2g. flatpak divergence

Already present in the baseline: `vm-test-fixtures.sh` installs `io.github.fragglet.sdl_sopwith` on pc1 only. Confirm on both:

```bash
flatpak list --user --app --columns=application
```

If pc2 has it: `flatpak uninstall --user -y io.github.fragglet.sdl_sopwith`.

### 2h. Unreproducible item with no snippet

On pc1:

```bash
sudo mkdir -p /opt/pcsw-uat-app
echo hi | sudo tee /opt/pcsw-uat-app/README >/dev/null
grep -c 'unowned-path:/opt/pcsw-uat-app' ~/.config/pc-switcher/package-snippets.yaml 2>/dev/null || true
```

The scan covers the immediate children of `/usr/local`, `/opt`, `/usr/local/bin`, `/usr/local/lib` (`src/pcswitcher/jobs/manual_installs_sync.py:89,417-431`), so this becomes item id `unreproducible:unowned-path:/opt/pcsw-uat-app` (`src/pcswitcher/jobs/manual_installs_sync.py:131-133`) with no registry entry, which plans `REPORT_ONLY` and lands in the three-way resolution group (`src/pcswitcher/jobs/manual_installs_sync.py:459-495,497-527`). The grep must print `0` or fail — a pre-existing snippet would make it an ordinary install instead. The list may also contain other unowned paths that pre-exist on the VM; that is expected.

Undo: `sudo rm -rf /opt/pcsw-uat-app` on pc1 and, if the snippet ran, on pc2.

## 3. The run

Write the config on pc1 (source). Job order follows the key order in `sync_jobs` (`src/pcswitcher/default-config.yaml:41,52-55`), so the review screens arrive apt, snap, flatpak, manual_installs. `folder_sync` and `vscode_state_sync` are left out deliberately to keep the run short.

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

Confirm the automation escape hatch is NOT set — if it is, nothing prompts at all (`src/pcswitcher/jobs/packages/review.py:461-463`):

```bash
env | grep -i PCSWITCHER_PACKAGE_REVIEW_AUTOMATION || echo "not set — good"
```

Confirm you are on a real TTY on both ends of the pipe; `is_interactive` requires both (`src/pcswitcher/terminal.py:21`):

```bash
python3 -c 'import sys; print(sys.stdin.isatty(), sys.stdout.isatty())'   # must print True True
```

Then, from your workstation, with a TTY forced:

```bash
ssh -t testuser@"$PC1"
# on pc1:
pc-switcher sync pc2 --yes --allow-first-sync
```

`--yes` auto-accepts the config-sync confirmation only; `--allow-first-sync` skips the first-sync overwrite prompt. Neither touches the package review. Optionally do a rehearsal first with `--dry-run`, which still runs every first-pass review (`src/pcswitcher/jobs/packages/sync_core.py:493-497`) but records nothing (`src/pcswitcher/jobs/packages/sync_core.py:432`) and skips the second review entirely (`src/pcswitcher/jobs/apt_sync.py:1885`).

## 4. Checklist

Answer the review with a deliberate mix so each outcome is provable: tick some installs and untick others, tick `X`'s removal, leave at least one install unticked AND tick it on the never-offer-again screen, and author a snippet for `/opt/pcsw-uat-app`.

- The Rich `Live` region is erased before the first questionary widget draws and rebuilt after it (`src/pcswitcher/jobs/packages/review.py:478,521-522`; `src/pcswitcher/ui.py:179,195`). No duplicated panel, no stale frame, no overwritten prompt line.
- Each group is preceded by a bordered panel listing its entries with untrusted text wrapped in `Text` (`src/pcswitcher/jobs/packages/review.py:228-245,483-484`). A package name containing brackets must not crash the run.
- Installs and removals are separate screens, one `ReviewGroup` per `(action, item_class)` (`src/pcswitcher/jobs/packages/sync_core.py:254-285`), titled with the concrete verb ("Install apt packages", "Remove apt packages").
- Install-direction entries arrive pre-ticked, removal-direction entries arrive unticked (`src/pcswitcher/jobs/packages/review.py:494-502` with `_REMOVAL_ACTIONS` at `:100`).
- After a checkbox screen with anything left unticked, a second checkbox appears — `"<title> — never offer again on this machine?"` — preceded by the dim "Enter leaves them for next run" hint (`src/pcswitcher/jobs/packages/review.py:363-375`). A fully-ticked group must NOT show it (`:518-520`).
- `REPORT_ONLY` groups (version mismatches, held/pinned echoes, repo-unavailable) get no never-offer-again screen (`src/pcswitcher/jobs/packages/review.py:108,517`).
- Apply lands as real convergence; skip-once leaves the item untouched and re-offers next run; skip-always writes a `DecisionEntry` (`src/pcswitcher/jobs/packages/sync_core.py:414-459`).
- Skip-always on an INSTALL or CHANGE item writes to `~/.config/pc-switcher/<manager>.decisions.yaml` on **pc1**; skip-always on a REMOVE item writes to the same path on **pc2**, over the remote executor (`src/pcswitcher/jobs/packages/sync_core.py:195-204,449-459`; path template `src/pcswitcher/jobs/packages/state.py:73`). Getting the end wrong is the D-08a failure this test exists to catch.
- The unreproducible group is a per-entry three-way `select`, never a checkbox (`src/pcswitcher/jobs/packages/review.py:294-302`), with the non-interactive-replay warning printed before the editor (`:219-225,324`).
- Choosing "Add an install snippet" opens a multi-line editor; questionary's own instruction says `Finish with 'Alt+Enter' or 'Esc then Enter'`, the prompt label says "Esc then Enter to finish" (`src/pcswitcher/jobs/packages/review.py:325-327`). Submitting an EMPTY body must print the yellow "cannot be empty" line and re-prompt the three-way choice, not fall through (`:329-337`).
- The authored snippet lands verbatim (never stripped) in `~/.config/pc-switcher/package-snippets.yaml` on pc1 (`src/pcswitcher/jobs/packages/state.py:83`), is promoted to an INSTALL for this same run (`src/pcswitcher/jobs/manual_installs_sync.py:236`), is pushed to pc2 before apply (`src/pcswitcher/jobs/manual_installs_sync.py:273`), and replays there (`src/pcswitcher/jobs/manual_installs_sync.py:529-541`). A registry overwrite that would lose or change an existing snippet triggers its own confirmation prompt (`src/pcswitcher/jobs/manual_installs_sync.py:314`).
- The collateral prompt names the package and offers install-anyway / skip / abort (`src/pcswitcher/jobs/packages/review.py:420-427`). Abort raises `SyncAbortedByUser` naming the package (`:433-435`); skip leaves the triggering install unapproved (`:436-439`).
- After the apt repository group converges, a SECOND set of review screens appears, every title suffixed `(revealed by this run's /etc/apt changes)` (`src/pcswitcher/jobs/apt_sync.py:2038-2041`), containing `Y` as a removal. This is the screen nothing has ever exercised: confirm the Live display pauses and resumes cleanly here too, mid-`apply()`, with `/etc/apt` already written.
- Withdrawn approvals are logged, not re-asked (`src/pcswitcher/jobs/apt_sync.py:1954-1960`).
- Snap and flatpak each render their own review, separate from apt's — one review per manager inside that manager's own `execute()` (`src/pcswitcher/jobs/packages/sync_core.py:493-497`).

## 5. Abort paths

Behaviour per code: Ctrl-C at any review screen aborts the WHOLE sync with `SyncAbortedByUser`, never a per-item skip. Verified call sites:

- apply checkbox: `raise SyncAbortedByUser("package review aborted at a checkbox screen (Ctrl-C/EOF)")` (`src/pcswitcher/jobs/packages/review.py:506-511`).
- never-offer-again checkbox: `src/pcswitcher/jobs/packages/review.py:378-379`.
- unreproducible three-way select: `src/pcswitcher/jobs/packages/review.py:305-311`.
- collateral select: only the explicit "Abort the sync" choice raises; a cancelled select falls into the `else` branch and records `SKIP_ONCE` (`src/pcswitcher/jobs/packages/review.py:436-439`). This one is deliberately different — check it behaves that way.

The exception is caught once at WARNING and the CLI prints `Sync aborted: <msg>` and exits 1 (`src/pcswitcher/cli.py:398-403`). `ui.resume()` still runs in the `finally` (`src/pcswitcher/jobs/packages/review.py:521-522`), so the terminal must be left usable.

Ctrl-C mechanics, verified against the vendored library: questionary binds Ctrl-C and Ctrl-Q on checkbox and select to `app.exit(exception=KeyboardInterrupt)` (`.venv/lib/python3.14/site-packages/questionary/prompts/checkbox.py:229-232`, `.../select.py:209-212`), and `Question.ask()` catches `KeyboardInterrupt`, prints `Cancelled by user`, and returns `None` (`.venv/lib/python3.14/site-packages/questionary/question.py:48-65`). `None` is what the review turns into `SyncAbortedByUser`.

EOF (Ctrl-D) is NOT the same, and the code comments saying "Ctrl-C / EOF (`ask` returns `None`)" appear to be wrong for the EOF half. `Question.ask()` catches only `KeyboardInterrupt`; `EOFError` is not caught. The multi-line snippet editor is a plain prompt_toolkit `PromptSession` (`.venv/lib/python3.14/site-packages/questionary/prompts/text.py:91-99`), whose default binding raises `EOFError` on Ctrl-D at an empty buffer. Hypothesis, not measured: Ctrl-D in the snippet editor surfaces as an uncaught `EOFError` traceback rather than a clean `Sync aborted:` line. Checkbox and select bind no Ctrl-D at all, so Ctrl-D there most likely does nothing — also unmeasured.

Test each of these separately, resetting pc2 between destructive attempts:

1. Ctrl-C at the first apt install checkbox → expect `Cancelled by user`, then `Sync aborted: package review aborted at a checkbox screen (Ctrl-C/EOF)`, exit 1, nothing converged on pc2.
2. Ctrl-C at the never-offer-again checkbox → `Sync aborted: package review aborted at a never-offer-again screen (Ctrl-C/EOF)`.
3. Ctrl-C at the unreproducible three-way select → `Sync aborted: package review aborted while resolving unreproducible item '/opt/pcsw-uat-app' (Ctrl-C/EOF)`.
4. "Abort the sync" at the collateral prompt → `Sync aborted: collateral removal of manually-installed <pkg> declined (abort chosen in review)`.
5. Ctrl-C at the SECOND review (post-`/etc/apt`) → same clean abort, but note that `/etc/apt` is already converged on pc2; the docstring calls this a reviewed, coherent state (`src/pcswitcher/jobs/apt_sync.py:1877-1880`). Verify pc2's `/etc/apt` matches pc1's for the items you ticked.
6. Ctrl-D at the snippet editor → record exactly what happens (clean abort vs traceback). This is the likeliest defect in the set.
7. Ctrl-D at a checkbox and at a select → record whether the key is ignored.

In every case the terminal must be usable afterwards (`reset` not required) and `pc-switcher logs --last` must contain the abort at WARNING exactly once.

## 6. Verification after the run

On pc1:

```bash
cat ~/.config/pc-switcher/apt.decisions.yaml
cat ~/.config/pc-switcher/snap.decisions.yaml
cat ~/.config/pc-switcher/flatpak.decisions.yaml
cat ~/.config/pc-switcher/manual_installs.decisions.yaml
cat ~/.config/pc-switcher/package-snippets.yaml
pc-switcher logs --last
```

On pc2:

```bash
cat ~/.config/pc-switcher/apt.decisions.yaml         # only REMOVE-direction skip-always entries
cat ~/.config/pc-switcher/package-snippets.yaml      # pushed copy, byte-identical to pc1's
dpkg-query -W -f='${Package}\t${Status}\n' | grep -E '^(sl|cowsay|tree)\b'
apt-mark showmanual | grep -E '^(X|Y)$'
ls /etc/apt/preferences.d/
snap list
flatpak list --user --app --columns=application
ls -l /opt/pcsw-uat-app
```

Expected: pc1's `apt.decisions.yaml` holds only INSTALL/CHANGE-direction skip-always entries, pc2's only REMOVE-direction ones. The two `package-snippets.yaml` files must be identical (`diff <(ssh pc1 cat ~/.config/pc-switcher/package-snippets.yaml) ~/.config/pc-switcher/package-snippets.yaml`). `99-pcsw-uat.pref` is gone from pc2 if you ticked its removal.

Log checks (`pc-switcher logs --last` on pc1): every command appears verbatim at DEBUG with job and host; the second review's group titles carry the `(revealed by this run's /etc/apt changes)` suffix; any withdrawn approval is logged as `withdrawing <label>: this run's /etc/apt changes make it ...` (`src/pcswitcher/jobs/apt_sync.py:1955-1960`).

Btrfs snapshots, if you need to inspect or roll back:

```bash
sudo find /.snapshots/pc-switcher -mindepth 2 -maxdepth 2   # both hosts
```

Pre/post pairs per host per session (`src/pcswitcher/btrfs_snapshots.py:185`). Nothing else runs during the sync, so these are quiescent.

Cleanup: undo the 2a-2h steps, or reset both VMs and release the lock.

```bash
tests/integration/scripts/reset-vm.sh pc1
tests/integration/scripts/reset-vm.sh pc2
tests/integration/scripts/internal/lock.sh release "janfr-uat-02-01"
```

## 7. Known unknowns

- Ctrl-D behaviour at every prompt type is inferred from the vendored questionary/prompt_toolkit sources, not measured. The snippet editor almost certainly raises an uncaught `EOFError`; checkbox and select bind no Ctrl-D. Both are things this run should settle.
- Whether `sl`, `cowsay` and `tree` have candidates on these VMs is unverified — the `apt-cache policy` gate in 2a is there for that reason.
- 2d needs a vendor package with no Ubuntu-archive candidate on pc2. I could not confirm any specific package satisfies that from this repository; the `apt-cache policy` gate decides it.
- Whether any pc2 package has a manually-installed reverse dependency (2e's collateral) is unverified; the `apt-get -s remove` check decides it. If none qualifies, record the collateral case as not exercised rather than fabricating a conflict pair.
- Whether the VMs currently exist, are running, hold the baseline snapshot, and carry fixtures version 3 is unverified — I ran no `hcloud` command.
- Whether the second review can be reached with fewer moving parts than 2c is untested; the pin construction is derived from `089ea985`'s own description of the bug, not from an executed run.
- The exact rendering of a questionary widget inside a paused Rich `Live` region — the corruption this test is looking for — cannot be predicted from the code and is the point of running it.
