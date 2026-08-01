# UAT 02-01 runbook: the package review on two machines

Every command below is for you to run. `pc1` is the source, `pc2` the target; every title, answer and message prints those two hostnames, and "source" or "target" naming a machine anywhere you read is a finding.

## 1. Machines

Acquire the Hetzner label lock yourself and release it yourself; never run `lock.sh clear`.

```bash
cd /home/janfr/dev/pc-switcher
export HCLOUD_TOKEN="$(pass show dev/pc-switcher/testing/hcloud_token_rw)"
tests/integration/scripts/internal/lock.sh status
tests/integration/scripts/internal/lock.sh acquire "janfr-uat-02-01"
export PC1="$(hcloud server ip pc1)"
export PC2="$(hcloud server ip pc2)"
for h in "$PC1" "$PC2"; do
  ssh testuser@"$h" 'curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash -s -- --ref gsd/phase-02-package-management-sync'
done
ssh testuser@"$PC1" 'bash -s -- --with-app' < tests/integration/scripts/internal/vm-test-fixtures.sh
ssh testuser@"$PC2" 'bash -s' < tests/integration/scripts/internal/vm-test-fixtures.sh
ssh testuser@"$PC1" 'mkdir -p ~/.config/pc-switcher && printf "logging:\n  file: DEBUG\nsync_jobs:\n  apt_sync: true\n  snap_sync: true\n  flatpak_sync: true\n  manual_installs_sync: true\n" > ~/.config/pc-switcher/config.yaml'
```

## 2. Diverge the two machines

On pc1, the hold on `sl` is a hold on a package pc1 has, so it travels with the install and is never a question. The four filesystem fixtures are one scan case each: `/opt/pcsw-uat-app` holds a file, so it is the finding; `/opt/pcsw-uat-vendor` holds two directories and no file, so only you can say what it is; `/opt/pcsw-uat-empty` holds nothing anywhere beneath it; `/usr/local/share` is not scanned at all. The symlink in `/usr/local/bin` is put on both machines, so pc2 already holds it and it is never presented.

```bash
ssh -t testuser@"$PC1"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y sl cmatrix tree
sudo apt-mark hold sl
apt-cache policy sl | head -2                    # note pc1's installed version of sl
snap list hello                                  # note pc1's revision of hello
sudo mkdir -p /opt/pcsw-uat-app /opt/pcsw-uat-vendor/alpha /opt/pcsw-uat-vendor/beta /opt/pcsw-uat-empty
echo hi | sudo tee /opt/pcsw-uat-app/README /opt/pcsw-uat-vendor/alpha/run /opt/pcsw-uat-vendor/beta/run >/dev/null
sudo touch /usr/local/share/pcsw-uat-note
sudo ln -s /opt/pcsw-uat-app/README /usr/local/bin/pcsw-uat
printf 'allow *\n' > /home/testuser/uat.filter && flatpak remote-modify --user --filter=/home/testuser/uat.filter flathub
exit
```

On pc2: recording `fortunes` as pc2's own makes it invisible to the review and protected as collateral; the filter on `flathub` is pc2's own and excludes the application pc1 has; of the two repository files pc1 does not have, one is used by nothing and one feeds everything pc2 has; and `hello` is moved off pc1's revision so the run has a revision to converge.

```bash
ssh -t testuser@"$PC2"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y fortunes cowsay tree
sudo apt-mark manual fortunes-min
apt-get -s remove -y fortunes-min | grep '^Remv'    # must list fortunes-min AND fortunes
mkdir -p ~/.config/pc-switcher
printf 'machine_specific:\n  "apt:package:fortunes":\n    item_class: apt_package\n    label: fortunes\n    reason: null\n    recorded_at: "2026-07-30T00:00:00+00:00"\n' > ~/.config/pc-switcher/apt.decisions.yaml
sudo tee /etc/apt/sources.list.d/99-pcsw-uat.sources >/dev/null <<'EOF'
Enabled: no
Types: deb
URIs: https://uat:s3c'ret@packages.example.com/apt
Suites: noble
Components: main
EOF
sudo cp /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/99-pcsw-inuse.sources
sudo sed -i 's/^Types:/Enabled: no\nTypes:/' /etc/apt/sources.list.d/99-pcsw-inuse.sources
sudo snap remove hello-world
snap info hello | sed -n '/^channels:/,$p'          # pick a revision, in parentheses, other than pc1's
sudo snap refresh --revision=<that revision> hello
flatpak remote-add --user --if-not-exists pcsw-uat https://dl.flathub.org/beta-repo/flathub-beta.flatpakrepo
printf 'allow org.freedesktop.*\n' > /home/testuser/pc2.filter && flatpak remote-modify --user --filter=/home/testuser/pc2.filter flathub
sudo ln -s /opt/pcsw-uat-app/README /usr/local/bin/pcsw-uat   # a dangling symlink: the path is what pc2 holds
apt-cache policy tree                               # note the two versions
sudo apt-get install -y --allow-downgrades tree=<the older version from that table>
exit
```

## 3. Three bookkeeping failures that end the run

Each of these ends the run while planning, before any question and before anything is written, and each names the repair. They are checked one job at a time because a job's own review comes before the next job plans: enabling only the job under test is what keeps the run short. Every one of them is a `--dry-run`, so a check that does NOT abort changes nothing — walk out of the review with `<ctrl-c>` and record it as a finding.

Each run must end with `Sync aborted: <message>` and exit code 1, and neither machine may change.

```bash
# 1. A hold naming a package the machine does not have
ssh testuser@"$PC2" 'sudo apt-mark hold sl'
ssh -t testuser@"$PC1"
printf 'logging:\n  file: DEBUG\nsync_jobs:\n  apt_sync: true\n  snap_sync: false\n  flatpak_sync: false\n  manual_installs_sync: false\n' > ~/.config/pc-switcher/config.yaml
pc-switcher sync pc2 --dry-run --yes --allow-first-sync
```

The message must name the package and the machine, and give the `sudo apt-mark unhold sl` that clears it: `pc2 holds apt package(s) it does not have installed: sl.` Clear it before going on.

```bash
exit
ssh testuser@"$PC2" 'sudo apt-mark unhold sl'

# 2. A source filter that hides what the source itself installed
ssh -t testuser@"$PC1"
printf 'allow org.freedesktop.*\n' > /home/testuser/uat.filter     # narrows pc1's own flathub
printf 'logging:\n  file: DEBUG\nsync_jobs:\n  apt_sync: false\n  snap_sync: false\n  flatpak_sync: true\n  manual_installs_sync: false\n' > ~/.config/pc-switcher/config.yaml
pc-switcher sync pc2 --dry-run --yes --allow-first-sync
```

The message must name all three things: the application `io.github.fragglet.sdl_sopwith/x86_64/stable`, the remote `flathub`, and the filter `/home/testuser/uat.filter` — and must offer both repairs, correcting the filter or uninstalling the application on pc1.

```bash
printf 'allow *\n' > /home/testuser/uat.filter                     # back to a filter that offers it

# 3. A snippet registry nobody can parse
printf 'snippets: [\n' > ~/.config/pc-switcher/package-snippets.yaml
printf 'logging:\n  file: DEBUG\nsync_jobs:\n  apt_sync: false\n  snap_sync: false\n  flatpak_sync: false\n  manual_installs_sync: true\n' > ~/.config/pc-switcher/config.yaml
pc-switcher sync pc2 --dry-run --yes --allow-first-sync
```

The message must name the file and the machine — `the install-snippet registry ~/.config/pc-switcher/package-snippets.yaml on pc1 cannot be read as a registry` — and say to repair or delete it before starting a new sync. An absent registry is ordinary data, so deleting it is the repair here.

```bash
rm ~/.config/pc-switcher/package-snippets.yaml
printf 'logging:\n  file: DEBUG\nsync_jobs:\n  apt_sync: true\n  snap_sync: true\n  flatpak_sync: true\n  manual_installs_sync: true\n' > ~/.config/pc-switcher/config.yaml
exit
```

## 4. The run

```bash
ssh -t testuser@"$PC1"
env | grep PCSWITCHER_PACKAGE_REVIEW_AUTOMATION   # must print nothing
pc-switcher sync pc2 --dry-run --yes --allow-first-sync
pc-switcher sync pc2 --yes --allow-first-sync
```

Add `--allow-out-of-order` if the topology check asks. The dry run shows every question and converges nothing; walk it first, then answer the real run so every outcome is provable: `cmatrix` `<x>` and `sl` left at install; the `cowsay` removal `<x>` and the `fortunes-min` removal `<y>`; the collateral question "go ahead"; the repository deletion `<s>` in the second round; `hello`'s revision change left applied; `/opt/pcsw-uat-vendor` answered "one application" and then `<x>`; `/opt/pcsw-uat-app` answered `<y>`, writing `sudo mkdir -p /opt/pcsw-uat-app && echo hi | sudo tee /opt/pcsw-uat-app/README` in the snippet editor. Use a shifted key on one multi-item group first, to confirm it sets every line, then correct the lines you did not mean.

## 5. What to check while answering

The jobs run in order — apt, snap, flatpak, manual — and each one's questions come before the next one plans.

- Each group is one question: every item on a line, the answer it carries in a column to the right, arrows moving between lines, `<space>` cycling the focused line, `<enter>` confirming. Nothing is asked a second time.
- The legend's permanent answer reads `keep on pc2 for good; it is pc2's own, and will not be asked again` on a removal and `do not install on pc2 for good; it is pc1's own, and will not be asked again` on an install; a question that records nothing is the same widget with `<x>` missing.
- `sl` is offered for install and nothing anywhere asks about its hold: a hold travels with its package, so no group, line or legend mentions one.
- The report group is titled `Version differences (apt packages)` and notes ``These converge on their own: run `sudo apt update && sudo apt upgrade` on pc2.``
- The collateral question is titled `Packages you installed on pc2 or marked as its own that this sync would remove, downgrade or upgrade (apt)`, names `fortunes`, states `Removing fortunes-min on pc2 would remove fortunes`, says it is protected on both grounds, and offers `remove fortunes-min from pc2, so fortunes is removed as well`, `skip now` and `stop the sync`.
- Only after those screens are confirmed does `Delete repositories pc1 no longer has (apt)` appear — a second round, asked because of the answers you just gave. It comes one file at a time and offers `99-pcsw-uat.sources` only, its detail reading `pc2 would stop getting software from https://***@packages.example.com/apt` — the password appears nowhere. `99-pcsw-inuse.sources` is not asked about at all.
- `snap_sync` asks `Change snap packages` with two answers and no `<x>`: `hello`'s line reads `overwrites revision <pc2's> on pc2 with revision <pc1's>`, and the row starts applied rather than skipped.
- `manual_installs_sync` asks `What is /opt/pcsw-uat-vendor on pc1?` while it is still planning, before its review: it names `alpha` and `beta`, and its two answers say what pc2 would be given either way.
- Its review then offers `/opt/pcsw-uat-app` and `/opt/pcsw-uat-vendor` and nothing else — `/opt/pcsw-uat-empty`, `/usr/local/share/pcsw-uat-note` and `/usr/local/bin/pcsw-uat` are named nowhere, the last of them because pc2 already has it. Each item is asked on its own with three answers, and answering `<y>` opens the editor saying `(Ctrl-D to finish)`. Submit an empty body, then one of only spaces: each must be refused and the three answers offered again.
- Ctrl-C on any group ends the whole sync with `package review aborted at '<that group's title>' (Ctrl-C)`, exit code 1, and a terminal that still works.

## 6. What to check afterwards

On pc1:

```bash
grep -c cmatrix ~/.config/pc-switcher/apt.decisions.yaml       # 1 — an install is recorded here
grep -c cowsay ~/.config/pc-switcher/apt.decisions.yaml        # 0
grep -c pcsw-uat-vendor ~/.config/pc-switcher/manual.decisions.yaml  # 1 — pc1 holds it, so pc1 records it
grep -c pcsw-uat-app ~/.config/pc-switcher/package-snippets.yaml     # 1 — the snippet you wrote
LOG=$(ls -t ~/.local/share/pc-switcher/logs/sync-*.log | head -1)
grep -E 'reviewed cowsay|reviewed fortunes \(report_only\)' "$LOG"   # "marked as this machine's own", then "applied"
grep -n -E 'apt-get.*sl=|apt-mark hold sl' "$LOG"              # pc1's exact version, then the hold, in that order
grep -E 'keeping repository 99-pcsw-inuse.sources' "$LOG"
grep -n -E 'apply the ref filter /home/testuser/uat.filter|sdl_sopwith' "$LOG" | head
grep -E 'delete user flatpak remote pcsw-uat' "$LOG"
grep -c "s3c'ret" "$LOG"                                       # 0
grep -c 'stdout: ' "$LOG"                                      # apt's, snap's and flatpak's own output, verbatim
grep -c 'send_file.*package-snippets.yaml' "$LOG"              # 1 — the job's own push, the registry's only route
```

The filter line must come before any `sdl_sopwith` install: the filter is in force before anything installs from that remote, and pc2's own filter is never taken off for the install's benefit.

On pc2:

```bash
grep -c cowsay ~/.config/pc-switcher/apt.decisions.yaml    # 1 — a removal is recorded here
grep -c cmatrix ~/.config/pc-switcher/apt.decisions.yaml   # 0
dpkg-query -W -f='${Package} ${Version} ${Status}\n' sl cmatrix fortunes fortunes-min
apt-mark showhold                                # sl, registered after its install landed
snap list hello hello-world                      # hello at pc1's revision, hello-world arrived
flatpak remotes --user --columns=name,filter     # flathub carries uat.filter, pcsw-uat is gone
flatpak list --user --app --columns=application  # sopwith arrived under pc1's filter
ls /etc/apt/sources.list.d/ /opt /opt/pcsw-uat-app
```

`sl` is installed at pc1's version and `cmatrix` is absent; `fortunes` and `fortunes-min` are both gone, the collateral answer having taken them; both `.sources` files are still there; `/opt/pcsw-uat-app` exists with its `README`, the snippet having replayed in the run that authored it, and `/opt/pcsw-uat-vendor` does not — it is pc1's own.

## 7. Two runs with no terminal

`ssh` without `-t` leaves the run no terminal, which is the only way to reach these paths. Sideload `hello-world` on pc2 first: a sideloaded snap is out of scope on both machines, so the run must neither move it nor mention it.

The first run must report apt_sync SKIPPED — the repository deletion you left for now is still to be answered and nobody was there — snap_sync, flatpak_sync and manual_installs_sync successful with nothing left to decide, and a count of 0: no snippet registry is transferred without an answer, on the success outcome as much as the skipped one.

```bash
ssh testuser@"$PC2" 'cd /tmp && sudo snap download hello-world --basename=uat-hello-world && sudo snap remove hello-world && sudo snap install --dangerous /tmp/uat-hello-world.snap'
ssh testuser@"$PC1" 'pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order'
ssh testuser@"$PC1" 'grep -c "send_file.*package-snippets.yaml" "$(ls -t ~/.local/share/pc-switcher/logs/sync-*.log | head -1)"'
ssh testuser@"$PC2" 'snap list hello-world'      # still the sideloaded x-revision, and the log names it nowhere
ssh testuser@"$PC2" 'sudo chmod -x /usr/bin/flatpak /usr/bin/snap'
ssh testuser@"$PC1" 'sudo chmod 000 /opt'
ssh testuser@"$PC1" 'pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order'
ssh testuser@"$PC2" 'sudo chmod +x /usr/bin/flatpak /usr/bin/snap'
ssh testuser@"$PC1" 'sudo chmod 755 /opt'
```

The second ends `Sync finished with failures:` with one line per failed job — flatpak_sync, snap_sync and manual_installs_sync — each naming the command that did not answer rather than the job alone, while apt_sync still reaches its own outcome. `manual_installs_sync` fails on the scan of pc1's own `/opt`, which is half of a diff it now runs on both machines. The log also warns that snapd auto-refresh was not paused on the machine whose `refresh.hold` could not be read.

## 8. Cleanup

```bash
tests/integration/scripts/reset-vm.sh pc1
tests/integration/scripts/reset-vm.sh pc2
tests/integration/scripts/internal/lock.sh release "janfr-uat-02-01"
```

## 9. Not exercised here

Pin deletion; the repository-conflict and flatpak remote-conflict questions; the Ubuntu Pro gate; the snippet-registry overwrite confirmation and the credentials it withholds; an `/etc/apt/apt.conf.d` file differing on both machines, the only group whose permanent answer reads `do not update on pc2 for good`; a repository whose only remaining users are packages apt installed automatically; a repository kept because the removal that would have freed it was declined; the "one application per directory" answer to the `/opt` shape question; a hold whose package's install you declined, which is reported as declined rather than failed; a filter that cannot be copied or applied, which warns and fails the applications from that remote; and `--confirm-each-command` (test 3 of `02-UAT.md`).

`folder_sync` is off here — the config lists the four package jobs and nothing else — so the folder boundary goes with it: that the mirror carries neither the snippet registry nor a snap revision directory the target is not on is not checked by this runbook.

The late collateral question needs a setup this runbook does not build: an install whose repository the same run writes, whose transaction also removes or moves a package pc2 protects. Its three answers, the withdrawn install that is neither applied nor failed, and the log line naming the repository left behind go with it. Record all of the above as not exercised rather than improvising a trigger.
