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

```bash
ssh -t testuser@"$PC1"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y sl cmatrix tree
sudo apt-mark hold sl
apt-cache policy sl | head -2                    # note pc1's installed version of sl
sudo mkdir -p /opt/pcsw-uat-app && echo hi | sudo tee /opt/pcsw-uat-app/README >/dev/null
printf 'allow *\n' > /home/testuser/uat.filter && flatpak remote-modify --user --filter=/home/testuser/uat.filter flathub
exit
```

On pc2: recording `fortunes` as pc2's own makes it invisible to the review and protected as collateral; the hold on `sl` is a hold for a package pc2 does not have; the filter on `flathub` is pc2's own and excludes the application pc1 has; and of the two repository files pc1 does not have, one is used by nothing and one feeds everything pc2 has.

```bash
ssh -t testuser@"$PC2"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y fortunes cowsay tree
sudo apt-mark manual fortunes-min
sudo apt-mark hold sl
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
cd /tmp && sudo snap download hello --basename=uat-hello && sudo snap install --dangerous /tmp/uat-hello.snap
flatpak remote-add --user --if-not-exists pcsw-uat https://dl.flathub.org/beta-repo/flathub-beta.flatpakrepo
printf 'allow org.freedesktop.*\n' > /home/testuser/pc2.filter && flatpak remote-modify --user --filter=/home/testuser/pc2.filter flathub
apt-cache policy tree                               # note the two versions
sudo apt-get install -y --allow-downgrades tree=<the older version from that table>
exit
```

## 3. The run

```bash
ssh -t testuser@"$PC1"
env | grep PCSWITCHER_PACKAGE_REVIEW_AUTOMATION   # must print nothing
pc-switcher sync pc2 --dry-run --yes --allow-first-sync
pc-switcher sync pc2 --yes --allow-first-sync
```

Add `--allow-out-of-order` if the topology check asks. The dry run shows every question and converges nothing; walk it first, then answer the real run so every outcome is provable: `cmatrix` `<x>` and `sl` left at install; the `sl` hold left at hold; the `cowsay` removal `<x>` and the `fortunes-min` removal `<y>`; the collateral question "go ahead"; the repository deletion `<s>`; write `sudo mkdir -p /opt/pcsw-uat-app` in the snippet editor. Use a shifted key on one multi-item group first, to confirm it sets every line, then correct the lines you did not mean.

## 4. What to check while answering

- Each group is one question: every item on a line, the answer it carries in a column to the right, arrows moving between lines, `<space>` cycling the focused line, `<enter>` confirming. Nothing is asked a second time.
- The legend's permanent answer reads `keep on pc2 for good; it is pc2's own, and will not be asked again` on a removal and `do not install on pc2 for good; it is pc1's own, and will not be asked again` on an install; a question that records nothing is the same widget with `<x>` missing.
- `sl` is offered for install and `Hold apt packages` offers its hold as a second, separate line — pc2 holding a package it does not have suppresses neither.
- The report group is titled `Version differences (apt packages)` and notes ``These converge on their own: run `sudo apt update && sudo apt upgrade` on pc2.``
- `Delete repositories pc1 no longer has (apt)` comes one file at a time and offers `99-pcsw-uat.sources` only, its detail reading `pc2 would stop getting software from https://***@packages.example.com/apt` — the password appears nowhere. `99-pcsw-inuse.sources` is not asked about at all.
- The collateral question is titled `Packages you installed on pc2 or marked as its own that this sync would remove, downgrade or upgrade (apt)`, names `fortunes`, states `Removing fortunes-min on pc2 would remove fortunes` and offers `remove fortunes-min from pc2, so fortunes is removed as well`, `skip now` and `stop the sync`.
- `/opt/pcsw-uat-app` is asked about on its own with three answers, then the editor opens saying `(Ctrl-D to finish)`. Submit an empty body, then one of only spaces: each must be refused and the three answers offered again.
- Ctrl-C on any group ends the whole sync with `package review aborted at '<that group's title>' (Ctrl-C)`, exit code 1, and a terminal that still works.

## 5. What to check afterwards

On pc1:

```bash
grep -c cmatrix ~/.config/pc-switcher/apt.decisions.yaml   # 1 — an install is recorded here
grep -c cowsay ~/.config/pc-switcher/apt.decisions.yaml    # 0
LOG=$(ls -t ~/.local/share/pc-switcher/logs/sync-*.log | head -1)
grep -E 'reviewed cowsay|reviewed fortunes \(report_only\)' "$LOG"    # "marked as this machine's own", then "applied"
grep -E 'sl=|clear the stale apt hold on sl' "$LOG"        # installed at pc1's exact version, stale hold cleared first
grep -E 'keeping repository 99-pcsw-inuse.sources|Ignoring snap' "$LOG"
grep -E 'take the ref filter /home/testuser/pc2.filter off|apply the ref filter /home/testuser/uat.filter|delete user flatpak remote pcsw-uat' "$LOG"
grep -c "s3c'ret" "$LOG"          # 0
grep -c 'stdout: ' "$LOG"         # apt's, snap's and flatpak's own output, verbatim
```

On pc2:

```bash
grep -c cowsay ~/.config/pc-switcher/apt.decisions.yaml    # 1 — a removal is recorded here
grep -c cmatrix ~/.config/pc-switcher/apt.decisions.yaml   # 0
dpkg-query -W -f='${Package} ${Version} ${Status}\n' sl cmatrix fortunes fortunes-min
apt-mark showhold                                # sl, registered after its install landed
snap list hello hello-world                      # hello still the sideloaded revision, hello-world arrived
flatpak remotes --user --columns=name,filter     # flathub carries uat.filter, pcsw-uat is gone
flatpak list --user --app --columns=application  # sopwith arrived, so pc2's own filter came off first
ls /etc/apt/sources.list.d/ /opt/pcsw-uat-app
```

`sl` is installed at pc1's version and `cmatrix` is absent; `fortunes` and `fortunes-min` are both gone, the collateral answer having taken them; both `.sources` files are still there; `/opt/pcsw-uat-app` exists, the snippet having replayed in the run that authored it.

## 6. Two runs with no terminal

`ssh` without `-t` leaves the run no terminal, which is the only way to reach these paths. The first must report apt_sync SKIPPED — its plan still holds the `tree` version difference and nobody was there to answer it — manual_installs_sync successful with nothing to review, and a count of 0: no snippet registry is transferred without an answer.

```bash
ssh testuser@"$PC1" 'pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order'
ssh testuser@"$PC1" 'grep -c "send_file.*package-snippets.yaml" "$(ls -t ~/.local/share/pc-switcher/logs/sync-*.log | head -1)"'
ssh testuser@"$PC2" 'sudo chmod -x /usr/bin/flatpak /usr/bin/snap'
ssh testuser@"$PC1" 'sudo chmod 000 /opt'
ssh testuser@"$PC1" 'pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order'
ssh testuser@"$PC2" 'sudo chmod +x /usr/bin/flatpak /usr/bin/snap'
ssh testuser@"$PC1" 'sudo chmod 755 /opt'
```

The second ends `Sync finished with failures:` with one line per failed job — flatpak_sync, snap_sync and manual_installs_sync — each naming the command that did not answer rather than the job alone, while apt_sync still reaches its own outcome. The log also warns that snapd auto-refresh was not paused on the machine whose `refresh.hold` could not be read.

## 7. Cleanup

```bash
tests/integration/scripts/reset-vm.sh pc1
tests/integration/scripts/reset-vm.sh pc2
tests/integration/scripts/internal/lock.sh release "janfr-uat-02-01"
```

## 8. Not exercised here

Pin deletion; the repository-conflict and flatpak remote-conflict questions; the Ubuntu Pro gate; the snippet-registry overwrite confirmation and the credentials it withholds; an `/etc/apt/apt.conf.d` file differing on both machines, the only group whose permanent answer reads `do not update on pc2 for good`; a repository whose only remaining users are packages apt installed automatically; and `--confirm-each-command` (test 3 of `02-UAT.md`).

The late collateral question needs a setup this runbook does not build: an install whose repository the same run writes, whose transaction also removes or moves a package pc2 protects. Its three answers, the withdrawn install that is neither applied nor failed, and the log line naming the repository left behind go with it. Record all of the above as not exercised rather than improvising a trigger.
