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
ssh testuser@"$PC1" 'mkdir -p ~/.config/pc-switcher && cat > ~/.config/pc-switcher/config.yaml' <<'EOF'
logging:
  file: DEBUG
sync_jobs:
  apt_sync: true
  snap_sync: true
  flatpak_sync: true
  manual_installs_sync: true
EOF
```

## 2. Diverge the two machines

```bash
ssh -t testuser@"$PC1"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y sl cmatrix tree
sudo apt-mark hold sl
apt-cache policy sl | head -2                    # note pc1's installed version of sl
sudo mkdir -p /opt/pcsw-uat-app && echo hi | sudo tee /opt/pcsw-uat-app/README >/dev/null
printf 'allow *\n' > /home/testuser/uat.filter
flatpak remote-modify --user --filter=/home/testuser/uat.filter flathub
flatpak remotes --user --columns=name,filter     # flathub carries /home/testuser/uat.filter
exit
```

On pc2, recording `fortunes` as pc2's own makes it invisible to the review and protected as collateral, and the two repository files pc1 does not have are one nothing installs from and one that feeds everything pc2 has:

```bash
ssh -t testuser@"$PC2"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y fortunes cowsay tree
sudo apt-mark manual fortunes-min
apt-get -s remove -y fortunes-min | grep '^Remv'    # must list fortunes-min AND fortunes
mkdir -p ~/.config/pc-switcher
cat > ~/.config/pc-switcher/apt.decisions.yaml <<'EOF'
machine_specific:
  "apt:package:fortunes":
    item_class: apt_package
    label: fortunes
    reason: null
    recorded_at: "2026-07-30T00:00:00+00:00"
EOF
sudo tee /etc/apt/sources.list.d/99-pcsw-uat.sources >/dev/null <<'EOF'
Enabled: no
Types: deb
URIs: https://uat:s3cret@packages.example.com/apt
Suites: noble
Components: main
EOF
sudo cp /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/99-pcsw-inuse.sources
sudo sed -i 's/^Types:/Enabled: no\nTypes:/' /etc/apt/sources.list.d/99-pcsw-inuse.sources
sudo snap remove hello-world
cd /tmp && sudo snap download hello --basename=uat-hello && sudo snap install --dangerous /tmp/uat-hello.snap
flatpak remote-add --user --if-not-exists pcsw-uat https://dl.flathub.org/beta-repo/flathub-beta.flatpakrepo
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

Add `--allow-out-of-order` if the topology check asks. The dry run shows every question and converges nothing; walk it first, then answer the real run so every outcome is provable: `cmatrix` `<x>` and `sl` left at install; the `cowsay` removal `<x>` and the `fortunes-min` removal `<y>`; the collateral question "go ahead"; the repository deletion `<s>`; write `sudo mkdir -p /opt/pcsw-uat-app` in the snippet editor. Use a shifted key on one multi-item group first, to confirm it sets every line, then correct the lines you did not mean.

## 4. What to check while answering

- Each group is one question: every item on a line, the answer it carries in a column to the right, arrows moving between lines, `<space>` cycling the focused line, `<enter>` confirming. Nothing is asked a second time.
- The legend's permanent answer reads `keep on pc2 for good; it is pc2's own, and will not be asked again` on a removal, `do not install on pc2 for good; it is pc1's own, and will not be asked again` on an install, and `do not update on pc2 for good; it is pc2's own, …` on a change.
- The report group is titled `Version differences (apt packages)`, is the same widget with `<x>` missing from the legend, and notes that `sudo apt update && sudo apt upgrade` on pc2 converges it.
- `Delete repositories pc1 no longer has (apt)` comes one file at a time and offers `99-pcsw-uat.sources` only, its detail naming `https://***@packages.example.com/apt` — the password appears nowhere. `99-pcsw-inuse.sources` is not asked about at all.
- The collateral question names `fortunes` and says it is a package marked as pc2's own; its third answer stops the whole sync.
- `/opt/pcsw-uat-app` is asked about on its own with three answers, then the editor opens saying `(Ctrl-D to finish)`. Submit an empty body, then one of only spaces: each must be refused and the three answers offered again.
- Ctrl-C on any group ends the whole sync with `package review aborted at '<that group's title>' (Ctrl-C)`, exit code 1, and a terminal that still works.

## 5. What to check afterwards

On pc1:

```bash
grep -c cmatrix ~/.config/pc-switcher/apt.decisions.yaml   # 1 — an install is recorded here
grep -c cowsay ~/.config/pc-switcher/apt.decisions.yaml    # 0
LOG=$(ls -t ~/.local/share/pc-switcher/logs/sync-*.log | head -1)
grep 'reviewed cowsay' "$LOG"     # names the item and "marked as this machine's own"
grep 'sl=' "$LOG"                 # the held package installed at pc1's exact version
grep 'keeping repository 99-pcsw-inuse.sources' "$LOG"
grep 'Ignoring snap' "$LOG"       # names hello on pc2
grep -c s3cret "$LOG"             # 0
grep -c 'stdout: ' "$LOG"         # apt's, snap's and flatpak's own output, verbatim
```

On pc2:

```bash
grep -c cowsay ~/.config/pc-switcher/apt.decisions.yaml    # 1 — a removal is recorded here
grep -c cmatrix ~/.config/pc-switcher/apt.decisions.yaml   # 0
dpkg-query -W -f='${Package} ${Version} ${Status}\n' sl cmatrix fortunes fortunes-min
snap list hello                                  # still the sideloaded revision, untouched
flatpak remotes --user --columns=name,filter     # flathub carries the filter, pcsw-uat is gone
cat /home/testuser/uat.filter
ls /etc/apt/sources.list.d/ /opt/pcsw-uat-app
```

`sl` is installed at pc1's version and `cmatrix` is absent; `fortunes` and `fortunes-min` are both gone, the collateral answer having taken them; both `.sources` files are still there; `/opt/pcsw-uat-app` exists, the snippet having replayed in the run that authored it.

## 6. One more run: a dead package-manager read

```bash
ssh testuser@"$PC2" 'sudo chmod -x /usr/bin/flatpak'
ssh -t testuser@"$PC1" 'pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order'
ssh testuser@"$PC2" 'sudo chmod +x /usr/bin/flatpak'
```

flatpak_sync reports FAILED naming the command that did not answer, and apt_sync, snap_sync and manual_installs_sync still run.

## 7. Cleanup

```bash
tests/integration/scripts/reset-vm.sh pc1
tests/integration/scripts/reset-vm.sh pc2
tests/integration/scripts/internal/lock.sh release "janfr-uat-02-01"
```

## 8. Not exercised here

Pin deletion, the repository-conflict and flatpak remote-conflict questions, the Ubuntu Pro gate, the snippet-registry overwrite confirmation, and `--confirm-each-command` (test 3 of `02-UAT.md`). Record them as not exercised rather than improvising a trigger.
