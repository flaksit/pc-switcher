# UAT 02-02 runbook: the four snippet jobs, version convergence, and the unattended flags

Every command below is for you to run. `pc1` is the source, `pc2` the target; every title, answer and message prints those two hostnames, and "source" or "target" naming a machine anywhere you read is a finding.

This runbook covers what changed after the phase-2 UAT: the split of `manual_installs_sync` into four jobs, the second snippet an unowned-path registry entry carries and no other kind does, version convergence and its retry loop, removals of unreproducible items, the two unattended apply flags, the machine-specific follow-up question, the apt update-timer pause, the question a snippet for an apt-installable package raises, and the end-of-run outcome block. What it does not cover is in §9. `02-UAT-01-RUNBOOK.md` remains the runbook for the base review.

## 1. Machines

Acquire the Hetzner label lock yourself and release it yourself; never run `lock.sh clear`.

```bash
cd /home/janfr/dev/pc-switcher
export HCLOUD_TOKEN="$(pass show dev/pc-switcher/testing/hcloud_token_rw)"
tests/integration/scripts/internal/lock.sh status
tests/integration/scripts/internal/lock.sh acquire "janfr-uat-02-02"
export PCSWITCHER_LOCK_HOLDER=janfr-uat-02-02   # reset-vm.sh takes its own lock otherwise
tests/integration/scripts/reset-vm.sh pc1
tests/integration/scripts/reset-vm.sh pc2
export PC1="$(hcloud server ip pc1)"
export PC2="$(hcloud server ip pc2)"
for h in "$PC1" "$PC2"; do
  ssh testuser@"$h" 'curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash -s -- --ref main'
done
ssh testuser@"$PC1" 'bash -s -- --with-app' < tests/integration/scripts/internal/vm-test-fixtures.sh
ssh testuser@"$PC2" 'bash -s' < tests/integration/scripts/internal/vm-test-fixtures.sh
ssh testuser@"$PC1" '~/.local/bin/pc-switcher init --force'
ssh testuser@"$PC1" 'printf "logging:\n  file: DEBUG\nsync_jobs:\n  apt_sync: true\n  snap_sync: true\n  flatpak_sync: true\n  manual_deb_sync: true\n  manual_snap_sync: true\n  manual_flatpak_sync: true\n  manual_installs_sync: true\n  folder_sync: true\nfolder_sync:\n  folders:\n    - path: /home\n      enabled: true\n      filter_file: ~/.config/pc-switcher/home.filter\n" > ~/.config/pc-switcher/config.yaml'
```

All seven package jobs are on, which is what this runbook is about: three of the four snippet jobs did not exist when the base runbook was written, and the exclusions they are the other half of only take effect when both sides of each pair are enabled. `init` is run for the two starter filter files it ships, and the config it writes is replaced on the next line. `/home` is the mirrored folder because the registry and the four decision files live under it, and `home.filter` is what keeps each machine's own `authorized_keys`.

The reset to baseline is not optional here. A previous session's leftovers land in the same detectors these fixtures use — a stray hand-installed `.deb` becomes an extra `manual_deb_sync` finding, and an already-installed `sdl_sopwith` makes §2.1's flatpak fixture impossible, because flatpak will not install an application that is already there from another remote.

Confirm the config was accepted before diverging anything — a key the schema does not know ends the run, and finding that out after the fixtures are in place costs you the setup:

```bash
ssh testuser@"$PC1" '~/.local/bin/pc-switcher sync pc2 --dry-run --yes --allow-first-sync --allow-out-of-order'
```

### 1.1 Level the two machines' phased updates

The two baselines were built on different days, so Ubuntu's phased rollouts have reached them differently: a package at `1:8.2.2+ds-0ubuntu1.17` on one machine and `.18` on the other is neither machine's fault and no fixture's doing. It produces real version-difference items that no step below asks for, so level it first and read every later screen as the fixtures' own (#285):

```bash
for h in "$PC1" "$PC2"; do ssh testuser@"$h" 'sudo apt-get update'; done
ssh testuser@"$PC1" "dpkg-query --show --showformat='\${Package}\t\${Version}\n'" | sort > /tmp/pc1.versions
ssh testuser@"$PC2" "dpkg-query --show --showformat='\${Package}\t\${Version}\n'" | sort > /tmp/pc2.versions
comm -3 /tmp/pc1.versions /tmp/pc2.versions
```

Every name `comm` prints is a package the two machines disagree about before any fixture exists. Upgrade each of them on whichever machine is behind — the rollout will serve it, since the other machine already has it:

```bash
ssh testuser@"$PC1" 'sudo apt-get install --only-upgrade --assume-yes <package> …'
```

Re-run the `comm` line until it prints nothing. Rebuilt baselines taken the same day would remove the step; until then it is part of the setup.

## 2. Diverge the two machines

Eight fixtures, one per surface under test. Each is a case for one article, named as it is set up.

### 2.1 On pc1

`pcsw-uat-deb` is a package no repository declares, so pc1's apt reports its installed version as the whole of its version table and names no origin for it — the shape `PKG-FR-DEB-OWNERSHIP` turns on. `pcsw-uat-drift` is the same shape at a higher version than pc2's copy, which is `PKG-FR-MANUAL-VERSION`'s case. The sideload declares a base pc1 already holds, so snapd fetches nothing, and `snap try` gives it the `x`-prefixed revision `PKG-FR-SNAP-SIDELOAD` keys on. `/opt/pcsw-uat-app` holds a file, so it is a finding rather than a shape question. `/opt/pcsw-uat-loop` is the converge loop's item: pc1 is at `2.0`, pc2 at `1.0`, and the registry entry written below replays a body that exits zero and moves nothing. `/opt/pcsw-uat-crash` is the same shape with the other defect — a body pc2 refuses outright — which is the case `PKG-FR-MANUAL-FAIL-ITEM` now offers for correction.

```bash
ssh -t testuser@"$PC1"

# manual_deb_sync: a package no repository can supply, and one that drifts
for spec in pcsw-uat-deb:1.0 pcsw-uat-drift:2.0; do
  name="${spec%:*}"; ver="${spec#*:}"
  sudo mkdir -p "/var/tmp/$name/DEBIAN"
  printf 'Package: %s\nVersion: %s\nSection: misc\nPriority: optional\nArchitecture: all\nMaintainer: pc-switcher UAT <noreply@example.invalid>\nDescription: A package installed by hand, which no repository can supply.\n' "$name" "$ver" | sudo tee "/var/tmp/$name/DEBIAN/control" >/dev/null
  sudo dpkg-deb --build "/var/tmp/$name" "/var/tmp/$name.deb"
  sudo dpkg --install "/var/tmp/$name.deb"
done

# manual_snap_sync: a sideload, and one whose version differs from pc2's at the SAME revision
snap list | awk '$1 ~ /^core[0-9]/ {print $1}'          # note a base snap this machine holds
for spec in pcsw-uat-snap:1.0 pcsw-uat-snapdrift:2.0; do
  name="${spec%:*}"; ver="${spec#*:}"
  sudo mkdir -p "/var/tmp/$name/meta"
  printf "name: %s\nversion: '%s'\nsummary: pc-switcher UAT sideload\ndescription: A snap installed from local bytes.\nbase: <that base snap>\nconfinement: strict\ngrade: stable\n" "$name" "$ver" | sudo tee "/var/tmp/$name/meta/snap.yaml" >/dev/null
  sudo snap try "/var/tmp/$name"
done
snap list pcsw-uat-snap pcsw-uat-snapdrift              # both at x1 — note the revisions

# manual_flatpak_sync: an app whose origin names no remote this machine configures.
# The base fixtures already installed sopwith from flathub, and flatpak refuses to
# install an app that is already there from another remote — so it comes off first.
flatpak uninstall --user --assumeyes io.github.fragglet.sdl_sopwith
flatpak remote-add --user --if-not-exists pcsw-uat-src https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user --assumeyes pcsw-uat-src io.github.fragglet.sdl_sopwith
flatpak remote-delete --user --force pcsw-uat-src
flatpak list --user --app --columns=application,origin   # origin reads pcsw-uat-src, which is gone

# manual_installs_sync: one plain finding, one that drifts, one whose snippet fails
sudo mkdir -p /opt/pcsw-uat-app /opt/pcsw-uat-crash /opt/pcsw-uat-loop
echo hi | sudo tee /opt/pcsw-uat-app/README >/dev/null
echo 2.0 | sudo tee /opt/pcsw-uat-crash/version >/dev/null
echo 2.0 | sudo tee /opt/pcsw-uat-loop/version >/dev/null

# Four recorded entries. An item both machines have at different versions only
# reaches the update screen when a snippet is ALREADY recorded for it — with nothing
# to replay, the run asks how to reproduce it instead, on the install screen with
# the verb "update". So the two drifting package items get real entries;
# /opt/pcsw-uat-loop gets one whose install body exits 0 and moves nothing, and
# /opt/pcsw-uat-crash one that pc2 refuses outright. Only the unowned paths carry a
# version_body: dpkg and snap answer that question for the packages, and an entry of
# theirs carrying one is malformed. Substitute the same base snap in the snapdrift
# body as above — it is replayed on pc2 in §3.5.
cat > ~/.config/pc-switcher/package-snippets.yaml <<'YAML'
snippets:
  "unreproducible:unowned-path:/opt/pcsw-uat-crash":
    label: /opt/pcsw-uat-crash
    install_body: |
      echo "pcsw-uat: this body was written wrong on purpose" >&2
      exit 42
    version_body: "cat /opt/pcsw-uat-crash/version"
    authored_at: "2026-08-01T00:00:00+00:00"
    authored_on: pc1
  "unreproducible:unowned-path:/opt/pcsw-uat-loop":
    label: /opt/pcsw-uat-loop
    install_body: "true"
    version_body: "cat /opt/pcsw-uat-loop/version"
    authored_at: "2026-08-01T00:00:00+00:00"
    authored_on: pc1
  "unreproducible:apt-no-candidate:pcsw-uat-drift":
    label: pcsw-uat-drift
    install_body: |
      set -eu
      b=/var/tmp/pcsw-uat-drift
      sudo mkdir -p "$b/DEBIAN"
      printf "Package: pcsw-uat-drift\nVersion: 2.0\nSection: misc\nPriority: optional\nArchitecture: all\nMaintainer: pc-switcher UAT <noreply@example.invalid>\nDescription: A package installed by hand, which no repository can supply.\n" | sudo tee "$b/DEBIAN/control" >/dev/null
      sudo dpkg-deb --build "$b" "$b.deb" >/dev/null
      sudo dpkg --install "$b.deb"
    authored_at: "2026-08-01T00:00:00+00:00"
    authored_on: pc1
  "unreproducible:snap-sideload:pcsw-uat-snapdrift":
    label: pcsw-uat-snapdrift
    install_body: |
      set -eu
      d=/var/tmp/pcsw-uat-snapdrift
      sudo mkdir -p "$d/meta"
      printf "name: pcsw-uat-snapdrift\nversion: '2.0'\nsummary: pc-switcher UAT sideload\ndescription: A snap installed from local bytes.\nbase: <that base snap>\nconfinement: strict\ngrade: stable\n" | sudo tee "$d/meta/snap.yaml" >/dev/null
      sudo snap try "$d"
    authored_at: "2026-08-01T00:00:00+00:00"
    authored_on: pc1
YAML

# mark-side: an apt.conf.d file both machines have, with different content
printf 'APT::Install-Recommends "false";\n' | sudo tee /etc/apt/apt.conf.d/99-pcsw-uat >/dev/null
exit
```

### 2.2 On pc2

`pcsw-uat-drift` at the lower version and `pcsw-uat-snapdrift` at the lower version are the other halves of the two version cases; the snap's revision is `x1` on both machines, so a run that converges it is provably comparing the declared version and not the revision. `/opt/pcsw-uat-crash` and `/opt/pcsw-uat-loop` are the two whose recorded bodies do not work — one pc2 refuses outright, one that exits 0 and moves nothing — and both need their lower version here for the difference to be raised at all. `pcsw-uat-gone` is a hand `.deb` pc1 does not have, which is `PKG-FR-MANUAL-REMOVE`'s case, and `/opt/pcsw-uat-orphan` is the same case one job over. The `apt.conf.d` file differs from pc1's, which is what makes it the conflicting item the mark-side follow-up asks about.

```bash
ssh -t testuser@"$PC2"

for spec in pcsw-uat-drift:1.0 pcsw-uat-gone:1.0; do
  name="${spec%:*}"; ver="${spec#*:}"
  sudo mkdir -p "/var/tmp/$name/DEBIAN"
  printf 'Package: %s\nVersion: %s\nSection: misc\nPriority: optional\nArchitecture: all\nMaintainer: pc-switcher UAT <noreply@example.invalid>\nDescription: A package installed by hand, which no repository can supply.\n' "$name" "$ver" | sudo tee "/var/tmp/$name/DEBIAN/control" >/dev/null
  sudo dpkg-deb --build "/var/tmp/$name" "/var/tmp/$name.deb"
  sudo dpkg --install "/var/tmp/$name.deb"
done

snap list | awk '$1 ~ /^core[0-9]/ {print $1}'          # note a base snap this machine holds
sudo mkdir -p /var/tmp/pcsw-uat-snapdrift/meta
printf "name: pcsw-uat-snapdrift\nversion: '1.0'\nsummary: pc-switcher UAT sideload\ndescription: A snap installed from local bytes.\nbase: <that base snap>\nconfinement: strict\ngrade: stable\n" | sudo tee /var/tmp/pcsw-uat-snapdrift/meta/snap.yaml >/dev/null
sudo snap try /var/tmp/pcsw-uat-snapdrift
snap list pcsw-uat-snapdrift                            # x1, the same revision pc1 has

sudo mkdir -p /opt/pcsw-uat-orphan /opt/pcsw-uat-crash /opt/pcsw-uat-loop
echo hi | sudo tee /opt/pcsw-uat-orphan/README >/dev/null
echo 1.0 | sudo tee /opt/pcsw-uat-crash/version >/dev/null
echo 1.0 | sudo tee /opt/pcsw-uat-loop/version >/dev/null

printf 'APT::Install-Recommends "true";\n' | sudo tee /etc/apt/apt.conf.d/99-pcsw-uat >/dev/null
exit
```

## 3. The interactive walk

Run the dry run first to see every screen without converging anything, then answer the real run. Add `--allow-out-of-order` if the topology check asks.

```bash
ssh -t testuser@"$PC1"
env | grep PCSWITCHER_PACKAGE_REVIEW_AUTOMATION   # must print nothing
systemctl is-active apt-daily.timer apt-daily-upgrade.timer   # note what they are before the run
pc-switcher sync pc2 --dry-run --yes --allow-first-sync
pc-switcher sync pc2 --yes --allow-first-sync
```

A dry run converges nothing, so three things below cannot happen in it: no snippet is recorded, no registry is pushed, and the converge loop of §3.8 and the failure screen of §3.7 never open — `_converge_one` (`jobs/packages/sync_core.py`) is not called at all. Walk the dry run for the screens, then answer the real run for the outcomes.

Every finding raised from an earlier walk of these fixtures is fixed — review copy and titles (#276), the two panels and the per-file follow-up of §3.3 (#277), the follow-up's keys and default (#278), the repeated scrollback frames (#279), the job names below (#280), the snippet screens of §3.4, §3.5 and §3.8 (#281), the registry's two entry types (#282), the group order of §3.2 (#283), the heading each screen opens on and the blank line before every question (#287), the progress bars below (#288), the job tag on a job's closing log line (#289), this runbook's own order (#290) and the two pause messages of §3.9 (#292). None of them is still an open defect to observe: this runbook says what to expect instead, and seeing the old behaviour is a regression.

The status line, the progress bars and the outcome block name jobs the way a user would (#280 is fixed): `Apt packages`, `Snaps`, `Flatpaks`, `Manual debs`, `Sideloaded snaps`, `Manual flatpaks`, `Manually installed apps`, `Folder sync`, `Install on target`. A module name such as `manual_deb_sync` on screen is a regression. Config keys and the `job` field in the log file stay the module name, so §4's log greps are unaffected.

The Recent Logs panel follows the same rule in its message text (#276): a job counts its work in the same words its screens use — `Applying 1 change to manual debs`, `No snaps to change`, `1 manually installed app failed` — and the module name appears only in the `[manual_deb_sync]` prefix each line already carries.

That prefix holds for a job's whole life (#289 is fixed): `Job Apt packages started`, its own work, and `Job Apt packages completed successfully` all read `[apt_sync]`, as do the `skipped` and `failed` endings; the install-on-target skip reads `[install_on_target]`. An ending line tagged `[orchestrator]` is a regression. `[orchestrator]` still belongs to the run's own steps — locking, connecting, snapshots, config sync.

The routine half of that life is no longer on screen. `Job X started` and `Job X completed successfully` are FULL, so they are in the log file and below the panel's INFO floor: the bar says the job is running and the outcome block says how it ended, and fourteen lines of a ten-line panel for seven jobs said neither. Either of the two in the panel is a regression; the `skipped` and `failed` endings stay there, because a job that did not do its work is not routine. §4 reads the demoted pair out of the file. `Btrfs snapshots` and `Install on target` have no bar and keep their INFO lines.

None of those four lines carries a `(source)` or `(target)` side any more (#291 is fixed), and neither do the run's own lines about both machines — `Sync completed successfully`, `Creating pre-sync snapshots`, `Checking disk space on both hosts`. A side stays only where the record is about that one machine, so `(target)` on `Configuration sync completed` is right and a side on a job's start is a regression.

Every job of the job loop — the seven package jobs, `Folder sync` and `VS Code state` — has a bar from the moment it starts (#288 is fixed), and the bar says what the job is doing rather than sitting at a percentage it cannot know: it opens on `starting`, then a package job reads `checking both machines` while it plans, `waiting for your answers` through its review, and `applying your answers` until it has items to count, then a real percentage per item. One of those jobs named by the status line but absent from the bars is a regression, and so is a bar parked at `0%` for a minute of probing. `Btrfs snapshots` and `Install on target` run outside the loop and report no progress, so neither has a bar at any point — the status line and the Recent Logs panel are where you follow those two steps.

### 3.1 The three exclusions

Each pair of jobs decides its boundary by one shared rule, so a finding claimed by the snippet job must be silent in the package-manager job — on **both** machines, not just on pc1.

- `apt_sync` names `pcsw-uat-deb`, `pcsw-uat-drift` and `pcsw-uat-gone` nowhere: no item, no review line, no install and no removal. `pcsw-uat-gone` is the one to watch — it is only on pc2, so a job that had not excluded it would offer it as software pc1 had deleted.
- `snap_sync` names `pcsw-uat-snap` and `pcsw-uat-snapdrift` nowhere, and says nothing about a hold on either.
- `flatpak_sync` names `io.github.fragglet.sdl_sopwith` nowhere and derives no remote for it — in particular it does not try to add a remote with an empty URL.

`snap_sync` and `flatpak_sync` therefore have nothing of their own to do in this run and must report `success`, not `skipped` (as `Snaps` and `Flatpaks` in the outcome block): a review holding nothing to decide is the goal already met. `apt_sync`'s only item is the `apt.conf.d` file of §3.3.

Neither must leave a frame in the scrollback (#279 is fixed). A frozen copy of the status line, the progress bars and the Recent Logs panel belongs above a question and nowhere else, so count them: one per screen you are actually asked, and none for a job that asks nothing. In each of those frames the asking job is in the bars, reading `waiting for your answers` — the frame that lists only the jobs which already finished is the #288 regression. A job whose review is only reported findings still PRINTS them — the panels appear while the display keeps running.

### 3.2 The seven reviews, and the order the screens come in

The jobs run in the order the config lists them — apt, snap, flatpak, manual_deb, manual_snap, manual_flatpak, manual_installs, then the folder mirror — and each one's questions come before the next one plans. Inside one snippet job the groups come in a fixed order (#283 is fixed): **the items needing a snippet, then the version differences, then the ordinary install, change and removal screens** — what arrives, then what moves, then what goes away. Removals first is a regression.

The sections below follow the order you will meet the screens. On these fixtures that is:

| # | Job | Screen | Section |
|---|-----|--------|---------|
| 1 | `apt_sync` | `Update 99-pcsw-uat on pc2?` → the follow-up it raises | §3.3 |
| 2 | `manual_deb_sync` | install, `pcsw-uat-deb` — one editor | §3.4 |
| 3 | `manual_deb_sync` | version difference, `pcsw-uat-drift` | §3.5 |
| 4 | `manual_deb_sync` | `Remove manual debs from pc2?` | §3.6 |
| 5 | `manual_snap_sync` | install, then version difference | §3.4, §3.5 |
| 6 | `manual_flatpak_sync` | install, `sdl_sopwith` | §3.4 |
| 7 | `manual_installs_sync` | install, then version difference, then `Remove manually installed apps from pc2?` | §3.4, §3.5, §3.6 |
| 8 | `manual_installs_sync` | a snippet pc2 refused, then the converge-loop retry, during apply | §3.7, §3.8 |
| 9 | — | `Job outcomes:` | §3.9 |

The last two are last because neither is a review screen at all: both are put while the job is applying what you approved, after every screen that job asked. Between them, the refused snippet comes first because `manual_installs_sync` sorts its findings by path.

- Each of the four snippet jobs puts its own review. A job's findings never appear in another's.
- Every title names the machine the change lands on and ends in `?`. Nothing on any screen may print a job or manager identifier — `manual_deb`, `manual_flatpak`, `manual` or a job name — and seeing one is a finding (#276).
- The group offering software pc2 cannot get is asked one item at a time, so its own group title — `pc1 has <plural noun> that no package manager can put on pc2`, the noun being `manual debs`, `sideloaded snaps`, `manual flatpaks` or `manually installed apps` — reaches no screen in this walk. It is the wording of the warning a run with no terminal prints instead (§5). Each item is titled `Install <singular noun> <item> on pc2?` (§3.4).
- Items both machines have at different versions are asked about one at a time too, so no group title for them ever reaches the screen either: each is headed `<singular noun> <item>` and titled `pc1 and pc2 have different versions of <item> — update it on pc2?` (§3.5).
- Every question in the whole review has a blank line in front of it, whatever printed before it — two questions of one job running together is a finding (#287).
- And one behind it: once a question is answered, a blank line separates the answered row from the display coming back, so the decision just made and the run's next activity are not one block. The status line painted directly under the answer is a finding (#287).

The titles to confirm read well on a real terminal:

```plain
Remove manual debs from pc2?
Remove manually installed apps from pc2?
Install manual deb pcsw-uat-deb (1.0) on pc2?
Install manually installed app /opt/pcsw-uat-app on pc2?
pc1 and pc2 have different versions of pcsw-uat-drift (2.0) — update it on pc2?
pc1 and pc2 have different versions of pcsw-uat-snapdrift — update it on pc2?
```

An item is named as its own job lists it, which is why the two lines above differ: a `manual_deb_sync` row reads `<package> (<version>)`, a `manual_flatpak_sync` row reads `<ref> (user scope)`, and a snap or a path is its bare name. That suffix belongs to the item, not to the screen — a job inventing one for a snap, or dropping one from a deb, is a finding.

### 3.3 The two file bodies, and the machine-specific follow-up each answer raises

The first question of the run, in `apt_sync`. `/etc/apt/apt.conf.d/99-pcsw-uat` is on both machines with different content, so it is asked on a screen of its own, preceded by both machines' whole copies of it — never a digest of either (#277 is fixed). Confirm:

- A heading `apt configuration file 99-pcsw-uat`, then a cyan panel titled `On pc1` holding pc1's `APT::Install-Recommends "false";`, then a yellow `On pc2 now, would be updated by pc1 version above` holding pc2's `"true";`. Source first — it is the copy the screen proposes — and the target's panel says what the answer would spend, which is what makes the row's starting position readable.
- The question itself reads `Update 99-pcsw-uat on pc2?`. `Update apt configuration files on pc2?` over a single row is a finding (#277): a plural title reads as an offer to answer for all of them at once.
- No 64-character hexadecimal string anywhere on the screen.
- The row starts at **skip now** — replacing a file pc2's own user wrote is as irreversible as a deletion.

Answer `<x>` on it. Then a further screen must appear immediately — not folded into that one, and not held back to the end of the review — titled `Kept for good — whose own version is it?`, with the ONE row this answer raised. A screen collecting several items is a finding (#277): each is asked where its own two bodies are. Confirm:

- Neither panel is printed again. The two bodies are directly above, from the question you have just answered; reprinting them is a finding.
- Three answers, worded as the two hostnames and `both`: `pc1`, `pc2`, `both`. The keys number the machines as the block lists them — `<1> pc1`, `<2> pc2`, `<b> both`. No letter key here answers something else elsewhere: `<s>` is `skip now` on the screen one line above, and `<h>`/`<o>` are gone.
- Each hint says how long the mark lasts on that machine — `it is pc1's own version; nothing overwrites it while pc1 has it`, and for `both`, that each version is its own machine's.
- The row reads `not answered`, and the editing line reads `<space> cycles   <enter> confirm, once every row is answered` — `<shift+key> sets every row` must NOT be on it, because a digit has no shifted form. Press `<enter>` before answering: nothing must happen, and a line must appear naming the row still unanswered.
- Its explanation names both machines and says the answer lasts as long as that machine still has the item.

Answer `both` on this run, so §4 can check that a mark landed on each machine.

Nothing else in this run reaches that screen: an install is on pc1 alone and a removal on pc2 alone, so for those the action already names the holder. A version difference must not reach it either — it has no permanent answer at all.

### 3.4 The install screens

The first group of each snippet job: four items have no recorded snippet, so each is asked how to reproduce it on a screen of its own. In the order the jobs run: `pcsw-uat-deb (1.0)`, `pcsw-uat-snap`, `io.github.fragglet.sdl_sopwith/x86_64/stable (user scope)` and `/opt/pcsw-uat-app`.

Nothing prints above these questions. There is no recorded snippet to show, so the title — `Install manual deb pcsw-uat-deb (1.0) on pc2?` — is the whole screen, and a heading repeating the item above it would say the subject twice.

The first three take **one** editor each and are never asked for a version body: `dpkg`, `snap` and `flatpak` report those versions themselves. A second editor on any of them is a finding. Their refusal message says `The snippet cannot be empty — enter one, or choose a skip.`

Answer `<y>` on each, then type the body below verbatim, so a failure is comparable between runs. They are §2.1's own fixtures with the name and version changed; `<that base snap>` is the base snap §2.1 had you note.

`pcsw-uat-deb (1.0)`:

```bash
set -eu
b=/var/tmp/pcsw-uat-deb
sudo mkdir -p "$b/DEBIAN"
printf "Package: pcsw-uat-deb\nVersion: 1.0\nSection: misc\nPriority: optional\nArchitecture: all\nMaintainer: pc-switcher UAT <noreply@example.invalid>\nDescription: A package installed by hand, which no repository can supply.\n" | sudo tee "$b/DEBIAN/control" >/dev/null
sudo dpkg-deb --build "$b" "$b.deb" >/dev/null
sudo dpkg --install "$b.deb"
```

`pcsw-uat-snap`:

```bash
set -eu
d=/var/tmp/pcsw-uat-snap
sudo mkdir -p "$d/meta"
printf "name: pcsw-uat-snap\nversion: '1.0'\nsummary: pc-switcher UAT sideload\ndescription: A snap installed from local bytes.\nbase: <that base snap>\nconfinement: strict\ngrade: stable\n" | sudo tee "$d/meta/snap.yaml" >/dev/null
sudo snap try "$d"
```

`io.github.fragglet.sdl_sopwith/x86_64/stable (user scope)` — pc2 still has flathub, which is what makes this one reproducible by hand:

```bash
flatpak install --user --assumeyes flathub io.github.fragglet.sdl_sopwith
```

The deb body is the one to watch in the scrollback: `set -eu` followed by a `printf` of a whole `.deb` control file is the long line that used to wrap mid-token in a narrow column.

`/opt/pcsw-uat-app` is the last of the four and the one kind with no package manager to ask for a version, so it is the only one that takes two editors. On `Install manually installed app /opt/pcsw-uat-app on pc2?` answer `<y>` and confirm that **two** editors open in sequence:

1. `Enter the commands that install or update /opt/pcsw-uat-app on pc2:` — its own screen states the install-or-update contract, that the body is replayed onto a machine which may already hold an older version.
2. `Enter the commands that print the version of the INSTALLED /opt/pcsw-uat-app` — its own screen states that this one runs on both machines on every sync while the run is still planning, and must be read-only.

Each prompt INSTRUCTS: a label with a colon on the end (`Installed-version snippet for /opt/pcsw-uat-app:`) is a regression (#287). The second must carry the distinction on its own line — the version INSTALLED on whichever machine runs it, not the one the commands above would produce.

A blank line must separate the second editor from the first's echoed body: the two running together is the regression #287 shows.

Each editor closes onto a panel of its own, titled `Install-or-update snippet for /opt/pcsw-uat-app` / `Installed-version snippet for /opt/pcsw-uat-app` and holding the body you wrote at the terminal's full width (#281 is fixed). The authoring note and the `(Ctrl-D to finish)` marker must be gone from the scrollback; the body must be there, whole.

This one refuses an empty body and a body of only spaces with `Neither snippet can be empty — enter both, or choose a skip.`, and puts the three answers again. Try each refusal on each of its two editors: submitting a real install body and then an empty version body must not leave a half-written entry anywhere. Then supply real bodies:

Install or update:
```bash
sudo mkdir -p /opt/pcsw-uat-app && echo hi | sudo tee /opt/pcsw-uat-app/README >/dev/null
```

Installed-version
```bash
echo 1.0
```

### 3.5 Version convergence

Follows the install screens of §3.4 in each job that has both, and comes before that job's removal group. Four items reach this screen, and they are exactly the four that have a recorded snippet: `pcsw-uat-drift (2.0)` in `manual_deb_sync`, `pcsw-uat-snapdrift` in `manual_snap_sync`, and `/opt/pcsw-uat-crash` then `/opt/pcsw-uat-loop` in `manual_installs_sync` — that job's findings are sorted by path, which is what puts the crash before the loop here and again during apply. Each is asked on its own screen, headed `<singular noun> <item>` and then titled `pc1 and pc2 have different versions of <item> — update it on pc2?`, with exactly three answers and **no** `<x>`:

- `<y> update` — `run the recorded snippet on pc2`
- `<w> new snippet` — `rewrite the snippet first, then run it on pc2`
- `<s> skip now` — `leave pc2's version as it is for now; will be asked again next sync`

The heading comes FIRST, then the panel, then the question (#287 is fixed): a panel arriving before anything says what the screen is about is a regression. The noun belongs to the heading alone — reading it in the heading and again in the question is a regression too.

Between the heading and the question, a cyan panel titled `Recorded snippet — would run on pc2` holds that item's whole install body (#281 is fixed) — a screen asking whether the recorded snippet is still right without showing it is a regression. The first two answers both change pc2 and both read as acts; `<w>` rendered in the skip colour is a regression too.

The comparison must be made on the snap's **version**, `2.0` against `1.0`, and never on its revision — both machines are at `x1`, so a run comparing revisions would find nothing to do. The revision must not be printed either: it is not something the user decides on (#276).

Answer, in the order the three come:

- `pcsw-uat-drift (2.0)` — `<w>`, to confirm the editor opens **on the recorded body** rather than empty, which is what makes a rewrite an edit. Change the `Description:` line so the body is provably yours, submit the one editor, and let it run.
- `pcsw-uat-snapdrift` — `<y>`, the plain replay of a recorded snippet, with no editor in the way.
- `/opt/pcsw-uat-crash` — `<y>`. Its recorded body exits 42, which is what §3.7 is about; nothing says so yet.
- `/opt/pcsw-uat-loop` — `<y>`. Its recorded body exits 0 and moves nothing, which is what §3.8 is about; nothing says so yet, and the screen that does comes much later.

### 3.6 Removals

The last screen of `manual_deb_sync`, and again of `manual_installs_sync`. `pcsw-uat-gone` and `/opt/pcsw-uat-orphan` are on pc2 only. Each is offered for removal by the job whose own detector claims it there, and by no other. Confirm each row starts at **skip now**, and that the path deletion's screen carries the line saying its reach is smaller than its name: `Only the path itself is deleted from pc2. There may be config or files in other folders, like a launcher, a symlink or a service unit outside these folders. Remove them manually.`

Approve both. What to check here is that the warning appears at all, on the path deletion and not on the package one.

### 3.7 A snippet pc2 refuses

**Real run only**, for §3.8's reason: nothing is replayed in a dry run, so no body can fail in one.

`/opt/pcsw-uat-crash` is the item whose recorded install body exits 42, and §3.5 is where you answered `<y>` on it. Its replay is the first thing `manual_installs_sync` does with your answers, so this screen comes before §3.8's. Confirm:

- The job does **not** end here. A snippet that fails takes its own item and nothing else; the run coming back with a question rather than a failed job is the whole of #293.
- The failure is recorded **once**, naming the item and carrying pc2's own words: `/opt/pcsw-uat-crash failed: pcsw-uat: this body was written wrong on purpose`. The same text printed a second and third time, inside a count line or a per-job summary, is the regression.
- A screen follows, headed `manually installed app /opt/pcsw-uat-crash`, then a cyan panel titled `Recorded snippet — failed on pc2` holding the body that failed. `would run on pc2` here is a regression: that body HAS run, and pc2 refused it.
- Under it, the item's OWN question again, word for word as §3.5 put it — `pc1 and pc2 have different versions of /opt/pcsw-uat-crash — update it on pc2?` — with pc2's own words on the row, and three answers:
  - `<y> update` — `correct the snippet that updates it; pc2 runs it`
  - `<s> skip now` — `do not update on pc2 for now; will be asked again next sync; the body that failed is dropped`
  - `<x> never update` — `do not update on pc2 for good; it is pc1's own; the body that failed is dropped`

There is no `<w>` on this screen and there must not be: the act IS the correction, so `<y>` opens the editor rather than replaying anything. Press `<y>` and confirm **two** editors open — this item carries an installed-version body as well — the first preloaded with the body that failed, so a typo is one keystroke to fix. Replace the install body with one that works and leave the version body alone:

```bash
echo 2.0 | sudo tee /opt/pcsw-uat-crash/version >/dev/null
```

The corrected body must be replayed straight away and the item must come out applied, in this run. Answering `<s>` instead would leave the item unapplied and take the entry off BOTH machines' registries — not exercised on this walk, because the item is needed applied for §4.

### 3.8 The converge loop

**Real run only.** This is not a review screen: it is put while `manual_installs_sync` applies what you approved, and a dry run applies nothing — `_converge_one` (`jobs/packages/sync_core.py`) is never reached, so no snippet is replayed, no version is re-read and this screen cannot appear. Seeing it in a dry run is a finding.

`/opt/pcsw-uat-loop` is the item whose recorded install body is `true` — it exits zero and moves nothing — and §3.5 is where you answered `<y>` on it. Confirm:

- The version is read again on pc2 after the replay.
- The item is **not** reported as applied.
- A second screen appears, headed `manually installed app /opt/pcsw-uat-loop` and titled `/opt/pcsw-uat-loop on pc2 is still 1.0, not 2.0`, offering exactly **two** answers — `<w> new snippet` and `<s> skip now`. The replay answer is gone, because replaying the same bytes cannot change the outcome.
- The body that moved nothing is printed between that heading and those answers, in a cyan panel titled `Recorded snippet — didn't install 2.0 on pc2`. `would run on pc2` is a regression here for §3.7's reason: that body has run, and this title says what it did.
- Its editor opens on the body that just failed.

Answer `<w>` and write a body that actually converges, then confirm the item comes out applied:

```bash
echo 2.0 | sudo tee /opt/pcsw-uat-loop/version >/dev/null
```

### 3.9 The end of the run

The last block must be headed `Job outcomes:` and give one line per job in execution order — a mark (`✔`, `⏭`, `✖`), the job's display name (`Apt packages`, not `apt_sync`), its status (`success`, `skipped`, `failed`), and for a skipped or failed job the reason that job recorded. Confirm the failures are printed **once**: nothing else prints them again in a second shape.

The run must have said so on each machine, once each way, with no rationale in the line — `Pausing the system apt update timers` tagged pc1 and again tagged pc2 near the start, and `Restarting the system apt update timers` tagged each at the end. The unit names belong to DEBUG and must not appear in either line. The snapd pair reads the same way: `Pausing snapd auto-refresh` and `Restoring the snapd auto-refresh policy`, once per machine.

Then check the apt timers were suspended for the run and put back:

```bash
systemctl is-active apt-daily.timer apt-daily-upgrade.timer
ssh testuser@"$PC2" 'systemctl is-active apt-daily.timer apt-daily-upgrade.timer'
```

Both machines must read exactly as they did before the run started. The pause no longer follows `apt_sync` alone: `manual_deb_sync` and `manual_installs_sync` ask for it too, so a run with `apt_sync: false` and either of those on must still show it. The snapd pause is asked for by `snap_sync` and `folder_sync`, and by neither `manual_snap_sync` nor anything else. Whichever pauses a run's jobs ask for, the sudo they need on both machines is now checked in the validation step, so a machine without passwordless sudo refuses the run up front naming the pause instead of warning mid-run — both VMs grant it, so nothing here changes for this walk. To see the suspension itself, run the sync again with `--confirm-each-command` and watch for the `systemctl stop` on each machine and the `systemd-run` that schedules the restart; or read them out of the log afterwards:

```bash
LOG=$(ls -t ~/.local/share/pc-switcher/logs/sync-*.log | head -1)
grep -nE 'apt-daily|systemd-run' "$LOG" | head -20
```

## 4. What to check afterwards

On pc1:

```bash
ls ~/.config/pc-switcher/*.decisions.yaml     # four manual files can exist, plus apt/snap/flatpak
grep -c 'label: /opt/pcsw-uat-app' ~/.config/pc-switcher/package-snippets.yaml   # 1
grep -c 'version_body' ~/.config/pc-switcher/package-snippets.yaml               # one per unowned-path entry only
grep -c 'install_body' ~/.config/pc-switcher/package-snippets.yaml               # one per entry, of every kind
grep -c '99-pcsw-uat' ~/.config/pc-switcher/apt.decisions.yaml                   # 1 — "both" recorded here too
LOG=$(ls -t ~/.local/share/pc-switcher/logs/sync-*.log | head -1)
grep -nE 'pcsw-uat-deb|pcsw-uat-snap|sdl_sopwith' "$LOG" | grep -vE 'manual_deb|manual_snap|manual_flatpak' | head
grep -c '"event": "Job Apt packages started"' "$LOG"          # 1 — the panel dropped it, the file keeps it
grep -n 'Job Apt packages ' "$LOG" | head -2                  # "level": "FULL", "job": "apt_sync", and no "host"
grep -c 'exit 42' ~/.config/pc-switcher/package-snippets.yaml # 0 — §3.7's correction replaced the body that failed
grep -c '/opt/pcsw-uat-crash failed' "$LOG"                   # 1 — one record of that failure, not four
```

The exclusion is read from the log first: every mention of those three items must carry one of the snippet jobs, never `apt_sync`, `snap_sync` or `flatpak_sync`. The two lines after it are the demoted lifecycle pair — present, at FULL, tagged with the job and carrying no side.

Every count reads `label:` and `install_body:` lines rather than the item's name, which repeats down an entry and would count twice.

On pc2:

```bash
dpkg-query -W -f='${Package} ${Version} ${Status}\n' pcsw-uat-deb pcsw-uat-drift pcsw-uat-gone
snap list pcsw-uat-snap pcsw-uat-snapdrift
flatpak list --user --app --columns=application,version
cat /opt/pcsw-uat-crash/version               # 2.0 — the corrected snippet converged it
cat /opt/pcsw-uat-loop/version                # 2.0 — the rewritten snippet converged it
ls /opt                                       # pcsw-uat-app arrived, pcsw-uat-orphan is gone
grep -c '99-pcsw-uat' ~/.config/pc-switcher/apt.decisions.yaml   # 1 — the other half of "both"
cat /etc/apt/apt.conf.d/99-pcsw-uat           # still pc2's own "true"
```

`pcsw-uat-deb` arrived at pc1's version and `pcsw-uat-drift` moved to `2.0`; `pcsw-uat-gone` is gone, removed with `apt-get remove` and never `purge` — check the log for that:

```bash
ssh testuser@"$PC1" 'grep -nE "apt-get.*(remove|purge).*pcsw-uat-gone" "$(ls -t ~/.local/share/pc-switcher/logs/sync-*.log | head -1)"'
```

The mark-side answer `both` must have written one entry on each machine, which is what makes it more than a hedge: either machine losing its copy leaves the other's mark standing.

## 5. The unattended run

No terminal, so nothing can be asked. `ssh` without `-t` is what leaves the run none, and the tool is spelled by path because a non-interactive shell never reads `.bashrc`.

Re-diverge first, so the flags have something to converge:

```bash
ssh testuser@"$PC1" 'sudo mkdir -p /opt/pcsw-uat-flag && echo hi | sudo tee /opt/pcsw-uat-flag/README >/dev/null'
ssh testuser@"$PC1" 'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y cmatrix'
ssh testuser@"$PC2" 'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y cowsay'
ssh testuser@"$PC1" 'printf "APT::Install-Recommends \"false\";\n" | sudo tee /etc/apt/apt.conf.d/98-pcsw-flag >/dev/null'
ssh testuser@"$PC2" 'printf "APT::Install-Recommends \"true\";\n" | sudo tee /etc/apt/apt.conf.d/98-pcsw-flag >/dev/null'
```

### 5.1 Both directions

```bash
ssh testuser@"$PC1" '~/.local/bin/pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order --apply-package-installs --apply-package-removals'
```

- `cmatrix` is installed on pc2 and `cowsay` removed from it: the run converged both directions with nobody watching.
- The outcome block reports the jobs the flags answered as `success`, not `skipped` — someone decided, and skipped means nobody did.
- **No** machine-specific mark was recorded anywhere, and **no** snippet was written. Check both:

```bash
ssh testuser@"$PC2" 'grep -c "label: cowsay" ~/.config/pc-switcher/apt.decisions.yaml'   # 0
ssh testuser@"$PC1" 'grep -c "pcsw-uat-flag" ~/.config/pc-switcher/package-snippets.yaml' # 0
```

- The four things neither flag answers are each named in a warning and left for this run: the `98-pcsw-flag` apt.conf.d file pc2 already holds, `/opt/pcsw-uat-flag` which needs a snippet nobody can write, a repository conflict if one arises, and the Ubuntu Pro gate if the target is unattached. `manual_installs_sync` must therefore report `skipped` (as `Manually installed apps` in the outcome block) with `/opt/pcsw-uat-flag` named, in the same run where the other jobs report `success`.

```bash
ssh testuser@"$PC2" 'cat /etc/apt/apt.conf.d/98-pcsw-flag'    # still pc2's own "true"
ssh testuser@"$PC2" 'ls /opt'                                  # no pcsw-uat-flag
```

### 5.2 Installs alone do not carry a removal

```bash
ssh testuser@"$PC2" 'sudo apt-mark manual fortunes-min && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y fortunes'
ssh testuser@"$PC1" 'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y sl'
ssh testuser@"$PC1" '~/.local/bin/pc-switcher sync pc2 --yes --allow-out-of-order --apply-package-installs'
ssh testuser@"$PC2" 'dpkg-query -W -f="${Package} ${Status}\n" sl fortunes fortunes-min'
```

Where installing `sl` on pc2 would take a package pc2's own apt marks manual, that install must **not** land: the collateral question is a loss on pc2, so it belongs to the removal flag, and a declined collateral question leaves the change causing it unapproved. `fortunes` and `fortunes-min` are both still there. If this fixture produces no collateral on these images, record it as not exercised rather than improvising a trigger.

### 5.3 A registry transfer that would lose an entry still ends the run

No flag approves it.

```bash
ssh testuser@"$PC2" 'printf "snippets:\n  \"unreproducible:unowned-path:/opt/pcsw-uat-pc2\":\n    label: /opt/pcsw-uat-pc2\n    install_body: \"true\"\n    version_body: \"echo 1.0\"\n    authored_at: \"2026-08-01T00:00:00+00:00\"\n    authored_on: pc2\n" > ~/.config/pc-switcher/package-snippets.yaml'
ssh testuser@"$PC1" '~/.local/bin/pc-switcher sync pc2 --yes --allow-out-of-order --apply-package-installs --apply-package-removals'
echo "exit code: $?"
ssh testuser@"$PC2" 'grep -c "label: /opt/pcsw-uat-pc2" ~/.config/pc-switcher/package-snippets.yaml'   # 1 — still there
```

The run must end rather than overwrite, naming the entry pc2 would lose, with exit code 1 — so the two registries can be consolidated by hand.

## 6. A registry entry carrying the wrong bodies

An entry with the wrong set of bodies is as unreadable as a corrupt file: the run ends naming the file, and nothing is completed by a default. Two ways to get it wrong, one per entry type — an unowned path with no `version_body`, and any other kind carrying one.

```bash
ssh testuser@"$PC1" 'printf "snippets:\n  \"unreproducible:unowned-path:/opt/pcsw-uat-half\":\n    label: /opt/pcsw-uat-half\n    install_body: \"true\"\n    authored_at: \"2026-08-01T00:00:00+00:00\"\n    authored_on: pc1\n" > ~/.config/pc-switcher/package-snippets.yaml'
ssh testuser@"$PC1" '~/.local/bin/pc-switcher sync pc2 --dry-run --yes --allow-out-of-order'
echo "exit code: $?"
```

The message must name `~/.config/pc-switcher/package-snippets.yaml` and the machine, and say to repair or delete it before starting a new sync. Exit code 1, and neither machine changed.

The mirror case, an `apt-no-candidate` entry carrying a `version_body` nothing would ever run:

```bash
ssh testuser@"$PC1" 'printf "snippets:\n  \"unreproducible:apt-no-candidate:pcsw-uat-extra\":\n    label: pcsw-uat-extra\n    install_body: \"true\"\n    version_body: \"echo 1.0\"\n    authored_at: \"2026-08-01T00:00:00+00:00\"\n    authored_on: pc1\n" > ~/.config/pc-switcher/package-snippets.yaml'
ssh testuser@"$PC1" '~/.local/bin/pc-switcher sync pc2 --dry-run --yes --allow-out-of-order'
echo "exit code: $?"
```

The same ending, naming the same file and this entry.

```bash
ssh testuser@"$PC1" 'rm ~/.config/pc-switcher/package-snippets.yaml'
```

## 7. A snippet for a package apt can install too

The one state the detection rule cannot resolve (#285): the registry claims a package that apt on pc1 can install from a repository. Take any ordinary repository package pc1 has installed — `tree` here — and write a snippet claiming it:

```bash
ssh testuser@"$PC1" 'sudo apt-get install --assume-yes tree'
ssh testuser@"$PC1" 'printf "snippets:\n  \"unreproducible:apt-no-candidate:tree\":\n    label: tree\n    install_body: \"sudo dpkg --install /tmp/tree.deb\"\n    authored_at: \"2026-08-01T00:00:00+00:00\"\n    authored_on: pc1\n" > ~/.config/pc-switcher/package-snippets.yaml'
ssh testuser@"$PC1" '~/.local/bin/pc-switcher sync pc2 --allow-out-of-order'
```

`manual_deb_sync`'s last screen must ask **Who put tree on pc1?**, headed `manual deb tree (<version>)` with the recorded snippet body between that heading and the question, and the row's detail naming the repository apt would install from. The heading carries the installed version because every item label does; the question does not, because it is about the package. The body arriving before anything says what the screen is about is a finding (#287). Exactly two answers, `y` "apt did" and `k` "I did", and no third — `x` must do nothing.

Answer `k`. The run must print the pin file — `/etc/apt/preferences.d/keep-tree`, `Pin-Priority: -1` — and say `apt-mark hold` is the weaker lever. Then check nothing was recorded:

```bash
ssh testuser@"$PC1" 'cat ~/.config/pc-switcher/package-snippets.yaml; cat ~/.config/pc-switcher/manual_deb.decisions.yaml 2>/dev/null'
```

The entry is still there and the decision file holds nothing about `tree`. Run the same sync again: the same question must be put again, unchanged.

Now answer `y`, and the entry must be gone from BOTH machines with no consent question about the registry push in between:

```bash
ssh testuser@"$PC1" 'cat ~/.config/pc-switcher/package-snippets.yaml'
ssh testuser@"$PC2" 'cat ~/.config/pc-switcher/package-snippets.yaml'
```

Neither file names `tree`, and `tree` is still installed on pc1 — the answer deletes a registry entry, never software. A third sync must ask nothing.

Finally, prove the pin ends the question the other way: rewrite the entry, write the pin file on pc1, and sync again.

```bash
ssh testuser@"$PC1" 'printf "snippets:\n  \"unreproducible:apt-no-candidate:tree\":\n    label: tree\n    install_body: \"sudo dpkg --install /tmp/tree.deb\"\n    authored_at: \"2026-08-01T00:00:00+00:00\"\n    authored_on: pc1\n" > ~/.config/pc-switcher/package-snippets.yaml'
ssh testuser@"$PC1" 'printf "Package: tree\nPin: release *\nPin-Priority: -1\n" | sudo tee /etc/apt/preferences.d/keep-tree'
ssh testuser@"$PC1" 'apt-cache policy tree'
ssh testuser@"$PC1" '~/.local/bin/pc-switcher sync pc2 --allow-out-of-order'
```

`apt-cache policy` must report `Candidate: (none)`, and the run must no longer ask who installed it: `tree` is now an ordinary `manual_deb_sync` item resolved by its own snippet.

```bash
ssh testuser@"$PC1" 'sudo rm /etc/apt/preferences.d/keep-tree; rm ~/.config/pc-switcher/package-snippets.yaml'
```

## 8. Cleanup

```bash
tests/integration/scripts/reset-vm.sh pc1
tests/integration/scripts/reset-vm.sh pc2
tests/integration/scripts/internal/lock.sh release "janfr-uat-02-02"
```

## 9. Not exercised here

The base review, which `02-UAT-01-RUNBOOK.md` covers and whose code has not changed: credential redaction, apt repository deletion, the apt collateral cascade and its second round, the flatpak filter and its ordering against the installs it governs, the repository-conflict question, the Ubuntu Pro gate, and the folder-mirror boundary against the package jobs.

The apt update-timer **self-heal** path: a run killed mid-flight leaves a deferred restart pending, and a later run must push it past its own end rather than let it fire inside one, then carry it out at cleanup. It needs a deliberately killed run and inspection of both machines' pending `systemd-run` state, and is excluded by choice.

`--confirm-each-command` over the four snippet jobs' own writes, beyond the incidental use in §3.9. The installed-version snippet is deliberately outside that gate and is not checked here either.

A `manual_flatpak_sync` finding installed from a local `.flatpak` bundle: §2 produces the other route to the same state, an app whose remote was deleted, and the two are one predicate. A bundle's pseudo-origin carrying no URL at all is the case not reached.

The shape question for an `/opt` directory holding several directories and no file, which `02-UAT-01-RUNBOOK.md` §4 covers unchanged.
