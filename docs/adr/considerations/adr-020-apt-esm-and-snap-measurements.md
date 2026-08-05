# apt config files, pin epochs, ESM and snap identity, measured

The third-party-tool behaviour ADR-020's apt, ESM and snap decisions rest on, measured rather than reasoned about. The flatpak filter and trust premises are in [adr-020-flatpak-filter-and-trust-measurements.md](adr-020-flatpak-filter-and-trust-measurements.md).

## Which files apt reads in its three fragment directories (D-13)

Measured against apt 2.8.3, one filename at a time. `sources.list.d` is a directory users and packagers also drop `.save`, `.distUpgrade` and editor backups into; `preferences.d` and `apt.conf.d` get the same treatment from `dpkg`'s own `.dpkg-dist` files. apt reads only `.sources`/`.list` under `sources.list.d`, no extension or `.pref` under `preferences.d`, no extension or `.conf` under `apt.conf.d`, and in all three only names apt's own character rule accepts. Treating any other file as configuration would propose a change apt would never act on.

## A pin, not just a repository, decides which origin wins (D-36)

Measured on the development machine. Ubuntu's archive offers `firefox` at `1:1snap1-0ubuntu5` at priority 500. That version carries **epoch 1**; Mozilla's own `firefox` deb carries no epoch, and under equal priority apt takes the highest version, where any epoch-1 version outranks every epoch-0 version regardless of the upstream number. Adding the vendor's repository alone therefore still installs Ubuntu's package. Only the vendor's `preferences.d` pin, at priority 1000, changes the outcome — so a design in which repositories travel and pins do not would replicate the repository and still install the wrong package.

## An unattached target fails on ESM at install time, not refresh time (D-38)

Measured in a stock `ubuntu:24.04` container carrying both real ESM source files copied from a Pro-attached host. `esm.ubuntu.com` serves its repository *index* publicly — HTTP 200 on `.../dists/noble-apps-security/InRelease` — so the suites are fetched, marked `Trusted: yes`, and enter candidate selection at priority 500, above `noble/universe`. Only the *pool* is 401. The failure therefore lands at install time: `apt-get install 7zip` exits 100 with `401 Unauthorized` on the `.deb`. That container had 0 of 13 upgradable packages with an ESM candidate, which its tiny package set explains.

Measured since on a Pro-attached 24.04 desktop: 60 of 2297 installed packages resolve their candidate to `esm.ubuntu.com` — `ffmpeg`, `gimp`, `imagemagick`, `7zip` and the `libav*` set among them — so roughly one installed package in forty is exposed.

An unattached target's `apt-get update` does **not** fail and does not roll the transactional `/etc/apt` group back — measured in the same container: it exits 0 with the ESM sources present and no credentials. A source that genuinely fails does not abort the others either: with the ESM keyrings removed the run exits 100 with `E: The repository ... is not signed.` and still fetches and writes all 19 other lists, and against a synthetic index-level 401 it exits 100 and writes all 27 others — a non-zero exit is an aggregate signal, never an abort, and triggers no rollback. The missing-keyring case cannot arise in a real sync: `/usr/share/keyrings` is one of the three key directories `apt_sync` captures, so `ubuntu-pro-esm-apps.gpg` travels with the source file.

The two pins `ubuntu-pro-client` ships are conffiles present on every Ubuntu regardless of attachment, so they are identical on both machines and are not what travels — measured.

Detection is `pro status --format json` on the target — exit 0 for an unprivileged user, top-level `attached: true|false`, measured. Its payload also carries the subscriber's account; only the parsed boolean may be logged or shown.

## apt holds block install, upgrade and removal alike (D-04, D-05)

Measured on Ubuntu 24.04: an apt hold blocks install, upgrade and removal alike, so it carries the intent "do not move this off the version that works" as well as "do not lose this", and apt cannot distinguish them. This is why a held package the target lacks takes the source's exact version rather than a floated one.

## snap resolves one name to one publisher (D-42)

Measured. name→publisher is pinned store-side by a canonical-signed `snap-declaration` assertion snapd validates itself: one name resolves to one snap-id resolves to one publisher, so there is no second `firefox` for the target to install by accident. snapd auto-refreshes in the background (~4×/day, even for closed apps), which is what a run's sync-window refresh pause guards against. A sideloaded snap (installed from a local `.snap`) carries an `x`-prefixed revision (`x1`, `x2`) no store can serve.
