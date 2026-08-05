#!/usr/bin/env bash
# Put the machine's package managers into the state the integration suite requires: the
# subjects it operates on present, and nothing else touching them behind its back.
#
# The suite proves things about apt, snap and flatpak convergence, so both machines must
# actually own a package per manager the tests may hold, diverge, remove and reinstall.
# A stock Ubuntu 24.04 VM owns none: `snap list` shows only snapd/core*/bare (all of which
# every other snap depends on, so none is a safe subject), flatpak is not installed at all,
# and every apt package it carries is one the machine or pc-switcher itself needs. This
# script creates those subjects.
#
# It also removes the machine's automatic apt updater, which is not a subject but a rival
# for the same locks (see remove_automatic_updates).
#
# WITHOUT --with-app (the sync TARGET, pc2), the machine ends up with:
#   - apt: snapd and flatpak installed, plus the five subject packages cmatrix, figlet,
#     nyancat, rolldice and sysvbanner, installed and marked manual;
#   - snaps: `hello` and `hello-world`, from the stable channel, system scope;
#   - flatpak remote `flathub`, user scope, added from Flathub's own `.flatpakrepo`;
#   - flatpak runtime org.freedesktop.Platform/x86_64/25.08 plus its related refs, user
#     scope;
#   - NO `flathub-beta` remote and NO subject application — both are actively REMOVED if
#     present, not merely skipped, so a crashed test run cannot leave them behind.
#
# WITH --with-app (the sync SOURCE, pc1), the machine ends up with everything above, and
# additionally:
#   - flatpak remote `flathub-beta`, user scope, from which nothing is ever installed;
#   - the subject application io.github.fragglet.sdl_sopwith, user scope, from `flathub`.
#
# The asymmetry IS the fixture: it hands the suite a genuine source->target ref
# divergence, and a genuine source-only remote that feeds no ref, without any test having
# to manufacture them.
#
# Runs ON a test VM as the test user (passwordless sudo assumed), from two callers:
#   - provision-test-infra.sh, before the baseline btrfs snapshot is taken, so the
#     fixtures live in the baseline and cost nothing per test run;
#   - tests/integration/conftest.py, which re-runs it before the package-sync module so
#     the suite works against a VM whose baseline predates this script.
#
# Idempotent and cheap on the satisfied path: every step is guarded by a presence check,
# so a re-run over a baseline that already has everything is a handful of local queries.
#
# Usage: ssh testuser@vm 'bash -s -- [--with-app]' < vm-test-fixtures.sh
#        (or: send the file and run `bash vm-test-fixtures.sh [--with-app]`)
set -euo pipefail

# Bumping this forces provisioning to rebuild the baseline: provision-test-infra.sh and
# run-integration-tests.sh compare the marker file's contents against their own copy of
# this number (PCSWITCHER_TEST_FIXTURES_VERSION in internal/common.sh — keep in sync).
readonly FIXTURES_VERSION=6
readonly MARKER=/etc/pcswitcher-test-fixtures

INSTALL_APP=false
for arg in "$@"; do
    case "$arg" in
        --with-app) INSTALL_APP=true ;;
        *)
            echo "vm-test-fixtures.sh: unknown argument '$arg' (only --with-app is accepted)" >&2
            exit 2
            ;;
    esac
done
readonly INSTALL_APP

# Two snaps, because `test_system_refresh_hold_does_not_mask_a_per_snap_held_note` needs
# one snap held and a second one provably NOT held in the same `snap list` output.
#
# `hello` carries distinct revisions across its channels (stable/beta/edge), which is
# what lets the revision-divergence test move the target off the source's revision and
# watch the sync bring it back. `hello-world` is the canonical 20 kB demo snap. Both are
# strictly confined, contain no daemon, and are safe to hold, unhold, remove and
# reinstall; neither is a base snap anything else depends on.
readonly -a FIXTURE_SNAPS=(hello hello-world)

# The apt subjects, on BOTH machines: three the suite removes from the target so a run has
# installs to converge, one it removes from the source so a run has a removal to converge,
# and one both machines carry at the same version for the hold scenarios. Which is which is
# pinned in FIXTURE_APT_SUBJECTS (tests/integration/jobs/package_sync_scenario.py); this
# script only has to put all five on the machine.
#
# Dedicated packages rather than borrowed ones, because apt's own reverse-dependency check
# has no idea pc-switcher exists: removing `btrfs-progs` succeeds cleanly and a LATER test's
# sync then reports `btrfs: command not found`. Each of these is a few dozen kB, has no
# reverse dependency on these VMs, pulls in no dependency the baseline lacks (measured with
# `apt-get --dry-run install`), and is needed by nothing pc-switcher does.
readonly -a FIXTURE_APT_PACKAGES=(cmatrix figlet nyancat rolldice sysvbanner)

# THE REAL FLATHUB, deliberately, not a local stand-in. A synthetic signed OSTree
# repository is cheap (~270 kB against Flathub's ~270 MB runtime) but it only ever tests
# this project's MODEL of a flatpak remote: our own repo layout, our own key, our own
# `gpg-verify` state. The remote under test has to carry Flathub's real trust
# configuration for `flatpak_sync`'s GPG-trust replication (#215) to be proven rather
# than assumed — a `flatpak remotes --columns=options` field that is genuinely empty, a
# real `flathub.trustedkeys.gpg` in the installation's repo, and a real `--gpg-import`
# round trip. The baseline download is paid ONCE, when the baseline btrfs snapshot is
# built, and never per test run.
readonly FLATPAK_REMOTE=flathub
readonly FLATHUB_REPOFILE=https://dl.flathub.org/repo/flathub.flatpakrepo

# A SECOND real remote, on the source only and feeding nothing. It is what makes the
# derivation claim falsifiable: a remote the source has and no synced ref comes from must
# not travel, and with only one remote in the baseline "the target ends up with the
# source's remotes" and "the target ends up with the remotes its refs need" are
# indistinguishable. Nothing is installed from it, so it costs one `.flatpakrepo` fetch
# and no download at all.
#
# Both Flathub keyrings have the same sha256 (measured), so no assertion anywhere may key
# a remote's identity on its key digest — only on its name and URL.
readonly FLATPAK_UNUSED_REMOTE=flathub-beta
readonly FLATHUB_BETA_REPOFILE=https://dl.flathub.org/beta-repo/flathub-beta.flatpakrepo

# The subject application, and the runtime it declares. Measured live against Flathub
# (flatpak 1.14.6, x86_64):
#   io.github.fragglet.sdl_sopwith  146 kB download / 448 kB installed, one `stable`
#                                   branch only, so `flatpak install flathub <id>` with
#                                   no branch is unambiguous;
#   org.freedesktop.Platform/25.08  pulled together with its related refs (GL.default and
#                                   its -extra, GL.nvidia-*, Locale, VAAPI.nvidia,
#                                   codecs-extra) — 95 s and 2.8 GB on disk.
# The runtime is why it is installed HERE rather than left to the test: with it already
# present, installing the app itself takes under a second, so the sync under test pays no
# download at all. Installing the runtime with `--no-related` does not help — the app
# install then pulls those same related refs (measured: +72 s, +2.2 GB), so the runtime
# has to be seeded exactly the way an app install would seed it.
readonly FLATPAK_APP=io.github.fragglet.sdl_sopwith
readonly FLATPAK_RUNTIME_REF=org.freedesktop.Platform/x86_64/25.08

# Machines provisioned before FIXTURES_VERSION=3 carry the synthetic repository this
# script used to build, its system-wide trust anchor (pre-#215) and the refs installed
# from it. The version bump drops all of it, so nothing that used to make the flatpak
# test pass can still be lying around.
readonly LEGACY_FLATPAK_ROOT=/opt/pcswitcher-test-flatpak
readonly LEGACY_OSTREE_TRUSTED_KEY=/usr/share/ostree/trusted.gpg.d/pcswitcher-test.gpg
readonly LEGACY_FLATPAK_REMOTE=pcswitcher-test
readonly LEGACY_FLATPAK_APP=org.pcswitcher.TestApp
readonly LEGACY_FLATPAK_RUNTIME=org.pcswitcher.TestRuntime

log() { echo "[vm-test-fixtures] $*"; }

# -- the machine's own updater -------------------------------------------------------

# Ubuntu patches itself in the background, and the suite cannot tell that apart from a
# sync misbehaving: `apt_sync.validate` probes the target's dpkg frontend lock once and
# ends the whole run when it is held, so a test scheduled into the post-boot window fails
# on package state that never had a chance to change. reset-vm.sh reboots into the
# baseline before every run and the updater fires minutes later, so which test pays is
# decided by pytest-randomly's seed rather than by anything under test (#249).
#
# Patching is not lost with it: upgrade-vms.sh applies updates explicitly and rebuilds the
# baseline, daily, from .github/workflows/vm-updates.yml.
remove_automatic_updates() {
    # Timers first, so the purge below cannot lose the dpkg lock to the very updater it is
    # removing. `disable --now` settles the current boot, `mask` every later one — and the
    # .service units too, since the timer is not the only thing that can start them.
    sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
    sudo systemctl mask apt-daily.timer apt-daily-upgrade.timer \
        apt-daily.service apt-daily-upgrade.service >/dev/null

    # A run already in flight holds the lock the purge needs. Stopping the service waits
    # for it to finish rather than tearing down a dpkg transaction half-applied.
    sudo systemctl stop unattended-upgrades.service >/dev/null 2>&1 || true

    # Matched on the install status, not on the package being known: `dpkg-query --show`
    # succeeds for a package that is merely available, so it would report every already
    # purged machine as needing the purge again and make this step lie on every run.
    if dpkg-query -W -f='${Status}' unattended-upgrades 2>/dev/null | grep --quiet '^install ok installed'; then
        log "purging unattended-upgrades"
        sudo DEBIAN_FRONTEND=noninteractive apt-get purge --assume-yes unattended-upgrades
    fi

    # The purge takes the package's own conffiles, but 20auto-upgrades is written by the
    # installer rather than shipped by the package, so it outlives it — and it is the file
    # that would switch automatic updates back on if the package ever returned.
    sudo rm --force /etc/apt/apt.conf.d/20auto-upgrades

    # Masked while absent, too: a reinstall arriving as somebody's dependency must not
    # quietly resume updating.
    sudo systemctl mask unattended-upgrades.service >/dev/null
}

# -- apt ---------------------------------------------------------------------------

install_apt_subjects() {
    # Matched on the install status, not on the package being known: `dpkg-query --show`
    # succeeds for a package that is merely available in the archive.
    local missing=() name
    for name in "${FIXTURE_APT_PACKAGES[@]}"; do
        if ! dpkg-query -W -f='${Status}' "$name" 2>/dev/null | grep --quiet '^install ok installed'; then
            missing+=("$name")
        fi
    done
    if ((${#missing[@]} == 0)); then
        log "apt subjects already installed"
        return
    fi
    log "installing apt subjects: ${missing[*]}"
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes "${missing[@]}"
    # Explicit, so a subject that arrived as somebody else's dependency is still in the
    # manual set the sync under test compares.
    sudo apt-mark manual "${missing[@]}" >/dev/null
}

# -- snaps -------------------------------------------------------------------------

install_snaps() {
    if ! command -v snap >/dev/null 2>&1; then
        log "installing snapd"
        sudo apt-get update
        sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes snapd
    fi

    # An install issued while snapd is still seeding fails with "too early for
    # operation"; on a freshly created VM this script can easily win that race.
    sudo snap wait system seed.loaded

    local name
    for name in "${FIXTURE_SNAPS[@]}"; do
        if snap list "$name" >/dev/null 2>&1; then
            log "snap $name already installed"
            continue
        fi
        log "installing snap $name"
        sudo snap install "$name"
    done
}

# -- flatpak -----------------------------------------------------------------------

install_flatpak_packages() {
    if command -v flatpak >/dev/null 2>&1; then
        log "flatpak already installed"
        return
    fi
    log "installing apt package: flatpak"
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes flatpak
}

add_flathub_remote() {
    if flatpak remotes --user --columns=name | grep --quiet --line-regexp "$FLATPAK_REMOTE"; then
        log "flatpak remote $FLATPAK_REMOTE already configured"
        return
    fi
    # Added from Flathub's own `.flatpakrepo`, exactly as a user would: that is what
    # brings the real URL, the real `gpg-verify=true` and the real signing key into
    # `~/.local/share/flatpak/repo/flathub.trustedkeys.gpg`. Replicating it with
    # `--gpg-import` (what flatpak_sync does) reproduces that keyring byte-for-byte
    # (verified live), so the two machines never show a spurious trust divergence.
    log "adding user-scope flatpak remote $FLATPAK_REMOTE from $FLATHUB_REPOFILE"
    flatpak remote-add --user --if-not-exists "$FLATPAK_REMOTE" "$FLATHUB_REPOFILE"
}

converge_unused_remote() {
    local configured=false
    if flatpak remotes --user --columns=name | grep --quiet --line-regexp "$FLATPAK_UNUSED_REMOTE"; then
        configured=true
    fi

    if [[ "$INSTALL_APP" == "true" ]]; then
        if [[ "$configured" == "true" ]]; then
            log "flatpak remote $FLATPAK_UNUSED_REMOTE already configured"
            return
        fi
        log "adding user-scope flatpak remote $FLATPAK_UNUSED_REMOTE from $FLATHUB_BETA_REPOFILE (feeds nothing)"
        flatpak remote-add --user --if-not-exists "$FLATPAK_UNUSED_REMOTE" "$FLATHUB_BETA_REPOFILE"
        return
    fi

    # No --with-app: this machine is the sync TARGET and must NOT have it, or the test
    # that proves an unused remote does not travel proves nothing. Deleted rather than
    # merely not added, so a test that crashed mid-run cannot leave it behind.
    if [[ "$configured" == "true" ]]; then
        log "removing user-scope flatpak remote $FLATPAK_UNUSED_REMOTE (this machine is the sync target)"
        flatpak remote-delete --user --force "$FLATPAK_UNUSED_REMOTE"
    else
        log "flatpak remote $FLATPAK_UNUSED_REMOTE correctly absent"
    fi
}

# Whether `ref` is installed in the user installation.
flatpak_user_ref_installed() {
    flatpak list --user --columns=ref | grep --quiet --line-regexp "$1"
}

assert_app_runtime_unchanged() {
    # Flathub decides which runtime it builds the subject app against, and it moves the
    # app to a newer runtime major roughly once a year. FLATPAK_RUNTIME_REF is what this
    # script seeds, so a drift has to be caught here — loudly, with the fix — rather than
    # as an unexplained timeout when the sync under test suddenly has to download 2.8 GB.
    #
    # Tolerant of an unreachable Flathub (the query needs the network): only an answer
    # that actually disagrees is an error.
    local declared
    if ! declared=$(flatpak remote-info --user "$FLATPAK_REMOTE" "$FLATPAK_APP" --show-runtime 2>/dev/null); then
        log "WARNING: could not ask $FLATPAK_REMOTE which runtime $FLATPAK_APP needs; skipping the drift check"
        return
    fi
    declared="${declared//[[:space:]]/}"
    if [[ "$declared" != "$FLATPAK_RUNTIME_REF" ]]; then
        cat >&2 <<EOF
[vm-test-fixtures] Flathub now builds $FLATPAK_APP against $declared,
not the $FLATPAK_RUNTIME_REF this fixture seeds. Leaving it would make
test_flatpak_derives_the_remote_its_ref_needs_and_carries_its_key download a whole runtime inside the
sync it is timing. Fix, in tests/integration/scripts/internal/vm-test-fixtures.sh:
  1. set FLATPAK_RUNTIME_REF=$declared
  2. bump FIXTURES_VERSION (and PCSWITCHER_TEST_FIXTURES_VERSION in internal/common.sh)
     so the baseline is rebuilt with the new runtime
EOF
        exit 1
    fi
}

install_flatpak_runtime() {
    if flatpak_user_ref_installed "$FLATPAK_RUNTIME_REF"; then
        log "flatpak runtime $FLATPAK_RUNTIME_REF already installed"
        return
    fi
    # No --no-related: the related refs (GL, VAAPI, codecs, Locale) have to be seeded
    # here, or the app install inside the test pulls them instead.
    log "installing flatpak runtime $FLATPAK_RUNTIME_REF (a few hundred MB, once per baseline)"
    flatpak install --user --assumeyes --noninteractive "$FLATPAK_REMOTE" "$FLATPAK_RUNTIME_REF"
}

converge_flatpak_app() {
    local installed=false
    if flatpak list --user --app --columns=application | grep --quiet --line-regexp "$FLATPAK_APP"; then
        installed=true
    fi

    if [[ "$INSTALL_APP" == "true" ]]; then
        if [[ "$installed" == "true" ]]; then
            log "flatpak $FLATPAK_APP already installed"
            return
        fi
        log "installing user-scope flatpak $FLATPAK_APP"
        flatpak install --user --assumeyes --noninteractive "$FLATPAK_REMOTE" "$FLATPAK_APP"
        return
    fi

    # No --with-app: this machine is the sync TARGET and must not hold the app, or the
    # source->target ref divergence the flatpak test needs does not exist. Removed rather
    # than merely not installed, so a test that crashed mid-run cannot leave it behind.
    if [[ "$installed" == "true" ]]; then
        log "removing user-scope flatpak $FLATPAK_APP (this machine is the sync target)"
        flatpak uninstall --user --assumeyes "$FLATPAK_APP"
    else
        log "flatpak $FLATPAK_APP correctly absent"
    fi
}

# -- main --------------------------------------------------------------------------

# A marker from an older version means the fixtures on this machine are not the ones
# this script now describes. Drop the artifacts of the synthetic-repo era so nothing left
# over from it can satisfy a presence check or show up as a flatpak item the tests do not
# know about (snaps need no such reset — they are checked name by name, so a newly added
# one is installed and an existing one left alone).
#
# `flatpak uninstall` WITHOUT --unused, deliberately: --unused would sweep runtimes this
# script deliberately keeps installed.
if [[ -f "$MARKER" ]] && [[ "$(cat "$MARKER")" != "$FIXTURES_VERSION" ]]; then
    log "marker reports version $(cat "$MARKER"), rebuilding for version $FIXTURES_VERSION"
    flatpak uninstall --user --assumeyes "$LEGACY_FLATPAK_APP" >/dev/null 2>&1 || true
    flatpak uninstall --user --assumeyes "$LEGACY_FLATPAK_RUNTIME" >/dev/null 2>&1 || true
    flatpak remote-delete --user --force "$LEGACY_FLATPAK_REMOTE" >/dev/null 2>&1 || true
    sudo rm --recursive --force "$LEGACY_FLATPAK_ROOT" "$LEGACY_OSTREE_TRUSTED_KEY"
fi

remove_automatic_updates
install_apt_subjects
install_snaps
install_flatpak_packages
add_flathub_remote
converge_unused_remote
assert_app_runtime_unchanged
install_flatpak_runtime
converge_flatpak_app

printf '%s\n' "$FIXTURES_VERSION" | sudo tee "$MARKER" >/dev/null
log "test fixtures ready (version $FIXTURES_VERSION)"
