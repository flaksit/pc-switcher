# Package Sync

Package sync replicates *what software is installed* — packages, snaps, flatpaks, and the software none of them can reproduce. Application data belongs to [`folder_sync`](folder-sync.md).

For what package sync is for, read [package-sync-user-requirements.md](../planning/package-sync-user-requirements.md) first. This page tells you what to enable, what pc-switcher will ask you, and what the answers do.

## The seven jobs

Seven independent jobs share one review model. Each has its own enable flag, its own review, and its own failure isolation.

The flag is the config key; the second column is the name the run shows you on screen.

| Flag | Shown as | Covers |
| --- | --- | --- |
| `apt_sync` | Apt packages | Manually-installed apt packages, plus the `/etc/apt` state they depend on: repositories, pins, keys, apt config, holds |
| `snap_sync` | Snaps | Store snaps, converged to the source's exact revision and channel |
| `flatpak_sync` | Flatpaks | Flatpak refs (per user/system scope), the remotes they need, and mask patterns |
| `manual_deb_sync` | Manual debs | apt packages installed by hand from a downloaded `.deb` |
| `manual_snap_sync` | Sideloaded snaps | Sideloaded snaps installed from a local `.snap` file |
| `manual_flatpak_sync` | Manual flatpaks | Flatpak apps installed from a local bundle or a since-deleted remote |
| `manual_installs_sync` | Manually installed apps | Unowned software under `/usr/local` and `/opt` |

All seven ship **disabled**. Enable them individually:

```yaml
sync_jobs:
  apt_sync: true
  snap_sync: true
  flatpak_sync: true
  manual_deb_sync: true
  manual_snap_sync: true
  manual_flatpak_sync: true
  manual_installs_sync: true
```

Enabling one authorises pc-switcher to install and remove software on the target for that job — hence opt-in per job.

## The three-way ownership split

Three jobs handle software the store or archive can supply: `apt_sync`, `snap_sync`, `flatpak_sync`. Three others handle software of the same shape that no store or archive can supply: `manual_deb_sync`, `manual_snap_sync`, `manual_flatpak_sync`. **The two never overlap.** For every ecosystem, one detection rule assigns each package to exactly one job:

| If you disable | Then software of this kind is synced by nobody |
| --- | --- |
| `manual_deb_sync` | Every apt package installed from a downloaded `.deb` |
| `manual_snap_sync` | Every sideloaded snap |
| `manual_flatpak_sync` | Every flatpak app from a local bundle or since-deleted remote |

Keep the `manual_*` jobs enabled if you have any such software. `apt_sync`/`snap_sync`/`flatpak_sync` will not fill in — they cannot, because a package the store does not offer is not something the store can install.

`manual_installs_sync` is separate: unowned software under `/opt` and `/usr/local` belongs to no other job.

## Job ordering

All seven package jobs **must** be listed before `folder_sync` in your `sync_jobs`. pc-switcher validates this and refuses to start otherwise: software must exist before its data lands on top of it, or the fresh install's stock config overwrites your synced version.

## Batched review

Each enabled job shows you a review before it changes anything. Every screen names the two machines by hostname (`atlas`, `nomad`) — never "source" and "target".

Every question is titled `<Verb> <what> on nomad?`, or `from nomad?` where the change takes something off it: `Install apt packages on nomad?`, `Remove sideloaded snaps from nomad?`, `Align snap versions on nomad?`. A one-item screen names the item too — `Install manual deb pcsw-uat-deb on nomad?`. The reported-only groups are the exception: they ask nothing, so they are titled by the condition instead (`Version differences (apt packages)`).

Most items take three answers:

| Key | Answer | Effect |
| --- | --- | --- |
| `y` | Apply (`install`, `remove`, `overwrite`, …) | Make this change on the target |
| `s` | Skip now | Leave it this sync; ask again next sync |
| `x` | Skip for good | Mark it machine-specific; never asked again |

Four questions take **two** answers only (act, or leave it — no record either way):

- Deleting an apt repository or pin file
- Overwriting a repository file both machines have with different content
- Repointing a flatpak remote a machine-specific app depends on

A snap's revision/channel change also takes two answers: nobody keeps a revision as a standing preference per machine.

**Removal groups start at skip-now.** A bulk confirm can never silently delete something. So do groups that would overwrite an `apt.conf.d` file the target already holds.

Some questions come one-per-screen because they have to show you something first: a file body, a collateral package, an unreproducible item. Where both machines hold the file and their copies differ — an `apt.conf.d` file, a repository file — you see both versions whole, the target's first, because that is the one it would replace.

Some findings are only reported, not asked about: version differences, origin divergences, repositories the source cannot reproduce. Neither answering nor declining would change anything, so there is nothing to answer.

A question freezes the live display above itself, so you can see where the run is while you answer. A job that asks nothing — an empty review, or one holding only reported findings — does not: it prints what it found and the display keeps running. With seven package jobs enabled, only the ones that ask leave a frame behind.

## Machine-specific items

Choosing `x` marks the item as belonging to *this specific machine*. The mark is written on the **holding machine** (the one whose copy the answer keeps) and never synced.

Where both machines have the item and their copies differ, you get one follow-up screen asking whose own version this is — one machine, the other, or both. The keys are `s` for the source machine, `t` for the target and `b` for both, and the column shows the two hostnames. Naming both records one on each, so the answer survives either machine losing its copy. Both versions are printed again above that screen: by then the screen that showed them has scrolled away, and a filename is not something you can answer this from.

It is the one screen that starts on no answer at all. `<enter>` does nothing until every row has a key, and says which rows are still outstanding — neither machine is the holder by right, and the record it writes is permanent.

The mark lasts as long as the software is on the holding machine. Once it is gone, the mark is dropped and the run says so.

Marks live under `~/.config/pc-switcher/<manager>.decisions.yaml`. To un-mark, delete the entry (or the whole file).

## Install snippets for unreproducible items

The four `manual_*` jobs share one snippet registry at `~/.config/pc-switcher/package-snippets.yaml`. When one of these jobs finds an item, you have three choices:

- `y` — Write an install snippet. One editor opens for the install-or-update body; `manual_installs_sync` opens a second for an installed-version body.
- `s` — Skip now.
- `x` — Never install here.

The **installed-version snippet** belongs to `manual_installs_sync` alone: nothing else can say which version of an unowned path is installed, while the other three jobs ask `dpkg-query`, `snap list` and `flatpak list`. It runs on both machines every sync to detect drift, and must be read-only — pc-switcher cannot check that. Example: `/opt/foo/bin/foo --version`.

The **install-or-update snippet** runs on the target to install or update. Runs as the target user with no wrapping sudo — put `sudo` inside the snippet if you need it. Example:

```bash
sudo DEBIAN_FRONTEND=noninteractive dpkg --install /path/to/package.deb || \
sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --fix-broken
```

Every body an entry takes is stored verbatim and mandatory. An empty body is refused.

Convergence means the target reading back the source's version, not the snippet exiting zero. Where a replay leaves the version unchanged, you are asked again — this time to write a new snippet or leave it for this run. There is no purge-and-retry option: if your installer will not overwrite, `rm -rf … &&` goes into your own new snippet.

The registry syncs between machines with each snippet job's push. A transfer that would lose or change an entry the target holds asks you to confirm; declining aborts the run so you can consolidate the two registries by hand.

## Ubuntu Pro and ESM

If your source uses Ubuntu Pro's ESM repositories and your target is not attached, `apt_sync` asks before writing anything. Two answers: attach the target (pc-switcher gives you the `sudo pro attach <token>` and `sudo pro enable esm-apps esm-infra` commands to run there, plus a link to [Ubuntu's tutorial](https://documentation.ubuntu.com/pro/attach-tutorial/)) or skip Apt packages for this run. Every other job still runs.

Skipping costs the whole apt job, not just the two ESM files: pins always-sync, so the source's ESM pins would reach a target without the sources they name, leaving a candidate selection matching neither machine.

A non-interactive run takes the skip. pc-switcher cannot attach the target on your behalf.

## Prerequisites

Each enabled job needs passwordless sudo where it writes:

| Job | Source | Target |
| --- | --- | --- |
| `apt_sync` | required | required |
| `snap_sync` | required | required |
| `flatpak_sync` | none | required only when a system-scope item is in play on either machine |
| `manual_deb_sync` | none | none |
| `manual_snap_sync` | none | none |
| `manual_flatpak_sync` | none | none |
| `manual_installs_sync` | none | none |

The `manual_*` jobs pre-validate no sudo: detection only reads. An approved removal on any of the first three needs root on the target and fails as its own item if that is not available. Snippet privilege is the author's — put `sudo` inside the snippet if needed.

`apt_sync` and `snap_sync` also pause each machine's auto-update timers (apt) and auto-refresh (snapd) for the sync window, and restore them afterwards. The pauses expire on their own if the run dies.

## Auditing every command

`pc-switcher sync <hostname> --confirm-each-command` inserts one prompt before every operation that changes state on either machine — package installs, `/etc/apt` writes, snippet replays, decision-file writes, the snap refresh pause, the sync-history update. Read-only commands stay silent (a read under `sudo` is still a read).

Each prompt shows the command verbatim, the job, and the hostname. Answer `p` to proceed or `a` to abort the whole sync. There is no per-command skip: one reviewed item can span several commands.

The flag needs a real terminal and is meant for auditing runs you do not trust yet, not for everyday syncs.

## Converging unattended

Two flags answer a package review in advance so a run with nobody watching can still converge:

- `--apply-package-installs` — everything that ADDS software (installs, adds, enables, converges an item to the source's content)
- `--apply-package-removals` — everything that TAKES software AWAY (removes, deletes, disables, deletes repositories and pins, loses a protected package)

Pass both to replicate the source's whole state. Pass one to converge that direction only.

Four things neither flag answers, because none of them can be settled from the source's package list:

- A repository conflict where a machine-specific package depends on the file
- An `apt.conf.d` file the target already holds
- An unreproducible item (writing a snippet needs an editor)
- The Ubuntu Pro attachment question

Each is left as a run with nobody to ask leaves it: named, and skipped for this run.

A registry transfer that would lose an entry is the one exception — it still ends the run so you can consolidate by hand.

Neither flag records a machine-specific mark, and neither writes a snippet.

`--yes` is unrelated (it belongs to `config_sync`) and answers no package review.

## When something goes wrong

Every approved item is attempted. Failures are collected and reported together, each naming its item. One failed item does not block the rest of its job; one failed job does not stop the others.

A read that does not answer — snapd unreachable, apt lock held on the source, a network drop — fails the job once, naming the command and the tool's own stderr. It never proposes removing everything on the other machine because one read returned nothing.

## What the log keeps

The log is where you reconstruct why, months later:

- Every reviewed item with the decision it got, skipped items included
- Every applied change, one line per item (act, item, manager, machine)
- Every self-directed change by a package manager (dependencies apt resolves for itself)
- At `log.file: DEBUG`, every command's output verbatim

One class of content is withheld everywhere: a credential embedded in a URL is redacted in logs, prompts, review lines, file bodies shown for decisions, and snippet bodies shown for the registry-consent question. What is stored and replayed stays exactly as written.
