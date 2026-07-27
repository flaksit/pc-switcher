#!/usr/bin/env bash
# Create the package-manager subjects the integration suite operates on.
#
# The suite proves things about snap and flatpak convergence, so both machines must
# actually own a snap and a flatpak the tests may hold, diverge, remove and reinstall.
# A stock Ubuntu 24.04 VM owns neither: `snap list` shows only snapd/core*/bare (all of
# which every other snap depends on, so none is a safe subject) and flatpak is not
# installed at all. This script creates those subjects.
#
# Runs ON a test VM as the test user (passwordless sudo assumed), from two callers:
#   - provision-test-infra.sh, before the baseline btrfs snapshot is taken, so the
#     fixtures live in the baseline and cost nothing per test run;
#   - tests/integration/conftest.py, which re-runs it before the package-sync module so
#     the suite works against a VM whose baseline predates this script.
#
# The two machines get DIFFERENT flatpak fixtures: `--with-app` installs the test
# application, and is passed for pc1 (the source) only, so a genuine source->target ref
# divergence exists for `test_flatpak_installs_into_source_scope_after_remote` without
# any test having to manufacture one. Both machines get the remote and the runtime.
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
readonly FIXTURES_VERSION=3
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
test_flatpak_installs_into_source_scope_after_remote download a whole runtime inside the
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

install_snaps
install_flatpak_packages
add_flathub_remote
assert_app_runtime_unchanged
install_flatpak_runtime
converge_flatpak_app

printf '%s\n' "$FIXTURES_VERSION" | sudo tee "$MARKER" >/dev/null
log "test fixtures ready (version $FIXTURES_VERSION)"
