# UAT 02-02 runbook: the four snippet jobs, version convergence, and the unattended flags

Every command below is for you to run. `pc1` is the source, `pc2` the target; every title, answer and message prints those two hostnames, and "source" or "target" naming a machine anywhere you read is a finding.

This runbook covers what changed after the phase-2 UAT: the split of `manual_installs_sync` into four jobs, the second snippet every registry entry now carries, version convergence and its retry loop, removals of unreproducible items, the two unattended apply flags, the machine-specific follow-up question, the apt update-timer pause, and the end-of-run outcome block. What it does not cover is in §8. `02-UAT-01-RUNBOOK.md` remains the runbook for the base review.

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

## 2. Diverge the two machines

Eight fixtures, one per surface under test. Each is a case for one article, named as it is set up.

### 2.1 On pc1

`pcsw-uat-deb` is a package no repository declares, so pc1's apt reports its installed version as the whole of its version table and names no origin for it — the shape `PKG-FR-DEB-OWNERSHIP` turns on. `pcsw-uat-drift` is the same shape at a higher version than pc2's copy, which is `PKG-FR-MANUAL-VERSION`'s case. The sideload declares a base pc1 already holds, so snapd fetches nothing, and `snap try` gives it the `x`-prefixed revision `PKG-FR-SNAP-SIDELOAD` keys on. `/opt/pcsw-uat-app` holds a file, so it is a finding rather than a shape question. `/opt/pcsw-uat-loop` is the converge loop's item: pc1 is at `2.0`, pc2 at `1.0`, and the registry entry written below replays a body that exits zero and moves nothing.

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

# manual_installs_sync: one plain finding, one that drifts
sudo mkdir -p /opt/pcsw-uat-app /opt/pcsw-uat-loop
echo hi | sudo tee /opt/pcsw-uat-app/README >/dev/null
echo 2.0 | sudo tee /opt/pcsw-uat-loop/version >/dev/null

# Three recorded entries. An item both machines have at different versions only
# reaches the update screen when a snippet is ALREADY recorded for it — with nothing
# to replay, the run asks how to reproduce it instead, on the resolution screen with
# the verb "update". So the two drifting package items get real entries, and
# /opt/pcsw-uat-loop gets one whose install body exits 0 and moves nothing.
cat > ~/.config/pc-switcher/package-snippets.yaml <<'YAML'
snippets:
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
    version_body: "dpkg-query -W -f='${Version}' pcsw-uat-drift"
    authored_at: "2026-08-01T00:00:00+00:00"
    authored_on: pc1
  "unreproducible:snap-sideload:pcsw-uat-snapdrift":
    label: pcsw-uat-snapdrift
    install_body: |
      set -eu
      d=/var/tmp/pcsw-uat-snapdrift
      sudo mkdir -p "$d/meta"
      printf "name: pcsw-uat-snapdrift\nversion: '2.0'\nsummary: pc-switcher UAT sideload\ndescription: A snap installed from local bytes.\nbase: core20\nconfinement: strict\ngrade: stable\n" | sudo tee "$d/meta/snap.yaml" >/dev/null
      sudo snap try "$d"
    version_body: "snap list pcsw-uat-snapdrift | awk 'NR==2 {print $2}'"
    authored_at: "2026-08-01T00:00:00+00:00"
    authored_on: pc1
YAML

# mark-side: an apt.conf.d file both machines have, with different content
printf 'APT::Install-Recommends "false";\n' | sudo tee /etc/apt/apt.conf.d/99-pcsw-uat >/dev/null
exit
```

### 2.2 On pc2

`pcsw-uat-drift` at the lower version and `pcsw-uat-snapdrift` at the lower version are the other halves of the two version cases; the snap's revision is `x1` on both machines, so a run that converges it is provably comparing the declared version and not the revision. `pcsw-uat-gone` is a hand `.deb` pc1 does not have, which is `PKG-FR-MANUAL-REMOVE`'s case, and `/opt/pcsw-uat-orphan` is the same case one job over. The `apt.conf.d` file differs from pc1's, which is what makes it the conflicting item the mark-side follow-up asks about.

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

sudo mkdir -p /opt/pcsw-uat-orphan /opt/pcsw-uat-loop
echo hi | sudo tee /opt/pcsw-uat-orphan/README >/dev/null
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

A dry run converges nothing, so three things below cannot happen in it: no snippet is recorded, no registry is pushed, and the converge loop of §3.7 never opens — `_converge_one` is not called at all (`jobs/packages/sync_core.py:804`). Walk the dry run for the screens, then answer the real run for the outcomes.

Findings already raised from a walk of these fixtures, so they need no re-reporting — check they still read as described and move on: review copy and titles (#276), the `apt.conf.d` digests of §3.3 (#277), that screen's keys and default (#278), the repeated scrollback frames (#279), job names in the status line (#280), the snippet screens of §3.5 and §3.6 (#281), the second editor of §3.6 (#282), and the group order of §3.2 (#283).

### 3.1 The three exclusions

Each pair of jobs decides its boundary by one shared rule, so a finding claimed by the snippet job must be silent in the package-manager job — on **both** machines, not just on pc1.

- `apt_sync` names `pcsw-uat-deb`, `pcsw-uat-drift` and `pcsw-uat-gone` nowhere: no item, no review line, no install and no removal. `pcsw-uat-gone` is the one to watch — it is only on pc2, so a job that had not excluded it would offer it as software pc1 had deleted.
- `snap_sync` names `pcsw-uat-snap` and `pcsw-uat-snapdrift` nowhere, and says nothing about a hold on either.
- `flatpak_sync` names `io.github.fragglet.sdl_sopwith` nowhere and derives no remote for it — in particular it does not try to add a remote with an empty URL.

`snap_sync` and `flatpak_sync` therefore have nothing of their own to do in this run and must report `success`, not `skipped`: a review holding nothing to decide is the goal already met. `apt_sync`'s only item is the `apt.conf.d` file of §3.3.

### 3.2 The seven reviews, and the order the screens come in

The jobs run in the order the config lists them — apt, snap, flatpak, manual_deb, manual_snap, manual_flatpak, manual_installs, then the folder mirror — and each one's questions come before the next one plans. Inside one snippet job the groups come in a fixed order: **removal first, then the version differences, then the items needing a snippet.**

The sections below follow the order you will meet the screens. On these fixtures that is:

| # | Job | Screen | Section |
|---|-----|--------|---------|
| 1 | `apt_sync` | `Update apt configuration files` → the follow-up it raises | §3.3 |
| 2 | `manual_deb_sync` | `Remove manual_deb packages` | §3.4 |
| 3 | `manual_deb_sync` | version difference, `pcsw-uat-drift` | §3.5 |
| 4 | `manual_deb_sync` | resolution, `pcsw-uat-deb` — two editors | §3.6 |
| 5 | `manual_snap_sync` | version difference, then resolution | §3.5, §3.6 |
| 6 | `manual_flatpak_sync` | resolution, `sdl_sopwith` | §3.6 |
| 7 | `manual_installs_sync` | `Remove manual packages`, then version difference, then resolution | §3.4, §3.5, §3.6 |
| 8 | `manual_installs_sync` | the converge-loop retry, during apply | §3.7 |
| 9 | — | `Job outcomes:` | §3.8 |

The converge-loop retry is last because it is not a review screen at all: it is put while the job is applying what you approved, after every screen that job asked.

- Each of the four snippet jobs puts its own review. A job's findings never appear in another's.
- The group offering software pc2 cannot get is titled `pc1 has these and no package manager can reproduce them on pc2 (<manager>)`, where `<manager>` is `manual_deb`, `manual_snap`, `manual_flatpak` or `manual`.
- The group for an item both machines have at different versions is titled `pc1 and pc2 have these at different versions (<manager>)`.
- **The removal group titles are a confirmed finding, not a pass.** They are built from the internal manager id and default to the noun "packages". A dry run on these fixtures prints `Remove manual packages` for the `/opt/pcsw-uat-orphan` path deletion — wrong about both words — and `Remove manual_deb packages`, which leaks an internal id at the user. `manual_flatpak` would say "packages" where flatpak says "applications". Confirm the strings on your run and record them.

The group titles that ARE right, and worth confirming read well on a real terminal:

```plain
pc1 has these and no package manager can reproduce them on pc2 (manual_deb)
pc1 and pc2 have these at different versions (manual_snap)
```

### 3.3 The machine-specific follow-up

The first question of the run, in `apt_sync`. `/etc/apt/apt.conf.d/99-pcsw-uat` is on both machines with different content, so its row starts at **skip now** — replacing a file pc2's own user wrote is as irreversible as a deletion. Answer `<x>` on it.

After the batch screen is confirmed — not folded into it — a further screen must appear, titled `Kept for good — whose own version is it?`, with one row per permanently-kept conflicting item. Confirm:

- Three answers, keyed and worded as the two hostnames and `both`: `pc1`, `pc2`, `both`.
- Each hint says how long the mark lasts on that machine — `it is pc1's own version; nothing overwrites it while pc1 has it`, and for `both`, that each version is its own machine's.
- The row defaults to `pc2`, so confirming the screen unread records what the permanent answer already said in its own words.
- Its explanation names both machines and says the answer lasts as long as that machine still has the item.

Answer `both` on this run, so §4 can check that a mark landed on each machine.

Nothing else in this run reaches that screen: an install is on pc1 alone and a removal on pc2 alone, so for those the action already names the holder. A version difference must not reach it either — it has no permanent answer at all.

### 3.4 Removals

The first screen of `manual_deb_sync`, and again of `manual_installs_sync`. `pcsw-uat-gone` and `/opt/pcsw-uat-orphan` are on pc2 only. Each is offered for removal by the job whose own detector claims it there, and by no other. Confirm each row starts at **skip now**, and that the path deletion's screen carries the line saying its reach is smaller than its name: `Only the path itself is deleted on pc2. Whatever installed it may also have left a launcher, a symlink or a service unit outside these directories, and nothing here knows where; those stay.`

Approve both. The warning's wording is #276's; what to check here is that it appears at all, on the path deletion and not on the package one.

### 3.5 Version convergence

Follows the removal group in each job that has one. Three items reach this screen, one per ecosystem, and they are exactly the three that have a recorded snippet: `pcsw-uat-drift` (deb), `pcsw-uat-snapdrift` (snap) and `/opt/pcsw-uat-loop` (path). Each is asked on its own screen titled `pc2 has a different version of <item> — update it?`, with exactly three answers and **no** `<x>`:

- `<y> update` — `run the recorded snippet on pc2`
- `<w> new snippet` — `rewrite the snippet first, then run it on pc2`
- `<s> skip now` — `leave pc2's version as it is for now; will be asked again next sync`

The comparison must be made on the snap's **version**, `2.0` against `1.0`, and never on its revision — both machines are at `x1`, so a run comparing revisions would find nothing to do. The revision must not be printed either: it is not something the user decides on (#276).

Answer, in the order the three come:

- `pcsw-uat-drift` (deb) — `<w>`, to confirm the editor opens **on the recorded body** rather than empty, which is what makes a rewrite an edit. Change the `Description:` line so the body is provably yours, submit both editors, and let it run.
- `pcsw-uat-snapdrift` (snap) — `<y>`, the plain replay of a recorded snippet, with no editor in the way.
- `/opt/pcsw-uat-loop` (path) — `<y>`. Its recorded body cannot converge it, which is what §3.7 is about; nothing says so yet, and the screen that does comes much later.

### 3.6 The two editors

The last group of each snippet job. Four items have no recorded snippet and so are asked how to reproduce them, one screen each: `pcsw-uat-deb`, `pcsw-uat-snap`, `io.github.fragglet.sdl_sopwith/x86_64/stable` and `/opt/pcsw-uat-app`. On `Install /opt/pcsw-uat-app on pc2?` answer `<y>` and confirm that **two** editors open in sequence, not one:

1. `Install-or-update snippet for /opt/pcsw-uat-app:` — its own screen states the install-or-update contract, that the body is replayed onto a machine which may already hold an older version.
2. `Installed-version snippet for /opt/pcsw-uat-app:` — its own screen states that this one runs on both machines on every sync while the run is still planning, and must be read-only.

Both must refuse an empty body and a body of only spaces, printing `Neither snippet can be empty — enter both, or choose a skip.` and putting the three answers again. Try each refusal on each editor: submitting a real install body and then an empty version body must not leave a half-written entry anywhere.

Then supply real bodies:

```bash
# install-or-update
sudo mkdir -p /opt/pcsw-uat-app && echo hi | sudo tee /opt/pcsw-uat-app/README >/dev/null
# installed-version
echo 1.0
```

The same screen for the three package-backed jobs asks for both bodies too, even though only `manual_installs_sync` ever runs the version body — the other three ask `dpkg`, `snap` and `flatpak` instead. That is what the code does today and it is going away: #282 makes the second body required only for unowned paths. Record what you see; do not read the second editor here as correct.

Answer the other three like this, each `<y>` followed by both editors:

| Item | Install-or-update body | Installed-version body |
|---|---|---|
| `pcsw-uat-deb` | rebuild and `dpkg --install` it, as §2.1 does — copy that block with the name changed | `dpkg-query -W -f='${Version}' pcsw-uat-deb` |
| `pcsw-uat-snap` | `snap try` a directory you write, as §2.1 does | `snap list pcsw-uat-snap \| awk 'NR==2 {print $2}'` |
| `io.github.fragglet.sdl_sopwith/x86_64/stable` | `flatpak install --user --assumeyes flathub io.github.fragglet.sdl_sopwith` — pc2 still has flathub, which is what makes this one reproducible by hand | `flatpak list --user --columns=application,version \| awk '/sdl_sopwith/ {print $2}'` |

None of the three version bodies is ever executed, so their exactness is not what is under test. Once #282 lands these three take one editor and no version body at all, and this table loses its right-hand column.

### 3.7 The converge loop

**Real run only.** This is not a review screen: it is put while `manual_installs_sync` applies what you approved, and a dry run applies nothing — `_converge_one` is never reached (`jobs/packages/sync_core.py:804`), so no snippet is replayed, no version is re-read and this screen cannot appear. Seeing it in a dry run is a finding.

`/opt/pcsw-uat-loop` is the item whose recorded install body is `true` — it exits zero and moves nothing — and §3.5 is where you answered `<y>` on it. Confirm:

- The version is read again on pc2 after the replay.
- The item is **not** reported as applied.
- A second screen appears, titled `/opt/pcsw-uat-loop on pc2 is still 1.0, not 2.0`, offering exactly **two** answers — `<w> new snippet` and `<s> skip now`. The replay answer is gone, because replaying the same bytes cannot change the outcome.
- Its editor opens on the body that just failed.

Answer `<w>` and write a body that actually converges, then confirm the item comes out applied:

```bash
echo 2.0 | sudo tee /opt/pcsw-uat-loop/version >/dev/null
```

### 3.8 The end of the run

The last block must be headed `Job outcomes:` and give one line per job in execution order — a mark (`✔`, `⏭`, `✖`), the job name, its status (`success`, `skipped`, `failed`), and for a skipped or failed job the reason that job recorded. Confirm the failures are printed **once**: nothing else prints them again in a second shape.

Then check the apt timers were suspended for the run and put back:

```bash
systemctl is-active apt-daily.timer apt-daily-upgrade.timer
ssh testuser@"$PC2" 'systemctl is-active apt-daily.timer apt-daily-upgrade.timer'
```

Both machines must read exactly as they did before the run started. To see the suspension itself, run the sync again with `--confirm-each-command` and watch for the `systemctl stop` on each machine and the `systemd-run` that schedules the restart; or read them out of the log afterwards:

```bash
LOG=$(ls -t ~/.local/share/pc-switcher/logs/sync-*.log | head -1)
grep -nE 'apt-daily|systemd-run' "$LOG" | head -20
```

## 4. What to check afterwards

On pc1:

```bash
ls ~/.config/pc-switcher/*.decisions.yaml     # four manual files can exist, plus apt/snap/flatpak
grep -c 'label: /opt/pcsw-uat-app' ~/.config/pc-switcher/package-snippets.yaml   # 1
grep -c 'version_body' ~/.config/pc-switcher/package-snippets.yaml               # one per entry
grep -c 'install_body' ~/.config/pc-switcher/package-snippets.yaml               # the same number
grep -c '99-pcsw-uat' ~/.config/pc-switcher/apt.decisions.yaml                   # 1 — "both" recorded here too
LOG=$(ls -t ~/.local/share/pc-switcher/logs/sync-*.log | head -1)
grep -nE 'pcsw-uat-deb|pcsw-uat-snap|sdl_sopwith' "$LOG" | grep -vE 'manual_deb|manual_snap|manual_flatpak' | head
```

That last line is the exclusion read from the log: every mention of those three items must carry one of the snippet jobs, never `apt_sync`, `snap_sync` or `flatpak_sync`.

Every count reads `label:` and `install_body:` lines rather than the item's name, which repeats down an entry and would count twice.

On pc2:

```bash
dpkg-query -W -f='${Package} ${Version} ${Status}\n' pcsw-uat-deb pcsw-uat-drift pcsw-uat-gone
snap list pcsw-uat-snap pcsw-uat-snapdrift
flatpak list --user --app --columns=application,version
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

- The four things neither flag answers are each named in a warning and left for this run: the `98-pcsw-flag` apt.conf.d file pc2 already holds, `/opt/pcsw-uat-flag` which needs a snippet nobody can write, a repository conflict if one arises, and the Ubuntu Pro gate if the target is unattached. `manual_installs_sync` must therefore report `skipped` with `/opt/pcsw-uat-flag` named, in the same run where the other jobs report `success`.

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

## 6. A registry entry missing its second body

An entry carrying only `install_body` is as unreadable as a corrupt file: the run ends naming the file, and nothing is completed by a default. The fixture is deliberately an **unowned path**, the one kind that genuinely needs a version body, so this check survives #282 — which drops the requirement for the three package-backed kinds.

```bash
ssh testuser@"$PC1" 'printf "snippets:\n  \"unreproducible:unowned-path:/opt/pcsw-uat-half\":\n    label: /opt/pcsw-uat-half\n    install_body: \"true\"\n    authored_at: \"2026-08-01T00:00:00+00:00\"\n    authored_on: pc1\n" > ~/.config/pc-switcher/package-snippets.yaml'
ssh testuser@"$PC1" '~/.local/bin/pc-switcher sync pc2 --dry-run --yes --allow-out-of-order'
echo "exit code: $?"
```

The message must name `~/.config/pc-switcher/package-snippets.yaml` and the machine, and say to repair or delete it before starting a new sync. Exit code 1, and neither machine changed.

```bash
ssh testuser@"$PC1" 'rm ~/.config/pc-switcher/package-snippets.yaml'
```

## 7. Cleanup

```bash
tests/integration/scripts/reset-vm.sh pc1
tests/integration/scripts/reset-vm.sh pc2
tests/integration/scripts/internal/lock.sh release "janfr-uat-02-02"
```

## 8. Not exercised here

The base review, which `02-UAT-01-RUNBOOK.md` covers and whose code has not changed: credential redaction, apt repository deletion, the apt collateral cascade and its second round, the flatpak filter and its ordering against the installs it governs, the repository-conflict question, the Ubuntu Pro gate, and the folder-mirror boundary against the package jobs.

The apt update-timer **self-heal** path: a run killed mid-flight leaves a deferred restart pending, and a later run must push it past its own end rather than let it fire inside one, then carry it out at cleanup. It needs a deliberately killed run and inspection of both machines' pending `systemd-run` state, and is excluded by choice.

`--confirm-each-command` over the four snippet jobs' own writes, beyond the incidental use in §3.8. The installed-version snippet is deliberately outside that gate and is not checked here either.

A `manual_flatpak_sync` finding installed from a local `.flatpak` bundle: §2 produces the other route to the same state, an app whose remote was deleted, and the two are one predicate. A bundle's pseudo-origin carrying no URL at all is the case not reached.

The shape question for an `/opt` directory holding several directories and no file, which `02-UAT-01-RUNBOOK.md` §4 covers unchanged.
