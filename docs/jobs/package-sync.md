# Package Sync

The package jobs replicate *what is installed* — apt packages plus the `/etc/apt` repository state they depend on, snaps, flatpaks, and the things no package manager can reproduce — rather than user data. Package *data* (`~/.var/app`, `~/snap/<app>/common`, dotfiles) stays `folder_sync`'s territory; these jobs are about the presence, version and provenance of the packages themselves.

Configuration for these jobs is limited to their `sync_jobs` enable flags; see the [configuration reference](../configuration.md#sync_jobs). There are no per-job config keys.

## The four jobs

Four independent jobs share one item -> diff -> review -> converge model. Each has its own enable flag, its own validation, its own review and its own failure isolation, so enabling one never drags in another.

```yaml
sync_jobs:
  apt_sync: false             # apt packages plus the /etc/apt repository state they depend on
  snap_sync: false            # installed snaps, converged to the source's revision and channel
  flatpak_sync: false         # installed flatpak refs and their remotes, per scope
  manual_installs_sync: false # things no package manager can reproduce, plus the install-snippet registry
```

All four ship **disabled**: enabling any of them lets pc-switcher change installed software on the target, so it is opt-in. The three package-manager jobs run **before** `folder_sync`, so applications exist on the target before their data lands on top of them — decisive for flatpak, where `flatpak install` must create `~/.local/share/flatpak` before `folder_sync` would otherwise place `~/.var/app` content there.

### What each job covers

- **`apt_sync`** — the manually-installed apt package set (`apt-mark showmanual`, not the full dpkg selection — apt resolves dependencies on the target itself), plus the repository state that governs where packages come from: sources under `/etc/apt/sources.list.d`, signing keys (`/etc/apt/keyrings`, legacy `/etc/apt/trusted.gpg.d`), pins (`/etc/apt/preferences.d`) and apt config (`/etc/apt/apt.conf.d`).
- **`snap_sync`** — installed snaps, converged to the source's exact revision and tracking channel, never held or otherwise blocked from auto-refresh.
- **`flatpak_sync`** — installed flatpak refs and their remotes, per user/system installation scope.
- **`manual_installs_sync`** — everything no package manager can reproduce: apt packages with no repository candidate, plus unowned files under `/usr/local` and `/opt`. It also owns the install-snippet registry. It runs its own `dpkg-query` and `apt-cache policy` queries rather than sharing `apt_sync`'s, and carries its own enable flag, so disabling `apt_sync` never silently disables manual-install detection.

## The per-manager batched review

Each enabled package job shows its **own** batched review before it applies anything, and only that job's items appear in it — there is no single review spanning every manager, and nothing sits between the jobs merging their reviews into one. If you enable all four, each job presents its own review as it runs, and each applies nothing until its own review returns.

The review appears before any change that job makes and shows every difference it found, grouped by action. Installs and removals are always separate groups: a group proposing to remove software is never mixed into a group proposing to install it, and its title names the removal explicitly (for example "Remove packages"), never the word "apply". Removal entries start **unticked**, so a bulk tick can never silently delete something. Every item offers a three-way choice: apply it, skip it for this run only, or skip it always (making it machine-specific — see below).

### apt collateral

apt may need to remove or change packages other than the one you named in order to satisfy a request, so the item you ticked is not always the whole transaction apt would run. `apt_sync` simulates every approved change with `apt-get -s` at plan time and classifies the collateral before anything is refused. Collateral that is **auto-installed** — a dependency apt itself pulled in, absent from the target's `apt-mark showmanual` set — is apt doing its job and proceeds without asking. Collateral that is **manually installed** — a package the user chose to have — becomes its own reviewable item offering install-anyway, skip or abort. The classification is decided in the review, never mid-apply: a prompt during apply would reintroduce exactly the prompt-flooding the batched review exists to prevent.

## Machine-specific packages

Some packages belong to one machine only — a hardware driver, a vendor tool tied to a peripheral. Marking a package **machine-specific** (choosing "skip always" in the review) is the way to tell pc-switcher: *don't touch that package on that machine*. This is deliberately not called an "exclusion" — the file records what belongs to this machine, not what to hide from it.

Marking an item machine-specific writes an entry to that machine's own decision file at `~/.config/pc-switcher/<manager>.decisions.yaml` (one file per manager, for example `apt.decisions.yaml`, `snap.decisions.yaml`, `flatpak.decisions.yaml`). This file is **never synced** — it stays local to the machine it describes, in both roles: the marked item is never pushed from this machine when it is the source, and never installed or removed on this machine when it is the target. An annotated example lives at [`src/pcswitcher/machine-packages.example.yaml`](../../src/pcswitcher/machine-packages.example.yaml).

To un-mark something, delete its entry from the decision file (or delete the whole file to clear every machine-specific decision for that manager). The next sync treats the item as live again.

## Install snippets

Some installed things no package manager can reproduce — a bare `.deb` someone downloaded and installed by hand, or a manual install under `/usr/local` or `/opt`. `manual_installs_sync` detects these (apt packages with no repository candidate, and files under `/usr/local`/`/opt` that no package owns) and surfaces them in its review as items needing a resolution. For each one, the review offers three choices: add an install snippet, mark it machine-specific (see above), or skip for now.

An install snippet is a shell command that reproduces the item — the tool never parses, interprets, or reasons about it. It is **stored and replayed verbatim**, and it must run **non-interactively**: no stdin is supplied during replay, so a command that prompts (for example a debconf question) hangs the sync rather than failing cleanly. A typical shape:

```bash
sudo DEBIAN_FRONTEND=noninteractive dpkg -i /path/to/package.deb || \
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -f
```

The snippet registry lives at `~/.config/pc-switcher/package-snippets.yaml`. Unlike the machine-local decision files, it **does** reach the target: how to install something is knowledge about the *package*, not about the machine, so a snippet authored on one machine should be usable to reproduce the same item on any peer. `manual_installs_sync` pushes the registry to the target **itself** with `send_file()`, immediately after its own review — so a snippet you add on the fly during that review reaches the target in the same run. It does not ride `config_sync` (which runs before any review, and so could not carry a snippet you have not authored yet), and it does not rely on `folder_sync` (a user-controlled job that can be disabled or filtered — no job's correctness may depend on another job running).

## Resolving unreproducible items

An unreproducible item ends a run resolved in one of three ways: it gets a snippet, it is marked machine-specific (skip-always), or you skip it once. Skip-once is a real decision, not an unresolved state — you may be declining something temporary, and a run where you made that choice is clean. The intended workflow for skip-once is that you install the thing yourself on the target after the sync finishes, so the next sync shows no difference at all. Only an item nobody decided on leaves a run visibly unclean, and that happens only on a non-interactive run, where you were never given the chance to resolve it (see below).

## Versions

pc-switcher installs packages **by name** and lets versions float to whatever each machine's own repositories currently offer — it never pins the source's exact version. A version difference between source and target is detected and reported in the review, never silently forced. Deliberate version pinning does replicate, because `/etc/apt/preferences.d` pin files are synced as items like any other apt state.

## Non-interactive runs

A sync run without a TTY (for example from a script or CI) changes **nothing**: every item that needed a decision is treated as skipped for this run only, nothing is recorded permanently (no machine-specific marks, no snippets), and everything left unresolved is reported. A non-interactive run does not fail on undecided items alone, because you were never given the chance to resolve them. Re-run interactively to actually apply or resolve anything.

## Prerequisites: passwordless sudo

Each enabled package job needs passwordless sudo for a handful of binaries — `apt_sync` on both source (to read `/etc/apt` state) and target (to install packages and write `/etc/apt` state); `snap_sync` and `flatpak_sync` on the target only, and only when the diff involves a system-scope item; `manual_installs_sync` on the target to replay snippets. `validate()` checks this before anything runs and, on failure, prints the exact `visudo` command and sudoers line to add. The binaries it names are a **lower bound** on what must be permitted, not an exact scope to lock the grant down to — a broader existing grant is fine.
