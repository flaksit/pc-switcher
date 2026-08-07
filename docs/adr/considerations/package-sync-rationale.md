# Package sync — rationale

Why the counter-intuitive articles in [`docs/system/package-sync.md`](../../system/package-sync.md) are the way they are. Keyed by article ID.

Articles obvious from their own text — "MUST ship disabled: enabling authorises install and removal" — are not here. This file exists for behaviour that a future reader (human or agent) might otherwise "simplify" away.

## `PKG-FR-BLOCKS-DERIVED`

A block replicates because the software it applies to does, and never gets its own review row. Costs nothing, cannot get a per-item derivation wrong.

Asking about it separately puts a question to the user whose two answers can contradict the answer they gave about the software itself: yes-install-this and no-do-not-freeze-it is not a coherent state, and neither is its inverse.

No machine-specific mark either. A block is not something anyone holds as a preference about one machine, and a mark on one leaves the two machines' records disagreeing about software neither would raise again. The mark that reaches the block is the one given about the software, which is the thing the user was actually asked about.

## `PKG-FR-MARK-SIDE`: why "both" is a real answer

A mark is dropped once the machine holding it no longer has the item (`PKG-FR-MARK-LIFETIME`). Recording one on each machine is the only answer that survives either machine losing its copy — a hedge that outlives either half of the fleet.

Recording on the target by default, before this question existed, silently made where the mark lives depend on which machine the run happened to be launched from.

## `PKG-FR-APT-TIMER-PAUSE`

Four constraints compose:

1. `PKG-FR-APT-DPKG-LOCK` refuses a run whose lock is already held, but nothing stops the updater STARTING once the run is under way. An update landing mid-convergence collides with the transaction `apt_sync` is applying.
2. Unlike snapd's `refresh.hold` (a timestamp that expires on its own), `systemctl stop` on a timer stays stopped forever. Leaving a user's machine without security updates is worse than the collision being avoided.
3. So each stop is paired with a `systemd-run --on-active` unit that restarts the timer after six hours. A run that dies without cleanup cannot leave a machine's updates off.
4. But `apt-daily`/`apt-daily-upgrade` ship `Persistent=true` — a persistent timer whose window elapsed while inactive fires **immediately** on activation. Starting the timers at the head of a sync would provoke `apt-daily-upgrade` into the very run the suspension protects.

Hence the split: at suspend, `restart` the pending unit (re-arms `OnActiveSec`, changes no timer state) to push it past this run. At cleanup, start any unit still loaded — the sync is over, and an overdue upgrade is what the machine wanted.

## `PKG-FR-COLLATERAL-MANUAL`: why the third answer says "stop the sync"

"Abort" alone reads as abandoning the question. Stopping here ends the ENTIRE sync, not just the package job, and a user who cannot tell those apart cannot choose between them. The answer's sentence names its scope for that reason.

The protection is also not the machine-specific mark: nobody recorded a preference, the target's own package manager reports that a person asked for the package, and saying otherwise would describe a decision the user never made.

## `PKG-FR-HOLD-WITHOUT-PACKAGE`

An `apt-mark hold` on a package the machine does not have freezes nothing while refusing every later install of that name. It is bookkeeping garbage rather than state to replicate or protect. Should not exist, is rare, carries logic for it costs more than it is worth.

Reporting only the first machine found makes the user clear it, sync again, and only then learn of the second. The commands stay separate because each runs on its own machine. Snap and flatpak need no equivalent: snapd records a refresh hold only against an installed snap, and a flatpak mask is a rule about what may be installed rather than a freeze on an installed copy.

## `PKG-FR-ESM-GATE`

Writing ESM sources to an unattached target and only warning: rejected. The metadata refresh succeeds because ESM indexes are public, the ESM suites enter candidate selection above the ordinary archive, and the failure lands later at install time on a package nobody will connect to the sync.

Withholding the two ESM files silently: rejected. Pins always-sync (`PKG-FR-PIN-ALWAYS`), so the source's ESM pins reach a target without the sources they name, leaving a candidate selection matching neither machine.

pc-switcher cannot self-attach: attaching needs a subscription token from the Pro dashboard or an interactive browser flow, the source's credentials are root-only and not reusable for another machine, and holding a token would put a secret on a command line.

Measured on an attached Ubuntu 24.04 desktop with `esm-apps` and `esm-infra` enabled: 60 of 2297 installed packages resolve their candidate to `esm.ubuntu.com`, among them `ffmpeg`, `gimp`, `imagemagick` and the `libav*` set.

## `PKG-FR-FLATPAK-REMOTE-TRUST`: machine-level anchors

libostree verifies against `/usr/share/ostree/trusted.gpg.d` (the machine anchor directory) as well as `<installation>/repo/<remote>.trustedkeys.gpg` (the remote's own keyring). A remote can be verified while carrying no keyring of its own, resting entirely on a keyring some vendor package dropped in the anchor directory.

Replicating name, URL and `gpg-verify` alone hands the target a remote that refuses every install with `Can't check signature: public key not found`.

EVERY anchor travels rather than one picked as the remote's own: libostree merges the whole directory into a single verifier and accepts a signature any key in it validates, and records nowhere which key verified what. The trust a keyless verified remote rests on is that merged set.

The anchors land in the replicated remote's OWN keyring rather than machine-wide: the target's remote then trusts exactly what the source's remote trusted, and no other remote on the target gains anything.

## `PKG-FR-FLATPAK-FILTER`: why a self-contradicting filter ends the run

Measured on Flatpak 1.14.6: installing a ref its remote's filter denies exits 1 with `Nothing matches <id> in remote <remote>` and lands nothing. So a source that has installed what its own filter withholds is contradicting itself — same shape as `PKG-FR-HOLD-WITHOUT-PACKAGE`.

Why the check is `remote-ls` rather than reading the filter file: the filter's matching rules belong to flatpak, and reimplementing them here would drift from them unannounced the moment flatpak changed one. `remote-ls` applies the remote's filter naturally.

Why the abort's message names both repairs (fix the filter, or uninstall the app) instead of picking: `remote-ls` names no cause for a missing application and has no unfiltered counterpart to compare against, so "the filter denies it" is not a fact this tool can establish. "The remote does not offer it under that filter" is — and it is equally fatal to replicating the two together.

## `PKG-FR-MANUAL-VERSION`: why versions do not float here

For apt and flatpak, `PKG-FR-VERSION-FLOAT` rests on a repository eventually moving the target onto the version the source has. Nothing will ever do that for a `.deb` you downloaded, a snap you sideloaded or a tarball unpacked into `/opt`. Reporting the difference and waiting would leave the machine on that build for good.

The version and nothing else: the guarantee is then exactly the one a package manager gives — equal version means converged — and the accepted cost is that a corrupted or half-applied item at an unchanged version is invisible (`PKG-NG-MANUAL-CONTENT`).

Version-first, before looking at the bodies: a snippet edited to change a comment or a mirror URL moves no version, and raising a review item for it would ask the user about software that has not changed.

No machine-specific mark: `PKG-FR-NO-MARK-ON-SNAP-REVISION` one ecosystem over — nobody holds a version as a standing preference about one machine, and a mark would leave the two machines' records disagreeing about software neither would raise again.

## `PKG-FR-VERSION-SNIPPET`: both bodies mandatory, no fallback

An entry whose version is guessed states something about what is installed that nobody established. One whose version is defaulted to "unknown" silently converges on presence again — the behaviour the second body exists to replace.

Why the version snippet is not gated by `--confirm-each-command`: it runs on every sync, on both machines, before the run has proposed anything. A confirmation would put a question per item per machine in front of the user before they had been shown a single change. The obligation moves to the author instead, and the editor screen says so.

## `PKG-FR-MANUAL-REMOVE`: no uninstall snippet

Three of the four jobs have a manager whose own removal is exact (`apt-get remove`, `snap remove`, `flatpak uninstall`). The fourth has a path (`rm -rf`).

A second authored body would buy the one direction the user can always carry out by hand, at the price of an entry nobody can complete — every existing entry becomes malformed under the new schema. The tradeoff was rejected: `PKG-NG-MANUAL-REMOVE-REACH` names the limit instead.

## `PKG-FR-REGISTRY-CONSENT`: abort, not fail

Aborting lets the user consolidate the two registries by hand; the alternative silently drops the target's snippets. An unreadable file says nothing about which entries exist, so the comparison cannot be made at all — and the reading that costs least to implement, "no snippets", is exactly the one that makes a wholesale push discard every entry nobody could see.

Ending the run rather than failing the job puts the repair in front of the user before anything else in the sync depends on it. A `manual_installs_sync` FAILED report leaves a run looking otherwise-successful, which the user reads as "green" rather than "go fix this file".

## `PKG-NG-MANUAL-CONTENT`

The alternative to trusting the version string is a recursive comparison or a payload hash of an arbitrary tree on both machines, on every sync, to buy a guarantee no package manager gives about the software it manages. Cost-benefit lands on the string.

The concrete failure mode this accepts: a half-unpacked archive, a truncated download, a manual `rm` of one file — the version snippet still reports `2.1.0`, no run notices, and no repair is proposed. Invisible by design.

## `PKG-NG-UNATTENDED`

A standing answer decides runs the user has forgotten they configured; a per-run option decides the run they just typed. No `pcswitcher.yaml` key pre-answers a review, and no assume-yes flag exists. `--yes` is unrelated — it belongs to `config_sync`.

`--apply-package-installs`/`--apply-package-removals` were added for the one case the user is actually asking for: converging their own fleet unattended overnight. The scope guards (`PKG-FR-APPLY-FLAGS-SCOPE`, `PKG-FR-APPLY-FLAGS-NO-MARK`) keep them from answering anything only a person can decide.
